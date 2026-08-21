"""Shared local embedding provider for Intent and RAG."""

from __future__ import annotations

import threading
from typing import List, Sequence


class LocalSentenceTransformerEmbedding:
    """Load one SentenceTransformer and expose Chroma-compatible embeddings.

    Generic calls are instruction-free so the same vectors can be used for
    documents and intent templates. RAG queries use the model's dedicated
    query prompt through :meth:`embed_query`.
    """

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        query_prompt_name: str = "query",
        query_instruction: str = "",
        normalize_embeddings: bool = True,
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.device = device
        self.query_prompt_name = query_prompt_name
        self.query_instruction = query_instruction.strip()
        self.normalize_embeddings = normalize_embeddings
        self._model = SentenceTransformer(model_name, device=device)
        self._lock = threading.RLock()

    def __call__(self, input: Sequence[str]) -> List[List[float]]:
        with self._lock:
            vectors = self._model.encode(
                list(input),
                normalize_embeddings=self.normalize_embeddings,
                convert_to_numpy=True,
            )
        return vectors.tolist()

    def embed_query(self, query: str) -> List[float]:
        encoded_query = query
        prompt_name = self.query_prompt_name
        if self.query_instruction:
            encoded_query = f"Instruct: {self.query_instruction}\nQuery: {query}"
            prompt_name = None
        with self._lock:
            vectors = self._model.encode(
                [encoded_query],
                prompt_name=prompt_name,
                normalize_embeddings=self.normalize_embeddings,
                convert_to_numpy=True,
            )
        return vectors[0].tolist()
