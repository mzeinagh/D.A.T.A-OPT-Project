"""Tokenization, keyword-candidate generation, and a dependency-free BM25
implementation used by VectorStore's hybrid search.

Ported unchanged from the original DATA_Project pipeline.
"""
import math
import re
from collections import Counter, defaultdict

# Generic, not domain-specific — just enough to keep candidate phrases from
# being pure filler ("the", "of", "is"...). Safe to trim/extend.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "is", "are", "was",
    "were", "be", "been", "being", "of", "to", "in", "on", "at", "for",
    "with", "by", "from", "as", "this", "that", "these", "those", "it",
    "its", "does", "do", "did", "has", "have", "had", "can", "could",
    "should", "would", "will", "shall", "may", "might", "not", "so",
}


def tokenize(text: str) -> list[str]:
    return re.findall(r"\b(?:[A-Za-z]+(?:/[A-Za-z]+)?|\d+(?:\.\d+)?%?)\b|%", text.lower())  # All alphanumeric numbers and instances containing '/' and '%' but none of the other symbols (like puncutation marks)


def generate_candidates(text: str, max_n: int = 2) -> list[str]:
    """
    Generic n-gram candidate generator (1 to max_n words), dropping
    candidates that are entirely stopwords or start/end on a stopword
    (keeps 'test guideline' but rejects 'to the' or 'in a').
    """
    tokens = tokenize(text)
    candidates = []
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            if all(t in STOPWORDS for t in gram):
                continue
            candidates.append(" ".join(gram))
    return list(dict.fromkeys(candidates))  # dedupe, keep order


class SimpleBM25:
    def __init__(self, tokenized_corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = tokenized_corpus
        self.doc_lens = [len(doc) for doc in tokenized_corpus]
        self.avg_doc_len = sum(self.doc_lens) / len(self.doc_lens) if self.doc_lens else 0
        self.doc_freqs: list[Counter] = [Counter(doc) for doc in tokenized_corpus]
        self.idf: dict[str, float] = self._compute_idf()
        self.max_idf = max(self.idf.values()) if self.idf else 1.0

    def _compute_idf(self) -> dict[str, float]:
        df = defaultdict(int)
        for doc in self.corpus:
            for term in set(doc):
                df[term] += 1
        n = len(self.corpus)
        return {term: math.log((n - freq + 0.5) / (freq + 0.5) + 1) for term, freq in df.items()}

    def get_scores(self, query_tokens: list[str], term_weights: dict[str, float] | None = None) -> list[float]:
        scores = [0.0] * len(self.corpus)
        for term in query_tokens:
            if term not in self.idf:
                continue
            weight = term_weights.get(term, 1.0) if term_weights else 1.0
            term_idf = self.idf[term]
            for i, doc_freqs in enumerate(self.doc_freqs):
                freq = doc_freqs.get(term, 0)
                if freq == 0:
                    continue
                doc_len = self.doc_lens[i]
                denom = freq + self.k1 * (1 - self.b + self.b * doc_len / self.avg_doc_len)
                scores[i] += weight * term_idf * (freq * (self.k1 + 1)) / denom
        return scores
