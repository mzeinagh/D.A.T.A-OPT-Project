"""Read a PDF's text once and share it with every detection step.

Before this, one upload extracted every page's text 4 to 5 times (split
check, integrated check x3, study building, summary search), plus once more
per split study. Each step now receives the same `PdfText`.

Two versions of each page are kept, because the steps need different ones:

- `raw_pages`: PyMuPDF's text exactly as extracted, with its original line
  breaks. Integrated-report detection (contents page, ALL-CAPS chapter
  headings, short sub-headings) and printed page numbers depend on those
  line breaks, so they always read this version.
- `clean_pages`: the same text after `clean_pymupdf_text`, which joins
  wrapped lines and tidies formatting. Split scoring, title pages and the
  study pages sent to the AI use this version, as before.

Only text is shared. Docling still opens the file itself for table pages,
because it reads the page image, not the text.
"""
from __future__ import annotations

import pymupdf

from .utils import clean_pymupdf_text


class PdfText:
    def __init__(self, raw_pages: list[str]):
        self.raw_pages = list(raw_pages)
        self._clean_pages: list[str] | None = None

    @classmethod
    def from_file(cls, pdf_fp: str) -> "PdfText":
        with pymupdf.open(pdf_fp) as doc:
            return cls([page.get_text() for page in doc])

    @property
    def clean_pages(self) -> list[str]:
        if self._clean_pages is None:
            self._clean_pages = [clean_pymupdf_text(p) for p in self.raw_pages]
        return self._clean_pages

    @property
    def page_count(self) -> int:
        return len(self.raw_pages)

    def __len__(self) -> int:
        return len(self.raw_pages)

    def raw_pages_in(self, page_range: list[int] | None) -> list[str]:
        """Raw pages with every page outside `page_range` (1-based) blanked
        out, so a search over them keeps the real page numbers but can only
        find text inside the range."""
        if page_range is None:
            return list(self.raw_pages)
        keep = set(page_range)
        return [text if (i + 1) in keep else "" for i, text in enumerate(self.raw_pages)]

    def as_document(self) -> "_TextOnlyDocument":
        """A read-only stand-in for an open `pymupdf.Document` that serves
        the stored raw text, for code written against `doc[i].get_text()`."""
        return _TextOnlyDocument(self.raw_pages)


class _TextOnlyPage:
    def __init__(self, text: str):
        self._text = text

    def get_text(self, option: str = "text", *args, **kwargs) -> str:
        if option != "text" or args or kwargs:
            raise NotImplementedError("Only plain text is stored; open the PDF for other extraction modes.")
        return self._text


class _TextOnlyDocument:
    def __init__(self, raw_pages: list[str]):
        self._pages = [_TextOnlyPage(t) for t in raw_pages]

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> _TextOnlyPage:
        return self._pages[index]

    def __iter__(self):
        return iter(self._pages)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
