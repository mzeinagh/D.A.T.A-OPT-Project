"""Tests for Phase 5's dedicated corpus-review page (`studies:review`) —
the required-v1-scope page any authenticated regular user can use to
review, edit, include/exclude, confirm, cancel, or fall back to single-
corpus processing for their own uploads (staff: anyone's). Replaces the
Phase 4 test that asserted no such controls existed anywhere yet.
"""
from unittest.mock import patch

import pymupdf
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from studies.models import DetectedCorpus, PipelineRun, PromptConfig, Question, Study, UploadBatch

pytestmark = pytest.mark.django_db

User = get_user_model()


def _make_pdf_bytes(page_texts):
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


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
def awaiting_batch(alice):
    pdf_bytes = _make_pdf_bytes(["Toxicity section page.", "Residue section page."])
    upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
    batch = UploadBatch.objects.create(
        uploaded_file=upload,
        original_filename="study.pdf",
        uploaded_by=alice,
        split_status=UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
    )
    tox = DetectedCorpus.objects.create(
        batch=batch,
        order=0,
        detection_type=DetectedCorpus.DetectionType.INTEGRATED_REPORT_SPLIT,
        title="Toxicity assessment",
        assessment_category="toxicity",
        page_numbers=[1, 3],
        shared_page_numbers=[3],
        preview_text="Toxicity preview text.",
        detection_warnings=["No title page detected."],
        cached_docs=[{"content": "Toxicity section page.", "metadata": {"page_num": 1}}],
        cached_page_provenance=[
            {
                "page_num": 1, "raw_text": "t", "cleaned_text": "t", "final_text": "t",
                "is_table": False, "is_toc": False, "extraction_method": "text",
                "ocr_attempted": False, "ocr_succeeded": False, "ocr_error": None, "ocr_duration_ms": None,
            }
        ],
    )
    residue = DetectedCorpus.objects.create(
        batch=batch,
        order=1,
        detection_type=DetectedCorpus.DetectionType.INTEGRATED_REPORT_SPLIT,
        title="Residue assessment",
        assessment_category="residue",
        page_numbers=[2],
        cached_docs=[{"content": "Residue section page.", "metadata": {"page_num": 2}}],
    )
    return batch, tox, residue


def _confirm_post_data(tox, residue, *, include_tox=True, include_residue=False, tox_title="Toxicity assessment", residue_title="Residue assessment"):
    data = {
        "action": "confirm",
        "form-TOTAL_FORMS": "2",
        "form-INITIAL_FORMS": "2",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "form-0-id": str(tox.id),
        "form-0-title": tox_title,
        "form-1-id": str(residue.id),
        "form-1-title": residue_title,
    }
    if include_tox:
        data["form-0-included"] = "on"
    if include_residue:
        data["form-1-included"] = "on"
    return data


class TestReviewOwnership:
    def test_anonymous_redirected(self, client, awaiting_batch):
        batch, _, _ = awaiting_batch
        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 302

    def test_owner_can_view(self, client, alice, awaiting_batch):
        batch, _, _ = awaiting_batch
        client.login(username="alice", password="pw12345")
        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 200

    def test_non_owner_gets_403(self, client, bob, awaiting_batch):
        batch, _, _ = awaiting_batch
        client.login(username="bob", password="pw12345")
        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 403

    def test_staff_can_view_and_act_on_anyones(self, client, staff_user, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="staff", password="pw12345")

        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 200

        with patch("studies.tasks.run_pipeline_task.delay"):
            resp = client.post(
                reverse("studies:review", kwargs={"batch_id": batch.id}),
                data=_confirm_post_data(tox, residue),
            )
        assert resp.status_code == 302
        assert Study.objects.filter(batch=batch).exists()

    def test_nonexistent_batch_404s(self, client, alice):
        client.login(username="alice", password="pw12345")
        resp = client.get(reverse("studies:review", kwargs={"batch_id": 999999}))
        assert resp.status_code == 404


class TestReviewDisplaysRequiredFields:
    def test_all_required_fields_present(self, client, alice, awaiting_batch):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))
        content = resp.content.decode()

        # Titles (as editable input values)
        assert "Toxicity assessment" in content
        assert "Residue assessment" in content
        # Category
        assert "toxicity" in content
        assert "residue" in content
        # Page ranges (compressed display)
        assert "1, 3" in content or "1-3" in content or tox.page_range_display in content
        # Shared vs specific pages
        assert tox.shared_page_range_display in content
        # Preview
        assert "Toxicity preview text." in content
        # Detection type (human-readable)
        assert tox.get_detection_type_display() in content
        # Warnings
        assert "No title page detected." in content
        # Include checkboxes present
        assert 'name="form-0-included"' in content
        assert 'name="form-1-included"' in content
        # All three actions present
        assert 'value="confirm"' in content
        assert 'value="process_as_single"' in content
        assert 'value="cancel"' in content


class TestConfirmAction:
    def test_only_included_corpora_become_studies_with_edited_titles(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            resp = client.post(
                reverse("studies:review", kwargs={"batch_id": batch.id}),
                data=_confirm_post_data(tox, residue, include_tox=True, include_residue=False, tox_title="Toxicity (edited)"),
            )

        assert resp.status_code == 302
        assert resp.url == reverse("studies:upload_status", kwargs={"batch_id": batch.id})

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CONFIRMED
        assert batch.confirmed_by_id == alice.id

        studies = Study.objects.filter(batch=batch)
        assert studies.count() == 1
        assert studies.first().label == "Toxicity (edited)"
        assert studies.first().assessment_category == "toxicity"

        assert PipelineRun.objects.filter(study__batch=batch).count() == 1
        mock_delay.assert_called_once()

        tox.refresh_from_db()
        residue.refresh_from_db()
        assert tox.title == "Toxicity (edited)"
        assert tox.included is True
        assert residue.included is False

    def test_including_both_creates_two_studies_and_two_runs(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            client.post(
                reverse("studies:review", kwargs={"batch_id": batch.id}),
                data=_confirm_post_data(tox, residue, include_tox=True, include_residue=True),
            )

        assert Study.objects.filter(batch=batch).count() == 2
        assert PipelineRun.objects.filter(study__batch=batch).count() == 2
        assert mock_delay.call_count == 2

    def test_nothing_selected_shows_error_and_creates_nothing(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        resp = client.post(
            reverse("studies:review", kwargs={"batch_id": batch.id}),
            data=_confirm_post_data(tox, residue, include_tox=False, include_residue=False),
        )

        assert resp.status_code == 200  # re-renders the form, doesn't redirect
        assert b"Select at least one corpus" in resp.content
        assert Study.objects.filter(batch=batch).count() == 0

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.AWAITING_CONFIRMATION

    def test_empty_title_shows_validation_error_and_creates_nothing(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        resp = client.post(
            reverse("studies:review", kwargs={"batch_id": batch.id}),
            data=_confirm_post_data(tox, residue, include_tox=True, tox_title="   "),
        )

        assert resp.status_code == 200
        assert b"Title cannot be empty" in resp.content
        assert Study.objects.filter(batch=batch).count() == 0


class TestProcessAsSingleAction:
    def test_creates_one_study_for_whole_document(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            resp = client.post(
                reverse("studies:review", kwargs={"batch_id": batch.id}),
                data={"action": "process_as_single"},
            )

        assert resp.status_code == 302
        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CONFIRMED

        studies = Study.objects.filter(batch=batch)
        assert studies.count() == 1
        assert studies.first().page_range == [1, 2]  # whole 2-page document
        mock_delay.assert_called_once()


class TestCancelAction:
    def test_cancel_creates_no_studies(self, client, alice, awaiting_batch):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        resp = client.post(
            reverse("studies:review", kwargs={"batch_id": batch.id}),
            data={"action": "cancel"},
        )

        assert resp.status_code == 302
        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CANCELLED
        assert Study.objects.filter(batch=batch).count() == 0


class TestDuplicateSubmissionSafety:
    def test_confirming_twice_creates_only_one_set_of_studies(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        client.login(username="alice", password="pw12345")

        data = _confirm_post_data(tox, residue, include_tox=True)

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            client.post(reverse("studies:review", kwargs={"batch_id": batch.id}), data=data)

        assert Study.objects.filter(batch=batch).count() == 1
        assert mock_delay.call_count == 1

        # Second submission: the GET-first check in review_view now sees
        # CONFIRMED and redirects before even building the form again —
        # simulates a refresh/replay of the confirm page.
        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 302
        assert resp.url == reverse("studies:upload_status", kwargs={"batch_id": batch.id})

        # And a raw replayed POST (e.g. browser "resend form data") is
        # caught the same way, without ever reaching the formset/service.
        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay2:
            resp2 = client.post(reverse("studies:review", kwargs={"batch_id": batch.id}), data=data)
        assert resp2.status_code == 302
        assert mock_delay2.call_count == 0
        assert Study.objects.filter(batch=batch).count() == 1

    def test_service_layer_refuses_second_confirm_even_bypassing_the_view_guard(
        self, alice, awaiting_batch, one_question
    ):
        """Belt-and-suspenders: confirm_detected_corpora's own row lock is
        the real safety net, independent of the view's upfront check."""
        from studies.corpus_detection import confirm_detected_corpora

        batch, tox, residue = awaiting_batch

        with patch("studies.tasks.run_pipeline_task.delay"):
            confirm_detected_corpora(batch, confirmed_by=alice, corpus_ids=[tox.id])

        with patch("studies.tasks.run_pipeline_task.delay"), pytest.raises(ValueError):
            confirm_detected_corpora(batch, confirmed_by=alice, corpus_ids=[residue.id])

        assert Study.objects.filter(batch=batch).count() == 1


class TestStaleUploadHandling:
    @pytest.mark.parametrize(
        "status",
        [
            UploadBatch.SplitStatus.CONFIRMED,
            UploadBatch.SplitStatus.CANCELLED,
            UploadBatch.SplitStatus.FAILED,
            UploadBatch.SplitStatus.NOT_APPLICABLE,
        ],
    )
    def test_get_redirects_safely_for_non_awaiting_batch(self, client, alice, awaiting_batch, status):
        batch, _, _ = awaiting_batch
        batch.split_status = status
        batch.save(update_fields=["split_status"])
        client.login(username="alice", password="pw12345")

        resp = client.get(reverse("studies:review", kwargs={"batch_id": batch.id}))

        assert resp.status_code == 302
        assert resp.url == reverse("studies:upload_status", kwargs={"batch_id": batch.id})

    def test_post_redirects_safely_for_non_awaiting_batch(self, client, alice, awaiting_batch, one_question):
        batch, tox, residue = awaiting_batch
        batch.split_status = UploadBatch.SplitStatus.CONFIRMED
        batch.save(update_fields=["split_status"])
        client.login(username="alice", password="pw12345")

        resp = client.post(
            reverse("studies:review", kwargs={"batch_id": batch.id}),
            data=_confirm_post_data(tox, residue, include_tox=True),
        )

        assert resp.status_code == 302
        assert Study.objects.filter(batch=batch).count() == 0
