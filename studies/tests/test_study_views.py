"""Tests for Phase 6's study/run browsing and export views —
`study_list_view`, `study_detail_view`, `run_detail_view`, and the two
export views. Ownership is enforced the same way as the upload/review
views: the study's batch's uploader, or any staff user.

Real: the Postgres-backed data model, `services.start_pipeline_run` (so a
real `PromptConfigVersion`/`QuestionSnapshot` backs each `Answer`). No
Celery task or LLM call is exercised here — `Answer` rows are created
directly, since these views only ever read already-completed data.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from studies.models import Answer, PromptConfig, Question, Study, UploadBatch
from studies.services import start_pipeline_run

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def alice():
    return User.objects.create_user(username="alice", password="pw12345")


@pytest.fixture
def bob():
    return User.objects.create_user(username="bob", password="pw12345")


@pytest.fixture
def staff_user():
    return User.objects.create_user(username="staff", password="pw12345", is_staff=True)


@pytest.fixture
def active_prompt_config():
    return PromptConfig.objects.create(name="default", intro_text="i", few_shots_text="f", is_active=True)


@pytest.fixture
def one_question(active_prompt_config):
    return Question.objects.create(text="What is the exposure route?", order=0, active=True)


@pytest.fixture
def study_with_run(alice, one_question):
    upload = SimpleUploadedFile("study.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
    batch = UploadBatch.objects.create(
        uploaded_file=upload,
        original_filename="study.pdf",
        uploaded_by=alice,
        split_status=UploadBatch.SplitStatus.CONFIRMED,
    )
    study = Study.objects.create(batch=batch, label="Toxicity assessment", assessment_category="toxicity", status=Study.Status.COMPLETED)
    run = start_pipeline_run(study, alice)
    run.status = "completed"
    run.total_api_calls = 3
    run.total_input_tokens = 100
    run.total_output_tokens = 50
    run.estimated_cost_usd = 0.02
    run.save(update_fields=["status", "total_api_calls", "total_input_tokens", "total_output_tokens", "estimated_cost_usd"])

    snapshot = run.prompt_config_version.question_snapshots.first()
    answer = Answer.objects.create(
        run=run,
        question_snapshot=snapshot,
        order=0,
        handbook_guide_snapshot="Some handbook guidance.",
        augmented_prompt="Augmented prompt text.",
        raw_model_response="raw response",
        formatted_answer="The exposure route is oral.",
        keywords_used=["exposure", "route"],
    )
    return study, run, answer


class TestStudyListView:
    def test_owner_sees_own_studies(self, client, alice, study_with_run):
        study, _, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:study_list"))

        assert resp.status_code == 200
        assert "Toxicity assessment" in resp.content.decode()

    def test_non_owner_does_not_see_others_studies(self, client, bob, study_with_run):
        client.login(username="bob", password="pw12345")

        resp = client.get(reverse("studies:study_list"))

        assert resp.status_code == 200
        assert "Toxicity assessment" not in resp.content.decode()

    def test_staff_sees_everyones(self, client, staff_user, study_with_run):
        client.login(username="staff", password="pw12345")

        resp = client.get(reverse("studies:study_list"))

        assert resp.status_code == 200
        assert "Toxicity assessment" in resp.content.decode()

    def test_anonymous_redirected(self, client, study_with_run):
        resp = client.get(reverse("studies:study_list"))
        assert resp.status_code == 302


class TestStudyDetailView:
    def test_owner_can_view(self, client, alice, study_with_run):
        study, run, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:study_detail", kwargs={"study_id": study.id}))

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Toxicity assessment" in content
        assert str(run.run_number) in content

    def test_non_owner_gets_403(self, client, bob, study_with_run):
        study, _, _ = study_with_run
        client.login(username="bob", password="pw12345")

        resp = client.get(reverse("studies:study_detail", kwargs={"study_id": study.id}))

        assert resp.status_code == 403

    def test_staff_can_view(self, client, staff_user, study_with_run):
        study, _, _ = study_with_run
        client.login(username="staff", password="pw12345")

        resp = client.get(reverse("studies:study_detail", kwargs={"study_id": study.id}))

        assert resp.status_code == 200

    def test_nonexistent_study_404s(self, client, alice):
        client.login(username="alice", password="pw12345")
        resp = client.get(reverse("studies:study_detail", kwargs={"study_id": 999999}))
        assert resp.status_code == 404


class TestRunDetailView:
    def test_owner_can_view_answers(self, client, alice, study_with_run):
        study, run, answer = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:run_detail", kwargs={"study_id": study.id, "run_id": run.id}))

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "The exposure route is oral." in content
        assert "What is the exposure route?" in content

    def test_non_owner_gets_403(self, client, bob, study_with_run):
        study, run, _ = study_with_run
        client.login(username="bob", password="pw12345")

        resp = client.get(reverse("studies:run_detail", kwargs={"study_id": study.id, "run_id": run.id}))

        assert resp.status_code == 403

    def test_run_from_a_different_study_404s(self, client, alice, study_with_run):
        """A run_id that exists but doesn't belong to the study_id in the
        URL must 404, not silently render someone else's run."""
        study, run, _ = study_with_run
        other_batch = UploadBatch.objects.create(
            uploaded_file=SimpleUploadedFile("other.pdf", b"%PDF-1.4", content_type="application/pdf"),
            original_filename="other.pdf",
            uploaded_by=alice,
        )
        other_study = Study.objects.create(batch=other_batch, label="Other study")
        client.login(username="alice", password="pw12345")

        resp = client.get(
            reverse("studies:run_detail", kwargs={"study_id": other_study.id, "run_id": run.id})
        )
        assert resp.status_code == 404


class TestExportViews:
    def test_full_export_owner(self, client, alice, study_with_run):
        study, run, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:export_run_full", kwargs={"study_id": study.id, "run_id": run.id}))

        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("text/plain")
        assert "attachment;" in resp["Content-Disposition"]
        body = resp.content.decode()
        assert "Some handbook guidance." in body
        assert "Augmented prompt text." in body
        assert "raw response" in body
        assert "The exposure route is oral." in body

    def test_answers_only_export_excludes_prompt_and_raw_response(self, client, alice, study_with_run):
        study, run, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:export_run_answers", kwargs={"study_id": study.id, "run_id": run.id}))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "What is the exposure route?" in body
        assert "The exposure route is oral." in body
        assert "Augmented prompt text." not in body
        assert "raw response" not in body

    def test_non_owner_gets_403_on_export(self, client, bob, study_with_run):
        study, run, _ = study_with_run
        client.login(username="bob", password="pw12345")

        resp = client.get(reverse("studies:export_run_full", kwargs={"study_id": study.id, "run_id": run.id}))
        assert resp.status_code == 403

        resp = client.get(reverse("studies:export_run_answers", kwargs={"study_id": study.id, "run_id": run.id}))
        assert resp.status_code == 403

    def test_staff_can_export_anyones(self, client, staff_user, study_with_run):
        study, run, _ = study_with_run
        client.login(username="staff", password="pw12345")

        resp = client.get(reverse("studies:export_run_full", kwargs={"study_id": study.id, "run_id": run.id}))
        assert resp.status_code == 200
