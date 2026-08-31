"""Hybrid (semantic + BM25, fused via RRF) retrieval index over a PDF's pages.

Ported unchanged from the original DATA_Project pipeline, aside from import
paths and one deliberate change: `model_dir` no longer defaults to a
hardcoded local path. The embedding model stays local (`sentence-
transformers`, per the decision to avoid an embedding API and preserve
offline retrieval) but the *path* to it is configuration, not a pipeline
default — callers (the `studies` app's services, in Phase 3) pass
`settings.EMBEDDING_MODEL_PATH`, which is itself environment-driven.
"""
import numpy as np
from sentence_transformers import SentenceTransformer

from ..schemas import Document, SearchResult
from .bm25 import SimpleBM25, generate_candidates, tokenize


class VectorStore:
    def __init__(self, model_dir: str, keywords: list[str] | None = None):
        self.model = SentenceTransformer(model_dir)
        self.documents: list[Document] = []
        self.embeddings: np.ndarray | None = None
        self.bm25: SimpleBM25 | None = None
        self._tokenized_corpus: list[list[str]] = []
        self.keywords = keywords

    def add_documents(self, documents: list[Document]):
        new_embeddings = self.model.encode(
            [doc.content for doc in documents],
            normalize_embeddings=True,
            show_progress_bar=True,
        )
        self.documents.extend(documents)
        self.embeddings = (
            new_embeddings if self.embeddings is None
            else np.vstack([self.embeddings, new_embeddings])
        )
        self._tokenized_corpus.extend(tokenize(doc.content) for doc in documents)
        self.bm25 = SimpleBM25(self._tokenized_corpus)

    def _extract_keywords(self, query: str, query_embedding: np.ndarray, top_frac: float = 0.1):
        """
        Combines two independent signals per candidate phrase:
        - semantic centrality: cosine sim between candidate's embedding and
          the full query's embedding (via your local model)
        - corpus rarity: average IDF of the candidate's tokens, normalized
          by the corpus's max IDF (from BM25, already computed)
        Returns {phrase: combined_score} for the top_frac fraction of candidates.
        """
        candidates = generate_candidates(query)
        if not candidates:
            return {}

        candidate_embeddings = self.model.encode(candidates, normalize_embeddings=True)
        semantic_scores = (candidate_embeddings @ query_embedding.T).flatten()

        idf_scores = []
        for phrase in candidates:
            toks = phrase.split()
            avg_idf = sum(self.bm25.idf.get(t, 0.0) for t in toks) / len(toks)
            idf_scores.append(avg_idf / self.bm25.max_idf if self.bm25.max_idf else 0.0)
        idf_scores = np.array(idf_scores)

        # blend of "central to the query's meaning" and "rare in the corpus"
        combined = 0.7 * semantic_scores + 0.3 * idf_scores

        n_keep = max(1, int(len(candidates) * top_frac))
        top_idx = np.argsort(combined)[::-1][:n_keep]

        return {candidates[i]: float(combined[i]) for i in top_idx}

    def search(
        self,
        query: str,
        k: int = 3,
        score_threshold: float | None = None,
        candidate_pool: int = 15,
        rrf_k: int = 60,
        keyword_boost: float = 2.0,
    ):
        if self.embeddings is None or len(self.documents) == 0:
            raise ValueError("Vectorstore is empty. Call add_documents() first.")

        query_embedding = self.model.encode([query], normalize_embeddings=True)
        cosine_scores = (self.embeddings @ query_embedding.T).flatten()

        # --- keyword-aware BM25 ---
        if (self.keywords is not None) and (len(self.keywords) >= 1):
            keywords = {i: 1.0 for i in self.keywords}
        else:
            keywords = self._extract_keywords(query, query_embedding)  # {phrase: score in [0,1]-ish}

        query_tokens = tokenize(query)
        term_weights = {tok: 1.0 for tok in query_tokens}
        for phrase, kw_score in keywords.items():
            for tok in phrase.split():
                boosted = 1.0 + keyword_boost * kw_score
                term_weights[tok] = max(term_weights.get(tok, 1.0), boosted)

        bm25_scores = np.array(self.bm25.get_scores(query_tokens, term_weights=term_weights))

        # flat bonus for verbatim multi-word phrase matches, weighted by the phrase's own score
        phrase_keywords = {p: s for p, s in keywords.items() if " " in p}
        if phrase_keywords:
            for i, doc in enumerate(self.documents):
                content_lower = doc.content.lower()
                for phrase, kw_score in phrase_keywords.items():
                    if phrase in content_lower:
                        bm25_scores[i] += keyword_boost * kw_score

        # --- fuse via RRF ---
        cosine_rank = np.argsort(cosine_scores)[::-1]
        bm25_rank = np.argsort(bm25_scores)[::-1]

        n = min(candidate_pool, len(self.documents))
        cosine_rank_pos = {idx: r for r, idx in enumerate(cosine_rank[:n])}
        bm25_rank_pos = {idx: r for r, idx in enumerate(bm25_rank[:n])}
        candidate_indices = set(cosine_rank_pos) | set(bm25_rank_pos)

        fused_scores = {}
        for idx in candidate_indices:
            score = 0.0
            if idx in cosine_rank_pos:
                score += 1 / (rrf_k + cosine_rank_pos[idx] + 1)
            if idx in bm25_rank_pos:
                score += 1 / (rrf_k + bm25_rank_pos[idx] + 1)
            fused_scores[idx] = score

        ranked = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)[:k]
        results = [
            SearchResult(document=self.documents[idx], score=float(score))
            for idx, score in ranked
        ]

        if score_threshold is not None:
            results = [r for r in results if r.score >= score_threshold]

        return results
