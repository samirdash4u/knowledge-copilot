"""Post-hoc citation checking.

Every factual sentence must cite at least one source, and every cited number must refer to a
chunk that was actually retrieved for this caller. Invalid markers are removed; sentences
without a valid citation are reported so the API (and evals) can flag them.

:func:`faithfulness` then scores how much of each cited sentence its sources actually support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from copilot.generation.prompt import REFUSAL
from copilot.models import Citation, ScoredChunk

MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\-*•])|\n+")
WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")
STOPWORDS = frozenset(
    """a about after all also an and any are as at be been before by can do does each for from
    get gets got has have here if in into is it its just may more most must no not of on only or
    other our over per should some such than that the their them then there these they this
    those to under very was we were what when where which who will with you your""".split()
)


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
        nums = _numbers(sent)
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


def _numbers(sentence: str) -> list[int]:
    return [int(n) for g in MARKER.findall(sentence) for n in re.split(r"\s*,\s*", g)]


def _content_words(text: str) -> set[str]:
    return {
        w for w in WORD.findall(text.lower()) if w not in STOPWORDS and (len(w) > 2 or w.isdigit())
    }


def support(sentence: str, source_text: str) -> float:
    """Share of a sentence's content words that appear in the source text (0..1).

    Words match exactly or on their first five letters, so "approved" matches "approval".
    """
    words = _content_words(MARKER.sub("", sentence))
    if not words:
        return 1.0
    found = _content_words(source_text)
    stems = {w[:5] for w in found if len(w) > 4}
    hits = sum(1 for w in words if w in found or (len(w) > 4 and w[:5] in stems))
    return hits / len(words)


def faithfulness(text: str, sources: list[ScoredChunk]) -> float | None:
    """How well an answer is supported by the sources it cites, from 0 to 1.

    A lexical stand-in for an LLM judge, cheap enough to run on every request: each sentence
    scores the share of its content words found in the chunks it cites, and a sentence without
    a valid citation scores 0. ``None`` for a refusal or an answer with no factual sentences.
    """
    if text.strip() == REFUSAL:
        return None
    scores = []
    for sent in split_sentences(text):
        if _is_structural(sent):
            continue
        nums = [n for n in _numbers(sent) if 1 <= n <= len(sources)]
        if not nums:
            scores.append(0.0)
            continue
        chunks = [sources[n - 1].chunk for n in nums]
        cited = " ".join(f"{c.title} {c.section} {c.text}" for c in chunks)
        scores.append(support(sent, cited))
    return round(sum(scores) / len(scores), 3) if scores else None
