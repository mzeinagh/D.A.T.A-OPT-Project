"""Integrated reports (e.g. an APVMA Public Release Summary) become one
study per assessment chapter.

Modelled on a real report where these went wrong:
- a running header on every other page ("PUBLIC RELEASE SUMMARY—...")
  was taken for a heading and, because it says "summary", for the summary;
- the contents list ran onto a second page with no "Contents" heading, so
  every chapter listed there was lost;
- body pages that put section numbers ("4.1") on their own line were
  dropped as contents pages and never reached the AI;
- each section's text started part-way through its heading.
"""
import pymupdf

from core_pipeline import document_processor as dp
from core_pipeline import utils

BROAD = ["toxicity", "toxicology", "toxicological", "mammalian", "environmental", "residue", "occupational", "efficacy"]
HEADER = "PUBLIC RELEASE SUMMARY - ZETA 500 HERBICIDE"
PROSE = [
    "The test item was administered to rats by oral gavage for twenty eight days in this study.",
    "No adverse effects were observed at any dose level tested in the main study groups.",
    "Body weight and food consumption were comparable to the concurrent control animals.",
    "Clinical signs were recorded daily and no treatment related findings were reported.",
]


def _pdf(path, pages):
    doc = pymupdf.open()
    for i, lines in enumerate(pages, start=1):
        page = doc.new_page()
        # real prose differs from page to page; only headers repeat
        lines = [l[:-1] + f" (record {i})." if l.endswith(".") else l for l in lines]
        if i % 2 == 0:
            lines = [str(i), HEADER, ""] + lines  # running header on every other page
        y = 50
        for line in lines:
            page.insert_text((50, y), line, fontsize=9)
            y += 12
    doc.save(str(path))
    doc.close()
    return str(path)


def _report(tmp_path):
    return _pdf(tmp_path / "prs.pdf", [
        ["PUBLIC RELEASE SUMMARY", "on the evaluation of Compound Z"],
        ["CONTENTS", "1", "INTRODUCTION", "1", "2", "TOXICOLOGICAL ASSESSMENT", "2",
         "2.1", "Acute toxicity", "2", "3", "RESIDUES ASSESSMENT", "4", "3.1", "Metabolism", "4"],
        # contents continued, no "Contents" heading on this page
        ["4", "ENVIRONMENTAL ASSESSMENT", "6", "4.1", "Aquatic toxicity", "6",
         "5", "EFFICACY AND SAFETY ASSESSMENT", "7", "5.1", "Field trials", "7"],
        ["1", "INTRODUCTION", "1.1", "Purpose of application"] + PROSE,
        ["2", "TOXICOLOGICAL ASSESSMENT", "2.1", "Acute toxicity"] + PROSE,
        ["2.2", "Repeat-dose toxicity"] + PROSE + ["2.3", "Conclusion"] + PROSE[:2],
        ["3", "RESIDUES ASSESSMENT", "3.1", "Metabolism"] + PROSE,
        ["3.2", "Residue trials"] + PROSE,
        ["4", "ENVIRONMENTAL ASSESSMENT", "4.1", "Aquatic toxicity"] + PROSE,
        ["5", "EFFICACY AND SAFETY ASSESSMENT", "5.1", "Field trials"] + PROSE,
    ])


def test_running_header_is_found_as_repeated_line(tmp_path):
    from core_pipeline.pdf_text import PdfText
    text = PdfText.from_file(_report(tmp_path))
    assert HEADER in text.repeated_lines()
    assert "TOXICOLOGICAL ASSESSMENT" not in text.repeated_lines()


def test_chapters_include_those_on_the_continued_contents_page(tmp_path):
    chapters = utils.chunk_integrated(_report(tmp_path), chapters=True)
    titles = [c["title"] for c in chapters]
    assert "ENVIRONMENTAL ASSESSMENT" in titles
    assert "EFFICACY AND SAFETY ASSESSMENT" in titles
    by_title = {c["title"]: c for c in chapters}
    # found in the body, not on the contents pages
    assert by_title["INTRODUCTION"]["first_page"] == 4
    assert (by_title["TOXICOLOGICAL ASSESSMENT"]["first_page"], by_title["TOXICOLOGICAL ASSESSMENT"]["last_page"]) == (5, 6)


def test_one_study_per_assessment_chapter_with_every_page_reported(tmp_path):
    seen = []
    corpora = dp.build_corpora(_report(tmp_path), target_high_level=BROAD, negative_titles=[], on_page=seen.append)

    assert [c["label"] for c in corpora] == [
        "TOXICOLOGICAL ASSESSMENT", "RESIDUES ASSESSMENT", "ENVIRONMENTAL ASSESSMENT", "EFFICACY AND SAFETY ASSESSMENT",
    ]
    tox = corpora[0]
    assert tox["page_numbers"] == [4, 5, 6]  # introduction (shared) + its own two pages
    assert tox["shared_page_numbers"] == [4]
    assert {e["page_num"] for e in seen} >= {4, 5, 6, 7, 8, 9, 10}  # page text reaches StudyPage
    for c in corpora:
        assert all(len(d.content) > 100 for d in c["docs"])


def test_running_header_is_never_the_summary(tmp_path):
    corpora = dp.build_corpora(_report(tmp_path), target_high_level=BROAD, negative_titles=[])
    for c in corpora:
        if c["summary"] is not None:
            assert c["summary"].content.strip() != "ZETA 500 HERBICIDE"
            assert len(c["summary"].content) > 100
    # the toxicology chapter's "Conclusion" stands in for its summary
    assert corpora[0]["summary"] is not None and "Conclusion" in corpora[0]["summary"].content


def test_body_page_with_section_numbers_is_not_a_contents_page():
    page = "\n".join(["3", "RESIDUES ASSESSMENT", "10", "3.1", "Metabolism", "3.2", "Residue trials"] + PROSE)
    assert not utils.is_toc(page)


def test_real_contents_page_is_still_a_contents_page():
    page = "\n".join(["CONTENTS", "1", "INTRODUCTION", "1", "2", "TOXICOLOGICAL ASSESSMENT", "2", "3", "RESIDUES", "4"])
    assert utils.is_toc(page)


def test_section_text_starts_after_the_whole_heading(tmp_path):
    sections = utils.chunk_integrated(_report(tmp_path))
    for s in sections:
        assert not s.content.startswith("RY—"), s.title
    assert not any(HEADER in s.title for s in sections)
