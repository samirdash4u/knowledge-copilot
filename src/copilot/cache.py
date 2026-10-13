"""Semantic answer cache, partitioned by access scope.

The partition key is ``(access_scope(principal), corpus_version)``. Without the scope, a
manager's cached answer (built from manager-only chunks) would be served to an employee who
asks a similar question, bypassing the ACL entirely. Without the corpus version, re-ingesting
documents would leave stale answers behind.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Protocol

import numpy as np

from copilot.acl import access_scope
from copilot.models import Answer, Citation, Principal


def _answer_to_json(a: Answer) -> str:
    return json.dumps(asdict(a))


def _answer_from_json(s: str) -> Answer:
    d = json.loads(s)
    d["citations"] = [Citation(**c) for c in d["citations"]]
    return Answer(**d)


def partition_key(principal: Principal, corpus_version: str) -> str:
    return f"{access_scope(principal)}:{corpus_version}"


class AnswerCache(Protocol):
    def get(self, principal: Principal, query_vec: np.ndarray) -> Answer | None: ...

    def put(self, principal: Principal, query_vec: np.ndarray, answer: Answer) -> None: ...


class NullCache:
    def get(self, principal: Principal, query_vec: np.ndarray) -> Answer | None:
        return None

    def put(self, principal: Principal, query_vec: np.ndarray, answer: Answer) -> None:
        pass


class InMemorySemanticCache:
    def __init__(self, corpus_version: str, threshold: float = 0.95, ttl_s: int = 3600) -> None:
        self.corpus_version = corpus_version
        self.threshold = threshold
        self.ttl_s = ttl_s
        self._store: dict[str, list[tuple[np.ndarray, str, float]]] = {}

    def get(self, principal: Principal, query_vec: np.ndarray) -> Answer | None:
        now = time.time()
        entries = [
            e
            for e in self._store.get(partition_key(principal, self.corpus_version), [])
            if now - e[2] < self.ttl_s
        ]
        best, best_sim = None, -1.0
        for vec, payload, _ in entries:
            sim = float(np.dot(vec, query_vec))
            if sim > best_sim:
                best, best_sim = payload, sim
        if best is not None and best_sim >= self.threshold:
            ans = _answer_from_json(best)
            ans.cached = True
            return ans
        return None

    def put(self, principal: Principal, query_vec: np.ndarray, answer: Answer) -> None:
        key = partition_key(principal, self.corpus_version)
        self._store.setdefault(key, []).append(
            (np.asarray(query_vec, dtype=np.float32), _answer_to_json(answer), time.time())
        )


class RedisSemanticCache:
    """Same semantics as the in-memory cache, stored in a Redis hash per partition."""

    def __init__(
        self, url: str, corpus_version: str, threshold: float = 0.95, ttl_s: int = 3600
    ) -> None:
        import redis

        self.r = redis.Redis.from_url(url)
        self.corpus_version = corpus_version
        self.threshold = threshold
        self.ttl_s = ttl_s

    def _key(self, principal: Principal) -> str:
        return f"copilot:cache:{partition_key(principal, self.corpus_version)}"

    def get(self, principal: Principal, query_vec: np.ndarray) -> Answer | None:
        best, best_sim = None, -1.0
        for raw in self.r.hvals(self._key(principal)):
            entry = json.loads(raw)
            sim = float(np.dot(np.asarray(entry["vec"], dtype=np.float32), query_vec))
            if sim > best_sim:
                best, best_sim = entry["answer"], sim
        if best is not None and best_sim >= self.threshold:
            ans = _answer_from_json(best)
            ans.cached = True
            return ans
        return None

    def put(self, principal: Principal, query_vec: np.ndarray, answer: Answer) -> None:
        key = self._key(principal)
        field = f"{time.time_ns()}"
        value = json.dumps(
            {"vec": np.asarray(query_vec).tolist(), "answer": _answer_to_json(answer)}
        )
        self.r.hset(key, field, value)
        self.r.expire(key, self.ttl_s)
