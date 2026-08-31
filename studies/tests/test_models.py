"""Model-level tests, run against a real PostgreSQL test database (created
by pytest-django) — deliberately not SQLite, since decision 6 makes
PostgreSQL the authoritative store and one of the constraints under test
(`unique_active_prompt_config`) is a partial unique index, which only some
backends support the same way Postgres does.
"""
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from studies.models import (
    Answer,
    PipelineRun,
    PromptConfig,
    PromptConfigVersion,
    QuestionSnapshot,
    Study,
    StudyPage,
    UploadBatch,
)

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="researcher", password="x")


@pytest.fixture
def batch(user):
    return UploadBatch.objects.create(uploaded_file="uploads/test.pdf", original_filename="test.pdf", uploaded_by=user)


@pytest.fixture
def study(batch):
    return Study.objects.create(batch=batch, label="whole document")


@pytest.fixture
def prompt_version():
    config = PromptConfig.objects.create(name="default", intro_text="intro", few_shots_text="few shots")
    return PromptConfigVersion.objects.create(source_config=config, version_number=1, intro_text="intro", few_shots_text="few shots")


class TestUploadBatch:
    def test_uploader_cannot_be_deleted(self, batch, user):
        with pytest.raises(ProtectedError):
            user.delete()

    def test_str_falls_back_to_file_name(self, user):
        b = UploadBatch.objects.create(uploaded_file="uploads/x.pdf", uploaded_by=user)
        assert "x.pdf" in str(b)


class TestStudyCascade:
    def test_deleting_batch_deletes_studies(self, batch, study):
        study_id = study.id
        batch.delete()
        assert not Study.objects.filter(id=study_id).exists()


class TestStudyPageConstraint:
    def test_duplicate_page_number_rejected(self, study):
        StudyPage.objects.create(study=study, page_number=1)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                StudyPage.objects.create(study=study, page_number=1)

    def test_different_pages_allowed(self, study):
        StudyPage.objects.create(study=study, page_number=1)
        StudyPage.objects.create(study=study, page_number=2)
        assert StudyPage.objects.filter(study=study).count() == 2


class TestPipelineRunConstraints:
    def test_duplicate_run_number_for_same_study_rejected(self, study, user, prompt_version):
        PipelineRun.objects.create(study=study, run_number=1, started_by=user, prompt_config_version=prompt_version)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PipelineRun.objects.create(study=study, run_number=1, started_by=user, prompt_config_version=prompt_version)

    def test_prompt_config_version_cannot_be_deleted_once_used(self, study, user, prompt_version):
        PipelineRun.objects.create(study=study, run_number=1, started_by=user, prompt_config_version=prompt_version)
        with pytest.raises(ProtectedError):
            prompt_version.delete()

    def test_started_by_user_cannot_be_deleted(self, study, user, prompt_version):
        PipelineRun.objects.create(study=study, run_number=1, started_by=user, prompt_config_version=prompt_version)
        with pytest.raises(ProtectedError):
            user.delete()


class TestAnswerConstraints:
    def test_duplicate_answer_for_same_question_snapshot_rejected(self, study, user, prompt_version):
        run = PipelineRun.objects.create(study=study, run_number=1, started_by=user, prompt_config_version=prompt_version)
        qs = QuestionSnapshot.objects.create(prompt_config_version=prompt_version, text="q1", order=0)
        Answer.objects.create(run=run, question_snapshot=qs, order=0)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Answer.objects.create(run=run, question_snapshot=qs, order=0)


class TestPromptConfigActiveConstraint:
    def test_only_one_active_config_allowed_at_db_level(self):
        PromptConfig.objects.create(name="a", intro_text="i", few_shots_text="f", is_active=True)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PromptConfig.objects.create(name="b", intro_text="i", few_shots_text="f", is_active=True)

    def test_multiple_inactive_configs_allowed(self):
        PromptConfig.objects.create(name="a", intro_text="i", few_shots_text="f", is_active=False)
        PromptConfig.objects.create(name="b", intro_text="i", few_shots_text="f", is_active=False)
        assert PromptConfig.objects.count() == 2

    def test_activate_deactivates_previous(self):
        a = PromptConfig.objects.create(name="a", intro_text="i", few_shots_text="f", is_active=True)
        b = PromptConfig.objects.create(name="b", intro_text="i", few_shots_text="f", is_active=False)

        b.activate()

        a.refresh_from_db()
        b.refresh_from_db()
        assert a.is_active is False
        assert b.is_active is True
        assert PromptConfig.objects.filter(is_active=True).count() == 1


class TestImmutableSnapshotsInAdmin:
    def test_prompt_config_version_admin_blocks_change_and_delete(self, prompt_version):
        from studies.admin import PromptConfigVersionAdmin
        from django.contrib.admin.sites import AdminSite

        admin_instance = PromptConfigVersionAdmin(PromptConfigVersion, AdminSite())
        assert admin_instance.has_change_permission(None, prompt_version) is False
        assert admin_instance.has_delete_permission(None, prompt_version) is False

    def test_question_snapshot_admin_blocks_change_and_delete(self, prompt_version):
        from studies.admin import QuestionSnapshotAdmin
        from django.contrib.admin.sites import AdminSite

        snap = QuestionSnapshot.objects.create(prompt_config_version=prompt_version, text="q", order=0)
        admin_instance = QuestionSnapshotAdmin(QuestionSnapshot, AdminSite())
        assert admin_instance.has_change_permission(None, snap) is False
        assert admin_instance.has_delete_permission(None, snap) is False
