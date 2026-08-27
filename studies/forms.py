"""Phase 4's upload form (file + split checkbox) and Phase 5's corpus
review form (per-corpus title editing + include/exclude, as a formset).
"""
from django import forms
from django.forms import modelformset_factory

from .models import DetectedCorpus, UploadBatch

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


class DetectedCorpusForm(forms.ModelForm):
    """One row of the review page: the only two fields a user may actually
    change about a detected corpus — its proposed title, and whether it's
    included at all. Everything else shown on the review page (category,
    pages, preview, warnings...) is read-only, straight off the instance.
    """

    class Meta:
        model = DetectedCorpus
        fields = ["title", "included"]
        widgets = {"title": forms.TextInput(attrs={"size": 50})}

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Title cannot be empty.")
        return title


DetectedCorpusFormSet = modelformset_factory(DetectedCorpus, form=DetectedCorpusForm, extra=0)
