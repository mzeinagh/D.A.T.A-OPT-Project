"""Phase 4: the upload form. Deliberately small — just the file and the
split checkbox (decision 8's "Split this PDF into multiple studies",
unchecked by default). Everything past "create the UploadBatch and kick
off detection" (review, confirm, exclude, title editing) is Phase 5's
dedicated review page, not this form.
"""
from django import forms

from .models import UploadBatch

MAX_UPLOAD_SIZE_BYTES = 100 * 1024 * 1024  # 100MB — generous for a scanned regulatory PDF, not unbounded


class UploadBatchForm(forms.ModelForm):
    split_requested = forms.BooleanField(
        required=False,
        initial=False,
        label="Split this PDF into multiple studies",
        help_text="Leave unchecked to process the whole PDF as one study. "
        "Checking this does not guarantee a split — it only asks the pipeline to look for one.",
    )

    class Meta:
        model = UploadBatch
        fields = ["uploaded_file", "split_requested"]
        labels = {"uploaded_file": "Study PDF"}

    def clean_uploaded_file(self):
        file = self.cleaned_data["uploaded_file"]

        if not file.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Only PDF files are accepted.")

        # Sniff the actual header rather than trusting the browser-supplied
        # content_type, which is easy to spoof and sometimes just wrong.
        header = file.read(5)
        file.seek(0)
        if header != b"%PDF-":
            raise forms.ValidationError("This file doesn't look like a valid PDF.")

        if file.size > MAX_UPLOAD_SIZE_BYTES:
            raise forms.ValidationError(
                f"File is too large ({file.size / 1_048_576:.1f} MB) — the limit is "
                f"{MAX_UPLOAD_SIZE_BYTES / 1_048_576:.0f} MB."
            )

        return file
