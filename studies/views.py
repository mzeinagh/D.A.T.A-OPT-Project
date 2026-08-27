"""Phase 4: the upload flow's two views. Every study/run/answer/download/
status view must require authentication (decision 4) — enforced here via
`@login_required`, not just by hiding a link — and ownership is checked
server-side, not left to the template.

Deliberately not here yet: anything for reviewing/confirming/excluding
detected corpora. That's Phase 5's dedicated page — see
`docs/PHASE_STATUS.md`. `upload_status_view` only ever *shows* status; it
never lets a user act on a pending review.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render

from .forms import UploadBatchForm
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
    studies = batch.studies.order_by("-created_at")

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
