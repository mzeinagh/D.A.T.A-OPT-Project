import pytest

from studies.models import PromptConfig, PromptConfigVersion, Question, QuestionSnapshot
from studies.services import NoActivePromptConfigError, get_or_create_current_prompt_config_version

pytestmark = pytest.mark.django_db


class TestNoActiveConfig:
    def test_raises_when_nothing_is_active(self):
        with pytest.raises(NoActivePromptConfigError):
            get_or_create_current_prompt_config_version()

    def test_raises_when_all_configs_inactive(self):
        PromptConfig.objects.create(name="a", intro_text="i", few_shots_text="f", is_active=False)
        with pytest.raises(NoActivePromptConfigError):
            get_or_create_current_prompt_config_version()


class TestSnapshotCreation:
    def _active_config(self):
        return PromptConfig.objects.create(name="default", intro_text="You are an evaluator.", few_shots_text="Format: X.", is_active=True)

    def test_creates_version_matching_active_config(self):
        self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)
        Question.objects.create(text="Q2", order=1, active=True)

        version = get_or_create_current_prompt_config_version()

        assert version.version_number == 1
        assert version.intro_text == "You are an evaluator."
        assert version.few_shots_text == "Format: X."
        snapshots = list(version.question_snapshots.order_by("order"))
        assert [s.text for s in snapshots] == ["Q1", "Q2"]

    def test_inactive_questions_excluded(self):
        self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)
        Question.objects.create(text="Q2 (retired)", order=1, active=False)

        version = get_or_create_current_prompt_config_version()

        assert version.question_snapshots.count() == 1
        assert version.question_snapshots.first().text == "Q1"

    def test_repeated_call_with_no_changes_reuses_same_version(self):
        self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)

        v1 = get_or_create_current_prompt_config_version()
        v2 = get_or_create_current_prompt_config_version()

        assert v1.id == v2.id
        assert PromptConfigVersion.objects.count() == 1

    def test_editing_question_text_creates_new_version(self):
        self._active_config()
        q = Question.objects.create(text="Q1", order=0, active=True)

        v1 = get_or_create_current_prompt_config_version()

        q.text = "Q1 revised"
        q.save()

        v2 = get_or_create_current_prompt_config_version()

        assert v2.id != v1.id
        assert v2.version_number == v1.version_number + 1
        assert v2.question_snapshots.first().text == "Q1 revised"
        # The old version's own snapshot is untouched — history doesn't retroactively change.
        assert v1.question_snapshots.first().text == "Q1"

    def test_adding_a_new_active_question_creates_new_version(self):
        self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)
        v1 = get_or_create_current_prompt_config_version()

        Question.objects.create(text="Q2", order=1, active=True)
        v2 = get_or_create_current_prompt_config_version()

        assert v2.id != v1.id
        assert v1.question_snapshots.count() == 1
        assert v2.question_snapshots.count() == 2

    def test_editing_intro_text_creates_new_version(self):
        config = self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)
        v1 = get_or_create_current_prompt_config_version()

        config.intro_text = "Updated intro."
        config.save()
        v2 = get_or_create_current_prompt_config_version()

        assert v2.id != v1.id
        assert v2.intro_text == "Updated intro."

    def test_switching_active_config_does_not_reuse_other_configs_version(self):
        config_a = self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)
        v1 = get_or_create_current_prompt_config_version()

        config_b = PromptConfig.objects.create(name="alt", intro_text="Alt intro", few_shots_text="Alt few-shots")
        config_b.activate()

        v2 = get_or_create_current_prompt_config_version()

        assert v2.id != v1.id
        assert v2.source_config_id == config_b.id
        config_a.refresh_from_db()
        assert config_a.is_active is False

    def test_version_numbers_are_globally_sequential(self):
        self._active_config()
        Question.objects.create(text="Q1", order=0, active=True)
        v1 = get_or_create_current_prompt_config_version()

        config_b = PromptConfig.objects.create(name="alt", intro_text="Alt", few_shots_text="Alt fs")
        config_b.activate()
        v2 = get_or_create_current_prompt_config_version()

        assert v2.version_number == v1.version_number + 1
