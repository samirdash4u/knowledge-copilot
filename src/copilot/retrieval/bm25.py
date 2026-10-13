"""Keyword retrieval with BM25, pre-filtered by ACL."""

from __future__ import annotations

import numpy as np
from rank_bm25 import BM25Okapi

from copilot.acl import can_access
from copilot.models import Chunk, Principal, ScoredChunk
from copilot.retrieval.embeddings import tokenize


class BM25Index:
    """One BM25 index per tenant.

    Scores are computed over the tenant's corpus, then masked to the caller's visible chunks
    *before* taking top-k, so restricted chunks never displace visible ones. (Corpus-wide IDF
    statistics are shared; they reveal term frequencies, not text.)
    """

    def __init__(self, chunks: list[Chunk]) -> None:
        self._by_tenant: dict[str, tuple[list[Chunk], BM25Okapi]] = {}
        tenants: dict[str, list[Chunk]] = {}
        for c in chunks:
            tenants.setdefault(c.tenant, []).append(c)
        for tenant, items in tenants.items():
            corpus = [tokenize(f"{c.title} {c.section} {c.text}") for c in items]
            self._by_tenant[tenant] = (items, BM25Okapi(corpus))

    def search(self, principal: Principal, query: str, k: int) -> list[ScoredChunk]:
        entry = self._by_tenant.get(principal.tenant)
        if entry is None:
            return []
        items, bm25 = entry
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = bm25.get_scores(tokens)
        mask = np.array([can_access(principal, c) for c in items], dtype=bool)
        scores = np.where(mask, scores, -np.inf)
        order = np.argsort(-scores)[:k]
        return [
            ScoredChunk(items[i], float(scores[i]))
            for i in order
            if np.isfinite(scores[i]) and scores[i] > 0
        ]
