"""Tests for Phase 4's upload flow — the only views that exist so far.
Deliberately does not test anything about reviewing/confirming detected
corpora: there is no view for that yet (Phase 5; see docs/PHASE_STATUS.md).
"""
from unittest.mock import patch

import pymupdf
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from studies.models import UploadBatch

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
def logged_in_client(client, alice):
    client.login(username="alice", password="pw12345")
    return client


class TestUploadViewAuth:
    def test_anonymous_redirected_to_login(self, client):
        resp = client.get(reverse("studies:upload"))
        assert resp.status_code == 302
        assert resp.url.startswith("/accounts/login/")

    def test_logged_in_user_sees_form(self, logged_in_client):
        resp = logged_in_client.get(reverse("studies:upload"))
        assert resp.status_code == 200
        assert b"Study PDF" in resp.content or b"uploaded_file" in resp.content


class TestUploadSubmission:
    def test_valid_pdf_creates_batch_and_enqueues_detection(self, logged_in_client, alice):
        pdf = SimpleUploadedFile("study.pdf", _make_pdf_bytes(["A report page."]), content_type="application/pdf")

        with patch("studies.tasks.detect_corpus_task.delay") as mock_delay:
            resp = logged_in_client.post(
                reverse("studies:upload"), data={"uploaded_file": pdf, "split_requested": False}
            )

        assert resp.status_code == 302
        batch = UploadBatch.objects.get(uploaded_by=alice)
        assert batch.original_filename == "study.pdf"
        mock_delay.assert_called_once_with(batch.id)
        assert resp.url == reverse("studies:upload_status", kwargs={"batch_id": batch.id})

    def test_split_requested_checkbox_is_recorded(self, logged_in_client, alice):
        pdf = SimpleUploadedFile("study.pdf", _make_pdf_bytes(["p"]), content_type="application/pdf")
        with patch("studies.tasks.detect_corpus_task.delay"):
            logged_in_client.post(reverse("studies:upload"), data={"uploaded_file": pdf, "split_requested": True})

        batch = UploadBatch.objects.get(uploaded_by=alice)
        assert batch.split_requested is True

    def test_non_pdf_extension_rejected(self, logged_in_client):
        bad = SimpleUploadedFile("notes.txt", b"just some text", content_type="text/plain")
        resp = logged_in_client.post(reverse("studies:upload"), data={"uploaded_file": bad, "split_requested": False})

        assert resp.status_code == 200  # re-renders with errors, no redirect
        assert UploadBatch.objects.count() == 0
        assert b"Only PDF files" in resp.content

    def test_pdf_extension_but_wrong_content_rejected(self, logged_in_client):
        """A file named .pdf but not actually a PDF (header sniff catches
        what a spoofed content_type wouldn't)."""
        fake = SimpleUploadedFile("study.pdf", b"not really a pdf", content_type="application/pdf")
        resp = logged_in_client.post(reverse("studies:upload"), data={"uploaded_file": fake, "split_requested": False})

        assert resp.status_code == 200
        assert UploadBatch.objects.count() == 0
        assert b"doesn&#x27;t look like a valid PDF" in resp.content or b"look like a valid PDF" in resp.content

    def test_oversized_file_rejected(self, logged_in_client):
        from studies.forms import MAX_UPLOAD_SIZE_BYTES

        oversized = SimpleUploadedFile(
            "study.pdf", b"%PDF-" + b"0" * (MAX_UPLOAD_SIZE_BYTES + 1), content_type="application/pdf"
        )
        resp = logged_in_client.post(
            reverse("studies:upload"), data={"uploaded_file": oversized, "split_requested": False}
        )

        assert resp.status_code == 200
        assert UploadBatch.objects.count() == 0
        assert b"too large" in resp.content


class TestUploadStatusOwnership:
    def _batch_for(self, user):
        pdf_bytes = _make_pdf_bytes(["p"])
        upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
        return UploadBatch.objects.create(uploaded_file=upload, original_filename="study.pdf", uploaded_by=user)

    def test_owner_can_view(self, logged_in_client, alice):
        batch = self._batch_for(alice)
        resp = logged_in_client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 200
        assert b"study.pdf" in resp.content

    def test_non_owner_gets_403(self, client, alice, bob):
        batch = self._batch_for(alice)
        client.login(username="bob", password="pw12345")
        resp = client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 403

    def test_staff_can_view_anyones(self, client, alice, staff_user):
        batch = self._batch_for(alice)
        client.login(username="staff", password="pw12345")
        resp = client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 200

    def test_anonymous_redirected(self, client, alice):
        batch = self._batch_for(alice)
        resp = client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))
        assert resp.status_code == 302

    def test_nonexistent_batch_404s_not_500s(self, logged_in_client):
        resp = logged_in_client.get(reverse("studies:upload_status", kwargs={"batch_id": 999999}))
        assert resp.status_code == 404


class TestAwaitingReviewMessaging:
    def test_awaiting_confirmation_shows_notice_without_action_controls(self, logged_in_client, alice):
        """The status page must never offer a confirm/exclude control —
        that doesn't exist yet (Phase 5). It should only ever show status."""
        batch = self._make_awaiting_batch(alice)

        resp = logged_in_client.get(reverse("studies:upload_status", kwargs={"batch_id": batch.id}))

        assert resp.status_code == 200
        assert b"needs review" in resp.content
        # No form/button that could plausibly confirm or exclude a corpus.
        assert b"name=\"confirm\"" not in resp.content
        assert b"name=\"exclude\"" not in resp.content

    def _make_awaiting_batch(self, user):
        pdf_bytes = _make_pdf_bytes(["p"])
        upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
        batch = UploadBatch.objects.create(
            uploaded_file=upload,
            original_filename="study.pdf",
            uploaded_by=user,
            split_status=UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
        )
        from studies.models import DetectedCorpus

        DetectedCorpus.objects.create(
            batch=batch,
            order=0,
            detection_type=DetectedCorpus.DetectionType.INTEGRATED_REPORT_SPLIT,
            title="Toxicity assessment",
            assessment_category="toxicity",
            page_numbers=[1, 2],
        )
        return batch
