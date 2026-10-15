"""Split sections into overlapping chunks on paragraph and sentence boundaries."""

from __future__ import annotations

import re

from copilot.ingest.loaders import Document, _slug
from copilot.models import Chunk

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _pieces(text: str, size: int) -> list[str]:
    """Break text into units no longer than ``size``: paragraphs, then sentences, then words."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            out.append(para)
            continue
        for sent in _SENTENCE.split(para):
            if len(sent) <= size:
                out.append(sent)
            else:
                words, cur = sent.split(), ""
                for w in words:
                    # Text extracted from some PDFs has no spaces; cut such runs to size.
                    while len(w) > size:
                        if cur:
                            out.append(cur)
                            cur = ""
                        out.append(w[:size])
                        w = w[size:]
                    if cur and len(cur) + 1 + len(w) > size:
                        out.append(cur)
                        cur = w
                    else:
                        cur = f"{cur} {w}".strip()
                if cur:
                    out.append(cur)
    return out


def split_text(text: str, size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for piece in _pieces(text, size):
        if cur and cur_len + len(piece) + 2 > size:
            chunks.append("\n\n".join(cur))
            # carry trailing pieces forward as overlap
            carry: list[str] = []
            carry_len = 0
            for p in reversed(cur):
                if carry_len + len(p) > overlap:
                    break
                carry.insert(0, p)
                carry_len += len(p) + 2
            if carry_len + len(piece) > size:
                carry, carry_len = [], 0  # no room for overlap next to a full-size piece
            cur, cur_len = carry, carry_len
        cur.append(piece)
        cur_len += len(piece) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def chunk_document(doc: Document, size: int = 800, overlap: int = 120) -> list[Chunk]:
    chunks: list[Chunk] = []
    for s_idx, section in enumerate(doc.sections):
        section_slug = _slug(section.heading.split(" > ")[-1])
        for c_idx, text in enumerate(split_text(section.text, size, overlap)):
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.tenant}/{doc.doc_id}#{s_idx:02d}-{section_slug}#{c_idx}",
                    doc_id=doc.doc_id,
                    tenant=doc.tenant,
                    title=doc.title,
                    section=section.heading,
                    text=text,
                    min_role=section.min_role,
                    source=doc.source,
                    page=section.page,
                )
            )
    return chunks
