"""Service functions bridging the Django data model to `core_pipeline`/
`llm`. Dependency direction is one-way: this module imports `core_pipeline`
and `llm`, never the reverse — neither of those packages knows `studies`
(or Django) exists.

`get_or_create_current_prompt_config_version()` is Phase 2's piece of the
versioning requirement (decision 9): every `PipelineRun` must carry an
immutable snapshot of the question set and prompt config it used, and
editing `Question`/`PromptConfig` afterward must never retroactively change
a completed run. Phase 3 is what actually calls this at run start and sets
`PipelineRun.prompt_config_version` from its result — this function exists
now so that call site has something correct to call.
"""
from django.db import transaction
from django.db.models import Max

from .models import PromptConfig, PromptConfigVersion, Question, QuestionSnapshot


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
