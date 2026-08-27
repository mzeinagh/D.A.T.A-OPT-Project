"""Corpus detection and confirmation.

The pending-review workflow between "PDF uploaded" and "GPT-5 processing
begins" — generalized so an integrated multi-assessment regulatory report
pauses for review exactly like a requested long-report split does, rather
than being rejected outright. Neither is silently merged into one Study
and neither is silently dropped: every corpus `core_pipeline` detects is
either confirmed into its own `Study` or explicitly excluded by a person.

Only this module and `studies/tasks.py` talk to `core_pipeline`/`llm` — the
dependency direction stays one-way, same as `services.py`.
"""
import pymupdf
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from core_pipeline.document_processor import build_corpora, build_corpus_for_page_range

from .models import DetectedCorpus, Study, StudyPage, UploadBatch
from .services import start_pipeline_run

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


def run_corpus_detection(batch: UploadBatch, *, started_by=None) -> None:
    """Runs `build_corpora()` once — full construction, including any OCR;
    this is the "corpus detection and construction" step that must finish
    before any GPT-5 call, not before this function returns. Never starts
    GPT-5 processing itself except via the one narrow auto-confirm case
    below, which is exactly today's already-approved single-document
    behavior, unchanged.

    - Exactly one corpus, and the user never asked to review (didn't check
      "split into multiple studies"): auto-confirms immediately.
    - Otherwise — including a single corpus when review *was* requested,
      and unconditionally whenever more than one corpus comes back for
      ANY reason (long-report split or integrated-report multi-assessment
      alike) — saves every corpus as a `DetectedCorpus` row and stops at
      `AWAITING_CONFIRMATION`. Nothing is rejected; nothing proceeds to
      GPT-5 without a person confirming it (`confirm_detected_corpora`).
    - A detection failure never touches the original upload — only
      `split_status`/`split_error_message` change — and the caller can
      retry (call this again) or fall back to `process_as_single_corpus`.
    """
    started_by = started_by or batch.uploaded_by

    batch.split_status = UploadBatch.SplitStatus.DETECTING
    batch.split_error_message = ""
    batch.save(update_fields=["split_status", "split_error_message"])

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
        batch.split_status = UploadBatch.SplitStatus.FAILED
        batch.split_error_message = f"{type(exc).__name__}: {exc}"
        batch.save(update_fields=["split_status", "split_error_message"])
        return

    with transaction.atomic():
        detected_rows = [
            _create_detected_corpus(batch, idx, corpus, page_events) for idx, corpus in enumerate(corpora)
        ]

    auto_confirm = len(corpora) == 1 and not batch.split_requested
    if auto_confirm:
        confirm_detected_corpora(batch, confirmed_by=started_by, corpus_ids=[detected_rows[0].id])
    else:
        batch.split_status = UploadBatch.SplitStatus.AWAITING_CONFIRMATION
        batch.save(update_fields=["split_status"])


def process_as_single_corpus(batch: UploadBatch, *, started_by=None) -> None:
    """The explicit "process the original PDF as one corpus" fallback —
    for when corpus detection failed, or a person just wants to skip
    multi-corpus detection entirely.

    Deliberately bypasses `build_corpora`'s `chunk_report`/
    `chunk_integrated` branching altogether by calling
    `build_corpus_for_page_range` directly over every page — that builds
    exactly one corpus from an explicit page range and nothing else, so
    this is a hard guarantee of a single `Study`, not "detection happened
    to only find one corpus this time."
    """
    started_by = started_by or batch.uploaded_by

    batch.split_status = UploadBatch.SplitStatus.DETECTING
    batch.split_error_message = ""
    batch.save(update_fields=["split_status", "split_error_message"])

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
        batch.split_status = UploadBatch.SplitStatus.FAILED
        batch.split_error_message = f"{type(exc).__name__}: {exc}"
        batch.save(update_fields=["split_status", "split_error_message"])
        return

    with transaction.atomic():
        detected = _create_detected_corpus(batch, 0, corpus, page_events)

    confirm_detected_corpora(batch, confirmed_by=started_by, corpus_ids=[detected.id])


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
    """
    if batch.split_status not in (UploadBatch.SplitStatus.DETECTING, UploadBatch.SplitStatus.AWAITING_CONFIRMATION):
        raise ValueError(
            f"UploadBatch {batch.id} is not awaiting confirmation (split_status={batch.split_status})."
        )

    queryset = batch.detected_corpora.all()
    queryset = queryset.filter(id__in=corpus_ids) if corpus_ids is not None else queryset.filter(included=True)
    detected_list = list(queryset.order_by("order"))

    if not detected_list:
        raise ValueError("No corpus selected to confirm.")

    with transaction.atomic():
        studies = [_materialize_study(detected) for detected in detected_list]

        batch.split_status = UploadBatch.SplitStatus.CONFIRMED
        batch.confirmed_by = confirmed_by
        batch.confirmed_at = timezone.now()
        batch.save(update_fields=["split_status", "confirmed_by", "confirmed_at"])

    for study in studies:
        run = start_pipeline_run(study, confirmed_by)
        from .tasks import run_pipeline_task  # local import avoids any import-time cycle with tasks.py

        run_pipeline_task.delay(run.id)

    return studies


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
