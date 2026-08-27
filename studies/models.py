"""Phase 2 data model.

Every field here exists to satisfy a specific locked decision, not by
default — mainly the audit/reproducibility requirement (decision 6: every
answer/run must preserve its question/prompt snapshot, retrieved passages,
raw and formatted output, model/config info, usage/cost, and errors) and
the split-confirmation workflow (decision 8: detection is a separate,
reviewable, pre-billing step from confirmed processing).

Two ownership conventions used throughout:
- "who did this" FKs (`uploaded_by`, `started_by`, `confirmed_by`) use
  `on_delete=PROTECT` — decision 6 treats authorship as part of the audit
  trail, so a user can't be deleted out from under history they created.
  Deactivate the user account instead; Django admin handles that without
  touching this data.
- FKs to immutable snapshot rows a completed run depends on
  (`PipelineRun.prompt_config_version`, `Answer.question_snapshot`) are
  also `PROTECT`, for the same reason decision 9 requires: editing or
  removing history must never be possible once a run exists against it.

None of this app's code is imported by `core_pipeline` or `llm/` — the
dependency only goes one way, from here into them (see `services.py`,
Phase 3's task layer).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint


# ============================================================ Upload & split

class UploadBatch(models.Model):
    """One uploaded PDF. The source of truth for the original file — it is
    preserved regardless of whether splitting is requested, succeeds, or
    fails (decision 8)."""

    class SplitStatus(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        DETECTING = "detecting", "Detecting"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting confirmation"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"

    uploaded_file = models.FileField(upload_to="uploads/%Y/%m/%d/")
    original_filename = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_batches"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    split_requested = models.BooleanField(
        default=False, help_text="The 'Split this PDF into multiple studies' checkbox value at upload time."
    )
    split_status = models.CharField(max_length=32, choices=SplitStatus.choices, default=SplitStatus.NOT_APPLICABLE)
    split_error_message = models.TextField(blank=True, default="")
    split_params_snapshot = models.JSONField(
        default=dict, blank=True, help_text="e.g. {'minimum_page_for_split': 80, 'target_study_words': [...]}"
    )

    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="confirmed_batches",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_filename or self.uploaded_file.name or f"UploadBatch {self.pk}"


class DetectedStudyBoundary(models.Model):
    """A pre-confirmation candidate study boundary from `chunk_report`'s
    detection pass. No LLM/GPT-5 cost has been incurred for anything
    represented by this row — that only happens once a `Study` is created
    from it after user confirmation (decision 8)."""

    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="detected_boundaries")
    page_start = models.PositiveIntegerField()
    page_end = models.PositiveIntegerField()
    suggested_title = models.CharField(max_length=255, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    included = models.BooleanField(
        default=True, help_text="User can exclude a detected boundary before confirming. v1 does not support resizing page ranges."
    )

    class Meta:
        ordering = ["order", "page_start"]

    def __str__(self):
        return f"{self.batch_id}: pages {self.page_start}-{self.page_end}"


class Study(models.Model):
    """One confirmed unit of work: either the whole uploaded PDF (split not
    requested) or one confirmed split segment. Exactly one `PipelineRun`
    lineage is tracked per `Study`, independently of any sibling studies
    from the same batch (decision 8)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        BUILDING_CORPUS = "building_corpus", "Building corpus"
        INDEXING = "indexing", "Indexing"
        RUNNING_QUESTIONS = "running_questions", "Running questions"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="studies")
    source_boundary = models.ForeignKey(
        DetectedStudyBoundary, null=True, blank=True, on_delete=models.SET_NULL, related_name="studies"
    )
    label = models.CharField(max_length=255, blank=True, default="")
    page_range = models.JSONField(null=True, blank=True, help_text="List of 1-based page numbers, or null for the whole document.")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "studies"

    def __str__(self):
        return self.label or f"Study {self.pk}"


# ============================================================ Extraction & OCR

class StudyPage(models.Model):
    """One row per extracted page of a `Study`. `raw_text` is always kept
    even when OCR runs, so a failed/timed-out OCR call on one page never
    loses that page's content — it just falls back to `cleaned_text`
    (decision 5)."""

    class ExtractionMethod(models.TextChoices):
        TEXT = "text", "Text"
        OCR = "ocr", "OCR"
        FALLBACK_TEXT = "fallback_text", "Fallback text"

    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="pages")
    page_number = models.PositiveIntegerField()
    extraction_method = models.CharField(max_length=16, choices=ExtractionMethod.choices, default=ExtractionMethod.TEXT)

    raw_text = models.TextField(blank=True, default="", help_text="Unmodified pymupdf extraction — always kept as the fallback.")
    cleaned_text = models.TextField(blank=True, default="")
    final_text = models.TextField(blank=True, default="", help_text="What actually went into the corpus (may be OCR markdown).")

    is_table = models.BooleanField(default=False)
    is_toc = models.BooleanField(default=False)

    ocr_attempted = models.BooleanField(default=False)
    ocr_succeeded = models.BooleanField(default=False)
    ocr_error = models.TextField(blank=True, default="")
    ocr_duration_ms = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["page_number"]
        constraints = [
            UniqueConstraint(fields=["study", "page_number"], name="unique_study_page_number"),
        ]

    def __str__(self):
        return f"{self.study_id} p{self.page_number}"


# ============================================================ Execution & audit

class PipelineRun(models.Model):
    """One run of the 11-question graph against one `Study`. Carries the
    full usage/cost ledger (decision 2) and an immutable
    `prompt_config_version` so later edits to `Question`/`PromptConfig`
    can never change what a completed run says it used (decision 9)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        HALTED_COST_LIMIT = "halted_cost_limit", "Halted (cost limit)"

    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="runs")
    run_number = models.PositiveIntegerField()
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="started_runs"
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)

    debugging = models.BooleanField(default=False)
    split = models.BooleanField(default=False)

    prompt_config_version = models.ForeignKey(
        "PromptConfigVersion", on_delete=models.PROTECT, related_name="runs"
    )
    question_set_snapshot = models.JSONField(
        default=list,
        blank=True,
        help_text="Explicit redundant copy of the question set at run start, per decision 9 — "
        "in addition to prompt_config_version's QuestionSnapshot rows.",
    )

    llm_provider = models.CharField(max_length=64, blank=True, default="")
    llm_model = models.CharField(max_length=128, blank=True, default="")

    total_input_tokens = models.PositiveBigIntegerField(default=0)
    total_output_tokens = models.PositiveBigIntegerField(default=0)
    total_api_calls = models.PositiveIntegerField(default=0)
    total_latency_ms = models.FloatField(default=0.0)
    estimated_cost_usd = models.FloatField(null=True, blank=True)

    cost_limit_enabled = models.BooleanField(default=False)
    cost_limit_usd = models.FloatField(null=True, blank=True)
    halted_due_to_cost_limit = models.BooleanField(default=False)

    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            UniqueConstraint(fields=["study", "run_number"], name="unique_study_run_number"),
        ]

    def __str__(self):
        return f"Run {self.run_number} for {self.study_id}"


class Answer(models.Model):
    """One row per (run, question). Everything decision 6 requires for
    audit/reproducibility of a single answer lives here: the question/
    prompt snapshot (via `question_snapshot`), retrieved passages, handbook
    guidance, raw and formatted output."""

    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="answers")
    question_snapshot = models.ForeignKey(
        "QuestionSnapshot", on_delete=models.PROTECT, related_name="answers"
    )
    order = models.PositiveIntegerField(default=0)

    retrieved_page_refs = models.JSONField(
        default=list,
        blank=True,
        help_text="[{'study_page_id': ..., 'page_num': ...}, ...] — references StudyPage rows, not duplicated text.",
    )
    handbook_guide_snapshot = models.TextField(blank=True, default="")
    augmented_prompt = models.TextField(blank=True, default="")
    raw_model_response = models.TextField(blank=True, default="")
    formatted_answer = models.TextField(blank=True, default="")
    keywords_used = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        constraints = [
            UniqueConstraint(fields=["run", "question_snapshot"], name="unique_run_question_snapshot"),
        ]

    def __str__(self):
        return f"Answer for run {self.run_id} (q order {self.order})"


class LLMCallLog(models.Model):
    """One row per individual `LLMClient.invoke()` call — up to three per
    question (retrieve_guide, generate, formatter). This is the row-level
    detail `PipelineRun`'s totals are aggregated from, and what makes a
    failed API call visible per decision 2 rather than only reflected in
    an aggregate count.

    `answer` is nullable: `llm.base.OnCallHook` fires immediately after
    each call (see `llm/openai_client.py`), which is before the `Answer`
    row for that question necessarily exists yet — Phase 3's run service
    logs the call first and may link it to its `Answer` afterward.
    """

    class NodeName(models.TextChoices):
        RETRIEVE_GUIDE = "retrieve_guide", "Retrieve guide"
        GENERATE = "generate", "Generate"
        FORMATTER = "formatter", "Formatter"

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        TIMEOUT = "timeout", "Timeout"

    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="call_logs")
    answer = models.ForeignKey(
        Answer, null=True, blank=True, on_delete=models.SET_NULL, related_name="call_logs"
    )
    question_order = models.PositiveIntegerField(null=True, blank=True)
    node_name = models.CharField(max_length=32, choices=NodeName.choices)

    model = models.CharField(max_length=128, blank=True, default="")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.FloatField(default=0.0)
    retry_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_message = models.TextField(blank=True, default="")
    estimated_cost_usd = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.node_name} call for run {self.run_id}"


# ============================================================ Question & prompt governance

class Question(models.Model):
    """Live, admin-editable. Replaces the hardcoded `questions.py` list.
    Editing this never touches past runs — see `QuestionSnapshot`."""

    text = models.TextField()
    keywords = models.JSONField(default=list, blank=True)
    condition_note = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Optional free-text note. Conditional instructions already live inline in `text` "
        "(e.g. 'only if repeated-dose study') — this is not a separate rule engine.",
    )
    order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        first_line = self.text.strip().splitlines()[0].strip() if self.text.strip() else ""
        return (first_line[:60] + "…") if len(first_line) > 60 else (first_line or f"Question {self.pk}")


class PromptConfig(models.Model):
    """Live, admin-editable. A named intro/few-shots configuration; at most
    one is active at a time (enforced by the partial unique constraint
    below). Use `.activate()` rather than setting `is_active` directly so
    the previous active config is deactivated atomically."""

    name = models.CharField(max_length=100, unique=True)
    intro_text = models.TextField()
    few_shots_text = models.TextField()
    is_active = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_prompt_configs",
    )

    class Meta:
        constraints = [
            UniqueConstraint(fields=["is_active"], condition=Q(is_active=True), name="unique_active_prompt_config"),
        ]

    def __str__(self):
        return f"{self.name} ({'active' if self.is_active else 'inactive'})"

    def activate(self):
        """Atomically makes this the one active PromptConfig."""
        from django.db import transaction

        with transaction.atomic():
            PromptConfig.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
            self.is_active = True
            self.save(update_fields=["is_active", "updated_at"])


class PromptConfigVersion(models.Model):
    """Immutable snapshot of a `PromptConfig` at the moment a run needed
    one. Never edited after creation — enforced in `admin.py`, not just by
    convention. `PipelineRun.prompt_config_version` is `PROTECT`, so a
    version referenced by any run can never be deleted either."""

    source_config = models.ForeignKey(
        PromptConfig, null=True, blank=True, on_delete=models.SET_NULL, related_name="versions"
    )
    version_number = models.PositiveIntegerField(unique=True)
    intro_text = models.TextField()
    few_shots_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]

    def __str__(self):
        return f"PromptConfigVersion v{self.version_number}"


class QuestionSnapshot(models.Model):
    """Immutable per-question row inside a `PromptConfigVersion`. Frozen
    copy of `Question`'s fields at snapshot time — editing the live
    `Question` afterward never changes this."""

    prompt_config_version = models.ForeignKey(
        PromptConfigVersion, on_delete=models.CASCADE, related_name="question_snapshots"
    )
    source_question = models.ForeignKey(
        Question, null=True, blank=True, on_delete=models.SET_NULL, related_name="snapshots"
    )
    text = models.TextField()
    keywords = models.JSONField(default=list, blank=True)
    condition_note = models.CharField(max_length=255, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"Snapshot of question {self.source_question_id} @ v{self.prompt_config_version_id}"
