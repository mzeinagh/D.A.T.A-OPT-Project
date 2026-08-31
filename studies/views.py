"""Phase 4's upload flow, Phase 5's corpus-review page, and Phase 6's
study/run browsing views. Every view requires authentication (decision 4)
via `@login_required`, not just hidden links, and ownership is enforced
server-side — never left to the template. Two ownership helpers exist
because two different objects are being checked: `_can_access_batch` for
anything hung off an `UploadBatch` (upload status, review, retry,
process-as-single), `_can_access_study` for `Study`/`PipelineRun`
browsing — both reduce to the same rule (owner or staff), just checked
against a different FK path to `uploaded_by`.

`review_view` is the required-v1-scope page: any authenticated regular
user can review, edit titles on, include/exclude, confirm, cancel, or
fall back to single-corpus processing for their *own* uploads; staff can
do the same for anyone's. Django admin remains available for staff as an
alternate surface, but is no longer the only way a person can act on a
pending review — see `docs/PHASE_STATUS.md`.
"""
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .corpus_detection import (
    RetryLimitExceededError,
    cancel_review,
    confirm_detected_corpora,
    process_as_single_corpus_claim,
    retry_corpus_detection_claim,
)
from .forms import DetectedCorpusFormSet, UploadBatchForm
from .models import PipelineRun, Study, UploadBatch


def _can_access_batch(user, batch: UploadBatch) -> bool:
    return user.is_staff or batch.uploaded_by_id == user.id


def _can_access_study(user, study: Study) -> bool:
    return user.is_staff or study.batch.uploaded_by_id == user.id


def _start_process_as_single(request, batch: UploadBatch) -> None:
    """Shared by `review_view`'s inline "process_as_single" action and the
    standalone `process_as_single_view` (used from the FAILED-status page,
    where there's no corpus-review formset to submit alongside). Claims
    synchronously — the real duplicate-prevention, before any Celery task
    is even enqueued — then hands the slow work off to a task, so this
    request never blocks on PDF parsing/OCR the way the old (Phase 5)
    synchronous call did.
    """
    try:
        claimed = process_as_single_corpus_claim(batch)
    except ValueError as exc:
        messages.error(request, str(exc))
        return

    from .tasks import process_as_single_corpus_task

    process_as_single_corpus_task.delay(claimed.id, request.user.id)
    messages.success(request, "Processing the original PDF as a single study — this runs in the background.")


@login_required
def upload_view(request):
    if request.method == "POST":
        form = UploadBatchForm(request.POST, request.FILES)
        if form.is_valid():
            batch = form.save(commit=False)
            batch.uploaded_by = request.user
            batch.original_filename = form.cleaned_data["uploaded_file"].name
            batch.save()

            from .tasks import detect_corpus_task

            detect_corpus_task.delay(batch.id)
            messages.success(request, "Upload received — processing has started.")

            return redirect("studies:upload_status", batch_id=batch.id)
    else:
        form = UploadBatchForm()

    return render(request, "studies/upload.html", {"form": form})


@login_required
def upload_status_view(request, batch_id: int):
    batch = get_object_or_404(UploadBatch, pk=batch_id)
    if not _can_access_batch(request.user, batch):
        raise PermissionDenied("You don't have access to this upload.")

    detected_corpora = batch.detected_corpora.order_by("order")
    studies = batch.studies.order_by("-created_at").prefetch_related("runs")

    max_retries = settings.CORPUS_DETECTION_MAX_RETRIES

    return render(
        request,
        "studies/upload_status.html",
        {
            "batch": batch,
            "detected_corpora": detected_corpora,
            "studies": studies,
            "awaiting_review": batch.split_status == UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
            "max_retries": max_retries,
            "retry_limit_reached": batch.retry_count >= max_retries,
        },
    )


@login_required
def review_view(request, batch_id: int):
    """The dedicated corpus-review page. GET shows the current detected
    corpora as an editable formset; POST handles exactly one of three
    named actions (confirm / process_as_single / cancel) per submit.

    Stale-upload handling: if `batch` isn't awaiting confirmation — already
    confirmed, cancelled, failed, or still mid-detection — this never
    renders the form at all; it redirects to the status page with a
    message explaining why. The service layer
    (`corpus_detection.confirm_detected_corpora`/`cancel_review`) is the
    actual safety net against a race (two submits arriving almost
    together) via row locking; this upfront check is just so a stale page
    doesn't even get a chance to submit in the common case.
    """
    batch = get_object_or_404(UploadBatch, pk=batch_id)
    if not _can_access_batch(request.user, batch):
        raise PermissionDenied("You don't have access to this upload.")

    if batch.split_status != UploadBatch.SplitStatus.AWAITING_CONFIRMATION:
        messages.info(
            request,
            f"This upload is no longer awaiting review (current status: "
            f"{batch.get_split_status_display()}).",
        )
        return redirect("studies:upload_status", batch_id=batch.id)

    queryset = batch.detected_corpora.order_by("order")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "cancel":
            try:
                cancel_review(batch, cancelled_by=request.user)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Review cancelled — no studies were created.")
            return redirect("studies:upload_status", batch_id=batch.id)

        if action == "process_as_single":
            _start_process_as_single(request, batch)
            return redirect("studies:upload_status", batch_id=batch.id)

        if action == "confirm":
            formset = DetectedCorpusFormSet(request.POST, queryset=queryset)
            if formset.is_valid():
                # Title/included edits are saved regardless of what happens
                # next — even if confirmation itself then fails (e.g. a
                # concurrent request already confirmed this batch), there's
                # no reason to discard a valid title edit. The one thing
                # that can never happen either way is a duplicate Study/
                # PipelineRun — that's confirm_detected_corpora's row lock,
                # not anything enforced here.
                included_ids = []
                with transaction.atomic():
                    for form in formset:
                        corpus = form.save()
                        if corpus.included:
                            included_ids.append(corpus.id)

                if not included_ids:
                    messages.error(request, "Select at least one corpus to include before confirming.")
                else:
                    try:
                        confirm_detected_corpora(batch, confirmed_by=request.user, corpus_ids=included_ids)
                    except ValueError as exc:
                        messages.error(request, str(exc))
                        return redirect("studies:upload_status", batch_id=batch.id)
                    else:
                        messages.success(request, f"Confirmed {len(included_ids)} corpus/corpora — processing started.")
                        return redirect("studies:upload_status", batch_id=batch.id)
            else:
                messages.error(request, "Please fix the errors below.")
        else:
            messages.error(request, "Unrecognized action.")
            formset = DetectedCorpusFormSet(queryset=queryset)
    else:
        formset = DetectedCorpusFormSet(queryset=queryset)

    return render(request, "studies/review.html", {"batch": batch, "formset": formset})


@login_required
def retry_detection_view(request, batch_id: int):
    """POST-only. Available to the upload's owner and staff on a batch
    currently `FAILED` — retries corpus detection from scratch, up to
    `settings.CORPUS_DETECTION_MAX_RETRIES` times. The actual duplicate-
    prevention (no two retries racing, no retrying past the limit) is
    `retry_corpus_detection_claim`'s row lock, done synchronously here
    before any Celery task is enqueued — a double-submit's second request
    always gets a clean error, never a second detection attempt.
    """
    batch = get_object_or_404(UploadBatch, pk=batch_id)
    if not _can_access_batch(request.user, batch):
        raise PermissionDenied("You don't have access to this upload.")

    if request.method != "POST":
        return redirect("studies:upload_status", batch_id=batch.id)

    try:
        claimed = retry_corpus_detection_claim(batch)
    except RetryLimitExceededError as exc:
        messages.error(request, str(exc))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        from .tasks import retry_corpus_detection_task

        retry_corpus_detection_task.delay(claimed.id, request.user.id)
        messages.success(request, f"Retrying corpus detection (attempt {claimed.retry_count}).")

    return redirect("studies:upload_status", batch_id=batch.id)


@login_required
def process_as_single_view(request, batch_id: int):
    """POST-only standalone entry point for "process the original PDF as
    one corpus" — used from the FAILED-status page, where (unlike
    `review_view`) there's no corpus-review formset to submit alongside.
    Shares its logic with `review_view`'s inline action via
    `_start_process_as_single`.
    """
    batch = get_object_or_404(UploadBatch, pk=batch_id)
    if not _can_access_batch(request.user, batch):
        raise PermissionDenied("You don't have access to this upload.")

    if request.method != "POST":
        return redirect("studies:upload_status", batch_id=batch.id)

    _start_process_as_single(request, batch)
    return redirect("studies:upload_status", batch_id=batch.id)


# ==================================================================== study browsing

@login_required
def study_list_view(request):
    """Every `Study` the current user can see: their own uploads' studies,
    or every study when staff. `upload_status_view` remains the per-batch
    view (right after upload/review); this is the general "all my
    studies, across every upload" list decision 6/9's audit trail was
    always meant to support browsing, not just exporting.
    """
    studies = (
        Study.objects.select_related("batch", "batch__uploaded_by", "source_corpus")
        .prefetch_related("runs")
        .order_by("-created_at")
    )
    if not request.user.is_staff:
        studies = studies.filter(batch__uploaded_by=request.user)

    return render(request, "studies/study_list.html", {"studies": studies})


@login_required
def study_detail_view(request, study_id: int):
    study = get_object_or_404(Study.objects.select_related("batch", "source_corpus"), pk=study_id)
    if not _can_access_study(request.user, study):
        raise PermissionDenied("You don't have access to this study.")

    runs = study.runs.order_by("-run_number")
    return render(request, "studies/study_detail.html", {"study": study, "runs": runs})


@login_required
def run_detail_view(request, study_id: int, run_id: int):
    """One `PipelineRun`'s full transcript: every `Answer` in question
    order, alongside the usage/cost ledger and any failed `LLMCallLog`
    rows — the audit trail decision 6 requires, made browsable rather
    than only ever queried through Django admin.
    """
    study = get_object_or_404(Study.objects.select_related("batch"), pk=study_id)
    if not _can_access_study(request.user, study):
        raise PermissionDenied("You don't have access to this study.")

    run = get_object_or_404(
        PipelineRun.objects.select_related("prompt_config_version").prefetch_related(
            "answers__question_snapshot", "call_logs"
        ),
        pk=run_id,
        study=study,
    )
    answers = run.answers.order_by("order")
    failed_calls = run.call_logs.exclude(status="success").order_by("created_at")

    return render(
        request,
        "studies/run_detail.html",
        {"study": study, "run": run, "answers": answers, "failed_calls": failed_calls},
    )


# ==================================================================== export

def _safe_filename(name: str) -> str:
    """No permanent transcript files exist server-side (decision 6) —
    exports are generated on demand and streamed straight into the
    response, never written to disk. This just keeps the suggested
    download filename filesystem-safe across browsers/OSes."""
    name = re.sub(r"[^\w\-. ]", "_", name).strip() or "export"
    return name[:150]


def _get_run_for_export(request, study_id: int, run_id: int) -> tuple[Study, PipelineRun]:
    study = get_object_or_404(Study.objects.select_related("batch"), pk=study_id)
    if not _can_access_study(request.user, study):
        raise PermissionDenied("You don't have access to this study.")
    run = get_object_or_404(
        PipelineRun.objects.prefetch_related("answers__question_snapshot"), pk=run_id, study=study
    )
    return study, run


@login_required
def export_run_full_view(request, study_id: int, run_id: int):
    """The full conversation per question: the handbook guide retrieved,
    the augmented prompt sent to GPT-5, the raw model response, and the
    formatted answer — everything decision 6 requires kept, in one
    downloadable transcript.
    """
    study, run = _get_run_for_export(request, study_id, run_id)

    lines = [
        f"Study: {study.label or '(whole document)'}",
        f"Run: {run.run_number}  (status: {run.get_status_display()})",
        f"Model: {run.llm_model or '—'}",
        "=" * 72,
        "",
    ]
    for answer in run.answers.order_by("order"):
        lines.append(f"Q{answer.order + 1}: {answer.question_snapshot.text}")
        lines.append("")
        if answer.handbook_guide_snapshot:
            lines.append("--- Handbook guide ---")
            lines.append(answer.handbook_guide_snapshot)
            lines.append("")
        if answer.augmented_prompt:
            lines.append("--- Augmented prompt ---")
            lines.append(answer.augmented_prompt)
            lines.append("")
        lines.append("--- Raw model response ---")
        lines.append(answer.raw_model_response)
        lines.append("")
        lines.append("--- Formatted answer ---")
        lines.append(answer.formatted_answer)
        lines.append("")
        lines.append("=" * 72)
        lines.append("")

    filename = _safe_filename(f"{study.label or 'study'}-run{run.run_number}-full.txt")
    resp = HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
def export_run_answers_view(request, study_id: int, run_id: int):
    """Response-only export — just the question/formatted-answer pairs,
    for a reviewer who wants the results without the full retrieval/prompt
    transcript.
    """
    study, run = _get_run_for_export(request, study_id, run_id)

    lines = [
        f"Study: {study.label or '(whole document)'}",
        f"Run: {run.run_number}",
        "",
    ]
    for answer in run.answers.order_by("order"):
        lines.append(f"Q{answer.order + 1}: {answer.question_snapshot.text}")
        lines.append(answer.formatted_answer)
        lines.append("")

    filename = _safe_filename(f"{study.label or 'study'}-run{run.run_number}-answers.txt")
    resp = HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
