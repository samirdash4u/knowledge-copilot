"""Convert a PDF or DOCX into a tagged Markdown file under the corpus directory.

Markdown is what the loader handles best: each heading becomes a section that citations can point
at, and a ``<!-- acl: ... -->`` marker can restrict one section. Converting once, and keeping the
result in ``data/corpus``, also lets a person review and edit the text before it is indexed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from copilot.ingest.loaders import SUPPORTED, _slug, load_file
from copilot.models import Role

CONVERTIBLE = {".pdf", ".docx"}


@dataclass
class Converted:
    title: str | None  # from the file's own metadata or first heading, if any
    body: str  # Markdown without the leading "# Title"


# ---------------------------------------------------------------- DOCX


def _md_cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _docx_table(table) -> list[str]:
    rows = [[_md_cell(c.text) for c in row.cells] for row in table.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return lines


def _heading_level(style_name: str) -> int | None:
    m = re.fullmatch(r"Heading (\d)", style_name)
    return int(m.group(1)) if m else None


def convert_docx(path: Path) -> Converted:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "python-docx is not installed. With Docker, run 'python scripts/manage.py up' to "
            "rebuild the image; natively, run: pip install -e ."
        ) from exc

    document = docx.Document(str(path))
    title = (document.core_properties.title or "").strip() or None
    blocks: list[str] = []
    for item in document.iter_inner_content():
        if isinstance(item, docx.table.Table):
            table = _docx_table(item)
            if table:
                blocks.append("\n".join(table))
            continue
        text = item.text.strip()
        if not text:
            continue
        style = item.style.name if item.style is not None else ""
        level = _heading_level(style)
        if style == "Title":
            if title is None:
                title = text
            elif text != title:
                blocks.append(f"## {text}")
        elif level is not None:
            # "# " is the document title, so Heading 1 becomes "##". The loader reads up to "####".
            blocks.append(f"{'#' * min(level + 1, 4)} {text}" if level <= 3 else f"**{text}**")
        elif style.startswith("List Number"):
            blocks.append(f"1. {text}")
        elif style.startswith("List"):
            blocks.append(f"- {text}")
        else:
            blocks.append(text)
    return Converted(title=title, body=_join_blocks(blocks))


def _join_blocks(blocks: list[str]) -> str:
    """Blank lines between blocks, but keep consecutive list items together."""
    out: list[str] = []
    for block in blocks:
        is_item = block.startswith(("- ", "1. "))
        if out and is_item and out[-1].startswith(("- ", "1. ")):
            out[-1] += "\n" + block
        else:
            out.append(block)
    return "\n\n".join(out)


# ---------------------------------------------------------------- PDF

NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+[A-Z][^.!?;:,]*$")
BULLET = re.compile(r"^\s*(?:[•▪◦●\-*–]|\(?[a-z0-9]\))\s+")


def _pdf_heading(line: str) -> str | None:
    """Recognise headings conservatively: '2.1 Scope' style numbering, or a short ALL-CAPS line."""
    if len(line) > 80 or line.endswith((".", ",", ";", ":")):
        return None
    m = NUMBERED_HEADING.match(line)
    if m:
        depth = m.group(1).count(".") + 1
        return f"{'#' * min(depth + 1, 4)} {line}"
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 3 and line.isupper() and len(line.split()) <= 8:
        return f"## {line.title()}"
    return None


def _pdf_page_blocks(text: str) -> tuple[list[str], int]:
    blocks: list[str] = []
    para: list[str] = []
    headings = 0

    def flush() -> None:
        if para:
            blocks.append(" ".join(para))
            para.clear()

    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            flush()
            continue
        heading = _pdf_heading(line)
        if heading:
            flush()
            blocks.append(heading)
            headings += 1
        elif BULLET.match(line):
            flush()
            blocks.append("- " + BULLET.sub("", line, count=1))
        else:
            para.append(line)
    flush()
    return blocks, headings


def convert_pdf(path: Path) -> Converted:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta_title = reader.metadata.title if reader.metadata else None
    pages = []
    total_headings = 0
    for page in reader.pages:
        blocks, n = _pdf_page_blocks(page.extract_text() or "")
        pages.append(blocks)
        total_headings += n
    if not any(pages):
        raise ValueError(
            f"{path.name} has no extractable text (probably a scanned image); run OCR on it first"
        )
    blocks: list[str] = []
    for i, page_blocks in enumerate(pages, start=1):
        # Without headings of its own, label each page so citations still say where text came from.
        if total_headings == 0 and page_blocks:
            blocks.append(f"## Page {i}")
        blocks.extend(page_blocks)
    title = meta_title.strip() if meta_title and meta_title.strip() else None
    return Converted(title=title, body=_join_blocks(blocks))


# ---------------------------------------------------------------- writing


def convert(path: Path) -> Converted:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return convert_docx(path)
    if suffix == ".pdf":
        return convert_pdf(path)
    if suffix == ".doc":
        raise ValueError("old .doc files aren't supported; save it as .docx first")
    raise ValueError(f"can't convert {path.name}; expected a .pdf or .docx file")


def existing_doc_ids(tenant_dir: Path) -> dict[str, Path]:
    ids: dict[str, Path] = {}
    if not tenant_dir.is_dir():
        return ids
    for p in sorted(tenant_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED:
            ids[load_file(p, tenant_dir.name).doc_id] = p
    return ids


def import_document(
    source: Path,
    corpus_dir: Path,
    tenant: str,
    min_role: str | Role,
    title: str | None = None,
    doc_id: str | None = None,
    source_label: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Convert ``source`` and write ``<corpus_dir>/<tenant>/<doc_id>.md``. Returns the new path."""
    if not source.is_file():
        raise FileNotFoundError(f"no such file: {source}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", tenant):
        raise ValueError(f"tenant must be a lowercase folder name, got {tenant!r}")
    role = Role.parse(min_role)
    converted = convert(source)

    fallback = source.stem.replace("-", " ").replace("_", " ").title()
    title = (title or converted.title or fallback).strip()
    doc_id = _slug(doc_id or source.stem)
    tenant_dir = corpus_dir / tenant
    out = tenant_dir / f"{doc_id}.md"

    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists; pass --force to replace it")
    clash = existing_doc_ids(tenant_dir).get(doc_id)
    if clash is not None and clash.resolve() != out.resolve():
        raise FileExistsError(
            f"doc_id {doc_id!r} is already used by {clash}; pick another --doc-id"
        )

    front = {
        "doc_id": doc_id,
        "title": title,
        "min_role": role.label,
        "source": source_label or source.name,
    }
    text = (
        "---\n"
        + yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + f"# {title}\n\n"
        + converted.body.strip()
        + "\n"
    )
    tenant_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out
