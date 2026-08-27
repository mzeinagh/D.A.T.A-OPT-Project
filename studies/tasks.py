"""Phase 3's Celery task: one task per Study/PipelineRun, covering corpus
build, indexing, and the full 11-question graph run — never split any
finer than that, since that would defeat the whole point of building the
retrieval index once and reusing it for every question (the reason the
original CLI pipeline was structured this way in the first place).
"""
import functools

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core_pipeline.document_processor import build_corpora, build_corpus_for_page_range
from core_pipeline.graph import graph
from core_pipeline.search.vector_store import VectorStore
from llm.factory import build_default_llm_client

from .llm_integration import CostLimitedLLMClient, handle_llm_call
from .models import Answer, PipelineRun, Study, StudyPage


class UnsupportedCorpusError(RuntimeError):
    """Raised when `build_corpora`/`build_corpus_for_page_range` returns
    something this task doesn't yet know how to process safely:

    - More than one corpus for a `Study` with no `page_range` set. A
      `Study` models exactly one confirmed unit of work (decision 8); the
      'integrated' multi-assessment-report auto-detection path in
      `document_processor.py` can return multiple corpora even with
      `split=False`, and silently picking `corpora[0]` would silently
      drop the rest with no record of it.
    - A corpus whose `source` is `'integrated'`. That path never calls
      `_pages_to_documents`, so it has no OCR-provenance instrumentation
      at all — processing it here would silently create zero `StudyPage`
      rows rather than a faithful audit trail.

    Both cases fail the run loudly with a clear `error_message` rather
    than guessing. Resolving the 'integrated' report path's split/review
    workflow is flagged as open follow-up work, not decided here.
    """


@shared_task(bind=True)
def run_pipeline_task(self, pipeline_run_id: int, llm_client_factory=build_default_llm_client):
    """Entry point. `llm_client_factory` is overridable for tests only —
    it must always default to the real OpenAI client factory in production,
    which is why it's a keyword default rather than something read from
    settings inside the function body.
    """
    run = PipelineRun.objects.select_related("study", "study__batch", "prompt_config_version").get(
        pk=pipeline_run_id
    )
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
    pdf_path = study.batch.uploaded_file.path

    study.status = Study.Status.BUILDING_CORPUS
    study.started_at = study.started_at or timezone.now()
    study.save(update_fields=["status", "started_at"])

    def on_page(info: dict) -> None:
        # update_or_create rather than create: safe if this task is ever
        # retried after partially completing corpus build.
        StudyPage.objects.update_or_create(
            study=study,
            page_number=info["page_num"],
            defaults=dict(
                extraction_method=info["extraction_method"],
                raw_text=info["raw_text"],
                cleaned_text=info["cleaned_text"],
                final_text=info["final_text"],
                is_table=info["is_table"],
                is_toc=info["is_toc"],
                ocr_attempted=info["ocr_attempted"],
                ocr_succeeded=info["ocr_succeeded"],
                ocr_error=info["ocr_error"] or "",
                ocr_duration_ms=info["ocr_duration_ms"],
            ),
        )

    if study.page_range:
        corpus = build_corpus_for_page_range(
            pdf_path,
            page_range=study.page_range,
            label=study.label,
            on_page=on_page,
            ocr_timeout_seconds=settings.OCR_TIMEOUT_SECONDS,
        )
    else:
        corpora = build_corpora(
            pdf_path, split=False, on_page=on_page, ocr_timeout_seconds=settings.OCR_TIMEOUT_SECONDS
        )
        if len(corpora) != 1:
            raise UnsupportedCorpusError(
                f"build_corpora returned {len(corpora)} corpora for a Study with no page_range set "
                "(most likely an 'integrated' multi-assessment report) — this task only supports "
                "exactly one corpus per Study. See the migration plan's blockers list."
            )
        corpus = corpora[0]
        if corpus.get("source") != "single":
            raise UnsupportedCorpusError(
                f"corpus source={corpus.get('source')!r} has no OCR-provenance instrumentation; "
                "refusing to silently produce zero StudyPage rows for it."
            )

    study.status = Study.Status.INDEXING
    study.save(update_fields=["status"])

    store = VectorStore(model_dir=settings.EMBEDDING_MODEL_PATH)
    store.add_documents(documents=corpus["docs"])

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
                "summary": corpus["summary"],
                "title_page": corpus["title_page"],
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
