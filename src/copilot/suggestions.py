"""Suggested questions shown under the chat box.

A tenant can list its own in ``tenant.yaml``, either one list for everyone or one per role::

    suggestions:
      employee: ["How many days of leave do I get?"]
      manager: ["Up to what amount can I approve an expense claim?"]

``add-doc`` fills these in: it writes a few likely questions per role for each document it
imports (see ``document_questions``). Without any, questions are made at request time from the
titles and section headings of the documents the caller may see. Either way only sections
visible to a role are used, so a restricted section never shows up for a lower role.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

import yaml

from copilot.acl import visible
from copilot.generation.llm import LLM
from copilot.ingest.loaders import Document, Section
from copilot.models import Chunk, Principal, Role
from copilot.tenants import TENANT_FILE

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4


def configured(meta_suggestions: object, role: str) -> list[str] | None:
    """Suggestions from tenant.yaml for ``role``, or None when the tenant doesn't set any."""
    if isinstance(meta_suggestions, list):
        items = meta_suggestions
    elif isinstance(meta_suggestions, dict) and meta_suggestions.get(role):
        items = meta_suggestions[role]
    else:
        return None  # nothing set for this role: generate them instead
    return [str(q).strip() for q in items if str(q).strip()][:8]


def _topic(chunk: Chunk) -> str | None:
    """The innermost section heading, if it says more than the title or a page number."""
    last = chunk.section.split(" > ")[-1].strip()
    if not last or last == chunk.title or last.lower().startswith("page "):
        return None
    return last


def generated(principal: Principal, chunks: Iterable[Chunk]) -> list[str]:
    """Questions about visible sections, round-robin over documents so several are represented."""
    per_doc: dict[str, list[str]] = {}
    # Sections written for exactly this role come first, so a manager sees manager material.
    ordered = sorted(visible(principal, chunks), key=lambda c: c.min_role != principal.role)
    for c in ordered:
        topic = _topic(c)
        name = c.title if c.title.lower().startswith("the ") else f"the {c.title}"
        q = f'What does {name} say about "{topic}"?' if topic else f"What is {name} about?"
        qs = per_doc.setdefault(c.doc_id, [])
        if q not in qs:
            qs.append(q)
    out: list[str] = []
    for i in range(max((len(v) for v in per_doc.values()), default=0)):
        for qs in per_doc.values():
            if i < len(qs) and len(out) < MAX_SUGGESTIONS:
                out.append(qs[i])
    return out


# ---------------------------------------------------------------- written by add-doc

QUESTIONS_PER_ROLE = 3  # added per document and role
STORED_PER_ROLE = 6  # kept in tenant.yaml per role; the newest document's come first

QUESTION_PROMPT = (
    "You write one short question that an employee might type into a company knowledge "
    "assistant, and that the passage below answers. Use the words people would use, not the "
    "section title. Reply with the question only, on one line."
)


def _template(title: str, heading: str) -> str:
    name = title if title.lower().startswith("the ") else f"the {title}"
    topic = heading.split(" > ")[-1].strip()
    if not topic or topic == title or topic.lower().startswith("page "):
        return f"What is {name} about?"
    return f'What does {name} say about "{topic}"?'


def _clean_question(text: str) -> str | None:
    """Accept a model reply only if it looks like a single short question."""
    q = text.strip().strip('"').strip()
    q = re.sub(r"^(question|q)\s*[:.-]\s*", "", q, flags=re.I)
    if "\n" in q or not (10 <= len(q) <= 150) or not q.endswith("?"):
        return None
    return q


def document_questions(doc: Document, llm: LLM | None = None) -> tuple[dict[str, list[str]], bool]:
    """Likely questions about ``doc`` for each role, and whether the model wrote them.

    Each section yields one question, tagged with that section's role, so a manager-only section
    only ever produces a manager (and admin) question. With ``llm`` the question is written by
    the model from the section text; if it is unreachable or replies with something that isn't a
    question, the section heading is used instead.
    """
    sections = [s for s in doc.sections if len(s.text.strip()) >= 20]
    picked: list[Section] = []
    for role in Role:
        picked += [s for s in sections if s.min_role == role][:QUESTIONS_PER_ROLE]

    used_llm = False
    tagged: list[tuple[str, Role]] = []
    for s in picked:
        q = None
        if llm is not None:
            try:
                reply = llm.chat(
                    [
                        {"role": "system", "content": QUESTION_PROMPT},
                        {
                            "role": "user",
                            "content": f"Document: {doc.title}\nSection: {s.heading}\n\n"
                            f"{s.text[:1500]}",
                        },
                    ]
                )
                q = _clean_question(reply)
                used_llm = used_llm or q is not None
            except Exception as exc:  # noqa: BLE001 - any failure means: fall back to headings
                log.warning("could not reach the LLM for suggestions (%s); using headings", exc)
                llm = None
        tagged.append((q or _template(doc.title, s.heading), s.min_role))

    by_role: dict[str, list[str]] = {}
    for role in Role:
        # Questions written for exactly this role first, then those from lower roles.
        mine = [q for q, r in tagged if r == role] + [q for q, r in tagged if r < role]
        if mine:
            by_role[role.label] = list(dict.fromkeys(mine))[:QUESTIONS_PER_ROLE]
    return by_role, used_llm


def add_to_tenant(tenant_dir: Path, by_role: dict[str, list[str]]) -> bool:
    """Put ``by_role`` at the top of each role's list in ``tenant.yaml``. False if not written."""
    meta_file = tenant_dir / TENANT_FILE
    meta: dict = {}
    if meta_file.is_file():
        try:
            meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.warning("not changing %s, it has a YAML error: %s", meta_file, exc)
            return False
        if not isinstance(meta, dict):
            return False
    current = meta.get("suggestions")
    if isinstance(current, list):  # one list for everyone: keep it for every role
        current = {r.label: list(current) for r in Role}
    elif not isinstance(current, dict):
        current = {}
    for role, questions in by_role.items():
        existing = [str(q) for q in current.get(role) or []]
        current[role] = list(dict.fromkeys(questions + existing))[:STORED_PER_ROLE]
    meta["suggestions"] = {r.label: current[r.label] for r in Role if current.get(r.label)}
    meta_file.write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8"
    )
    return True
