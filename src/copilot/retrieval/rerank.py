"""Rerankers. A cross-encoder scores (query, passage) pairs jointly, which is slower but sharper."""

from __future__ import annotations

from typing import Protocol

from copilot.models import ScoredChunk


class Reranker(Protocol):
    def rerank(self, query: str, results: list[ScoredChunk], k: int) -> list[ScoredChunk]: ...


class PassthroughReranker:
    def rerank(self, query: str, results: list[ScoredChunk], k: int) -> list[ScoredChunk]:
        return results[:k]


class CrossEncoderReranker:
    """Local cross-encoder, e.g. ``BAAI/bge-reranker-base``."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name)

    def rerank(self, query: str, results: list[ScoredChunk], k: int) -> list[ScoredChunk]:
        if not results:
            return []
        scores = self.model.predict([(query, r.chunk.text) for r in results])
        ranked = sorted(zip(results, scores, strict=True), key=lambda x: -float(x[1]))
        return [ScoredChunk(r.chunk, float(s)) for r, s in ranked[:k]]
