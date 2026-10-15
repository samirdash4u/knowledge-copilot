"""Suggested questions shown under the chat box.

A tenant can list its own in ``tenant.yaml``, either one list for everyone or one per role::

    suggestions:
      employee: ["How many days of leave do I get?"]
      manager: ["Up to what amount can I approve an expense claim?"]

Without that, questions are made from the titles and section headings of the documents the
caller may see, so a new tenant gets suggestions as soon as it is indexed. Only sections visible
to the caller's role are used, so a restricted heading never shows up for a lower role.
"""

from __future__ import annotations

from collections.abc import Iterable

from copilot.acl import visible
from copilot.models import Chunk, Principal

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
