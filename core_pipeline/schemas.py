"""Shared plain-Python data classes used across the pipeline.

Renamed from the original `models.py` to avoid colliding — in every
reader's head as much as on disk — with Django's own per-app `models.py`
convention. Nothing here is a Django model; these are never persisted
directly. The `studies` app's actual Django models translate to/from these
where the pipeline needs them (see `document_processor.build_corpora` and
`search.vector_store.VectorStore`).
"""


class Document:
    def __init__(self, content: str, metadata: dict):
        self.content = content
        self.metadata = metadata


class SearchResult:
    def __init__(self, document: Document, score: float):
        self.document = document
        self.score = score

    def __repr__(self):
        preview = self.document.content[:80]
        return f"SearchResult(score={self.score:.4f}, content='{preview}...')"


class Chunk:
    def __init__(self, page_start: int, page_end: int, title: str | None):  # page numbers are 1-based.
        self.page_start = page_start
        self.page_end = page_end
        self.title = title
        self.page_range = [n for n in range(page_start, page_end + 1)]
