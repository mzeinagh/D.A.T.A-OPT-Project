"""PDF -> Documents.

Builds one or more retrieval corpora for a single PDF. A PDF can yield:
  1. Several corpora if it's a long, multi-study report and `split=True`
     (each study gets its own chunk, via `chunk_report`).
  2. Several corpora if it's an "integrated" multi-assessment regulatory
     report (via `chunk_integrated`) — one corpus per matching target
     section (e.g. per toxicity assessment), sharing any report-wide
     sections (summary/conclusion/etc).
  3. A single corpus covering the whole PDF, otherwise.

Each corpus is a dict: {'label', 'docs', 'summary', 'title_page'}, ready to
be handed to VectorStore.add_documents() once and reused for every question.

Ported unchanged from the original DATA_Project pipeline, aside from
import paths (`models` -> `.schemas`, `utils` -> `.utils`).
"""
import re

import pymupdf

from .schemas import Document
from .utils import (
    chunk_integrated,
    chunk_report,
    clean_pymupdf_text,
    detect_sections,
    is_table,
    is_toc,
    ocr_docling,
    title_page_likeliness,
)

# Includes common OCR misspellings, matching the original detection logic.
SUMMARY_TARGETS = ['summary', 'sumnary', 'abstract']


def _find_title_page(clean_pages: list[str], page_offset: int = 0, max_pages: int = 7):
    """Scans the first `max_pages` pages (relative to page_offset) for a title page."""
    for idx, text in enumerate(clean_pages[:max_pages]):
        if title_page_likeliness(text) >= 0.80:
            return Document(content=text, metadata={'page_num': page_offset + idx + 1})
    return None


def _pages_to_documents(study_fp: str, clean_pages: list[str], page_range: list[int] | None = None):
    """Cleans/OCRs table pages and drops TOC pages, turning the rest into Documents."""
    docs = []
    for pdx, text in enumerate(clean_pages):
        page_num = pdx + 1
        if page_range is not None and page_num not in page_range:
            continue

        if is_table(text):
            text = ocr_docling(study_fp, start_page=page_num)

        if not is_toc(text):
            docs.append(Document(content=text, metadata={'page_num': page_num}))
    return docs


def _get_summary(study_fp: str, page_range: list[int] | None = None):
    target_sections = detect_sections(pdf_fp=study_fp, target_titles=SUMMARY_TARGETS, searching=True)

    texts, page_nums = [], []
    for t in target_sections:
        if page_range is not None and t.page_num not in page_range:
            continue
        texts.append(t.content)
        page_nums.append(t.page_num)

    if not texts:
        return None
    return Document(content="\n\n".join(texts), metadata={'page_num': "+".join(str(p) for p in page_nums)})


def build_corpora(
    study_fp: str,
    split: bool = False,
    minimum_page_for_split: int = 80,
    target_study_words: list[str] | None = None,
    store_split_results: bool = False,
    store_fp: str | None = None,
    target_high_level: list[str] | None = None,
    negative_titles: list[str] | None = None,
):
    """
    Returns a list of corpus dicts for `study_fp`, checked in priority order:
    split chunks -> integrated-report target sections -> whole PDF as one study.
    """
    target_high_level = target_high_level or ["toxicity", "toxicology", "toxicological", "mammalian"]
    negative_titles = negative_titles or ["environmental", "residue"]

    chunks = None
    if split:
        with pymupdf.open(study_fp) as doc:
            study_pages = len(doc)
        if study_pages >= minimum_page_for_split:
            chunks = chunk_report(
                pdf_fp=study_fp,
                targets=target_study_words,
                store_chunks_locally=store_split_results,
                store_fp=store_fp,
            )

    # ---------------------------------------------------------- split reports
    if chunks:
        with pymupdf.open(study_fp) as doc:
            pages = [p.get_text() for p in doc]
        clean_pages = [clean_pymupdf_text(p) for p in pages]

        corpora = []
        for c in chunks:
            page_range = c.page_range
            docs = _pages_to_documents(study_fp, clean_pages, page_range=page_range)

            range_pages = [clean_pages[p - 1] for p in page_range if p - 1 < len(clean_pages)]
            title_page = _find_title_page(range_pages, page_offset=page_range[0] - 1, max_pages=6)
            summary = _get_summary(study_fp, page_range=page_range)

            corpora.append({
                'label': f"{page_range[0]}_{page_range[-1]}",
                'docs': docs,
                'summary': summary,
                'title_page': title_page,
            })
        return corpora

    # ----------------------------------------------------- integrated reports
    sections = chunk_integrated(pdf_fp=study_fp)

    shared_high_level_titles = ['summary', 'chemistry', 'conclusion', 'introduction']
    shared_in_level_titles = ['summary', 'conclusion', 'introduction']

    shared_sections = []
    target_sections = []

    if sections:
        for s in sections:
            match = re.match(r"^(.*?)\s*->\s*(.*?)$", s.title)

            if match:
                high_level_title, low_level_title = match.group(1), match.group(2)

                if negative_titles and (
                    any(n in high_level_title.lower() for n in negative_titles)
                    or any(n in low_level_title.lower() for n in negative_titles)
                ):
                    continue

                # shared sections apply to every corpus in this report — collect once
                if any(t in high_level_title.lower() for t in shared_high_level_titles):
                    shared_sections.append(s)
                    continue

                if any(t in low_level_title.lower() for t in shared_in_level_titles):
                    shared_sections.append(s)
                    continue

                if any(t in high_level_title.lower() for t in target_high_level):
                    target_sections.append(s)
            else:
                if any(t in s.title.lower() for t in shared_high_level_titles):
                    shared_sections.append(s)

    if target_sections:
        with pymupdf.open(study_fp) as doc:
            pages = [p.get_text() for p in doc]
        clean_pages = [clean_pymupdf_text(p) for p in pages]
        title_page = _find_title_page(clean_pages)

        corpora = []
        for sec in target_sections:
            match = re.match(r"^(.*?)\s*->\s*(.*?)$", sec.title)
            high_level_title, low_level_title = match.group(1), match.group(2)

            docs = [
                Document(content=s.content, metadata={'page_num': s.page_num, 'title': s.title})
                for s in shared_sections
            ]
            docs.append(Document(content=sec.content, metadata={'page_num': sec.page_num, 'title': sec.title}))

            # only take the first matching summary/abstract for this study
            summaries = [
                d for d in docs
                if any(t in d.metadata['title'].lower() for t in SUMMARY_TARGETS)
                and any(t in d.metadata['title'].lower() for t in target_high_level)
            ]
            summary = summaries[0] if summaries else None

            corpora.append({
                'label': f"{high_level_title}_{low_level_title}",
                'docs': docs,
                'summary': summary,
                'title_page': title_page,
            })
        return corpora

    # --------------------------------------------------------- single study
    with pymupdf.open(study_fp) as doc:
        pages = [p.get_text() for p in doc]
    clean_pages = [clean_pymupdf_text(p) for p in pages]

    docs = _pages_to_documents(study_fp, clean_pages)
    title_page = _find_title_page(clean_pages)
    summary = _get_summary(study_fp)

    return [{
        'label': None,
        'docs': docs,
        'summary': summary,
        'title_page': title_page,
    }]
