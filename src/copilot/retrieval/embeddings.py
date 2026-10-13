"""Embedding backends. ``HashEmbedder`` needs no model download and is used in tests and CI."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class HashEmbedder:
    """Feature-hashed bag of unigrams and bigrams, L2-normalised. Deterministic and fast."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = tokenize(text)
        for feat in toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim))

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


class SentenceTransformerEmbedder:
    """Local embedding model, e.g. ``BAAI/bge-small-en-v1.5`` (384-d, runs fine on CPU)."""

    # bge models recommend this prefix for queries (not for passages)
    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        self._prefix = self.QUERY_PREFIX if "bge" in model_name.lower() else ""

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32)

    def embed_query(self, text: str) -> np.ndarray:
        return self.model.encode(self._prefix + text, normalize_embeddings=True)
