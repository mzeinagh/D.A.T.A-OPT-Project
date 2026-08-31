"""Tests for Phase 3's additive OCR-provenance/timeout instrumentation in
document_processor.py. `_pages_to_documents` takes `study_fp` only to pass
through to `ocr_docling`, which every test here mocks — so a placeholder
path is fine for those. Tests that exercise `build_corpora` itself need a
real (tiny, synthetic) PDF, built with pymupdf directly rather than
checking in a binary fixture.
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest

from core_pipeline import document_processor as dp


def _make_pdf(tmp_path, page_texts):
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    fp = str(tmp_path / "synthetic.pdf")
    doc.save(fp)
    doc.close()
    return fp


class TestOnPageCallback:
    def test_called_once_per_page_in_range_including_toc(self):
        seen = []
        clean_pages = ["Regular page one.", "Table of Contents\n" + "\n".join(f"Section {i} ... {i}" for i in range(1, 10)), "Regular page three."]

        with patch.object(dp, "is_toc", side_effect=[False, True, False]):
            docs = dp._pages_to_documents("fake.pdf", clean_pages, on_page=seen.append)

        assert [p["page_num"] for p in seen] == [1, 2, 3]
        assert [p["is_toc"] for p in seen] == [False, True, False]
        # TOC page dropped from docs, others kept.
        assert [d.metadata["page_num"] for d in docs] == [1, 3]

    def test_page_range_filters_both_docs_and_callback(self):
        seen = []
        clean_pages = ["p1", "p2", "p3", "p4"]
        docs = dp._pages_to_documents("fake.pdf", clean_pages, page_range=[2, 3], on_page=seen.append)

        assert [p["page_num"] for p in seen] == [2, 3]
        assert [d.metadata["page_num"] for d in docs] == [2, 3]

    def test_raw_pages_supplies_raw_text_distinct_from_cleaned(self):
        seen = []
        raw_pages = ["  RAW   page   one  ", "  RAW   page   two  "]
        clean_pages = ["page one", "page two"]

        dp._pages_to_documents("fake.pdf", clean_pages, raw_pages=raw_pages, on_page=seen.append)

        assert seen[0]["raw_text"] == "  RAW   page   one  "
        assert seen[0]["cleaned_text"] == "page one"

    def test_without_raw_pages_raw_text_falls_back_to_cleaned(self):
        seen = []
        dp._pages_to_documents("fake.pdf", ["only cleaned"], on_page=seen.append)
        assert seen[0]["raw_text"] == "only cleaned"


class TestNonTablePage:
    def test_extraction_method_text_no_ocr_attempted(self):
        seen = []
        with patch.object(dp, "is_table", return_value=False):
            dp._pages_to_documents("fake.pdf", ["plain text page"], on_page=seen.append)

        assert seen[0]["extraction_method"] == "text"
        assert seen[0]["ocr_attempted"] is False
        assert seen[0]["ocr_succeeded"] is False
        assert seen[0]["ocr_duration_ms"] is None


class TestTablePageOcrSuccess:
    def test_ocr_result_used_as_final_text(self):
        seen = []
        with patch.object(dp, "is_table", return_value=True), patch.object(
            dp, "ocr_docling", return_value="| a | b |\n|---|---|\n| 1 | 2 |"
        ):
            docs = dp._pages_to_documents("fake.pdf", ["some table-looking text"], on_page=seen.append)

        assert seen[0]["extraction_method"] == "ocr"
        assert seen[0]["ocr_attempted"] is True
        assert seen[0]["ocr_succeeded"] is True
        assert seen[0]["ocr_error"] is None
        assert seen[0]["ocr_duration_ms"] >= 0
        assert docs[0].content == "| a | b |\n|---|---|\n| 1 | 2 |"


class TestTablePageOcrFailure:
    def test_exception_falls_back_to_cleaned_text_and_is_recorded(self):
        seen = []
        with patch.object(dp, "is_table", return_value=True), patch.object(
            dp, "ocr_docling", side_effect=RuntimeError("docling exploded")
        ):
            docs = dp._pages_to_documents("fake.pdf", ["table page content"], on_page=seen.append)

        assert seen[0]["extraction_method"] == "fallback_text"
        assert seen[0]["ocr_attempted"] is True
        assert seen[0]["ocr_succeeded"] is False
        assert "docling exploded" in seen[0]["ocr_error"]
        # The page survives — content falls back to the cleaned text rather than being lost.
        assert docs[0].content == "table page content"

    def test_one_failed_page_does_not_abort_remaining_pages(self):
        seen = []
        with patch.object(dp, "is_table", side_effect=[True, False]), patch.object(
            dp, "ocr_docling", side_effect=RuntimeError("boom")
        ):
            docs = dp._pages_to_documents("fake.pdf", ["bad table page", "fine page"], on_page=seen.append)

        assert len(docs) == 2
        assert docs[0].content == "bad table page"
        assert docs[1].content == "fine page"


class TestOcrTimeout:
    def test_slow_ocr_call_times_out_and_falls_back(self):
        def slow_ocr(*args, **kwargs):
            time.sleep(0.5)
            return "too slow"

        seen = []
        with patch.object(dp, "is_table", return_value=True), patch.object(dp, "ocr_docling", side_effect=slow_ocr):
            docs = dp._pages_to_documents(
                "fake.pdf", ["table page"], on_page=seen.append, ocr_timeout_seconds=0.05
            )

        assert seen[0]["extraction_method"] == "fallback_text"
        assert seen[0]["ocr_succeeded"] is False
        assert seen[0]["ocr_error"] is not None
        assert docs[0].content == "table page"

    def test_no_timeout_by_default_waits_for_slow_call(self):
        def slow_ocr(*args, **kwargs):
            time.sleep(0.05)
            return "eventually done"

        seen = []
        with patch.object(dp, "is_table", return_value=True), patch.object(dp, "ocr_docling", side_effect=slow_ocr):
            docs = dp._pages_to_documents("fake.pdf", ["table page"], on_page=seen.append)

        assert seen[0]["extraction_method"] == "ocr"
        assert docs[0].content == "eventually done"


class TestBackwardCompatibility:
    def test_output_identical_with_and_without_new_params(self):
        clean_pages = ["page one", "page two"]
        with patch.object(dp, "is_table", return_value=False):
            docs_old_style = dp._pages_to_documents("fake.pdf", clean_pages)
            docs_new_params_unused = dp._pages_to_documents("fake.pdf", clean_pages, on_page=None, ocr_timeout_seconds=None)

        assert [d.content for d in docs_old_style] == [d.content for d in docs_new_params_unused]
        assert [d.metadata for d in docs_old_style] == [d.metadata for d in docs_new_params_unused]


class TestBuildCorporaSourceKey:
    def test_single_study_path_has_source_single(self, tmp_path):
        pdf_fp = _make_pdf(tmp_path, ["Just a short simple report page with some content on it."])
        with patch.object(dp, "chunk_integrated", return_value=[]):
            corpora = dp.build_corpora(pdf_fp)

        assert len(corpora) == 1
        assert corpora[0]["source"] == "single"

    def test_on_page_reaches_through_build_corpora_single_path(self, tmp_path):
        pdf_fp = _make_pdf(tmp_path, ["A simple page of study text."])
        seen = []
        with patch.object(dp, "chunk_integrated", return_value=[]):
            dp.build_corpora(pdf_fp, on_page=seen.append)

        assert len(seen) == 1
        assert seen[0]["page_num"] == 1


class TestBuildCorpusForPageRange:
    def test_returns_single_corpus_for_explicit_range(self, tmp_path):
        pdf_fp = _make_pdf(tmp_path, ["Page one text.", "Page two text.", "Page three text."])
        seen = []
        corpus = dp.build_corpus_for_page_range(pdf_fp, page_range=[2, 3], label="2_3", on_page=seen.append)

        assert corpus["label"] == "2_3"
        assert corpus["source"] == "single"
        assert [p["page_num"] for p in seen] == [2, 3]
        assert [d.metadata["page_num"] for d in corpus["docs"]] == [2, 3]


class TestCorpusReviewMetadata:
    def test_single_corpus_has_page_numbers_no_shared_no_category(self, tmp_path):
        pdf_fp = _make_pdf(tmp_path, ["Page one text.", "Page two text."])
        with patch.object(dp, "chunk_integrated", return_value=[]):
            corpora = dp.build_corpora(pdf_fp)

        corpus = corpora[0]
        assert corpus["page_numbers"] == [1, 2]
        assert corpus["shared_page_numbers"] == []
        assert corpus["assessment_category"] == ""

    def test_missing_title_page_and_summary_produce_warnings(self, tmp_path):
        pdf_fp = _make_pdf(tmp_path, ["Just some ordinary body text, nothing special here."])
        with patch.object(dp, "chunk_integrated", return_value=[]), patch.object(
            dp, "_find_title_page", return_value=None
        ), patch.object(dp, "_get_summary", return_value=None):
            corpora = dp.build_corpora(pdf_fp)

        warnings = corpora[0]["detection_warnings"]
        assert "No title page detected." in warnings
        assert "No summary/abstract section found." in warnings

    def test_build_corpus_for_page_range_also_carries_review_metadata(self, tmp_path):
        pdf_fp = _make_pdf(tmp_path, ["Page one.", "Page two."])
        corpus = dp.build_corpus_for_page_range(pdf_fp, page_range=[1, 2])
        assert corpus["page_numbers"] == [1, 2]
        assert corpus["assessment_category"] == ""
        assert isinstance(corpus["detection_warnings"], list)

    def test_integrated_corpus_carries_category_and_shared_pages(self, tmp_path):
        """Uses SimpleNamespace stand-ins for chunk_integrated's Section
        (a local dataclass inside that function, not importable directly) —
        build_corpora's integrated branch only reads .title/.page_num/.content."""
        pdf_fp = _make_pdf(tmp_path, ["Title page.", "Toxicity section page.", "Summary page."])
        shared = SimpleNamespace(page_num=3, content="Overall report summary.", title="Summary")
        target = SimpleNamespace(page_num=2, content="Toxicity assessment details.", title="Toxicity -> Repeat Dose")

        with patch.object(dp, "chunk_integrated", return_value=[shared, target]):
            corpora = dp.build_corpora(pdf_fp)

        assert len(corpora) == 1
        corpus = corpora[0]
        assert corpus["source"] == "integrated"
        assert corpus["assessment_category"] == "toxicity"
        assert corpus["shared_page_numbers"] == [3]
        assert corpus["page_numbers"] == [2, 3]
        assert corpus["label"] == "Toxicity_Repeat Dose"

    def test_environmental_and_residue_are_not_toxicity(self, tmp_path):
        """With build_corpora's *default* target_high_level/negative_titles,
        an environmental section is filtered out entirely before it's ever
        classified (negative_titles defaults to ["environmental",
        "residue"]) — falling through to the 'single' path instead of
        surfacing as its own corpus. That default is exactly right for a
        caller that only ever wants toxicity sections. The corpus-detection
        service (studies/corpus_detection.py) deliberately calls
        build_corpora with a *broadened* target_high_level and
        negative_titles=[] instead, specifically so environmental/residue
        sections DO surface as their own reviewable corpora — correctly
        labeled, never silently dropped nor mislabeled as toxicity — per
        the requirement that users get to exclude assessment types
        themselves rather than have them disappear before review. This
        test exercises that broadened call shape directly.
        """
        pdf_fp = _make_pdf(tmp_path, ["Environmental section page."])
        target = SimpleNamespace(page_num=1, content="Environmental fate and effects.", title="Environmental -> Aquatic Toxicity")

        with patch.object(dp, "chunk_integrated", return_value=[target]):
            corpora = dp.build_corpora(
                pdf_fp,
                target_high_level=["toxicity", "toxicology", "toxicological", "mammalian", "environmental", "residue"],
                negative_titles=[],
            )

        assert corpora[0]["source"] == "integrated"
        assert corpora[0]["assessment_category"] == "environmental"
        assert corpora[0]["assessment_category"] != "toxicity"

    def test_default_negative_titles_drops_environmental_before_classification(self, tmp_path):
        """Documents the existing default behavior precisely, so the
        broadened call above reads as a deliberate choice, not a surprise:
        with defaults, this section never becomes its own corpus at all."""
        pdf_fp = _make_pdf(tmp_path, ["Environmental section page."])
        target = SimpleNamespace(page_num=1, content="Environmental fate and effects.", title="Environmental -> Aquatic Toxicity")

        with patch.object(dp, "chunk_integrated", return_value=[target]):
            corpora = dp.build_corpora(pdf_fp)

        assert len(corpora) == 1
        assert corpora[0]["source"] == "single"  # fell through, not 'integrated'
