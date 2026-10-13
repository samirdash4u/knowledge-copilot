"""Load source files into :class:`Document` objects with ACL metadata.

Metadata comes from YAML front matter (Markdown/text) or a sidecar ``<file>.meta.yaml``
(PDF/HTML). If a document has no ``min_role``, it is treated as ``admin``-only: a missing tag
should hide a document, not expose it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from copilot.models import Role

log = logging.getLogger(__name__)

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}
FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Section:
    heading: str
    text: str
    min_role: Role
    page: int | None = None


@dataclass
class Document:
    doc_id: str
    title: str
    tenant: str
    min_role: Role
    source: str
    path: Path
    sections: list[Section] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "doc"


def _read_meta(path: Path, inline: dict | None) -> dict:
    meta = dict(inline or {})
    sidecar = path.with_name(path.name + ".meta.yaml")
    if sidecar.exists():
        meta.update(yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {})
    return meta


def _doc_role(meta: dict, path: Path) -> Role:
    if "min_role" not in meta:
        log.warning("%s has no min_role; defaulting to admin (fail closed)", path)
        return Role.ADMIN
    return Role.parse(meta["min_role"])


ACL_MARKER = re.compile(r"<!--\s*acl:\s*(\w+)\s*-->")
HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


def split_markdown(body: str, doc_role: Role, title: str) -> list[Section]:
    """Split Markdown into heading-scoped sections.

    A ``<!-- acl: manager -->`` marker inside a section raises that section's minimum role.
    Markers can only tighten access, never loosen it below the document's role.
    Nested headings inherit their parent's restriction.
    """
    sections: list[Section] = []
    stack: list[tuple[int, str, Role]] = []  # (level, heading, role)
    buf: list[str] = []
    cur_heading, cur_role = title, doc_role

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append(Section(heading=cur_heading, text=text, min_role=cur_role))
        buf.clear()

    for line in body.splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            level, heading = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_role = stack[-1][2] if stack else doc_role
            stack.append((level, heading, parent_role))
            cur_heading = " > ".join(h for _, h, _ in stack)
            cur_role = parent_role
            continue
        marker = ACL_MARKER.search(line)
        if marker:
            cur_role = max(cur_role, Role.parse(marker.group(1)))
            if stack:
                lvl, h, _ = stack[-1]
                stack[-1] = (lvl, h, cur_role)
            line = ACL_MARKER.sub("", line)
            if not line.strip():
                continue
        buf.append(line)
    flush()
    return sections


def load_markdown(path: Path, tenant_default: str) -> Document:
    raw = path.read_text(encoding="utf-8")
    inline: dict = {}
    m = FRONT_MATTER.match(raw)
    if m:
        inline = yaml.safe_load(m.group(1)) or {}
        raw = raw[m.end() :]
    meta = _read_meta(path, inline)
    role = _doc_role(meta, path)
    title = meta.get("title") or path.stem.replace("-", " ").title()
    return Document(
        doc_id=meta.get("doc_id") or _slug(path.stem),
        title=title,
        tenant=meta.get("tenant") or tenant_default,
        min_role=role,
        source=meta.get("source", str(path)),
        path=path,
        sections=split_markdown(raw, role, title),
        metadata={k: v for k, v in meta.items() if k not in {"doc_id", "title", "tenant"}},
    )


def load_html(path: Path, tenant_default: str) -> Document:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    meta = _read_meta(path, None)
    role = _doc_role(meta, path)
    title = meta.get("title") or (
        soup.title.string.strip() if soup.title and soup.title.string else path.stem
    )
    # Re-express headings as Markdown so the same section splitter applies.
    lines: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "pre"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name.startswith("h"):
            lines.append(f"{'#' * int(el.name[1])} {text}")
        else:
            lines.append(text)
    return Document(
        doc_id=meta.get("doc_id") or _slug(path.stem),
        title=title,
        tenant=meta.get("tenant") or tenant_default,
        min_role=role,
        source=meta.get("source", str(path)),
        path=path,
        sections=split_markdown("\n\n".join(lines), role, title),
        metadata=meta,
    )


def load_pdf(path: Path, tenant_default: str) -> Document:
    from pypdf import PdfReader

    meta = _read_meta(path, None)
    role = _doc_role(meta, path)
    reader = PdfReader(str(path))
    title = meta.get("title") or (reader.metadata.title if reader.metadata else None) or path.stem
    sections = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            sections.append(
                Section(heading=f"{title} > Page {i}", text=text, min_role=role, page=i)
            )
    return Document(
        doc_id=meta.get("doc_id") or _slug(path.stem),
        title=title,
        tenant=meta.get("tenant") or tenant_default,
        min_role=role,
        source=meta.get("source", str(path)),
        path=path,
        sections=sections,
        metadata=meta,
    )


def load_file(path: Path, tenant_default: str) -> Document:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return load_markdown(path, tenant_default)
    if suffix in {".html", ".htm"}:
        return load_html(path, tenant_default)
    if suffix == ".pdf":
        return load_pdf(path, tenant_default)
    raise ValueError(f"unsupported file type: {path}")


def discover(corpus_dir: Path) -> list[Document]:
    """Load every supported file. The first directory level under ``corpus_dir`` is the tenant."""
    docs: list[Document] = []
    seen: dict[str, Path] = {}
    for path in sorted(corpus_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        rel = path.relative_to(corpus_dir)
        tenant_default = rel.parts[0] if len(rel.parts) > 1 else "default"
        doc = load_file(path, tenant_default)
        key = f"{doc.tenant}/{doc.doc_id}"
        if key in seen:
            raise ValueError(f"duplicate doc_id {key}: {seen[key]} and {path}")
        seen[key] = path
        docs.append(doc)
    return docs
