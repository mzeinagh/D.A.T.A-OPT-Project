"""Corpus detection and confirmation.

The pending-review workflow between "PDF uploaded" and "GPT-5 processing
begins" — generalized so an integrated multi-assessment regulatory report
pauses for review exactly like a requested long-report split does, rather
than being rejected outright. Neither is silently merged into one Study
and neither is silently dropped: every corpus `core_pipeline` detects is
either confirmed into its own `Study` or explicitly excluded by a person.

Only this module and `studies/tasks.py` talk to `core_pipeline`/`llm` — the
dependency direction stays one-way, same as `services.py`.

Status: detection, caching, confirm/exclude, the single-corpus fallback, a
dedicated review page, and failed-detection retry are all implemented —
see `docs/PHASE_STATUS.md` for the up-to-date picture.

Concurrency: every function that transitions `split_status` — including
`retry_corpus_detection_claim`/`process_as_single_corpus_claim`, added in
Phase 6 — re-fetches `batch` under `select_for_update()` before checking/
transitioning it, inside one atomic block. Two near-simultaneous attempts
on the same batch (a double-click, a replayed form submission after a
refresh) can never both succeed — the second one blocks on the row lock
until the first commits, then sees the now-changed status and raises
cleanly (`ValueError` or `RetryLimitExceededError`) instead of creating a
second `Study`/`PipelineRun`, or a second detection attempt.

Claim/body split: every entry point that does slow work (PDF parsing,
OCR) is split into a fast, synchronous, row-locked "claim" (the actual
duplicate-prevention) and a separate "body" that does the slow work
assuming the claim already succeeded. A view calls the claim directly —
so a double-click's second request fails before any Celery task is even
submitted, not just once that task eventually runs — then enqueues a task
for the body. `run_corpus_detection`/`process_as_single_corpus`/
`retry_corpus_detection` remain as convenience functions that do both
halves synchronously, for callers (tests, a from-scratch Celery task) that
don't need the split.

The `batch` object a caller passes in is only ever used for its `.pk` in
every function here — never trust its in-memory field values across a
lock boundary; call `refresh_from_db()` (or just re-fetch) to see
post-call state.
"""
import logging

import pymupdf
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from core_pipeline.document_processor import build_corpora, build_corpus_for_page_range

from .models import DetectedCorpus, Study, StudyPage, UploadBatch
from .services import start_pipeline_run

logger = logging.getLogger(__name__)

_SOURCE_TO_DETECTION_TYPE = {
    "split": DetectedCorpus.DetectionType.LONG_REPORT_SPLIT,
    "integrated": DetectedCorpus.DetectionType.INTEGRATED_REPORT_SPLIT,
    "single": DetectedCorpus.DetectionType.SINGLE_DOCUMENT,
}

# Deliberately broader than build_corpora's own defaults: those are
# toxicity-only (negative_titles excludes environmental/residue sections
# before they're ever classified at all — see
# core_pipeline/tests/test_document_processor_ocr.py for the exact
# behavior this bypasses). Detection here must surface every assessment
# type it can find so a person reviews and excludes the ones that
# shouldn't run through the toxicity question set — environmental/residue
# assessments are never silently dropped before review, and never silently
# mislabeled as toxicity either (see DetectedCorpus.assessment_category).
_DETECTION_TARGET_HIGH_LEVEL = [
    "toxicity", "toxicology", "toxicological", "mammalian",
    "environmental", "residue", "occupational", "efficacy",
]
_DETECTION_NEGATIVE_TITLES: list[str] = []


class RetryLimitExceededError(RuntimeError):
    """Raised by `retry_corpus_detection_claim` once `batch.retry_count`
    has reached `settings.CORPUS_DETECTION_MAX_RETRIES`. Distinct from
    `ValueError` (a state-machine violation — wrong status to retry from)
    so a caller can tell "you can't retry from here" apart from "you've
    used up your retries" and word the message accordingly.
    """


# ==================================================================== detection

def run_corpus_detection(batch: UploadBatch, *, started_by=None) -> None:
    """Full, synchronous first-detection attempt: claim + body. Used by
    `detect_corpus_task` right after upload, where there's no pre-existing
    row to race against (each upload creates a brand new `UploadBatch`),
    so claim-then-separately-enqueue isn't necessary the way it is for
    retry/confirm/cancel on an *existing* batch.
    """
    started_by = started_by or batch.uploaded_by
    claimed = _claim_batch(batch, forbid_statuses=(UploadBatch.SplitStatus.CONFIRMED,))
    _run_detection_body(claimed, started_by=started_by)


def _run_detection_body(batch: UploadBatch, *, started_by) -> None:
    """Assumes `batch` has already been claimed (status=DETECTING). Runs
    `build_corpora()` — full construction, including any OCR; this is the
    "corpus detection and construction" step that must finish before any
    GPT-5 call, not before this function returns.

    - Exactly one corpus, and the user never asked to review (didn't check
      "split into multiple studies"): auto-confirms immediately.
    - Otherwise — including a single corpus when review *was* requested,
      and unconditionally whenever more than one corpus comes back for
      ANY reason (long-report split or integrated-report multi-assessment
      alike) — saves every corpus as a `DetectedCorpus` row and stops at
      `AWAITING_CONFIRMATION`.
    - A failure never touches the original upload — only `split_status`/
      `split_error_message`/`detection_failure_history` change (see
      `_mark_failed`) — and the caller can retry
      (`retry_corpus_detection`) or fall back to
      `process_as_single_corpus`.
    """
    try:
        pdf_path = batch.uploaded_file.path
        page_events: list[dict] = []
        corpora = build_corpora(
            pdf_path,
            split=batch.split_requested,
            target_high_level=_DETECTION_TARGET_HIGH_LEVEL,
            negative_titles=_DETECTION_NEGATIVE_TITLES,
            on_page=page_events.append,
            ocr_timeout_seconds=django_settings.OCR_TIMEOUT_SECONDS,
        )
        if not corpora:
            raise RuntimeError("No corpus could be built from this PDF (no readable pages found).")
    except Exception as exc:
        logger.warning("Corpus detection failed for batch %s: %s", batch.id, exc)
        _mark_failed(batch, exc)
        return

    with transaction.atomic():
        detected_rows = [
            _create_detected_corpus(batch, idx, corpus, page_events) for idx, corpus in enumerate(corpora)
        ]
    logger.info("Batch %s: detected %d corpus/corpora.", batch.id, len(detected_rows))

    auto_confirm = len(corpora) == 1 and not batch.split_requested
    if auto_confirm:
        confirm_detected_corpora(batch, confirmed_by=started_by, corpus_ids=[detected_rows[0].id])
    else:
        batch.split_status = UploadBatch.SplitStatus.AWAITING_CONFIRMATION
        batch.save(update_fields=["split_status"])


# ============================================================ single-corpus fallback

def process_as_single_corpus_claim(batch: UploadBatch) -> UploadBatch:
    """The synchronous half of the "process the original PDF as one
    corpus" fallback — see `_claim_batch`. Call this directly from a view
    so a double-submit's duplicate-prevention happens before any Celery
    task is even enqueued; then enqueue a task for
    `_process_as_single_corpus_body`.
    """
    return _claim_batch(batch, forbid_statuses=(UploadBatch.SplitStatus.CONFIRMED,))


def process_as_single_corpus(batch: UploadBatch, *, started_by=None) -> None:
    """Full, synchronous fallback: claim + body. Fine to call directly in
    a test; a view should use `process_as_single_corpus_claim` + a Celery
    task instead, so the slow work never blocks the request (see the
    module docstring's "Claim/body split" note).
    """
    started_by = started_by or batch.uploaded_by
    claimed = process_as_single_corpus_claim(batch)
    _process_as_single_corpus_body(claimed, started_by=started_by)


def _process_as_single_corpus_body(batch: UploadBatch, *, started_by) -> None:
    """Assumes `batch` has already been claimed (status=DETECTING).

    Deliberately bypasses `build_corpora`'s `chunk_report`/
    `chunk_integrated` branching altogether by calling
    `build_corpus_for_page_range` directly over every page — that builds
    exactly one corpus from an explicit page range and nothing else, so
    this is a hard guarantee of a single `Study`, not "detection happened
    to only find one corpus this time."
    """
    try:
        pdf_path = batch.uploaded_file.path
        with pymupdf.open(pdf_path) as doc:
            page_count = len(doc)
        page_events: list[dict] = []
        corpus = build_corpus_for_page_range(
            pdf_path,
            page_range=list(range(1, page_count + 1)),
            on_page=page_events.append,
            ocr_timeout_seconds=django_settings.OCR_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("process_as_single_corpus failed for batch %s: %s", batch.id, exc)
        _mark_failed(batch, exc)
        return

    with transaction.atomic():
        detected = _create_detected_corpus(batch, 0, corpus, page_events)

    confirm_detected_corpora(batch, confirmed_by=started_by, corpus_ids=[detected.id])


# ==================================================================== retry

def retry_corpus_detection_claim(batch: UploadBatch) -> UploadBatch:
    """The synchronous, fast half of a retry: locks `batch`'s row,
    allows the transition *only* from `FAILED` (retry is for recovering a
    failed detection — a fresh attempt uses `run_corpus_detection`, the
    explicit fallback uses `process_as_single_corpus`, neither of which
    is "retry"), refuses once `retry_count` has reached
    `settings.CORPUS_DETECTION_MAX_RETRIES` (raising
    `RetryLimitExceededError`, not silently retrying forever), then
    increments `retry_count`, clears the *active* error (already
    preserved permanently in `detection_failure_history` — see
    `_mark_failed`), and transitions to `DETECTING`.

    Call this directly from a view so a double-click's second request
    fails here, before any Celery task is even submitted — the actual
    "prevent duplicate retry task submission" guarantee — then enqueue a
    task for `_run_detection_body`.
    """
    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().get(pk=batch.pk)

        if locked.split_status != UploadBatch.SplitStatus.FAILED:
            raise ValueError(
                f"UploadBatch {locked.id} can only be retried from a failed state "
                f"(current status: {locked.get_split_status_display()})."
            )

        max_retries = django_settings.CORPUS_DETECTION_MAX_RETRIES
        if locked.retry_count >= max_retries:
            raise RetryLimitExceededError(
                f"This upload has reached the maximum number of retries ({max_retries}). "
                "Try 'Process original PDF as one corpus' instead."
            )

        locked.retry_count += 1
        locked.split_status = UploadBatch.SplitStatus.DETECTING
        locked.split_error_message = ""
        locked.save(update_fields=["retry_count", "split_status", "split_error_message"])

    logger.info("Batch %s: retry #%d claimed.", locked.id, locked.retry_count)
    return locked


def retry_corpus_detection(batch: UploadBatch, *, started_by=None) -> None:
    """Full, synchronous retry: claim + body. Fine to call directly in a
    test; a view should use `retry_corpus_detection_claim` + a Celery task
    instead (see the module docstring's "Claim/body split" note)."""
    started_by = started_by or batch.uploaded_by
    claimed = retry_corpus_detection_claim(batch)
    _run_detection_body(claimed, started_by=started_by)


def _mark_failed(batch: UploadBatch, exc: Exception) -> None:
    """Records a detection failure both as the *active* error
    (`split_error_message`, shown prominently while the batch stays
    `FAILED`) and as a permanent entry in `detection_failure_history` —
    so a later retry can clear the active error (via
    `retry_corpus_detection_claim`) without losing the record of what
    happened on this attempt. Called from every failure path
    (`_run_detection_body`, `_process_as_single_corpus_body`) so the
    history is complete regardless of which path failed.
    """
    message = f"{type(exc).__name__}: {exc}"
    batch.detection_failure_history = [
        *batch.detection_failure_history,
        {"retry_number": batch.retry_count, "error_message": message, "occurred_at": timezone.now().isoformat()},
    ]
    batch.split_status = UploadBatch.SplitStatus.FAILED
    batch.split_error_message = message
    batch.save(update_fields=["detection_failure_history", "split_status", "split_error_message"])


# ==================================================================== confirm / cancel

def confirm_detected_corpora(
    batch: UploadBatch, *, confirmed_by, corpus_ids: list[int] | None = None
) -> list[Study]:
    """Materializes a `Study` (+ `StudyPage` rows, from cached provenance —
    never a PDF re-parse) and a `PipelineRun` for every confirmed
    `DetectedCorpus` in `batch`, then enqueues `run_pipeline_task` for
    each. An excluded corpus gets none of that: no `Study`, no cost,
    nothing.

    `corpus_ids`, if given, is exactly which rows to confirm — what a
    review view or the admin's "Confirm selected" action passes after the
    user's final include/exclude choices; omitted, falls back to whatever
    `included=True` already says on each row.

    Locks `batch`'s row for the whole check-materialize-transition, so a
    second concurrent call (double-click, a replayed submit after a
    refresh) can never also succeed — it blocks until this one commits,
    then sees `split_status` is no longer confirmable and raises
    `ValueError` instead of creating a second set of Studies/PipelineRuns.
    """
    with transaction.atomic():
        locked_batch = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        if locked_batch.split_status not in (
            UploadBatch.SplitStatus.DETECTING,
            UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
        ):
            raise ValueError(
                f"UploadBatch {locked_batch.id} is not awaiting confirmation "
                f"(status: {locked_batch.get_split_status_display()})."
            )

        queryset = locked_batch.detected_corpora.all()
        queryset = queryset.filter(id__in=corpus_ids) if corpus_ids is not None else queryset.filter(included=True)
        detected_list = list(queryset.order_by("order"))

        if not detected_list:
            raise ValueError("No corpus selected to confirm.")

        studies = [_materialize_study(detected) for detected in detected_list]

        locked_batch.split_status = UploadBatch.SplitStatus.CONFIRMED
        locked_batch.confirmed_by = confirmed_by
        locked_batch.confirmed_at = timezone.now()
        locked_batch.save(update_fields=["split_status", "confirmed_by", "confirmed_at"])

    logger.info("Batch %s: confirmed %d study/studies by user %s.", locked_batch.id, len(studies), confirmed_by)

    for study in studies:
        run = start_pipeline_run(study, confirmed_by)
        from .tasks import run_pipeline_task  # local import avoids any import-time cycle with tasks.py

        run_pipeline_task.delay(run.id)

    return studies


def cancel_review(batch: UploadBatch, *, cancelled_by=None) -> None:
    """Stops a pending review with no Study/PipelineRun ever created for
    it. Refuses (raises `ValueError`) if `batch` has already been
    confirmed — cancelling is for "don't proceed", not "undo a
    confirmation already acted on". Same row-locking discipline as
    `confirm_detected_corpora`, for the same reason.
    """
    with transaction.atomic():
        locked_batch = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        if locked_batch.split_status == UploadBatch.SplitStatus.CONFIRMED:
            raise ValueError(f"UploadBatch {locked_batch.id} has already been confirmed — nothing to cancel.")
        locked_batch.split_status = UploadBatch.SplitStatus.CANCELLED
        locked_batch.save(update_fields=["split_status"])
    logger.info("Batch %s: review cancelled by user %s.", locked_batch.id, cancelled_by)


# ==================================================================== internals

def _claim_batch(batch: UploadBatch, *, forbid_statuses: tuple) -> UploadBatch:
    """Locks `batch`'s row just long enough to check its current
    `split_status` against `forbid_statuses` and, if allowed, transition
    it to `DETECTING` — the short atomic "claim" every entry point that's
    about to do slow work (PDF parsing, OCR) uses, so the row is only
    locked for the instant check-and-transition, never for the slow work
    itself. Returns the fresh instance; raises `ValueError` if forbidden.
    """
    with transaction.atomic():
        locked = UploadBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.split_status in forbid_statuses:
            raise ValueError(
                f"UploadBatch {locked.id} cannot be processed from its current status "
                f"({locked.get_split_status_display()})."
            )
        locked.split_status = UploadBatch.SplitStatus.DETECTING
        locked.split_error_message = ""
        locked.save(update_fields=["split_status", "split_error_message"])
    return locked


def _create_detected_corpus(batch: UploadBatch, idx: int, corpus: dict, page_events: list[dict]) -> DetectedCorpus:
    detection_type = _SOURCE_TO_DETECTION_TYPE.get(corpus.get("source"), DetectedCorpus.DetectionType.OTHER)
    page_numbers = corpus.get("page_numbers", [])
    docs = corpus.get("docs", [])
    summary = corpus.get("summary")
    title_page = corpus.get("title_page")

    preview_source = title_page or (docs[0] if docs else None)
    preview_text = preview_source.content[:500] if preview_source else ""

    page_number_set = set(page_numbers)
    provenance = [e for e in page_events if e.get("page_num") in page_number_set]

    return DetectedCorpus.objects.create(
        batch=batch,
        order=idx,
        detection_type=detection_type,
        title=corpus.get("label") or f"Corpus {idx + 1}",
        assessment_category=corpus.get("assessment_category", ""),
        page_numbers=page_numbers,
        shared_page_numbers=corpus.get("shared_page_numbers", []),
        preview_text=preview_text,
        detection_warnings=corpus.get("detection_warnings", []),
        included=True,
        cached_docs=[{"content": d.content, "metadata": d.metadata} for d in docs],
        cached_summary={"content": summary.content, "metadata": summary.metadata} if summary else None,
        cached_title_page={"content": title_page.content, "metadata": title_page.metadata} if title_page else None,
        cached_page_provenance=provenance,
    )


def _materialize_study(detected: DetectedCorpus) -> Study:
    study = Study.objects.create(
        batch=detected.batch,
        source_corpus=detected,
        label=detected.title,
        page_range=detected.page_numbers or None,
        detection_type=detected.detection_type,
        assessment_category=detected.assessment_category,
    )
    for page_info in detected.cached_page_provenance:
        StudyPage.objects.update_or_create(
            study=study,
            page_number=page_info["page_num"],
            defaults=dict(
                extraction_method=page_info["extraction_method"],
                raw_text=page_info["raw_text"],
                cleaned_text=page_info["cleaned_text"],
                final_text=page_info["final_text"],
                is_table=page_info["is_table"],
                is_toc=page_info["is_toc"],
                ocr_attempted=page_info["ocr_attempted"],
                ocr_succeeded=page_info["ocr_succeeded"],
                ocr_error=page_info["ocr_error"] or "",
                ocr_duration_ms=page_info["ocr_duration_ms"],
            ),
        )
    return study
