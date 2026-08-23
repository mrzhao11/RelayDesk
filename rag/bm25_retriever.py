"""Small in-process BM25 retriever for the RelayDesk knowledge chunks."""
import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List


_TOKEN_RE = re.compile(
    r"https?://[^\s]+|/[A-Za-z0-9_./-]+|[A-Za-z][A-Za-z0-9_-]*|\d+|[\u4e00-\u9fff]+",
    re.I,
)


def tokenize(text: str) -> List[str]:
    """Preserve exact technical tokens and add Chinese unigram/bigram terms."""
    tokens: List[str] = []
    for match in _TOKEN_RE.finditer((text or "").lower()):
        token = match.group(0).rstrip(".,!?;:")
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.append(token)
            tokens.extend(token)
            tokens.extend(token[index:index + 2] for index in range(len(token) - 1))
        elif token:
            tokens.append(token)
    return tokens


class BM25Retriever:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._chunks: List[Dict[str, Any]] = []
        self._term_frequencies: List[Counter[str]] = []
        self._document_frequencies: Counter[str] = Counter()
        self._lengths: List[int] = []
        self._avg_length = 0.0

    def build(self, chunks: Iterable[Dict[str, Any]]) -> None:
        self._chunks = sorted((dict(chunk) for chunk in chunks), key=lambda item: item["chunk_id"])
        self._term_frequencies = []
        self._document_frequencies = Counter()
        self._lengths = []
        for chunk in self._chunks:
            searchable = " ".join((
                str(chunk.get("title", "")),
                str(chunk.get("section_path", "")),
                str(chunk.get("content", "")),
            ))
            counts = Counter(tokenize(searchable))
            self._term_frequencies.append(counts)
            length = sum(counts.values())
            self._lengths.append(length)
            self._document_frequencies.update(counts.keys())
        self._avg_length = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        if not self._chunks:
            return []
        query_terms = list(dict.fromkeys(tokenize(query)))
        size = len(self._chunks)
        scored = []
        for index, counts in enumerate(self._term_frequencies):
            score = 0.0
            length = self._lengths[index]
            for term in query_terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequencies[term]
                inverse_document_frequency = math.log(1 + (size - document_frequency + 0.5) / (document_frequency + 0.5))
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length / (self._avg_length or 1.0)
                )
                score += inverse_document_frequency * frequency * (self.k1 + 1) / denominator
            if score > 0:
                item = dict(self._chunks[index])
                item["bm25_score"] = round(score, 6)
                scored.append(item)
        scored.sort(key=lambda item: (-item["bm25_score"], item["chunk_id"]))
        return scored[: max(0, top_k)]
