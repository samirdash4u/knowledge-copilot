"""Core data types shared across ingestion, retrieval and generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Role(IntEnum):
    """Roles are hierarchical: a higher role can see everything a lower role can."""

    EMPLOYEE = 1
    MANAGER = 2
    ADMIN = 3

    @classmethod
    def parse(cls, value: str | Role) -> Role:
        if isinstance(value, Role):
            return value
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown role: {value!r}") from exc

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Built only from a verified token, never from request bodies."""

    user_id: str
    role: Role
    tenant: str


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    tenant: str
    title: str
    section: str
    text: str
    min_role: Role
    source: str = ""
    page: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def allowed_roles(self) -> list[str]:
        return [r.label for r in Role if r >= self.min_role]

    def to_payload(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "tenant": self.tenant,
            "title": self.title,
            "section": self.section,
            "text": self.text,
            "min_role": self.min_role.label,
            "allowed_roles": self.allowed_roles,
            "source": self.source,
            "page": self.page,
            "metadata": self.metadata,
        }

    @classmethod
    def from_payload(cls, p: dict) -> Chunk:
        return cls(
            chunk_id=p["chunk_id"],
            doc_id=p["doc_id"],
            tenant=p["tenant"],
            title=p["title"],
            section=p["section"],
            text=p["text"],
            min_role=Role.parse(p["min_role"]),
            source=p.get("source", ""),
            page=p.get("page"),
            metadata=p.get("metadata") or {},
        )


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float


@dataclass
class Citation:
    marker: int
    chunk_id: str
    doc_id: str
    title: str
    section: str
    source: str
    snippet: str


@dataclass
class Answer:
    text: str
    citations: list[Citation]
    refused: bool = False
    cached: bool = False
    uncited_sentences: list[str] = field(default_factory=list)
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    faithfulness: float | None = None  # see generation.citations.faithfulness
    usage: dict[str, int] = field(default_factory=dict)  # LLM tokens spent on this request
