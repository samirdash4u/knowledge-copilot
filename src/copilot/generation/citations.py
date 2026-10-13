"""Post-hoc citation checking.

Every factual sentence must cite at least one source, and every cited number must refer to a
chunk that was actually retrieved for this caller. Invalid markers are removed; sentences
without a valid citation are reported so the API (and evals) can flag them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from copilot.generation.prompt import REFUSAL
from copilot.models import Citation, ScoredChunk

MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\-*•])|\n+")


@dataclass
class CheckedAnswer:
    text: str
    citations: list[Citation]
    uncited_sentences: list[str] = field(default_factory=list)
    invalid_markers: list[int] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Share of sentences carrying at least one valid citation."""
        total = len(split_sentences(self.text))
        return 1.0 if total == 0 else 1 - len(self.uncited_sentences) / total


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s and s.strip()]


def _is_structural(sentence: str) -> bool:
    """Headers like 'Steps:' or very short connective lines don't need citations."""
    bare = MARKER.sub("", sentence).strip(" -*•:")
    return len(bare.split()) < 4 or sentence.rstrip().endswith(":")


def check_citations(answer: str, sources: list[ScoredChunk]) -> CheckedAnswer:
    if answer.strip() == REFUSAL:
        return CheckedAnswer(text=answer, citations=[])

    valid = set(range(1, len(sources) + 1))
    invalid: list[int] = []

    def _clean(m: re.Match) -> str:
        nums = [int(n) for n in re.split(r"\s*,\s*", m.group(1))]
        keep = [n for n in nums if n in valid]
        invalid.extend(n for n in nums if n not in valid)
        return "".join(f"[{n}]" for n in keep)

    cleaned = MARKER.sub(_clean, answer)

    used: list[int] = []
    uncited: list[str] = []
    for sent in split_sentences(cleaned):
        nums = [int(n) for g in MARKER.findall(sent) for n in re.split(r"\s*,\s*", g)]
        if nums:
            used.extend(n for n in nums if n not in used)
        elif not _is_structural(sent):
            uncited.append(sent)

    citations = []
    for n in sorted(used):
        c = sources[n - 1].chunk
        citations.append(
            Citation(
                marker=n,
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                title=c.title,
                section=c.section,
                source=c.source,
                snippet=c.text[:300],
            )
        )
    return CheckedAnswer(
        text=cleaned, citations=citations, uncited_sentences=uncited, invalid_markers=invalid
    )
