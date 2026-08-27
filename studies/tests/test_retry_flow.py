"""Tests for Phase 6's failed-detection recovery flow: the Retry button
and "process original PDF as one corpus" fallback available on a `FAILED`
`UploadBatch`, plus the underlying `retry_corpus_detection_claim` service
it's built on.

Real: pymupdf-built synthetic PDFs, the Postgres-backed data model.
Mocked: `run_pipeline_task.delay`/`retry_corpus_detection_task.delay`/
`process_as_single_corpus_task.delay`, so nothing here needs a live
Celery worker — same convention as `test_review_views.py`. Where a test
needs to see the full retry -> re-detection -> auto-confirm chain, it
patches `.delay` to call the task function directly instead (see
`test_review_views.py::TestProcessAsSingleAction` for precedent).
"""
from unittest.mock import patch

import pymupdf
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from studies.corpus_detection import RetryLimitExceededError, retry_corpus_detection_claim
from studies.models import PromptConfig, Question, Study, UploadBatch
from studies.tasks import retry_corpus_detection_task

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
def failed_batch(alice):
    pdf_bytes = _make_pdf_bytes(["A simple study report page."])
    upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
    return UploadBatch.objects.create(
        uploaded_file=upload,
        original_filename="study.pdf",
        uploaded_by=alice,
        split_status=UploadBatch.SplitStatus.FAILED,
        split_error_message="RuntimeError: no readable pages found",
        retry_count=1,
        detection_failure_history=[
            {
                "retry_number": 0,
                "error_message": "RuntimeError: no readable pages found",
                "occurred_at": "2026-01-01T00:00:00+00:00",
            },
        ],
    )


class TestRetryDetectionClaimService:
    """Service-layer coverage for `retry_corpus_detection_claim` itself —
    the row-locked check-and-transition every view/task call above it
    relies on for real duplicate-prevention.
    """

    def test_wrong_status_raises_value_error(self, alice):
        batch = UploadBatch.objects.create(
            uploaded_file=SimpleUploadedFile("x.pdf", _make_pdf_bytes(["p"]), content_type="application/pdf"),
            original_filename="x.pdf",
            uploaded_by=alice,
            split_status=UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
        )
        with pytest.raises(ValueError):
            retry_corpus_detection_claim(batch)

    def test_retry_limit_exceeded_raises_dedicated_error(self, failed_batch, settings):
        settings.CORPUS_DETECTION_MAX_RETRIES = 1
        failed_batch.retry_count = 1
        failed_batch.save(update_fields=["retry_count"])

        with pytest.raises(RetryLimitExceededError):
            retry_corpus_detection_claim(failed_batch)

    def test_successful_claim_increments_count_clears_active_error_keeps_history(self, failed_batch):
        claimed = retry_corpus_detection_claim(failed_batch)

        assert claimed.split_status == UploadBatch.SplitStatus.DETECTING
        assert claimed.retry_count == 2
        assert claimed.split_error_message == ""
        # The active error is cleared, but the permanent record isn't.
        assert len(claimed.detection_failure_history) == 1
        assert claimed.detection_failure_history[0]["error_message"] == "RuntimeError: no readable pages found"


class TestRetryDetectionView:
    def test_owner_can_retry(self, client, alice, failed_batch):
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay:
            resp = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        mock_delay.assert_called_once_with(failed_batch.id, alice.id)

        failed_batch.refresh_from_db()
        assert failed_batch.split_status == UploadBatch.SplitStatus.DETECTING
        assert failed_batch.retry_count == 2

    def test_non_owner_gets_403(self, client, bob, failed_batch):
        client.login(username="bob", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay:
            resp = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 403
        mock_delay.assert_not_called()

        failed_batch.refresh_from_db()
        assert failed_batch.split_status == UploadBatch.SplitStatus.FAILED  # untouched
        assert failed_batch.retry_count == 1

    def test_staff_can_retry_anyones(self, client, staff_user, failed_batch):
        client.login(username="staff", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay:
            resp = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        # The acting user (staff), not the original uploader, is what's
        # passed through for audit purposes.
        mock_delay.assert_called_once_with(failed_batch.id, staff_user.id)

    def test_double_submission_only_first_succeeds(self, client, alice, failed_batch):
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay1:
            resp1 = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))
        assert resp1.status_code == 302
        mock_delay1.assert_called_once()

        # A replayed/double submit now finds DETECTING, not FAILED — the
        # claim's row lock refuses it before any second task is enqueued.
        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay2:
            resp2 = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))
        assert resp2.status_code == 302
        mock_delay2.assert_not_called()

        failed_batch.refresh_from_db()
        assert failed_batch.retry_count == 2  # only incremented once

    @pytest.mark.parametrize(
        "status",
        [
            UploadBatch.SplitStatus.AWAITING_CONFIRMATION,
            UploadBatch.SplitStatus.CONFIRMED,
            UploadBatch.SplitStatus.CANCELLED,
            UploadBatch.SplitStatus.DETECTING,
            UploadBatch.SplitStatus.NOT_APPLICABLE,
        ],
    )
    def test_retry_refused_for_any_non_failed_status(self, client, alice, failed_batch, status):
        failed_batch.split_status = status
        failed_batch.save(update_fields=["split_status"])
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay:
            resp = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        mock_delay.assert_not_called()

        failed_batch.refresh_from_db()
        assert failed_batch.split_status == status  # unchanged

    def test_retry_limit_reached_shows_clear_message_and_enqueues_nothing(self, client, alice, failed_batch, settings):
        settings.CORPUS_DETECTION_MAX_RETRIES = 1
        failed_batch.retry_count = 1
        failed_batch.save(update_fields=["retry_count"])
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay:
            resp = client.post(
                reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}), follow=True
            )

        mock_delay.assert_not_called()
        assert b"maximum number of retries" in resp.content

        failed_batch.refresh_from_db()
        assert failed_batch.split_status == UploadBatch.SplitStatus.FAILED
        assert failed_batch.retry_count == 1  # not incremented

    def test_get_request_does_not_act(self, client, alice, failed_batch):
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.retry_corpus_detection_task.delay") as mock_delay:
            resp = client.get(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        mock_delay.assert_not_called()
        failed_batch.refresh_from_db()
        assert failed_batch.split_status == UploadBatch.SplitStatus.FAILED

    def test_successful_retry_transition_end_to_end(self, client, alice, failed_batch, one_question):
        """Simulate a worker picking up the enqueued task immediately: the
        whole retry -> re-detection -> auto-confirm chain runs, since this
        synthetic single-page PDF yields exactly one corpus and no split
        was requested on this batch.
        """
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.run_pipeline_task.delay") as mock_run_delay, patch(
            "studies.tasks.retry_corpus_detection_task.delay",
            side_effect=lambda batch_id, started_by_id: retry_corpus_detection_task(batch_id, started_by_id),
        ) as mock_retry_delay:
            resp = client.post(reverse("studies:retry_detection", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        mock_retry_delay.assert_called_once()
        mock_run_delay.assert_called_once()

        failed_batch.refresh_from_db()
        assert failed_batch.split_status == UploadBatch.SplitStatus.CONFIRMED
        assert Study.objects.filter(batch=failed_batch).count() == 1


class TestProcessAsSingleViewOnFailedBatch:
    """The standalone `process_as_single` endpoint — the FAILED-status
    page's fallback, used when the review page itself no longer renders
    (it only ever shows for AWAITING_CONFIRMATION).
    """

    def test_owner_can_fall_back_to_single_corpus(self, client, alice, failed_batch):
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.process_as_single_corpus_task.delay") as mock_delay:
            resp = client.post(reverse("studies:process_as_single", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        mock_delay.assert_called_once_with(failed_batch.id, alice.id)
        failed_batch.refresh_from_db()
        assert failed_batch.split_status == UploadBatch.SplitStatus.DETECTING

    def test_non_owner_gets_403(self, client, bob, failed_batch):
        client.login(username="bob", password="pw12345")

        with patch("studies.tasks.process_as_single_corpus_task.delay") as mock_delay:
            resp = client.post(reverse("studies:process_as_single", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 403
        mock_delay.assert_not_called()

    def test_available_even_when_retry_limit_reached(self, client, alice, failed_batch, settings):
        """The fallback stays available regardless of the retry count —
        it's not "another retry", it's the alternative to retrying."""
        settings.CORPUS_DETECTION_MAX_RETRIES = 1
        failed_batch.retry_count = 1
        failed_batch.save(update_fields=["retry_count"])
        client.login(username="alice", password="pw12345")

        with patch("studies.tasks.process_as_single_corpus_task.delay") as mock_delay:
            resp = client.post(reverse("studies:process_as_single", kwargs={"batch_id": failed_batch.id}))

        assert resp.status_code == 302
        mock_delay.assert_called_once()
