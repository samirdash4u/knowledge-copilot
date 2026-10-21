"""Faithfulness judged by the local LLM (``copilot eval --judge``).

The model sees the sources an answer cites and the answer's sentences, and lists the sentences
the sources do not support. Slower than the lexical score in ``generation.citations``, so it
runs only in the eval, not on every request.
"""

from __future__ import annotations

import json
import re

from copilot.generation.citations import _is_structural, split_sentences
from copilot.generation.llm import LLM
from copilot.models import Answer, Chunk

JUDGE_PROMPT = """You check whether an answer is supported by its sources.
A sentence is supported only if the sources state everything it says. Citation numbers like [2]
point to the source with that number. Reply with JSON only, in this form:
{"unsupported": [sentence numbers]}"""

_JSON = re.compile(r"\{.*\}", re.S)


def judge_faithfulness(llm: LLM, ans: Answer, chunks_by_id: dict[str, Chunk]) -> float | None:
    """Share of the answer's sentences the judge finds supported, or None if it can't say."""
    if ans.refused or not ans.citations:
        return None
    sentences = [s for s in split_sentences(ans.text) if not _is_structural(s)]
    if not sentences:
        return None
    sources = []
    for c in ans.citations:
        chunk = chunks_by_id.get(c.chunk_id)
        sources.append(
            f"[{c.marker}] {c.title} — {c.section}\n{chunk.text if chunk else c.snippet}"
        )
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, start=1))
    user = f"Sources:\n\n{chr(10).join(sources)}\n\nAnswer sentences:\n{numbered}"
    reply = llm.chat(
        [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}]
    )
    match = _JSON.search(reply)
    if not match:
        return None
    try:
        flagged = json.loads(match.group(0)).get("unsupported", [])
        bad = {int(n) for n in flagged if 1 <= int(n) <= len(sentences)}
    except (ValueError, TypeError, AttributeError):
        return None
    return 1 - len(bad) / len(sentences)
