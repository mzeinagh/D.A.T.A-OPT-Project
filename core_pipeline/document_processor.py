"""PDF -> Documents.

Builds one or more retrieval corpora for a single PDF. A PDF can yield:
  1. Several corpora if it's a long, multi-study report and `split=True`
     (each study gets its own chunk, via `chunk_report`).
  2. Several corpora if it's an "integrated" multi-assessment regulatory
     report (via `chunk_integrated`) — one corpus per matching target
     section (e.g. per toxicity assessment), sharing any report-wide
     sections (summary/conclusion/etc).
  3. A single corpus covering the whole PDF, otherwise.

Each corpus is a dict: {'label', 'docs', 'summary', 'title_page', 'source'},
ready to be handed to VectorStore.add_documents() once and reused for every
question. 'source' is one of 'single' / 'split' / 'integrated', identifying
which of the three paths above produced it — added in Phase 3 so a caller
(studies' Celery task) can tell whether a corpus went through the
per-page OCR-provenance instrumentation below, which only the 'single' and
'split' paths use (the 'integrated' path builds its documents directly from
`chunk_integrated`'s sections and never calls `ocr_docling` at all, so
there is nothing to instrument there).

Ported from the original DATA_Project pipeline; import paths were updated
(`models` -> `.schemas`, `utils` -> `.utils`) and, in Phase 3, per-page OCR
provenance/timeout support was added to `_pages_to_documents` — additively:
every new parameter defaults to the prior behavior exactly (no timeout,
no callback), so no existing caller's behavior changes unless it opts in.
The one non-additive change is that an `ocr_docling` exception is now always
caught rather than left to propagate — previously a single bad table page
would crash the entire `build_corpora` call (and, via it, the entire batch
run); it now falls back to the page's cleaned text instead, per the
decision that one failed table page must not fail the whole study.
"""
import concurrent.futures
import re
import time

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


def _run_ocr_with_timeout(study_fp: str, page_num: int, timeout_seconds: float | None):
    """Runs `ocr_docling` for one page, optionally bounded by a timeout.

    `timeout_seconds=None` (the default) means "block exactly as the
    original pipeline always did" — no behavior change for any caller that
    doesn't pass this.

    Known, disclosed limitation: this bounds how long the *caller* waits,
    not how long Docling itself runs. `ThreadPoolExecutor.submit(...)
    .result(timeout=...)` raises on timeout but cannot forcibly stop the
    underlying call — the worker thread keeps running in the background
    until Docling itself returns. A hard kill would need a subprocess-based
    approach instead; that's flagged as follow-up work, not implemented
    here, since it's a materially bigger change than "add a timeout".
    """
    if timeout_seconds is None:
        return ocr_docling(study_fp, start_page=page_num)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ocr_docling, study_fp, start_page=page_num)
        return future.result(timeout=timeout_seconds)


def _find_title_page(clean_pages: list[str], page_offset: int = 0, max_pages: int = 7):
    """Scans the first `max_pages` pages (relative to page_offset) for a title page.

    Pre-existing bug fixed here (Phase 3), confirmed identical in the
    untouched legacy `document_processor.py`: `title_page_likeliness`
    returns a `(probability, features)` tuple per its own docstring, but
    this compared that tuple directly against `0.80` — `TypeError` on any
    real call, unconditionally breaking `build_corpora`'s title-page
    detection (and, since the split/single/integrated paths all call it,
    `build_corpora` itself) for any non-empty page list. Not a behavior
    change to the *scoring logic* — `title_page_likeliness` itself is
    untouched — just unpacking what it always actually returned.
    """
    for idx, text in enumerate(clean_pages[:max_pages]):
        probability, _features = title_page_likeliness(text)
        if probability >= 0.80:
            return Document(content=text, metadata={'page_num': page_offset + idx + 1})
    return None


def _pages_to_documents(
    study_fp: str,
    clean_pages: list[str],
    page_range: list[int] | None = None,
    raw_pages: list[str] | None = None,
    on_page=None,
    ocr_timeout_seconds: float | None = None,
):
    """Cleans/OCRs table pages and drops TOC pages, turning the rest into Documents.

    `on_page`, if given, is called once per page *within `page_range`*
    (kept or dropped-as-TOC alike, so a caller building a full audit trail
    sees every page considered) with a plain dict:
    `{page_num, raw_text, cleaned_text, final_text, is_table, is_toc,
    extraction_method, ocr_attempted, ocr_succeeded, ocr_error,
    ocr_duration_ms}`, matching `studies.StudyPage`'s fields 1:1 — this
    module has no knowledge of that model, it just shapes the callback
    payload to line up with it. `raw_pages`, if given, supplies the
    pre-`clean_pymupdf_text` text for that dict's `raw_text`; without it,
    `raw_text` falls back to the (already-cleaned) `clean_pages` entry, and
    downstream behavior is unaffected either way — the returned `docs`
    list is identical regardless of whether `on_page`/`raw_pages` are used.
    """
    docs = []
    for pdx, text in enumerate(clean_pages):
        page_num = pdx + 1
        if page_range is not None and page_num not in page_range:
            continue

        raw_text = raw_pages[pdx] if raw_pages is not None and pdx < len(raw_pages) else text
        cleaned_text = text
        final_text = text
        is_tbl = is_table(text)
        extraction_method = "text"
        ocr_attempted = False
        ocr_succeeded = False
        ocr_error = None
        ocr_duration_ms = None

        if is_tbl:
            ocr_attempted = True
            ocr_start = time.monotonic()
            try:
                final_text = _run_ocr_with_timeout(study_fp, page_num, ocr_timeout_seconds)
                extraction_method = "ocr"
                ocr_succeeded = True
            except Exception as exc:
                # One failed/timed-out table page must not fail the whole
                # study — fall back to the page's cleaned text and record
                # the failure rather than losing the page or crashing here.
                extraction_method = "fallback_text"
                ocr_succeeded = False
                ocr_error = f"{type(exc).__name__}: {exc}"
                final_text = cleaned_text
            ocr_duration_ms = (time.monotonic() - ocr_start) * 1000

        page_is_toc = is_toc(final_text)

        if on_page is not None:
            on_page({
                "page_num": page_num,
                "raw_text": raw_text,
                "cleaned_text": cleaned_text,
                "final_text": final_text,
                "is_table": is_tbl,
                "is_toc": page_is_toc,
                "extraction_method": extraction_method,
                "ocr_attempted": ocr_attempted,
                "ocr_succeeded": ocr_succeeded,
                "ocr_error": ocr_error,
                "ocr_duration_ms": ocr_duration_ms,
            })

        if not page_is_toc:
            docs.append(Document(content=final_text, metadata={'page_num': page_num}))
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
    on_page=None,
    ocr_timeout_seconds: float | None = None,
):
    """
    Returns a list of corpus dicts for `study_fp`, checked in priority order:
    split chunks -> integrated-report target sections -> whole PDF as one study.

    `on_page`/`ocr_timeout_seconds` are forwarded to `_pages_to_documents`
    for the 'split' and 'single' paths (see its docstring) — the
    'integrated' path never calls it, so they have no effect there; see
    the module docstring's note on the 'source' key for how to tell which
    path a given corpus came from.
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
            docs = _pages_to_documents(
                study_fp, clean_pages, page_range=page_range, raw_pages=pages,
                on_page=on_page, ocr_timeout_seconds=ocr_timeout_seconds,
            )

            range_pages = [clean_pages[p - 1] for p in page_range if p - 1 < len(clean_pages)]
            title_page = _find_title_page(range_pages, page_offset=page_range[0] - 1, max_pages=6)
            summary = _get_summary(study_fp, page_range=page_range)

            corpora.append({
                'label': f"{page_range[0]}_{page_range[-1]}",
                'docs': docs,
                'summary': summary,
                'title_page': title_page,
                'source': 'split',
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
                'source': 'integrated',
            })
        return corpora

    # --------------------------------------------------------- single study
    with pymupdf.open(study_fp) as doc:
        pages = [p.get_text() for p in doc]
    clean_pages = [clean_pymupdf_text(p) for p in pages]

    docs = _pages_to_documents(
        study_fp, clean_pages, raw_pages=pages, on_page=on_page, ocr_timeout_seconds=ocr_timeout_seconds
    )
    title_page = _find_title_page(clean_pages)
    summary = _get_summary(study_fp)

    return [{
        'label': None,
        'docs': docs,
        'summary': summary,
        'title_page': title_page,
        'source': 'single',
    }]


def build_corpus_for_page_range(
    study_fp: str,
    page_range: list[int],
    label: str | None = None,
    on_page=None,
    ocr_timeout_seconds: float | None = None,
):
    """Builds exactly one corpus from an explicit, already-decided page
    range — for Phase 4's confirmed-split-boundary case, where the page
    range came from a user-reviewed `DetectedStudyBoundary` rather than
    from re-running `chunk_report`'s own detection.

    Mirrors the per-chunk corpus construction inside the 'split reports'
    branch of `build_corpora` above (same helpers, same shape of result —
    including 'source': 'single', since from an OCR-provenance standpoint
    this is the same single-fixed-range case, just sourced from a
    pre-confirmed boundary instead of live detection), so a single-study
    corpus and a confirmed-split-segment corpus are indistinguishable to
    everything downstream (VectorStore, the graph, StudyPage creation).
    """
    with pymupdf.open(study_fp) as doc:
        pages = [p.get_text() for p in doc]
    clean_pages = [clean_pymupdf_text(p) for p in pages]

    docs = _pages_to_documents(
        study_fp, clean_pages, page_range=page_range, raw_pages=pages,
        on_page=on_page, ocr_timeout_seconds=ocr_timeout_seconds,
    )

    range_pages = [clean_pages[p - 1] for p in page_range if p - 1 < len(clean_pages)]
    title_page = _find_title_page(range_pages, page_offset=page_range[0] - 1, max_pages=6)
    summary = _get_summary(study_fp, page_range=page_range)

    return {
        'label': label,
        'docs': docs,
        'summary': summary,
        'title_page': title_page,
        'source': 'single',
    }
