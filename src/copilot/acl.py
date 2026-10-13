"""Access control.

Three rules this module enforces:

1. Filtering happens *before* ranking (pre-filter), so restricted chunks never compete for
   top-k slots and never reach the reranker or the LLM.
2. After retrieval, every chunk is re-checked (defense in depth). A violation fails closed.
3. Anything keyed on "what this caller can see" (e.g. the answer cache) uses
   :func:`access_scope`, so two callers share an entry only if they can see the same documents.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from copilot.models import Chunk, Principal, ScoredChunk

log = logging.getLogger(__name__)


class AccessViolation(RuntimeError):
    """Raised when a chunk the caller may not see made it past retrieval."""


def can_access(principal: Principal, chunk: Chunk) -> bool:
    return chunk.tenant == principal.tenant and principal.role >= chunk.min_role


def visible(principal: Principal, chunks: Iterable[Chunk]) -> list[Chunk]:
    return [c for c in chunks if can_access(principal, c)]


def assert_all_allowed(principal: Principal, results: Iterable[ScoredChunk]) -> None:
    for r in results:
        if not can_access(principal, r.chunk):
            log.error(
                "ACL violation: user=%s role=%s tenant=%s chunk=%s min_role=%s chunk_tenant=%s",
                principal.user_id,
                principal.role.label,
                principal.tenant,
                r.chunk.chunk_id,
                r.chunk.min_role.label,
                r.chunk.tenant,
            )
            raise AccessViolation(f"chunk {r.chunk.chunk_id} is not visible to caller")


def access_scope(principal: Principal) -> str:
    """Identifier for the set of documents a principal can see.

    With a strict role hierarchy, (tenant, role) fully determines visibility. If attribute-based
    rules (departments, regions) are added later, extend this so the cache stays correct.
    """
    return f"{principal.tenant}:{principal.role.label}"
