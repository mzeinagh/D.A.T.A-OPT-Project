"""Tests for the corpus detection/confirmation workflow — the pending-
review step between "PDF uploaded" and "GPT-5 processing begins",
generalized to pause for review on ANY multi-corpus result (long-report
split or integrated-report multi-assessment alike), never reject either.

Real: pymupdf-built synthetic PDFs, the Postgres-backed data model,
`build_corpora()` itself for the single-corpus paths. Mocked:
`build_corpora()` for the multi-corpus scenarios (precise control over the
corpus dicts without needing real chunk_report/chunk_integrated-triggering
PDFs), and `run_pipeline_task.delay` (no need for a live Celery worker to
verify a task was *enqueued* — see the Phase 3 tests for the task actually
running).
"""
from unittest.mock import patch

import pymupdf
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from studies.corpus_detection import confirm_detected_corpora, process_as_single_corpus, run_corpus_detection
from studies.models import DetectedCorpus, PipelineRun, PromptConfig, Question, Study, StudyPage, UploadBatch

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
def user():
    return User.objects.create_user(username="researcher", password="x")


@pytest.fixture
def active_prompt_config():
    return PromptConfig.objects.create(name="default", intro_text="i", few_shots_text="f", is_active=True)


@pytest.fixture
def one_question(active_prompt_config):
    return Question.objects.create(text="What is the exposure route?", order=0, active=True)


@pytest.fixture
def batch(user):
    pdf_bytes = _make_pdf_bytes(["A simple study report page."])
    upload = SimpleUploadedFile("study.pdf", pdf_bytes, content_type="application/pdf")
    return UploadBatch.objects.create(uploaded_file=upload, original_filename="study.pdf", uploaded_by=user)


def _fake_corpus(*, source, label, page_numbers, shared_page_numbers=None, assessment_category=""):
    from core_pipeline.schemas import Document

    docs = [Document(content=f"Page {n} content.", metadata={"page_num": n}) for n in page_numbers]
    return {
        "label": label,
        "docs": docs,
        "summary": None,
        "title_page": None,
        "source": source,
        "assessment_category": assessment_category,
        "page_numbers": page_numbers,
        "shared_page_numbers": shared_page_numbers or [],
        "detection_warnings": [],
    }


class TestSingleCorpusAutoConfirm:
    def test_no_split_requested_auto_confirms_and_starts_processing(self, user, batch, one_question):
        assert batch.split_requested is False

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            run_corpus_detection(batch)

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CONFIRMED
        assert batch.confirmed_by_id == user.id

        assert DetectedCorpus.objects.filter(batch=batch).count() == 1
        assert Study.objects.filter(batch=batch).count() == 1
        assert PipelineRun.objects.filter(study__batch=batch).count() == 1
        mock_delay.assert_called_once()

    def test_split_requested_but_only_one_corpus_still_awaits_review(self, user, batch, one_question):
        """A user who explicitly asked to review sees that review even
        when there's only one corpus to look at — 'proceed automatically'
        only applies to the un-requested default path."""
        batch.split_requested = True
        batch.save(update_fields=["split_requested"])

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            run_corpus_detection(batch)

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.AWAITING_CONFIRMATION
        assert DetectedCorpus.objects.filter(batch=batch).count() == 1
        assert Study.objects.filter(batch=batch).count() == 0
        mock_delay.assert_not_called()


class TestMultiCorpusAlwaysPausesForReview:
    def test_long_report_split_pauses_for_review(self, user, batch, one_question):
        corpora = [
            _fake_corpus(source="split", label="1_50", page_numbers=[1, 2, 3]),
            _fake_corpus(source="split", label="51_100", page_numbers=[51, 52, 53]),
        ]
        with patch("studies.corpus_detection.build_corpora", return_value=corpora), patch(
            "studies.tasks.run_pipeline_task.delay"
        ) as mock_delay:
            run_corpus_detection(batch)

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.AWAITING_CONFIRMATION
        detected = list(DetectedCorpus.objects.filter(batch=batch).order_by("order"))
        assert len(detected) == 2
        assert all(d.detection_type == DetectedCorpus.DetectionType.LONG_REPORT_SPLIT for d in detected)
        assert Study.objects.filter(batch=batch).count() == 0
        mock_delay.assert_not_called()

    def test_integrated_report_pauses_for_review_not_rejected(self, user, batch, one_question):
        """The behavior this whole update replaces: an integrated report
        used to make the Celery task raise UnsupportedCorpusError. Now it
        pauses for review exactly like a long-report split does."""
        corpora = [
            _fake_corpus(
                source="integrated", label="Toxicity_RepeatDose", page_numbers=[2, 5],
                shared_page_numbers=[5], assessment_category="toxicity",
            ),
            _fake_corpus(
                source="integrated", label="Environmental_Aquatic", page_numbers=[3, 5],
                shared_page_numbers=[5], assessment_category="environmental",
            ),
        ]
        with patch("studies.corpus_detection.build_corpora", return_value=corpora), patch(
            "studies.tasks.run_pipeline_task.delay"
        ) as mock_delay:
            run_corpus_detection(batch)

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.AWAITING_CONFIRMATION
        detected = list(DetectedCorpus.objects.filter(batch=batch).order_by("order"))
        assert len(detected) == 2
        assert all(d.detection_type == DetectedCorpus.DetectionType.INTEGRATED_REPORT_SPLIT for d in detected)
        categories = {d.assessment_category for d in detected}
        assert categories == {"toxicity", "environmental"}
        # Never describe the environmental one as toxicity.
        env = next(d for d in detected if d.assessment_category == "environmental")
        assert env.assessment_category != "toxicity"
        assert env.shared_page_numbers == [5]
        assert env.assessment_specific_page_numbers == [3]
        mock_delay.assert_not_called()


class TestDetectionFailure:
    def test_failure_records_error_and_preserves_upload(self, user, batch, one_question):
        original_file_name = batch.uploaded_file.name

        with patch("studies.corpus_detection.build_corpora", side_effect=RuntimeError("pdf parse exploded")):
            run_corpus_detection(batch)

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.FAILED
        assert "pdf parse exploded" in batch.split_error_message
        assert batch.uploaded_file.name == original_file_name  # untouched
        assert DetectedCorpus.objects.filter(batch=batch).count() == 0

    def test_retry_after_failure_can_succeed(self, user, batch, one_question):
        with patch("studies.corpus_detection.build_corpora", side_effect=RuntimeError("boom")):
            run_corpus_detection(batch)
        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.FAILED

        with patch("studies.tasks.run_pipeline_task.delay"):
            run_corpus_detection(batch)  # retry, real build_corpora this time

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CONFIRMED

    def test_fallback_to_single_corpus_after_failure(self, user, batch, one_question):
        with patch("studies.corpus_detection.build_corpora", side_effect=RuntimeError("boom")):
            run_corpus_detection(batch)
        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.FAILED

        with patch("studies.tasks.run_pipeline_task.delay"):
            process_as_single_corpus(batch)

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CONFIRMED
        assert Study.objects.filter(batch=batch).count() == 1


class TestConfirmDetectedCorpora:
    def _awaiting_batch_with_two_corpora(self, batch):
        corpora = [
            _fake_corpus(source="integrated", label="Toxicity", page_numbers=[1], assessment_category="toxicity"),
            _fake_corpus(source="integrated", label="Residue", page_numbers=[2], assessment_category="residue"),
        ]
        with patch("studies.corpus_detection.build_corpora", return_value=corpora):
            run_corpus_detection(batch)
        batch.refresh_from_db()
        return batch

    def test_only_included_corpora_get_confirmed_by_default(self, user, batch, one_question):
        batch = self._awaiting_batch_with_two_corpora(batch)
        residue = DetectedCorpus.objects.get(batch=batch, assessment_category="residue")
        residue.included = False
        residue.save(update_fields=["included"])

        with patch("studies.tasks.run_pipeline_task.delay") as mock_delay:
            studies = confirm_detected_corpora(batch, confirmed_by=user)

        assert len(studies) == 1
        assert studies[0].assessment_category == "toxicity"
        assert Study.objects.filter(batch=batch).count() == 1
        mock_delay.assert_called_once()

        batch.refresh_from_db()
        assert batch.split_status == UploadBatch.SplitStatus.CONFIRMED
        assert batch.confirmed_by_id == user.id

    def test_explicit_corpus_ids_override_included_flags(self, user, batch, one_question):
        batch = self._awaiting_batch_with_two_corpora(batch)
        residue = DetectedCorpus.objects.get(batch=batch, assessment_category="residue")

        with patch("studies.tasks.run_pipeline_task.delay"):
            studies = confirm_detected_corpora(batch, confirmed_by=user, corpus_ids=[residue.id])

        assert len(studies) == 1
        assert studies[0].assessment_category == "residue"

    def test_raises_if_not_awaiting_confirmation(self, user, batch, one_question):
        with pytest.raises(ValueError):
            confirm_detected_corpora(batch, confirmed_by=user)

    def test_raises_if_nothing_selected(self, user, batch, one_question):
        batch = self._awaiting_batch_with_two_corpora(batch)
        DetectedCorpus.objects.filter(batch=batch).update(included=False)

        with pytest.raises(ValueError):
            confirm_detected_corpora(batch, confirmed_by=user)

    def test_excluded_corpus_never_gets_a_study_or_any_cost(self, user, batch, one_question):
        batch = self._awaiting_batch_with_two_corpora(batch)
        residue = DetectedCorpus.objects.get(batch=batch, assessment_category="residue")
        residue.included = False
        residue.save(update_fields=["included"])

        with patch("studies.tasks.run_pipeline_task.delay"):
            confirm_detected_corpora(batch, confirmed_by=user)

        assert Study.objects.filter(source_corpus=residue).count() == 0
        assert PipelineRun.objects.filter(study__source_corpus=residue).count() == 0


class TestMaterializeStudyPages:
    def test_study_page_rows_created_from_cached_provenance(self, user, batch, one_question):
        with patch("studies.tasks.run_pipeline_task.delay"):
            run_corpus_detection(batch)  # single corpus, real build_corpora, auto-confirms

        study = Study.objects.get(batch=batch)
        assert StudyPage.objects.filter(study=study).count() == 1
        page = StudyPage.objects.get(study=study, page_number=1)
        assert page.extraction_method == StudyPage.ExtractionMethod.TEXT

        assert study.detection_type == DetectedCorpus.DetectionType.SINGLE_DOCUMENT
        assert study.page_range == [1]

    def test_integrated_corpus_with_no_page_provenance_creates_zero_study_pages(self, user, batch, one_question):
        """The 'integrated' path never calls _pages_to_documents, so it
        has no per-page OCR provenance at all — confirming it must not
        crash, it should just produce a Study with zero StudyPage rows."""
        corpus = _fake_corpus(source="integrated", label="Toxicity", page_numbers=[1], assessment_category="toxicity")
        with patch("studies.corpus_detection.build_corpora", return_value=[corpus]), patch(
            "studies.tasks.run_pipeline_task.delay"
        ):
            run_corpus_detection(batch)  # single 'integrated' corpus, no split_requested -> auto-confirm path

        batch.refresh_from_db()
        # Auto-confirm still applies (exactly one corpus, split not requested) —
        # source being 'integrated' doesn't change that; only the *count* does.
        study = Study.objects.get(batch=batch)
        assert StudyPage.objects.filter(study=study).count() == 0
        assert study.assessment_category == "toxicity"
