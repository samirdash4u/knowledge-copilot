"""Ingestion: load documents, chunk them, embed them, write the index."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import numpy as np

from copilot.config import Settings
from copilot.ingest.chunker import chunk_document
from copilot.ingest.loaders import SUPPORTED, discover
from copilot.models import Chunk
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.vector import InMemoryVectorStore, VectorStore, save_chunks

log = logging.getLogger(__name__)

EMBED_BATCH = 64
# progress(done, total, message); may raise to stop the ingest before the index is written.
Progress = Callable[[int, int, str], None]


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


def corpus_files(corpus_dir: Path) -> list[Path]:
    """Every file ingest would read."""
    if not corpus_dir.is_dir():
        return []
    return sorted(p for p in corpus_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)


def read_manifest(index_dir: Path) -> dict | None:
    path = index_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    manifest.setdefault("built_at", path.stat().st_mtime)
    return manifest


def pending_changes(settings: Settings, manifest: dict | None = None) -> list[str]:
    """Corpus files added or changed since the last ingest, as ``tenant/file`` paths.

    A removed file shows up as ``(n document(s) removed)``. Empty means the index is current.
    """
    manifest = manifest if manifest is not None else read_manifest(settings.index_dir)
    files = corpus_files(settings.corpus_dir)
    if manifest is None:
        return [p.relative_to(settings.corpus_dir).as_posix() for p in files]
    built = float(manifest.get("built_at", 0))
    changed = [
        p.relative_to(settings.corpus_dir).as_posix() for p in files if p.stat().st_mtime > built
    ]
    known = manifest.get("files")
    if known is not None and len(files) - len(changed) < known:
        changed.append(f"({known - (len(files) - len(changed))} document(s) removed)")
    return changed


def run_ingest(
    settings: Settings,
    embedder: Embedder,
    store: VectorStore,
    progress: Progress | None = None,
) -> dict:
    started = time.time()
    files = len(corpus_files(settings.corpus_dir))

    def report(done: int, total: int, message: str) -> None:
        if progress:
            progress(done, total, message)

    report(0, 0, "Reading and chunking documents")
    chunks = build_chunks(settings)
    if not chunks:
        raise RuntimeError(f"no documents found under {settings.corpus_dir}")
    texts = [f"{c.title}. {c.section}. {c.text}" for c in chunks]
    batches = []
    for i in range(0, len(texts), EMBED_BATCH):
        report(i, len(texts), f"Embedding chunks ({i} of {len(texts)})")
        batches.append(np.asarray(embedder.embed_documents(texts[i : i + EMBED_BATCH])))
    vectors = np.concatenate(batches)
    report(len(texts), len(texts), "Writing the index")

    version = corpus_version(chunks)
    index_dir: Path = settings.index_dir
    previous = read_manifest(index_dir) or {}
    collection = getattr(store, "collection", None)
    if collection is not None:
        # Write a new Qdrant collection instead of replacing the live one, so questions keep
        # working while ingest runs. The manifest points the API at the new one.
        store.collection = f"chunks-{version}-{int(started)}"
    store.upsert(chunks, vectors)

    index_dir.mkdir(parents=True, exist_ok=True)
    save_chunks(chunks, index_dir / "chunks.jsonl")
    if isinstance(store, InMemoryVectorStore):
        store.save(index_dir)

    manifest = {
        "corpus_version": version,
        "documents": len({(c.tenant, c.doc_id) for c in chunks}),
        "chunks": len(chunks),
        "files": files,
        "by_tenant": dict(Counter(c.tenant for c in chunks)),
        "by_min_role": dict(Counter(c.min_role.label for c in chunks)),
        "embedder": type(embedder).__name__,
        "vector_store": type(store).__name__,
        # Files changed after this moment count as pending (see pending_changes).
        "built_at": started,
        "seconds": round(time.time() - started, 1),
    }
    if collection is not None:
        manifest["collection"] = store.collection
    (index_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if collection is not None:
        # Keep the previous collection: an API that has not reloaded yet still reads it.
        keep = {store.collection, previous.get("collection", "chunks")}
        store.drop_collections_except(keep)
    log.info("ingested %s", manifest)
    return manifest
