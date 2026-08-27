"""End-to-end tests for the Celery task, run synchronously (calling
`run_pipeline_task(...)` directly rather than `.delay()` — no worker/broker
needed for this).

Corpus construction is no longer this task's job — a `Study` is built here
via `corpus_detection._materialize_study` from a `DetectedCorpus` with
directly-constructed `cached_docs`/`cached_page_provenance`, rather than
through a real PDF/`build_corpora()` call; that path (including OCR
provenance) is exercised at the `corpus_detection` layer instead, see
`test_corpus_detection.py`. Real here: the Postgres-backed data model, the
LangGraph graph, `_materialize_study`/`start_pipeline_run` themselves.
Faked: the LLM (via `llm_client_factory`, the task's own injection point
for exactly this) and the embedding store (`VectorStore` is patched — no
real embedding model is available in this environment).
"""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from llm.base import LLMResult
from studies.corpus_detection import _materialize_study
from studies.models import (
    Answer,
    DetectedCorpus,
    LLMCallLog,
    PipelineRun,
    PromptConfig,
    Question,
    Study,
    StudyPage,
    UploadBatch,
)
from studies.services import start_pipeline_run
from studies.tasks import run_pipeline_task

pytestmark = pytest.mark.django_db

User = get_user_model()


class FakeLLMClient:
    model = "fake-model"

    def __init__(self, on_call=None, node_responses=None, fail_nodes=None):
        self.on_call = on_call
        self.node_responses = node_responses or {}
        self.fail_nodes = fail_nodes or set()
        self.calls = []

    def invoke(self, prompt, *, node_name=None, max_output_tokens=None):
        self.calls.append(node_name)
        if node_name in self.fail_nodes:
            result = LLMResult(text="", model=self.model, status="failed", error_message="simulated failure")
        else:
            default = "Non" if node_name == "retrieve_guide" else "some answer"
            result = LLMResult(
                text=self.node_responses.get(node_name, default),
                model=self.model,
                status="success",
                input_tokens=10,
                output_tokens=5,
                latency_ms=2.0,
                estimated_cost_usd=0.001,
            )
        if self.on_call:
            self.on_call(node_name, result)
        return result


class FakeVectorStore:
    """Stands in for core_pipeline.search.vector_store.VectorStore — real
    embeddings need a downloaded sentence-transformers model this
    environment doesn't have (network-blocked; see migration plan)."""

    def __init__(self, *args, **kwargs):
        self.keywords = None
        self.documents = []

    def add_documents(self, documents):
        self.documents = documents

    def search(self, query, **kwargs):
        from core_pipeline.schemas import SearchResult

        if not self.documents:
            return []
        return [SearchResult(document=self.documents[0], score=0.9)]


def _fake_factory_for(client):
    def factory(on_call=None):
        client.on_call = on_call
        return client

    return factory


def _make_detected_corpus(batch, *, page_texts, detection_type=DetectedCorpus.DetectionType.SINGLE_DOCUMENT):
    page_numbers = list(range(1, len(page_texts) + 1))
    return DetectedCorpus.objects.create(
        batch=batch,
        order=0,
        detection_type=detection_type,
        title="Test corpus",
        page_numbers=page_numbers,
        cached_docs=[{"content": text, "metadata": {"page_num": n}} for n, text in zip(page_numbers, page_texts)],
        cached_page_provenance=[
            {
                "page_num": n,
                "raw_text": text,
                "cleaned_text": text,
                "final_text": text,
                "is_table": False,
                "is_toc": False,
                "extraction_method": "text",
                "ocr_attempted": False,
                "ocr_succeeded": False,
                "ocr_error": None,
                "ocr_duration_ms": None,
            }
            for n, text in zip(page_numbers, page_texts)
        ],
    )


@pytest.fixture
def user():
    return User.objects.create_user(username="researcher", password="x")


@pytest.fixture
def active_prompt_config():
    return PromptConfig.objects.create(name="default", intro_text="You are an evaluator.", few_shots_text="Format: X.", is_active=True)


@pytest.fixture
def two_questions(active_prompt_config):
    return [
        Question.objects.create(text="What is the exposure route?", order=0, active=True),
        Question.objects.create(text="What is the purity?", order=1, active=True, keywords=["purity"]),
    ]


@pytest.fixture
def batch(user):
    return UploadBatch.objects.create(uploaded_file="uploads/study.pdf", uploaded_by=user)


@pytest.fixture
def study(batch):
    detected = _make_detected_corpus(batch, page_texts=["Study report page one.", "Study report page two."])
    return _materialize_study(detected)


class TestHappyPath:
    def test_completes_and_creates_expected_rows(self, user, study, two_questions):
        run = start_pipeline_run(study, user, debugging=True)
        fake_client = FakeLLMClient()

        with patch("studies.tasks.VectorStore", FakeVectorStore):
            run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(fake_client))

        run.refresh_from_db()
        study.refresh_from_db()

        assert run.status == PipelineRun.Status.COMPLETED
        assert study.status == Study.Status.COMPLETED
        assert study.started_at is not None
        assert study.finished_at is not None

        expected_calls = 3 * len(two_questions)  # retrieve_guide + generate + formatter per question
        assert run.total_api_calls == expected_calls
        assert run.total_input_tokens == 10 * expected_calls
        assert run.estimated_cost_usd == pytest.approx(0.001 * expected_calls)

        assert Answer.objects.filter(run=run).count() == len(two_questions)
        assert LLMCallLog.objects.filter(run=run).count() == expected_calls

        answer = Answer.objects.filter(run=run).order_by("order").first()
        assert answer.formatted_answer  # non-empty
        assert answer.question_snapshot.text == "What is the exposure route?"
        # retrieved_page_refs should link back to the real StudyPage rows
        # materialized at confirmation time (via _materialize_study).
        assert StudyPage.objects.filter(study=study).count() == 2

    def test_run_number_increments_across_runs_for_same_study(self, user, study, two_questions):
        run1 = start_pipeline_run(study, user)
        with patch("studies.tasks.VectorStore", FakeVectorStore):
            run_pipeline_task(run1.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run2 = start_pipeline_run(study, user)
        assert run2.run_number == run1.run_number + 1


class TestFailedLlmCallRecorded:
    def test_failed_generate_call_recorded_but_run_still_completes(self, user, study, two_questions):
        run = start_pipeline_run(study, user)
        fake_client = FakeLLMClient(fail_nodes={"generate"})

        with patch("studies.tasks.VectorStore", FakeVectorStore):
            run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(fake_client))

        run.refresh_from_db()
        assert run.status == PipelineRun.Status.COMPLETED

        failed_logs = LLMCallLog.objects.filter(run=run, node_name="generate", status="failed")
        assert failed_logs.count() == len(two_questions)

        answer = Answer.objects.filter(run=run).order_by("order").first()
        assert "LLM call failed" in answer.raw_model_response


class TestCostLimitHalts:
    def test_run_halts_once_cost_limit_reached(self, user, study, two_questions):
        run = start_pipeline_run(study, user, cost_limit_enabled=True, cost_limit_usd=0.0005)
        fake_client = FakeLLMClient()  # each call costs 0.001, over the 0.0005 limit after call 1

        with patch("studies.tasks.VectorStore", FakeVectorStore):
            run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(fake_client))

        run.refresh_from_db()
        assert run.halted_due_to_cost_limit is True
        assert run.status == PipelineRun.Status.HALTED_COST_LIMIT
        # Halted partway through question 1 — never reached question 2.
        assert Answer.objects.filter(run=run).count() == 1
        assert LLMCallLog.objects.filter(run=run).count() == 1


class TestFailureHandling:
    def test_run_and_study_marked_failed_when_study_has_no_source_corpus(self, user, batch, two_questions):
        """Every Study must now come from a confirmed DetectedCorpus — one
        created directly (bypassing corpus_detection, as an admin/bug
        scenario might) has source_corpus=None and must fail loudly rather
        than silently produce an empty run."""
        study = Study.objects.create(batch=batch, label="orphan study")
        run = start_pipeline_run(study, user)

        with pytest.raises(RuntimeError):
            with patch("studies.tasks.VectorStore", FakeVectorStore):
                run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run.refresh_from_db()
        study.refresh_from_db()
        assert run.status == PipelineRun.Status.FAILED
        assert "source_corpus" in run.error_message
        assert study.status == Study.Status.FAILED
