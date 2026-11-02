"""Template helpers for showing extracted text page by page.

Read-only: these only reshape text the app already stored, so the
Document page and the review page can share one way of showing it.
"""
from django import template

register = template.Library()

_METHOD_LABELS = {"text": "Text", "ocr": "OCR", "fallback_text": "Fallback text"}


def _page_view(number, method, raw, cleaned, final, is_table, is_toc, ocr_attempted, ocr_succeeded):
    clean = final or cleaned or raw or ""
    raw = raw or ""
    return {
        "number": number,
        "method": method or "text",
        "method_label": _METHOD_LABELS.get(method or "text", method),
        "clean": clean,
        # Only kept when it differs, so a page whose raw and clean text match
        # isn't sent to the browser twice.
        "raw": raw if raw.strip() != clean.strip() else "",
        "is_table": bool(is_table),
        "is_toc": bool(is_toc),
        "ocr_failed": bool(ocr_attempted and not ocr_succeeded),
        "chars": len(clean),
    }


@register.filter
def page_views(source):
    """Turns `StudyPage` rows, or a `DetectedCorpus`'s
    `cached_page_provenance` list (same fields, plain dicts), into one
    uniform list of pages for `studies/_page_text.html`.
    """
    pages = []
    for p in source or []:
        if isinstance(p, dict):
            pages.append(_page_view(
                p.get("page_num"), p.get("extraction_method"), p.get("raw_text"), p.get("cleaned_text"),
                p.get("final_text"), p.get("is_table"), p.get("is_toc"), p.get("ocr_attempted"), p.get("ocr_succeeded"),
            ))
        else:
            pages.append(_page_view(
                p.page_number, p.extraction_method, p.raw_text, p.cleaned_text, p.final_text,
                p.is_table, p.is_toc, p.ocr_attempted, p.ocr_succeeded,
            ))
    pages.sort(key=lambda pg: (pg["number"] is None, pg["number"] if isinstance(pg["number"], int) else 0))
    return pages


@register.simple_tag
def question_keywords():
    """Every keyword attached to an active question, for the "Highlight
    question keywords" option. Read-only."""
    from studies.models import Question

    keywords = []
    for kws in Question.objects.filter(active=True).values_list("keywords", flat=True):
        for kw in kws or []:
            if isinstance(kw, str) and kw.strip() and kw.strip() not in keywords:
                keywords.append(kw.strip())
    return keywords
