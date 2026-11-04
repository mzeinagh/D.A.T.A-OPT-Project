"""The PDF's text is read once per build and shared by every step.

Checks: one read per build; integrated sections are identical whether they
read the PDF themselves or the shared raw text; each split study gets its
own summary (it used to be only the first); a table page is never OCR'd
twice when an OCR cache is shared.
"""
from unittest.mock import patch

import pymupdf
import pytest

from core_pipeline import document_processor as dp
from core_pipeline import utils
from core_pipeline.pdf_text import PdfText
from core_pipeline.tests.test_chunk_report_boundaries import _make_multi_study_pdf

BROAD = ["toxicity", "toxicology", "toxicological", "mammalian", "environmental", "residue", "occupational", "efficacy"]

_PROSE = [
    "The test item was administered to rats by oral gavage for 28 days.",
    "no adverse effects were observed at any dose level in this study.",
    "Body weight and food consumption were comparable to controls.",
]
_TABLE = [
    "Table 3 Mean body weight (g)", "Dose  Day 1  Day 7  Day 14", "0  201.2  245.3  280.1",
    "100  199.8  240.2  275.4", "300  200.4  238.9  270.2", "1000  198.7  230.1  262.3",
    "p < 0.05 compared with control", "12.4  13.1  14.2  15.6", "22.1  23.4  24.8  25.0",
]


def _lines_pdf(path, pages):
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 60
        for line in lines:
            page.insert_text((50, y), line, fontsize=9)
            y += 12
    doc.save(str(path))
    doc.close()
    return str(path)


def _integrated_pdf(tmp_path):
    return _lines_pdf(tmp_path / "integrated.pdf", [
        ["PUBLIC RELEASE SUMMARY", "on the evaluation of Compound Z", "in the product Zeta Herbicide."],
        ["CONTENTS", "Summary", "1", "Chemistry Assessment", "2", "Toxicological Assessment", "3",
         "Residues Assessment", "7", "Environmental Assessment", "9"],
        ["SUMMARY"] + _PROSE,
        ["CHEMISTRY ASSESSMENT"] + _PROSE,
        ["TOXICOLOGICAL ASSESSMENT", "Acute Studies"] + _PROSE,
        ["Repeat Dose Studies"] + _PROSE,
        ["Genotoxicity Studies"] + _PROSE,
        ["Reproduction Studies"] + _PROSE,
        ["RESIDUES ASSESSMENT", "Metabolism Studies"] + _PROSE,
        ["Residue Trials"] + _PROSE,
        ["ENVIRONMENTAL ASSESSMENT", "Aquatic Studies"] + _PROSE,
        ["Soil Studies"] + _PROSE,
    ])


@pytest.fixture
def count_pdf_reads():
    """Counts pymupdf.open calls on a file path (opening an empty in-memory
    document doesn't count)."""
    opened = []
    real_open = pymupdf.open

    def counting_open(*args, **kwargs):
        if args and isinstance(args[0], str):
            opened.append(args[0])
        return real_open(*args, **kwargs)

    with patch("pymupdf.open", counting_open), patch("fitz.open", counting_open):
        yield opened


# ---------------------------------------------------------------- PdfText

def test_pdf_text_keeps_raw_line_breaks_and_cleaned_text(tmp_path):
    pdf = _lines_pdf(tmp_path / "a.pdf", [["First line", "second line."], ["Other page."]])
    text = PdfText.from_file(pdf)
    assert text.page_count == 2
    assert "First line\nsecond line." in text.raw_pages[0]
    assert text.clean_pages == [utils.clean_pymupdf_text(p) for p in text.raw_pages]


def test_raw_pages_in_blanks_other_pages_but_keeps_positions():
    text = PdfText(["p1", "p2", "p3", "p4"])
    assert text.raw_pages_in([2, 3]) == ["", "p2", "p3", ""]
    assert text.raw_pages_in(None) == ["p1", "p2", "p3", "p4"]


def test_text_only_document_serves_raw_text_and_refuses_other_modes():
    doc = PdfText(["one", "two"]).as_document()
    assert doc.page_count == 2 and len(doc) == 2
    assert doc[1].get_text() == "two"
    with pytest.raises(NotImplementedError):
        doc[0].get_text("dict")


# ---------------------------------------------------------------- read once

def test_single_study_build_reads_the_pdf_once(tmp_path, count_pdf_reads):
    pdf = _lines_pdf(tmp_path / "s.pdf", [["FINAL REPORT", "Study title"], ["SUMMARY"] + _PROSE, _PROSE])
    dp.build_corpora(pdf, target_high_level=BROAD, negative_titles=[])
    assert count_pdf_reads == [pdf]


def test_split_build_reads_the_pdf_once(tmp_path, count_pdf_reads):
    pdf = tmp_path / "multi.pdf"
    _make_multi_study_pdf(pdf, (30, 30, 30))
    count_pdf_reads.clear()
    corpora = dp.build_corpora(str(pdf), split=True, target_high_level=BROAD, negative_titles=[])
    assert len(corpora) == 3
    assert count_pdf_reads == [str(pdf)]


def test_integrated_build_reads_the_pdf_once(tmp_path, count_pdf_reads):
    pdf = _integrated_pdf(tmp_path)
    corpora = dp.build_corpora(pdf, target_high_level=BROAD, negative_titles=[])
    assert {c["source"] for c in corpora} == {"integrated"}
    assert count_pdf_reads == [pdf]


# ---------------------------------------------------------------- integrated unchanged

def test_integrated_sections_identical_with_shared_raw_text(tmp_path):
    pdf = _integrated_pdf(tmp_path)
    own_read = utils.chunk_integrated(pdf)
    shared = utils.chunk_integrated(pdf, pdf_text=PdfText.from_file(pdf))
    assert own_read, "fixture should be detected as an integrated report"
    assert [(s.page_num, s.title, s.content) for s in shared] == [(s.page_num, s.title, s.content) for s in own_read]


def test_cleaning_joins_heading_lines_so_integrated_must_use_raw_text():
    """Why integrated detection gets RAW text: cleaning joins a short
    heading onto a following line that starts in lower case, so the
    heading is no longer a line of its own and would not be found."""
    raw = "TOXICOLOGICAL ASSESSMENT\nAcute Studies\nin rats the oral LD50 was above 2000 mg/kg.\n"
    cleaned = utils.clean_pymupdf_text(raw)
    assert "Acute Studies" in raw.splitlines()
    assert "Acute Studies" not in cleaned.splitlines()


# ---------------------------------------------------------------- summaries

def test_every_split_study_gets_its_own_summary(tmp_path):
    pdf = tmp_path / "multi.pdf"
    _make_multi_study_pdf(pdf, (30, 30, 30))
    corpora = dp.build_corpora(str(pdf), split=True)

    summaries = [c["summary"] for c in corpora]
    assert all(s is not None for s in summaries)
    for corpus, compound in zip(corpora, ("X1", "X2", "X3")):
        first_summary_page = int(str(corpus["summary"].metadata["page_num"]).split("+")[0])
        assert first_summary_page in corpus["page_numbers"]
        assert compound in corpus["summary"].content
        assert "No summary/abstract section found." not in corpus["detection_warnings"]


def test_summary_search_never_crosses_into_another_study(tmp_path):
    pdf = tmp_path / "multi.pdf"
    _make_multi_study_pdf(pdf, (30, 30, 30))
    summary = dp._get_summary(str(pdf), page_range=list(range(31, 61)))
    pages = [int(p) for p in str(summary.metadata["page_num"]).split("+")]
    assert all(31 <= p <= 60 for p in pages)


# ---------------------------------------------------------------- OCR reuse

def test_table_page_is_not_ocrd_twice_with_a_shared_cache(tmp_path):
    pdf = _lines_pdf(tmp_path / "t.pdf", [["FINAL REPORT", "Study title"], _TABLE, _PROSE])
    calls = []

    def fake_ocr(path, start_page=None, end_page=None):
        calls.append(start_page)
        return f"OCR table page {start_page}"

    cache = {}
    with patch.object(dp, "ocr_docling", fake_ocr):
        first = dp.build_corpora(pdf, ocr_cache=cache)
        events = []
        again = dp.build_corpus_for_page_range(pdf, [1, 2, 3], ocr_cache=cache, on_page=events.append)

    assert calls == [2]
    assert cache[2]["final_text"] == "OCR table page 2"
    assert [d.content for d in again["docs"]] == [d.content for d in first[0]["docs"]]
    table_event = next(e for e in events if e["page_num"] == 2)
    assert table_event["extraction_method"] == "ocr" and table_event["final_text"] == "OCR table page 2"


def test_failed_ocr_is_not_cached(tmp_path):
    pdf = _lines_pdf(tmp_path / "t.pdf", [_TABLE])

    def broken_ocr(*args, **kwargs):
        raise RuntimeError("docling failed")

    cache = {}
    with patch.object(dp, "ocr_docling", broken_ocr):
        dp.build_corpora(pdf, ocr_cache=cache)
    assert cache == {}
