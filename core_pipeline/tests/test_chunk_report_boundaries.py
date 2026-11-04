"""Tests for how chunk_report turns page scores into study page ranges.

Covers four bugs fixed together:
1. Printed page numbers were never read (no MULTILINE flag, text returned
   instead of a number), so the "page numbering restarts" signal never fired.
2. Each study's range ended ON the next study's first page, so every study
   also carried the next study's title page.
3. Pages before the first detected start belonged to no study.
4. A start found in an 8-page block landed on the block's first page, not on
   the study's real first page (e.g. 33 instead of 31).
Plus the "new study ID" signal compared every page with the LAST page's ID.
"""
import pymupdf
import pytest

from core_pipeline import utils


# ---------------------------------------------------------------- page numbers

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Body text of the page.\nPage 6\n", 6),
        ("Body text.\nPage 6 of 14\n", 6),
        ("Body text.\n- 12 -\n", 12),
        ("Body text.\n[7]\n", 7),
        ("Body text.\n(8)\n", 8),
        ("Body text.\n9/40\n", 9),
        ("The dose was given on page 6 of the protocol.", None),  # not alone on its line
        ("No page number here.", None),
        ("(1)\n(2)\n(3)\n", None),  # several weak matches look like a table, not a page number
    ],
)
def test_printed_page_number(text, expected):
    assert utils._printed_page_number(text) == expected


# ---------------------------------------------------------------- ranges

def _covers_every_page_once(ranges, total):
    pages = [p for start, end in ranges for p in range(start, end + 1)]
    return pages == list(range(1, total + 1))


def test_ranges_do_not_overlap_and_cover_every_page():
    ranges = utils._chunk_ranges([1, 31, 61], 90)
    assert ranges == [(1, 30), (31, 60), (61, 90)]
    assert _covers_every_page_once(ranges, 90)


def test_many_leading_pages_become_their_own_study():
    assert utils._chunk_ranges([26, 66], 83) == [(1, 25), (26, 65), (66, 83)]


def test_few_leading_pages_join_the_first_study():
    assert utils._chunk_ranges([3, 40], 70) == [(1, 39), (40, 70)]


def test_no_starts_gives_no_ranges():
    assert utils._chunk_ranges([], 50) == []


# ---------------------------------------------------------------- snapping

def test_start_snaps_back_to_the_real_first_page():
    scores = {p: 0.0 for p in range(1, 41)}
    scores.update({31: 1.1, 32: 1.07, 33: 1.17, 34: 0.17})
    assert utils._snap_boundary_back(33, scores, lookback=7) == 31


def test_start_stays_when_nothing_stronger_is_before_it():
    scores = {p: 0.0 for p in range(1, 41)}
    scores.update({33: 1.17, 34: 0.17})
    assert utils._snap_boundary_back(33, scores, lookback=7) == 33


# ---------------------------------------------------------------- end to end

_FILLER = (
    "The test item was administered to groups of animals and observations were recorded daily. "
    "Body weights, food consumption and clinical signs were measured according to the protocol. "
) * 6


def _make_multi_study_pdf(path, study_lengths):
    """Synthetic GLP-style studies: title page, GLP statement, summary,
    introduction, then body pages; every study restarts "Page n of N" and
    carries its own study number."""
    doc = pymupdf.open()
    for sid, n in enumerate(study_lengths, start=1):
        for i in range(1, n + 1):
            if i == 1:
                txt = (f"FINAL REPORT\n\nAcute Oral Toxicity Study of Compound X{sid} in Rats\n\n"
                       f"Study Number: ABC-{sid}00\n\nTesting Facility: Lab Inc\n\nSponsor: Company\n\n"
                       f"Date: 2020\n\nPage {i} of {n}")
            elif i == 2:
                txt = (f"GLP COMPLIANCE STATEMENT\n\nThis study was conducted in compliance with GLP.\n"
                       f"Study Director signature\nDate: 2020\nQuality Assurance\n"
                       f"Study Number: ABC-{sid}00\n\nPage {i} of {n}")
            elif i == 3:
                txt = (f"SUMMARY\n\nThe acute oral LD50 of compound X{sid} was greater than 2000 mg/kg.\n"
                       f"{_FILLER}\nStudy Number: ABC-{sid}00\nPage {i} of {n}")
            elif i == 4:
                txt = f"1. INTRODUCTION\n\nThis study was performed to assess toxicity.\n{_FILLER}\nPage {i} of {n}"
            else:
                txt = f"{_FILLER}\nStudy Number: ABC-{sid}00\nPage {i} of {n}"
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), txt, fontsize=9)
    doc.save(str(path))
    doc.close()


def _expected_ranges(study_lengths):
    ranges, start = [], 1
    for n in study_lengths:
        ranges.append((start, start + n - 1))
        start += n
    return ranges


@pytest.mark.parametrize(
    "study_lengths",
    [
        (30, 30, 30),
        (25, 40, 18),
        (37, 29, 22, 26),
        (45, 45),
    ],
)
def test_chunk_report_finds_each_study_exactly(tmp_path, study_lengths):
    pdf = tmp_path / "multi.pdf"
    _make_multi_study_pdf(pdf, study_lengths)

    chunks = utils.chunk_report(str(pdf))

    assert chunks is not None
    assert [(c.page_start, c.page_end) for c in chunks] == _expected_ranges(study_lengths)
    for c in chunks:
        assert c.page_range == list(range(c.page_start, c.page_end + 1))
