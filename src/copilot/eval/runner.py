"""Evaluation over a hand-written question set.

Metrics, per retrieval mode:
- recall@k / MRR: did a gold document appear in the retrieved chunks, and how high?
- refusal accuracy: did the system refuse exactly when it should have? (needs an LLM)
- keyword accuracy: does the answer contain the expected facts? (needs an LLM)
- citation coverage: share of answer sentences that carry a valid citation (needs an LLM)
- leaks: restricted "canary" strings that reached a caller who may not see them, in retrieved
  context or in the answer. Includes a cache-poisoning pass: every restricted question is asked
  by an admin first, then by the lower role, with the cache on. The target is zero.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from copilot.acl import can_access
from copilot.generation.citations import split_sentences
from copilot.models import Chunk, Principal, Role
from copilot.retrieval.hybrid import MODES
from copilot.service import CopilotService

CANARY = re.compile(r"CANARY-[A-Z0-9-]+")


@dataclass
class Question:
    id: str
    tenant: str
    role: Role
    type: str
    question: str
    gold_doc_ids: list[str] = field(default_factory=list)
    must_contain: list[str] = field(default_factory=list)
    expect_refusal: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> Question:
        return cls(
            id=d["id"],
            tenant=d["tenant"],
            role=Role.parse(d["role"]),
            type=d.get("type", "factual"),
            question=d["question"],
            gold_doc_ids=d.get("gold_doc_ids", []),
            must_contain=d.get("must_contain", []),
            expect_refusal=d.get("expect_refusal", False),
        )


def load_questions(path: Path) -> list[Question]:
    with path.open(encoding="utf-8") as f:
        return [Question.from_dict(json.loads(line)) for line in f if line.strip()]


def forbidden_canaries(all_chunks: list[Chunk], principal: Principal) -> set[str]:
    out: set[str] = set()
    for c in all_chunks:
        if not can_access(principal, c):
            out.update(CANARY.findall(c.text))
    return out


def _leaks(text: str, forbidden: set[str]) -> set[str]:
    return {c for c in CANARY.findall(text) if c in forbidden}


def evaluate(
    service: CopilotService,
    questions: list[Question],
    all_chunks: list[Chunk],
    modes: list[str],
    retrieval_only: bool = False,
) -> dict:
    k = service.settings.top_k
    report: dict = {"k": k, "questions": len(questions), "modes": {}, "leak_details": []}

    for mode in modes:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        hits, rr, leaks = [], [], 0
        refusal_ok, keyword_ok, coverage, latencies = [], [], [], []
        per_q = []
        for q in questions:
            p = Principal(user_id=f"eval-{q.role.label}", role=q.role, tenant=q.tenant)
            forbidden = forbidden_canaries(all_chunks, p)
            results = service.retriever.retrieve(p, q.question, k, mode=mode)
            docs = [r.chunk.doc_id for r in results]
            leaked = set()
            for r in results:
                leaked |= _leaks(r.chunk.text, forbidden)

            row = {"id": q.id, "type": q.type, "role": q.role.label, "retrieved": docs}
            if q.gold_doc_ids:
                rank = next((i for i, d in enumerate(docs, 1) if d in q.gold_doc_ids), None)
                hits.append(rank is not None)
                rr.append(1 / rank if rank else 0.0)
                row["gold_rank"] = rank

            if not retrieval_only:
                ans = service.answer(p, q.question, mode=mode, use_cache=False)
                latencies.append(ans.timings_ms.get("total", 0.0))
                leaked |= _leaks(ans.text, forbidden)
                refusal_ok.append(ans.refused == q.expect_refusal)
                if q.must_contain and not q.expect_refusal:
                    text = ans.text.lower()
                    keyword_ok.append(all(m.lower() in text for m in q.must_contain))
                if not ans.refused:
                    n = len(split_sentences(ans.text))
                    coverage.append(1 - len(ans.uncited_sentences) / n if n else 1.0)
                row.update(answer=ans.text, refused=ans.refused)

            if leaked:
                leaks += len(leaked)
                report["leak_details"].append({"mode": mode, "id": q.id, "leaked": sorted(leaked)})
            row["leaked"] = sorted(leaked)
            per_q.append(row)

        def mean(xs: list) -> float | None:
            return round(statistics.mean(xs), 3) if xs else None

        report["modes"][mode] = {
            f"recall@{k}": mean(hits),
            "mrr": mean(rr),
            "refusal_accuracy": mean(refusal_ok),
            "keyword_accuracy": mean(keyword_ok),
            "citation_coverage": mean(coverage),
            "leaks": leaks,
            "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
            "p95_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1)
            if latencies
            else None,
            "per_question": per_q,
        }

    if not retrieval_only:
        report["cache_poisoning_leaks"] = cache_poisoning_test(service, questions, all_chunks)
    return report


def cache_poisoning_test(
    service: CopilotService, questions: list[Question], all_chunks: list[Chunk]
) -> int:
    """Ask each lower-role question as admin first (warming the cache), then as the real role."""
    leaks = 0
    for q in questions:
        if q.role == Role.ADMIN:
            continue
        admin = Principal(user_id="eval-admin", role=Role.ADMIN, tenant=q.tenant)
        user = Principal(user_id=f"eval-{q.role.label}", role=q.role, tenant=q.tenant)
        service.answer(admin, q.question, use_cache=True)
        ans = service.answer(user, q.question, use_cache=True)
        leaks += len(_leaks(ans.text, forbidden_canaries(all_chunks, user)))
    return leaks


def format_table(report: dict) -> str:
    k = report["k"]
    cols = [
        f"recall@{k}",
        "mrr",
        "refusal_accuracy",
        "keyword_accuracy",
        "citation_coverage",
        "leaks",
        "p50_ms",
        "p95_ms",
    ]
    lines = ["| mode | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for mode, m in report["modes"].items():
        cells = ["–" if m[c] is None else str(m[c]) for c in cols]
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    if "cache_poisoning_leaks" in report:
        lines.append(f"\nCache-poisoning leaks: {report['cache_poisoning_leaks']}")
    return "\n".join(lines)
