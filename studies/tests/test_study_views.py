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

from studies.models import Answer, DetectedCorpus, PromptConfig, Question, Study, StudyPage, UploadBatch
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


class TestUploadStatusLinksAndRefresh:
    """The upload status page links straight to each study and run, and
    reloads itself only while the worker still has something to do."""

    REFRESH_TAG = b'http-equiv="refresh"'

    def _get_status_page(self, client, batch):
        client.login(username="alice", password="pw12345")
        return client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))

    def test_links_to_study_and_run(self, client, study_with_run):
        study, run, _ = study_with_run

        resp = self._get_status_page(client, study.batch)

        assert resp.status_code == 200
        assert reverse("studies:study_detail", kwargs={"study_id": study.id}).encode() in resp.content
        assert reverse("studies:run_detail", kwargs={"study_id": study.id, "run_id": run.id}).encode() in resp.content

    def test_no_refresh_once_run_completed(self, client, study_with_run):
        study, _, _ = study_with_run

        resp = self._get_status_page(client, study.batch)

        assert self.REFRESH_TAG not in resp.content

    def test_refreshes_while_run_is_running(self, client, study_with_run):
        study, run, _ = study_with_run
        run.status = "running"
        run.save(update_fields=["status"])

        resp = self._get_status_page(client, study.batch)

        assert self.REFRESH_TAG in resp.content

    def test_refreshes_while_detecting(self, client, alice):
        upload = SimpleUploadedFile("study.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        batch = UploadBatch.objects.create(
            uploaded_file=upload,
            original_filename="study.pdf",
            uploaded_by=alice,
            split_status=UploadBatch.SplitStatus.DETECTING,
        )

        resp = self._get_status_page(client, batch)

        assert self.REFRESH_TAG in resp.content

    def test_no_refresh_while_waiting_for_review(self, client, alice):
        upload = SimpleUploadedFile("study.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        batch = UploadBatch.objects.create(
            uploaded_file=upload,
            original_filename="study.pdf",
            uploaded_by=alice,
            split_status=UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
        )

        resp = self._get_status_page(client, batch)

        assert self.REFRESH_TAG not in resp.content


class TestProgressStepsAndPageChips:
    """The upload page shows where an upload is in its life, and each
    answer on the results page shows the pages it was based on."""

    def _steps(self, client, batch):
        client.login(username="alice", password="pw12345")
        resp = client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))
        return {step["label"]: step["state"] for step in resp.context["progress_steps"]}

    def _batch(self, alice, split_status):
        upload = SimpleUploadedFile("study.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        return UploadBatch.objects.create(
            uploaded_file=upload, original_filename="study.pdf", uploaded_by=alice, split_status=split_status
        )

    def test_steps_while_detecting(self, client, alice):
        steps = self._steps(client, self._batch(alice, UploadBatch.SplitStatus.DETECTING))
        assert steps["Uploaded"] == "done"
        assert steps["Finding studies"] == "current"
        assert steps["Your review"] == "todo"

    def test_steps_waiting_for_review(self, client, alice):
        steps = self._steps(client, self._batch(alice, UploadBatch.SplitStatus.AWAITING_CONFIRMATION))
        assert steps["Finding studies"] == "done"
        assert steps["Your review"] == "attention"
        assert steps["Asking the questions"] == "todo"

    def test_steps_when_detection_failed(self, client, alice):
        steps = self._steps(client, self._batch(alice, UploadBatch.SplitStatus.FAILED))
        assert steps["Finding studies"] == "failed"

    def test_steps_while_run_is_running(self, client, study_with_run):
        study, run, _ = study_with_run
        run.status = "running"
        run.save(update_fields=["status"])
        steps = self._steps(client, study.batch)
        assert steps["Review: not needed"] == "skipped"
        assert "Your review" not in steps
        assert steps["Asking the questions"] == "current"
        assert steps["Results ready"] == "todo"

    def test_review_shows_done_when_the_split_box_was_ticked(self, client, study_with_run):
        study, _, _ = study_with_run
        study.batch.split_requested = True
        study.batch.save(update_fields=["split_requested"])
        steps = self._steps(client, study.batch)
        assert steps["Your review"] == "done"

    def test_steps_when_run_completed(self, client, study_with_run):
        study, _, _ = study_with_run
        steps = self._steps(client, study.batch)
        assert steps["Asking the questions"] == "done"
        assert steps["Results ready"] == "done"

    def test_answer_shows_source_pages(self, client, study_with_run):
        study, run, answer = study_with_run
        answer.retrieved_page_refs = [
            {"page_num": 7, "study_page_id": 2},
            {"page_num": 4, "study_page_id": 1},
            {"page_num": 4, "study_page_id": 1},
            {"page_num": "1+2"},
        ]
        answer.save(update_fields=["retrieved_page_refs"])
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:run_detail", kwargs={"study_id": study.id, "run_id": run.id}))

        assert resp.context["answers"][0].page_labels == [
            {"label": "4", "page": 4},
            {"label": "7", "page": 7},
            {"label": "1, 2", "page": None},
        ]
        html = resp.content.decode()
        document_url = reverse("studies:study_document", kwargs={"study_id": study.id})
        assert f'href="{document_url}#page-4"' in html
        assert "p. 1, 2" in html

    def test_answer_without_pages_says_so(self, client, study_with_run):
        study, run, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:run_detail", kwargs={"study_id": study.id, "run_id": run.id}))

        assert "No source pages recorded" in resp.content.decode()


class TestStudyDocumentPage:
    """The extracted text of a study, page by page, for its owner and staff only."""

    def _url(self, study):
        return reverse("studies:study_document", kwargs={"study_id": study.id})

    def _add_pages(self, study):
        StudyPage.objects.create(study=study, page_number=2, raw_text="raw two", cleaned_text="clean two", final_text="Purity 99.5%")
        StudyPage.objects.create(
            study=study, page_number=1, extraction_method="ocr", raw_text="", final_text="| Lot | Purity |\n|---|---|\n| A | 99 |",
            is_table=True, ocr_attempted=True, ocr_succeeded=True,
        )

    def test_owner_sees_every_page_in_order(self, client, study_with_run):
        study, _, _ = study_with_run
        self._add_pages(study)
        client.login(username="alice", password="pw12345")

        resp = client.get(self._url(study))

        assert resp.status_code == 200
        html = resp.content.decode()
        assert 'id="page-1"' in html and 'id="page-2"' in html
        assert html.index('id="page-1"') < html.index('id="page-2"')
        assert "Purity 99.5%" in html
        assert "raw two" in html  # raw differs from clean, so it is offered too
        assert ">OCR<" in html

    def test_other_user_is_refused(self, client, study_with_run, bob):
        study, _, _ = study_with_run
        client.login(username="bob", password="pw12345")

        resp = client.get(self._url(study))

        assert resp.status_code == 403

    def test_staff_can_open(self, client, study_with_run, staff_user):
        study, _, _ = study_with_run
        client.login(username="staff", password="pw12345")

        assert client.get(self._url(study)).status_code == 200

    def test_no_pages_says_so(self, client, study_with_run):
        study, _, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(self._url(study))

        assert "No page text was saved for this study." in resp.content.decode()

    def test_review_page_shows_full_text_of_each_page(self, client, alice):
        upload = SimpleUploadedFile("study.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        batch = UploadBatch.objects.create(
            uploaded_file=upload, original_filename="study.pdf", uploaded_by=alice,
            split_status=UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
        )
        long_text = "word " * 400 + "END-OF-PAGE-MARKER"
        DetectedCorpus.objects.create(
            batch=batch, order=0, detection_type=DetectedCorpus.DetectionType.SINGLE_DOCUMENT, title="One",
            page_numbers=[1], preview_text=long_text[:500],
            cached_page_provenance=[{
                "page_num": 1, "extraction_method": "text", "raw_text": long_text, "cleaned_text": long_text,
                "final_text": long_text, "is_table": False, "is_toc": False, "ocr_attempted": False,
                "ocr_succeeded": False, "ocr_error": None, "ocr_duration_ms": None,
            }],
        )
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))

        html = resp.content.decode()
        assert "END-OF-PAGE-MARKER" in html
        assert "Check the extracted text (1 page)" in html


class TestStudyPdf:
    """The original PDF, shown beside the extracted text: read-only, owner
    or staff only, and allowed in a frame on this site only."""

    def _url(self, study):
        return reverse("studies:study_pdf", kwargs={"study_id": study.id})

    def test_owner_gets_the_pdf_inline(self, client, study_with_run):
        study, _, _ = study_with_run
        client.login(username="alice", password="pw12345")

        resp = client.get(self._url(study))

        assert resp.status_code == 200
        assert resp["Content-Type"] == "application/pdf"
        assert resp["Content-Disposition"].startswith("inline")
        assert resp["X-Frame-Options"] == "SAMEORIGIN"
        assert b"".join(resp.streaming_content).startswith(b"%PDF")

    def test_other_user_is_refused(self, client, study_with_run, bob):
        study, _, _ = study_with_run
        client.login(username="bob", password="pw12345")

        assert client.get(self._url(study)).status_code == 403

    def test_missing_file_is_404_and_not_offered(self, client, study_with_run):
        study, _, _ = study_with_run
        StudyPage.objects.create(study=study, page_number=1, final_text="text")
        study.batch.uploaded_file.storage.delete(study.batch.uploaded_file.name)
        client.login(username="alice", password="pw12345")

        assert client.get(self._url(study)).status_code == 404
        resp = client.get(reverse("studies:study_document", kwargs={"study_id": study.id}))
        assert self._url(study) not in resp.content.decode()

    def test_document_page_offers_the_pdf(self, client, study_with_run):
        study, _, _ = study_with_run
        StudyPage.objects.create(study=study, page_number=1, final_text="text")
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:study_document", kwargs={"study_id": study.id}))

        assert f'data-pdf-url="{self._url(study)}"' in resp.content.decode()
