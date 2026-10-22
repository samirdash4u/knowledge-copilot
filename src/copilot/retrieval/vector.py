"""Vector stores. Both apply the ACL as a filter inside the search, not after it."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Protocol

import numpy as np

from copilot.models import Chunk, Principal, ScoredChunk


class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None: ...

    def search(self, principal: Principal, query_vec: np.ndarray, k: int) -> list[ScoredChunk]: ...


class InMemoryVectorStore:
    """Brute-force cosine search. Fine for a few thousand chunks; persisted as .npy + .jsonl."""

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.vectors: np.ndarray | None = None

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        self.chunks = list(chunks)
        self.vectors = np.asarray(vectors, dtype=np.float32)

    def search(self, principal: Principal, query_vec: np.ndarray, k: int) -> list[ScoredChunk]:
        if self.vectors is None or not len(self.chunks):
            return []
        idx = [
            i
            for i, c in enumerate(self.chunks)
            if c.tenant == principal.tenant and principal.role >= c.min_role
        ]
        if not idx:
            return []
        sims = self.vectors[idx] @ np.asarray(query_vec, dtype=np.float32)
        order = np.argsort(-sims)[:k]
        return [ScoredChunk(self.chunks[idx[i]], float(sims[i])) for i in order]

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "vectors.npy", self.vectors)

    @classmethod
    def load(cls, directory: Path, chunks: list[Chunk]) -> InMemoryVectorStore:
        store = cls()
        store.upsert(chunks, np.load(directory / "vectors.npy"))
        return store


class QdrantVectorStore:
    """Qdrant with a payload filter on ``tenant`` and ``allowed_roles``."""

    def __init__(self, url: str, collection: str = "chunks") -> None:
        from qdrant_client import QdrantClient

        self.client = QdrantClient(url=url)
        self.collection = collection

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        from qdrant_client import models as qm

        dim = int(vectors.shape[1])
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            self.collection,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )
        for field in ("tenant", "allowed_roles"):
            self.client.create_payload_index(
                self.collection, field_name=field, field_schema=qm.PayloadSchemaType.KEYWORD
            )
        points = [
            qm.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, c.chunk_id)),
                vector=v.tolist(),
                payload=c.to_payload(),
            )
            for c, v in zip(chunks, vectors, strict=True)
        ]
        for i in range(0, len(points), 256):
            self.client.upsert(self.collection, points=points[i : i + 256])

    def drop_collections_except(self, keep: set[str]) -> None:
        """Delete older index collections (named ``chunks*``) that are not in ``keep``."""
        for c in self.client.get_collections().collections:
            if c.name.startswith("chunks") and c.name not in keep:
                self.client.delete_collection(c.name)

    def search(self, principal: Principal, query_vec: np.ndarray, k: int) -> list[ScoredChunk]:
        from qdrant_client import models as qm

        flt = qm.Filter(
            must=[
                qm.FieldCondition(key="tenant", match=qm.MatchValue(value=principal.tenant)),
                qm.FieldCondition(
                    key="allowed_roles", match=qm.MatchValue(value=principal.role.label)
                ),
            ]
        )
        res = self.client.query_points(
            self.collection,
            query=np.asarray(query_vec).tolist(),
            query_filter=flt,
            limit=k,
            with_payload=True,
        )
        return [ScoredChunk(Chunk.from_payload(p.payload), float(p.score)) for p in res.points]


def save_chunks(chunks: list[Chunk], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c.to_payload()) + "\n")


def load_chunks(path: Path) -> list[Chunk]:
    with path.open(encoding="utf-8") as f:
        return [Chunk.from_payload(json.loads(line)) for line in f if line.strip()]
