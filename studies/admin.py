"""Django admin registration.

Per decision 9, Question/PromptConfig governance is Django-admin-only in
v1 — no separate editor UI. Django's own staff+permission model (a user
needs `is_staff` plus the relevant per-model permission, grantable via
Groups) is what gates who can add/edit/reorder/activate; nothing custom is
layered on top of it here, which also keeps this consistent with decision
4's requirement that group-based delegation be addable later without a
redesign.
"""
from django.contrib import admin

from .corpus_detection import confirm_detected_corpora
from .models import (
    Answer,
    DetectedCorpus,
    LLMCallLog,
    PipelineRun,
    PromptConfig,
    PromptConfigVersion,
    Question,
    QuestionSnapshot,
    Study,
    StudyPage,
    UploadBatch,
)


# ============================================================ Upload & split

class DetectedCorpusInline(admin.TabularInline):
    """The interim review surface for decision-update's "review page"
    requirement, until Phase 5 builds a dedicated one: an admin can see
    every detected corpus for a batch here, edit its title, toggle
    `included`, and use the "Confirm selected" action on the
    DetectedCorpus changelist (not available from an inline) to proceed.
    """
    model = DetectedCorpus
    extra = 0
    fields = ("order", "detection_type", "title", "assessment_category", "page_numbers", "included")
    readonly_fields = ("order", "detection_type", "page_numbers")
    ordering = ("order",)

    def has_add_permission(self, request, obj=None):
        # Only ever created by corpus detection itself.
        return False


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "original_filename",
        "uploaded_by",
        "uploaded_at",
        "split_requested",
        "split_status",
        "confirmed_by",
        "confirmed_at",
    )
    list_filter = ("split_requested", "split_status")
    search_fields = ("original_filename", "uploaded_by__username")
    readonly_fields = ("uploaded_at", "split_status", "split_error_message")
    inlines = [DetectedCorpusInline]


@admin.register(DetectedCorpus)
class DetectedCorpusAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "batch",
        "order",
        "detection_type",
        "title",
        "assessment_category",
        "page_count",
        "shared_page_count",
        "included",
        "warning_count",
    )
    list_filter = ("detection_type", "assessment_category", "included")
    list_editable = ("title", "included")
    search_fields = ("title", "batch__original_filename")
    readonly_fields = (
        "batch",
        "order",
        "detection_type",
        "assessment_category",
        "page_numbers",
        "shared_page_numbers",
        "preview_text",
        "detection_warnings",
        "created_at",
    )
    actions = ["confirm_selected"]

    @admin.display(description="Pages")
    def page_count(self, obj):
        return len(obj.page_numbers)

    @admin.display(description="Shared pages")
    def shared_page_count(self, obj):
        return len(obj.shared_page_numbers)

    @admin.display(description="Warnings")
    def warning_count(self, obj):
        return len(obj.detection_warnings)

    @admin.action(description="Confirm selected corpora (creates Study + starts GPT-5 processing)")
    def confirm_selected(self, request, queryset):
        batch_ids = set(queryset.values_list("batch_id", flat=True))
        if len(batch_ids) != 1:
            self.message_user(
                request,
                "Select corpora from exactly one upload batch at a time to confirm.",
                level="error",
            )
            return

        batch = UploadBatch.objects.get(pk=batch_ids.pop())
        try:
            studies = confirm_detected_corpora(
                batch, confirmed_by=request.user, corpus_ids=list(queryset.values_list("id", flat=True))
            )
        except ValueError as exc:
            self.message_user(request, str(exc), level="error")
            return

        self.message_user(request, f"Confirmed {len(studies)} corpus/corpora — GPT-5 processing has started.")


@admin.register(Study)
class StudyAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "label",
        "batch",
        "detection_type",
        "assessment_category",
        "status",
        "created_at",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "detection_type", "assessment_category")
    search_fields = ("label", "batch__original_filename")
    readonly_fields = ("created_at",)


# ============================================================ Extraction & OCR

@admin.register(StudyPage)
class StudyPageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "study",
        "page_number",
        "extraction_method",
        "is_table",
        "is_toc",
        "ocr_attempted",
        "ocr_succeeded",
        "ocr_duration_ms",
    )
    list_filter = ("extraction_method", "is_table", "is_toc", "ocr_attempted", "ocr_succeeded")
    search_fields = ("study__label",)


# ============================================================ Execution & audit

class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 0
    fields = ("order", "question_snapshot", "formatted_answer")
    readonly_fields = ("order", "question_snapshot", "formatted_answer")
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # Answers are only ever created by the pipeline run itself.
        return False


class LLMCallLogInline(admin.TabularInline):
    model = LLMCallLog
    extra = 0
    fields = ("node_name", "status", "model", "input_tokens", "output_tokens", "latency_ms", "retry_count", "estimated_cost_usd")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "study",
        "run_number",
        "status",
        "started_by",
        "started_at",
        "finished_at",
        "total_api_calls",
        "total_input_tokens",
        "total_output_tokens",
        "cost_display",
        "halted_due_to_cost_limit",
    )
    list_filter = ("status", "halted_due_to_cost_limit", "cost_limit_enabled")
    search_fields = ("study__label", "started_by__username")
    readonly_fields = ("started_at",)
    inlines = [AnswerInline, LLMCallLogInline]

    @admin.display(description="Est. cost")
    def cost_display(self, obj):
        if obj.estimated_cost_usd is None:
            return "—"
        return f"${obj.estimated_cost_usd:.4f}"


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("id", "run", "order", "question_snapshot", "short_answer")
    search_fields = ("formatted_answer", "raw_model_response")
    readonly_fields = ("created_at",)

    @admin.display(description="Formatted answer")
    def short_answer(self, obj):
        text = obj.formatted_answer or ""
        return (text[:80] + "…") if len(text) > 80 else text


@admin.register(LLMCallLog)
class LLMCallLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "run",
        "node_name",
        "status",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "retry_count",
        "estimated_cost_usd",
        "created_at",
    )
    list_filter = ("node_name", "status")
    search_fields = ("run__study__label", "error_message")
    readonly_fields = ("created_at",)


# ============================================================ Question & prompt governance

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("order", "short_text", "active", "updated_at")
    list_editable = ("active",)
    list_filter = ("active",)
    search_fields = ("text", "condition_note")
    ordering = ("order", "id")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Question")
    def short_text(self, obj):
        first_line = obj.text.strip().splitlines()[0].strip() if obj.text.strip() else ""
        return (first_line[:80] + "…") if len(first_line) > 80 else first_line


@admin.register(PromptConfig)
class PromptConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "updated_at", "updated_by")
    list_filter = ("is_active",)
    search_fields = ("name",)
    readonly_fields = ("is_active", "created_at", "updated_at")
    actions = ["activate_selected"]

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Activate selected prompt configuration")
    def activate_selected(self, request, queryset):
        count = queryset.count()
        if count != 1:
            self.message_user(
                request,
                f"Select exactly one prompt configuration to activate (selected {count}).",
                level="error",
            )
            return
        config = queryset.first()
        config.activate()
        self.message_user(request, f"Activated '{config.name}'.")


@admin.register(PromptConfigVersion)
class PromptConfigVersionAdmin(admin.ModelAdmin):
    """Read-only after creation — immutable snapshot, per decision 9. Never
    editable or deletable through admin once it exists; `PipelineRun`'s
    `on_delete=PROTECT` backs this up at the DB level for any version a run
    actually used, but this blocks it in the UI unconditionally, including
    for a version no run has used yet."""

    list_display = ("version_number", "source_config", "created_at")
    search_fields = ("source_config__name",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(QuestionSnapshot)
class QuestionSnapshotAdmin(admin.ModelAdmin):
    """Same immutability as PromptConfigVersionAdmin, one level down."""

    list_display = ("prompt_config_version", "order", "source_question", "short_text", "active")
    list_filter = ("active",)
    search_fields = ("text",)

    @admin.display(description="Text")
    def short_text(self, obj):
        first_line = obj.text.strip().splitlines()[0].strip() if obj.text.strip() else ""
        return (first_line[:80] + "…") if len(first_line) > 80 else first_line

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
