"""Ingestion: load documents, chunk them, embed them, write the index."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from pathlib import Path

from copilot.config import Settings
from copilot.ingest.chunker import chunk_document
from copilot.ingest.loaders import discover
from copilot.models import Chunk
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.vector import InMemoryVectorStore, VectorStore, save_chunks

log = logging.getLogger(__name__)


def corpus_version(chunks: list[Chunk]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.chunk_id.encode())
        h.update(c.min_role.label.encode())
        h.update(c.text.encode())
    return h.hexdigest()[:12]


def build_chunks(settings: Settings) -> list[Chunk]:
    docs = discover(settings.corpus_dir)
    chunks: list[Chunk] = []
    for doc in docs:
        chunks.extend(chunk_document(doc, settings.chunk_size, settings.chunk_overlap))
    return chunks


def run_ingest(settings: Settings, embedder: Embedder, store: VectorStore) -> dict:
    chunks = build_chunks(settings)
    if not chunks:
        raise RuntimeError(f"no documents found under {settings.corpus_dir}")
    vectors = embedder.embed_documents([f"{c.title}. {c.section}. {c.text}" for c in chunks])
    store.upsert(chunks, vectors)

    index_dir: Path = settings.index_dir
    save_chunks(chunks, index_dir / "chunks.jsonl")
    if isinstance(store, InMemoryVectorStore):
        store.save(index_dir)

    manifest = {
        "corpus_version": corpus_version(chunks),
        "documents": len({(c.tenant, c.doc_id) for c in chunks}),
        "chunks": len(chunks),
        "by_tenant": dict(Counter(c.tenant for c in chunks)),
        "by_min_role": dict(Counter(c.min_role.label for c in chunks)),
        "embedder": type(embedder).__name__,
        "vector_store": type(store).__name__,
    }
    (index_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("ingested %s", manifest)
    return manifest
