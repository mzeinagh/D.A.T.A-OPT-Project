"""Service functions bridging the Django data model to `core_pipeline`/
`llm`. Dependency direction is one-way: this module imports `core_pipeline`
and `llm`, never the reverse — neither of those packages knows `studies`
(or Django) exists.

`get_or_create_current_prompt_config_version()` is Phase 2's piece of the
versioning requirement (decision 9): every `PipelineRun` must carry an
immutable snapshot of the question set and prompt config it used, and
editing `Question`/`PromptConfig` afterward must never retroactively change
a completed run.

`start_pipeline_run()` is Phase 3's addition: it's what a future upload/
confirm view (Phase 4) will call to create the pending `PipelineRun` that
`studies.tasks.run_pipeline_task` then executes. It exists now, ahead of
that view, because the Celery task needs a real row to run against to be
testable at all — see `studies/tasks.py` and its tests.
"""
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Max

from .models import PipelineRun, PromptConfig, PromptConfigVersion, Question, QuestionSnapshot, Study


class NoActivePromptConfigError(RuntimeError):
    """Raised when a snapshot is requested but no PromptConfig is active.
    An administrator must activate one in Django admin first — there is no
    silent fallback, since running a pipeline with no defined intro/
    few-shots framing would silently produce nonsense prompts.
    """


def get_or_create_current_prompt_config_version() -> PromptConfigVersion:
    """Returns the `PromptConfigVersion` matching the currently active
    `PromptConfig` and the currently active `Question` set.

    Reuses the latest existing version for that `PromptConfig` if nothing
    has actually changed since it was taken (comparing intro/few-shots text
    and every active question's text/keywords/condition_note/order) —
    otherwise creates a new immutable version. This keeps `PipelineRun`s
    that start back-to-back with no intervening edits sharing one version
    rather than accumulating an identical snapshot per run.
    """
    active_config = PromptConfig.objects.filter(is_active=True).first()
    if active_config is None:
        raise NoActivePromptConfigError(
            "No active PromptConfig. An administrator must activate one in Django admin "
            "before a pipeline run can start."
        )

    active_questions = list(Question.objects.filter(active=True).order_by("order", "id"))

    latest_version = (
        PromptConfigVersion.objects.filter(source_config=active_config).order_by("-version_number").first()
    )
    if latest_version is not None and _snapshot_matches_live_state(latest_version, active_config, active_questions):
        return latest_version

    with transaction.atomic():
        next_number = (PromptConfigVersion.objects.aggregate(m=Max("version_number"))["m"] or 0) + 1
        version = PromptConfigVersion.objects.create(
            source_config=active_config,
            version_number=next_number,
            intro_text=active_config.intro_text,
            few_shots_text=active_config.few_shots_text,
        )
        QuestionSnapshot.objects.bulk_create(
            [
                QuestionSnapshot(
                    prompt_config_version=version,
                    source_question=q,
                    text=q.text,
                    keywords=q.keywords,
                    condition_note=q.condition_note,
                    order=q.order,
                    active=q.active,
                )
                for q in active_questions
            ]
        )
    return version


def _snapshot_matches_live_state(
    version: PromptConfigVersion, config: PromptConfig, active_questions: list[Question]
) -> bool:
    if version.intro_text != config.intro_text or version.few_shots_text != config.few_shots_text:
        return False

    snapshots = list(version.question_snapshots.order_by("order", "id"))
    if len(snapshots) != len(active_questions):
        return False

    for snapshot, question in zip(snapshots, active_questions):
        if (
            snapshot.text,
            snapshot.keywords,
            snapshot.condition_note,
            snapshot.order,
        ) != (
            question.text,
            question.keywords,
            question.condition_note,
            question.order,
        ):
            return False

    return True


def start_pipeline_run(
    study: Study,
    started_by,
    *,
    debugging: bool = False,
    split: bool = False,
    cost_limit_enabled: bool = False,
    cost_limit_usd: float | None = None,
) -> PipelineRun:
    """Creates a pending `PipelineRun` for `study`, snapshotting the current
    question set/prompt config (raises `NoActivePromptConfigError` if none
    is active — no silent fallback). Does not enqueue the Celery task
    itself; callers do `run_pipeline_task.delay(run.id)` (or call it
    directly, synchronously, in tests) once this returns.

    `cost_limit_enabled`/`cost_limit_usd` default to disabled/None, per the
    decision that the per-run cost limit stays off until benchmark data
    exists to set it sensibly.
    """
    version = get_or_create_current_prompt_config_version()

    with transaction.atomic():
        next_run_number = (
            PipelineRun.objects.filter(study=study).aggregate(m=Max("run_number"))["m"] or 0
        ) + 1

        run = PipelineRun.objects.create(
            study=study,
            run_number=next_run_number,
            started_by=started_by,
            debugging=debugging,
            split=split,
            prompt_config_version=version,
            question_set_snapshot=[
                {
                    "order": snap.order,
                    "text": snap.text,
                    "keywords": snap.keywords,
                    "condition_note": snap.condition_note,
                }
                for snap in version.question_snapshots.order_by("order", "id")
            ],
            llm_provider="openai",
            llm_model=django_settings.OPENAI_MODEL,
            cost_limit_enabled=cost_limit_enabled,
            cost_limit_usd=cost_limit_usd,
        )

    return run
