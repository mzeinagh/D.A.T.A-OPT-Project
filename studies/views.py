"""Phase 4's upload flow plus Phase 5's corpus-review page. Every view
requires authentication (decision 4) via `@login_required`, not just
hidden links, and ownership is enforced server-side via
`_can_access_batch` — never left to the template.

`review_view` is the required-v1-scope page: any authenticated regular
user can review, edit titles on, include/exclude, confirm, cancel, or
fall back to single-corpus processing for their *own* uploads; staff can
do the same for anyone's. Django admin remains available for staff as an
alternate surface, but is no longer the only way a person can act on a
pending review — see `docs/PHASE_STATUS.md`.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from .corpus_detection import cancel_review, confirm_detected_corpora, process_as_single_corpus
from .forms import DetectedCorpusFormSet, UploadBatchForm
from .models import UploadBatch


def _can_access_batch(user, batch: UploadBatch) -> bool:
    return user.is_staff or batch.uploaded_by_id == user.id


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

    return render(
        request,
        "studies/upload_status.html",
        {
            "batch": batch,
            "detected_corpora": detected_corpora,
            "studies": studies,
            "awaiting_review": batch.split_status == UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
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
            try:
                process_as_single_corpus(batch, started_by=request.user)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Processing the original PDF as a single study.")
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
