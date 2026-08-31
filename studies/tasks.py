"""Phase 3's Celery task: one task per Study/PipelineRun, covering
indexing and the full 11-question graph run — never split any finer than
that, since that would defeat the whole point of building the retrieval
index once and reusing it for every question (the reason the original CLI
pipeline was structured this way in the first place).

Corpus *construction* is no longer this task's job (as of the corpus-
detection/review update): every `Study` is now always materialized from an
already-confirmed `DetectedCorpus` (see `studies/corpus_detection.py`),
which cached the real `build_corpora()` result — including any OCR — at
detection time. This task just rehydrates that cached content; it never
calls `core_pipeline.document_processor` itself, never re-parses the PDF,
and therefore never needs to guess at or reject a corpus shape it doesn't
recognize (the `UnsupportedCorpusError` guard this task used to have is
gone — corpus detection now pauses for review instead of this task
refusing to run).
"""
import functools
import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core_pipeline.graph import graph
from core_pipeline.schemas import Document
from core_pipeline.search.vector_store import VectorStore
from llm.factory import build_default_llm_client

from .llm_integration import CostLimitedLLMClient, handle_llm_call
from .models import Answer, PipelineRun, Study, StudyPage, UploadBatch

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def run_pipeline_task(self, pipeline_run_id: int, llm_client_factory=build_default_llm_client):
    """Entry point. `llm_client_factory` is overridable for tests only —
    it must always default to the real OpenAI client factory in production,
    which is why it's a keyword default rather than something read from
    settings inside the function body.
    """
    run = PipelineRun.objects.select_related(
        "study", "study__source_corpus", "prompt_config_version"
    ).get(pk=pipeline_run_id)
    study = run.study

    run.status = PipelineRun.Status.RUNNING
    run.save(update_fields=["status"])
    logger.info("Run %s (study %s): started.", run.id, study.id)

    try:
        _execute_run(run, study, llm_client_factory)
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        now = timezone.now()

        run.status = PipelineRun.Status.FAILED
        run.error_message = error_message
        run.finished_at = now
        run.save(update_fields=["status", "error_message", "finished_at"])

        study.status = Study.Status.FAILED
        study.error_message = error_message
        study.finished_at = now
        study.save(update_fields=["status", "error_message", "finished_at"])
        logger.warning("Run %s (study %s): failed — %s", run.id, study.id, error_message)
        raise


def _execute_run(run: PipelineRun, study: Study, llm_client_factory) -> None:
    detected = study.source_corpus
    if detected is None:
        raise RuntimeError(
            f"Study {study.id} has no source_corpus — every Study must be materialized from a "
            "confirmed DetectedCorpus (see studies.corpus_detection.confirm_detected_corpora)."
        )

    study.status = Study.Status.INDEXING
    study.started_at = study.started_at or timezone.now()
    study.save(update_fields=["status", "started_at"])

    docs = [Document(content=d["content"], metadata=d["metadata"]) for d in detected.cached_docs]
    summary = Document(**detected.cached_summary) if detected.cached_summary else None
    title_page = Document(**detected.cached_title_page) if detected.cached_title_page else None

    store = VectorStore(model_dir=settings.EMBEDDING_MODEL_PATH)
    store.add_documents(documents=docs)

    study.status = Study.Status.RUNNING_QUESTIONS
    study.save(update_fields=["status"])

    # CostLimitedLLMClient wraps the same `run` instance handle_llm_call
    # mutates in place — no DB refetch needed to see the running total.
    llm_client = CostLimitedLLMClient(
        llm_client_factory(on_call=functools.partial(handle_llm_call, run)), run
    )

    page_lookup = {p.page_number: p.id for p in StudyPage.objects.filter(study=study)}
    question_snapshots = list(run.prompt_config_version.question_snapshots.order_by("order", "id"))

    for order, snapshot in enumerate(question_snapshots):
        result_state = graph.invoke(
            {
                "intro": run.prompt_config_version.intro_text,
                "few_shots": run.prompt_config_version.few_shots_text,
                "guidebook_fp": settings.HANDBOOK_PDF_PATH,
                "guide": None,
                "question": snapshot.text,
                "augmented_question": None,
                "context": [],
                "output": None,
                "chats_dir": "",
                "messages": [],
                "corpus_store": store,
                "summary": summary,
                "title_page": title_page,
                "corrected_output": None,
                "retrieved_pages": None,
                "debugging": run.debugging,
                "keywords": snapshot.keywords or None,
                "llm_client": llm_client,
            }
        )

        Answer.objects.create(
            run=run,
            question_snapshot=snapshot,
            order=order,
            retrieved_page_refs=_build_page_refs(result_state.get("retrieved_pages"), page_lookup),
            handbook_guide_snapshot=result_state.get("guide") or "",
            augmented_prompt=result_state.get("final_input") or "",
            raw_model_response=result_state.get("output") or "",
            formatted_answer=result_state.get("corrected_output") or "",
            keywords_used=snapshot.keywords or [],
        )

        if run.halted_due_to_cost_limit:
            break

    run.status = (
        PipelineRun.Status.HALTED_COST_LIMIT if run.halted_due_to_cost_limit else PipelineRun.Status.COMPLETED
    )
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at"])

    study.status = Study.Status.COMPLETED
    study.finished_at = timezone.now()
    study.save(update_fields=["status", "finished_at"])

    if run.halted_due_to_cost_limit:
        logger.warning(
            "Run %s (study %s): halted at cost limit ($%.4f / $%.4f) after %d question(s).",
            run.id, study.id, run.estimated_cost_usd or 0.0, run.cost_limit_usd or 0.0, run.answers.count(),
        )
    else:
        logger.info(
            "Run %s (study %s): completed — %d call(s), $%.4f estimated.",
            run.id, study.id, run.total_api_calls, run.estimated_cost_usd or 0.0,
        )


def _build_page_refs(retrieved_pages: dict | None, page_lookup: dict[int, int]) -> list[dict]:
    """Turns `GraphState['retrieved_pages']` (`{"length", "page numbers"}`)
    into `Answer.retrieved_page_refs` — a `StudyPage` id where the page
    number maps to one (regular retrieved pages), or just the raw value
    otherwise (e.g. a summary's "+"-joined page-number string, or a page
    outside this run — references, never duplicated text, per decision 6).
    """
    if not retrieved_pages:
        return []
    refs = []
    for page_num in retrieved_pages.get("page numbers", []):
        ref = {"page_num": page_num}
        if isinstance(page_num, int) and page_num in page_lookup:
            ref["study_page_id"] = page_lookup[page_num]
        refs.append(ref)
    return refs


@shared_task(bind=True)
def detect_corpus_task(self, upload_batch_id: int) -> None:
    """Runs corpus detection/construction for a freshly uploaded PDF in
    the background — this is Phase 4's task, not Phase 3's: detection can
    do real OCR work (see `document_processor.py`), so it must not block
    the upload request/response cycle any more than GPT-5 processing does.

    Deliberately thin: all the actual logic (including the single-corpus
    auto-confirm case, which itself enqueues `run_pipeline_task`) lives in
    `studies.corpus_detection.run_corpus_detection` — this task exists only
    so that function has a Celery entry point, same relationship
    `run_pipeline_task` has to `_execute_run`.

    A fresh upload has no pre-existing row to race against (each upload
    creates a brand new `UploadBatch`), so this task does both halves of
    the claim/body split itself (`run_corpus_detection` == claim + body) —
    unlike `retry_corpus_detection_task`/`process_as_single_corpus_task`
    below, whose claim already happened synchronously in the view.
    """
    from .corpus_detection import run_corpus_detection

    batch = UploadBatch.objects.select_related("uploaded_by").get(pk=upload_batch_id)
    logger.info("Batch %s: detect_corpus_task started.", batch.id)
    run_corpus_detection(batch)


def _resolve_started_by(batch: UploadBatch, started_by_id: int | None):
    """The acting user for a Phase 6 retry/fallback task — explicitly
    passed from the view (`request.user`), never defaulted to
    `batch.uploaded_by`, because staff can act on someone else's upload
    and the *acting* user is what audit fields like `confirmed_by` should
    record.
    """
    if started_by_id is None:
        return batch.uploaded_by
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(pk=started_by_id)


@shared_task(bind=True)
def retry_corpus_detection_task(self, upload_batch_id: int, started_by_id: int | None = None) -> None:
    """Body-only counterpart to a retry already claimed synchronously in
    `views.retry_detection_view` (via
    `corpus_detection.retry_corpus_detection_claim`) — the real duplicate-
    prevention for "don't submit this task twice" happens there, before
    this task is even enqueued (see the module docstring's "Claim/body
    split" note in `corpus_detection.py`). This task just runs the slow
    part (`_run_detection_body`) against the already-claimed batch.
    """
    from .corpus_detection import _run_detection_body

    batch = UploadBatch.objects.select_related("uploaded_by").get(pk=upload_batch_id)
    logger.info("Batch %s: retry_corpus_detection_task started (attempt %d).", batch.id, batch.retry_count)
    _run_detection_body(batch, started_by=_resolve_started_by(batch, started_by_id))


@shared_task(bind=True)
def process_as_single_corpus_task(self, upload_batch_id: int, started_by_id: int | None = None) -> None:
    """Body-only counterpart to a claim already done synchronously in
    the view (`corpus_detection.process_as_single_corpus_claim`) before
    this task is enqueued — same split as `retry_corpus_detection_task`
    above.
    """
    from .corpus_detection import _process_as_single_corpus_body

    batch = UploadBatch.objects.select_related("uploaded_by").get(pk=upload_batch_id)
    logger.info("Batch %s: process_as_single_corpus_task started.", batch.id)
    _process_as_single_corpus_body(batch, started_by=_resolve_started_by(batch, started_by_id))
