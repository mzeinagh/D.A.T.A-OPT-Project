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

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core_pipeline.graph import graph
from core_pipeline.schemas import Document
from core_pipeline.search.vector_store import VectorStore
from llm.factory import build_default_llm_client

from .llm_integration import CostLimitedLLMClient, handle_llm_call
from .models import Answer, PipelineRun, Study, StudyPage


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
