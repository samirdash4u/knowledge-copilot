"""Hybrid retrieval: BM25 + vector, fused with Reciprocal Rank Fusion, then reranked."""

from __future__ import annotations

from copilot.acl import assert_all_allowed
from copilot.models import Principal, ScoredChunk
from copilot.retrieval.bm25 import BM25Index
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.rerank import PassthroughReranker, Reranker
from copilot.retrieval.vector import VectorStore

MODES = ("bm25", "vector", "hybrid", "hybrid_rerank")


def reciprocal_rank_fusion(ranked_lists: list[list[ScoredChunk]], k: int = 60) -> list[ScoredChunk]:
    """score(d) = sum over lists of 1 / (k + rank).

    Rank-based, so the different score scales of BM25 and cosine similarity don't matter.
    """
    scores: dict[str, float] = {}
    chunks: dict[str, ScoredChunk] = {}
    for results in ranked_lists:
        for rank, r in enumerate(results, start=1):
            cid = r.chunk.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            chunks.setdefault(cid, r)
    order = sorted(scores, key=lambda c: -scores[c])
    return [ScoredChunk(chunks[c].chunk, scores[c]) for c in order]


class HybridRetriever:
    def __init__(
        self,
        bm25: BM25Index,
        vectors: VectorStore,
        embedder: Embedder,
        reranker: Reranker | None = None,
        candidates: int = 30,
        rrf_k: int = 60,
    ) -> None:
        self.bm25 = bm25
        self.vectors = vectors
        self.embedder = embedder
        self.reranker = reranker or PassthroughReranker()
        self.candidates = candidates
        self.rrf_k = rrf_k

    def retrieve(
        self, principal: Principal, query: str, k: int, mode: str = "hybrid_rerank"
    ) -> list[ScoredChunk]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        n = self.candidates
        if mode == "bm25":
            results = self.bm25.search(principal, query, k)
        elif mode == "vector":
            results = self.vectors.search(principal, self.embedder.embed_query(query), k)
        else:
            fused = reciprocal_rank_fusion(
                [
                    self.bm25.search(principal, query, n),
                    self.vectors.search(principal, self.embedder.embed_query(query), n),
                ],
                k=self.rrf_k,
            )
            if mode == "hybrid":
                results = fused[:k]
            else:
                results = self.reranker.rerank(query, fused[:n], k)
        # Defense in depth: the stores already filter, but never trust a single layer.
        assert_all_allowed(principal, results)
        return results
