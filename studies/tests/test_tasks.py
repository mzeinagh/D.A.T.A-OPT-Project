"""End-to-end tests for the Celery task, run synchronously (calling
`run_pipeline_task(...)` directly rather than `.delay()` — no worker/broker
needed for this). Real: PDF parsing (pymupdf), the handbook PDF lookup, the
LangGraph graph, the Postgres-backed data model. Faked: the LLM (via
`llm_client_factory`, the task's own injection point for exactly this) and
the embedding store (`VectorStore` is patched — no real embedding model is
available in this environment; see the migration plan's disclosed
limitations). OCR (`ocr_docling`) is mocked per-test since real Docling
OCR is unrelated to what these tests verify and would make them slow/
network-dependent for no benefit.
"""
from unittest.mock import patch

import pymupdf
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from core_pipeline.schemas import SearchResult
from llm.base import LLMResult
from studies.models import Answer, LLMCallLog, PipelineRun, PromptConfig, Question, Study, StudyPage, UploadBatch
from studies.services import start_pipeline_run
from studies.tasks import UnsupportedCorpusError, run_pipeline_task

pytestmark = pytest.mark.django_db

User = get_user_model()


def _make_pdf_bytes(page_texts):
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


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
        if not self.documents:
            return []
        return [SearchResult(document=self.documents[0], score=0.9)]


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
def study(user):
    pdf_bytes = _make_pdf_bytes(["Study report page one, plain text.", "Study report page two, also plain text."])
    upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
    batch = UploadBatch.objects.create(uploaded_file=upload, original_filename="study.pdf", uploaded_by=user)
    return Study.objects.create(batch=batch, label="whole document")


def _fake_factory_for(client):
    def factory(on_call=None):
        client.on_call = on_call
        return client

    return factory


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
        assert StudyPage.objects.filter(study=study).count() == 2
        assert all(p.extraction_method == StudyPage.ExtractionMethod.TEXT for p in StudyPage.objects.filter(study=study))

        answer = Answer.objects.filter(run=run).order_by("order").first()
        assert answer.formatted_answer  # non-empty
        assert answer.question_snapshot.text == "What is the exposure route?"

    def test_run_number_increments_across_runs_for_same_study(self, user, study, two_questions):
        run1 = start_pipeline_run(study, user)
        with patch("studies.tasks.VectorStore", FakeVectorStore):
            run_pipeline_task(run1.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run2 = start_pipeline_run(study, user)
        assert run2.run_number == run1.run_number + 1


class TestOcrProvenanceThroughTask:
    def test_ocr_success_recorded_on_study_page(self, user, two_questions):
        pdf_bytes = _make_pdf_bytes(["Table-looking page.", "Plain page."])
        upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
        batch = UploadBatch.objects.create(uploaded_file=upload, uploaded_by=user)
        study = Study.objects.create(batch=batch)
        run = start_pipeline_run(study, user)

        with patch("studies.tasks.VectorStore", FakeVectorStore), patch(
            "core_pipeline.document_processor.is_table", side_effect=[True, False]
        ), patch("core_pipeline.document_processor.ocr_docling", return_value="| ocr | markdown |"):
            run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        page1 = StudyPage.objects.get(study=study, page_number=1)
        assert page1.extraction_method == StudyPage.ExtractionMethod.OCR
        assert page1.ocr_succeeded is True
        assert page1.final_text == "| ocr | markdown |"

        page2 = StudyPage.objects.get(study=study, page_number=2)
        assert page2.extraction_method == StudyPage.ExtractionMethod.TEXT

    def test_ocr_failure_falls_back_and_study_still_completes(self, user, two_questions):
        pdf_bytes = _make_pdf_bytes(["Table-looking page."])
        upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
        batch = UploadBatch.objects.create(uploaded_file=upload, uploaded_by=user)
        study = Study.objects.create(batch=batch)
        run = start_pipeline_run(study, user)

        with patch("studies.tasks.VectorStore", FakeVectorStore), patch(
            "core_pipeline.document_processor.is_table", return_value=True
        ), patch("core_pipeline.document_processor.ocr_docling", side_effect=RuntimeError("docling exploded")):
            run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run.refresh_from_db()
        study.refresh_from_db()
        assert run.status == PipelineRun.Status.COMPLETED
        assert study.status == Study.Status.COMPLETED

        page1 = StudyPage.objects.get(study=study, page_number=1)
        assert page1.extraction_method == StudyPage.ExtractionMethod.FALLBACK_TEXT
        assert page1.ocr_succeeded is False
        assert "docling exploded" in page1.ocr_error


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
        # Only the one call that actually succeeded before the limit tripped was logged.
        assert LLMCallLog.objects.filter(run=run).count() == 1


class TestFailureHandling:
    def test_run_and_study_marked_failed_on_missing_pdf(self, user, two_questions):
        batch = UploadBatch.objects.create(uploaded_file="uploads/does-not-exist.pdf", uploaded_by=user)
        study = Study.objects.create(batch=batch)
        run = start_pipeline_run(study, user)

        with pytest.raises(Exception):
            run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run.refresh_from_db()
        study.refresh_from_db()
        assert run.status == PipelineRun.Status.FAILED
        assert run.error_message
        assert study.status == Study.Status.FAILED

    def test_multi_corpus_without_page_range_is_rejected(self, user, study, two_questions):
        run = start_pipeline_run(study, user)

        fake_corpora = [
            {"label": "a", "docs": [], "summary": None, "title_page": None, "source": "integrated"},
            {"label": "b", "docs": [], "summary": None, "title_page": None, "source": "integrated"},
        ]
        with patch("studies.tasks.build_corpora", return_value=fake_corpora):
            with pytest.raises(UnsupportedCorpusError):
                run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run.refresh_from_db()
        assert run.status == PipelineRun.Status.FAILED
        assert "corpora" in run.error_message.lower()

    def test_single_integrated_corpus_is_also_rejected(self, user, study, two_questions):
        """A single corpus from the 'integrated' path is still rejected —
        it has no OCR-provenance instrumentation, so silently accepting it
        would produce zero StudyPage rows with no explanation."""
        run = start_pipeline_run(study, user)

        fake_corpus = [{"label": None, "docs": [], "summary": None, "title_page": None, "source": "integrated"}]
        with patch("studies.tasks.build_corpora", return_value=fake_corpus):
            with pytest.raises(UnsupportedCorpusError):
                run_pipeline_task(run.id, llm_client_factory=_fake_factory_for(FakeLLMClient()))

        run.refresh_from_db()
        assert run.status == PipelineRun.Status.FAILED
