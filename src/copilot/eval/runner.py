"""Evaluation over a hand-written question set.

Metrics, per retrieval mode:
- recall@5, recall@k / MRR: did a gold document appear in the retrieved chunks, and how high?
- refusal accuracy: did the system refuse exactly when it should have? (needs an LLM)
- keyword accuracy: does the answer contain the expected facts? (needs an LLM)
- citation coverage: share of answer sentences that carry a valid citation (needs an LLM)
- leaks: restricted "canary" strings that reached a caller who may not see them, in retrieved
  context or in the answer. Includes a cache-poisoning pass: an admin answer holding restricted
  canaries is cached for each lower-role question, then looked up as the lower role. The target
  is zero.
- unauthorized rate: share of questions where a chunk the caller may not see was retrieved or a
  canary leaked. The target is zero.
- groundedness / faithfulness: share of answers supported by their cited sources, and the mean
  support score (lexical; ``--judge`` adds an LLM-judged score). (needs an LLM)
- cost per successful answer: estimated cost of every question divided by the questions answered
  correctly (right refusal, expected facts, grounded). (needs an LLM)
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from copilot.acl import CANARY, AccessViolation, can_access, forbidden_canaries  # noqa: F401
from copilot.acl import leaked_canaries as _leaks
from copilot.eval.judge import judge_faithfulness
from copilot.generation.citations import split_sentences
from copilot.generation.llm import LLM
from copilot.generation.prompt import REFUSAL
from copilot.metrics import is_grounded, percentile, request_cost
from copilot.models import Answer, Chunk, Principal, Role
from copilot.retrieval.hybrid import MODES
from copilot.service import CopilotService, make_cache


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


def sample_questions(questions: list[Question], n: int) -> list[Question]:
    """``n`` questions spread over the question types, so a short run still covers each kind."""
    if n >= len(questions):
        return questions
    by_type: dict[str, list[Question]] = {}
    for q in questions:
        by_type.setdefault(q.type, []).append(q)
    picked: list[Question] = []
    while len(picked) < n:
        for group in by_type.values():
            if group and len(picked) < n:
                picked.append(group.pop(0))
    return sorted(picked, key=questions.index)


def evaluate(
    service: CopilotService,
    questions: list[Question],
    all_chunks: list[Chunk],
    modes: list[str],
    retrieval_only: bool = False,
    judge: LLM | None = None,
    answer_modes: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    """Score every mode on retrieval. Only ``answer_modes`` (default: all of ``modes``) also get
    LLM answers, since those dominate the run time. ``progress(done, total, label)`` is called
    after each LLM-backed step."""
    s = service.settings
    if answer_modes is None:
        answer_modes = [] if retrieval_only else list(modes)
    for mode in [*modes, *answer_modes]:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
    total = len(questions) * sum(m in answer_modes for m in modes)
    done = 0

    def step(label: str) -> None:
        nonlocal done
        done += 1
        if progress:
            progress(done, total, label)

    k = s.top_k
    depth = max(k, 5)  # Recall@5 is a KPI even when top_k is smaller
    report: dict = {
        "k": k,
        "questions": len(questions),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "modes": {},
        "leak_details": [],
    }

    for mode in modes:
        answering = mode in answer_modes
        hits, hits5, rr, leaks, unauthorized = [], [], [], 0, 0
        refusal_ok, keyword_ok, coverage, latencies = [], [], [], []
        faith, faith_judge, grounded = [], [], []
        cost, successes = 0.0, 0
        per_q = []
        for q in questions:
            p = Principal(user_id=f"eval-{q.role.label}", role=q.role, tenant=q.tenant)
            forbidden = forbidden_canaries(all_chunks, p)
            blocked = 0  # retrieved chunks the caller may not see
            try:
                results = service.retriever.retrieve(p, q.question, depth, mode=mode)
            except AccessViolation:
                results, blocked = [], 1
            blocked += sum(not can_access(p, r.chunk) for r in results)
            docs = [r.chunk.doc_id for r in results]
            leaked = set()
            for r in results[:k]:
                leaked |= _leaks(r.chunk.text, forbidden)

            row = {"id": q.id, "type": q.type, "role": q.role.label, "retrieved": docs[:k]}
            if q.gold_doc_ids:
                rank = next((i for i, d in enumerate(docs, 1) if d in q.gold_doc_ids), None)
                hits.append(rank is not None and rank <= k)
                hits5.append(rank is not None and rank <= 5)
                rr.append(1 / rank if rank and rank <= k else 0.0)
                row["gold_rank"] = rank

            if answering:
                try:
                    ans = service.answer(p, q.question, mode=mode, use_cache=False)
                except AccessViolation:
                    ans = Answer(text=REFUSAL, citations=[], refused=True)
                    blocked += 1
                total_ms = ans.timings_ms.get("total", 0.0)
                latencies.append(total_ms)
                leaked |= _leaks(ans.text, forbidden)
                blocked += sum(
                    1
                    for cid in ans.retrieved_chunk_ids
                    if (c := service.chunks_by_id.get(cid)) and not can_access(p, c)
                )
                refused_right = ans.refused == q.expect_refusal
                refusal_ok.append(refused_right)
                facts_ok = None
                if q.must_contain and not q.expect_refusal:
                    text = ans.text.lower()
                    facts_ok = all(m.lower() in text for m in q.must_contain)
                    keyword_ok.append(facts_ok)
                is_grounded_ = None
                if not ans.refused:
                    n = len(split_sentences(ans.text))
                    coverage.append(1 - len(ans.uncited_sentences) / n if n else 1.0)
                    if ans.faithfulness is not None:
                        faith.append(ans.faithfulness)
                    is_grounded_ = is_grounded(ans, s.grounded_threshold)
                    grounded.append(is_grounded_)
                    if judge is not None:
                        judged = judge_faithfulness(judge, ans, service.chunks_by_id)
                        if judged is not None:
                            faith_judge.append(judged)
                            row["faithfulness_judge"] = round(judged, 3)
                usage = ans.usage
                cost += request_cost(
                    s,
                    total_ms,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
                # Successful: refused exactly when it should, has the expected facts, and an
                # answer (not a refusal) is grounded in its sources.
                success = refused_right and facts_ok is not False and is_grounded_ is not False
                successes += success
                row.update(
                    answer=ans.text,
                    refused=ans.refused,
                    faithfulness=ans.faithfulness,
                    grounded=is_grounded_,
                    success=success,
                )
                step(f"{mode} {q.id}")

            if leaked:
                leaks += len(leaked)
                report["leak_details"].append({"mode": mode, "id": q.id, "leaked": sorted(leaked)})
            if leaked or blocked:
                unauthorized += 1
            row["leaked"] = sorted(leaked)
            row["unauthorized_chunks"] = blocked
            per_q.append(row)

        def mean(xs: list) -> float | None:
            return round(statistics.mean(xs), 3) if xs else None

        report["modes"][mode] = {
            f"recall@{k}": mean(hits),
            "recall@5": mean(hits5),
            "mrr": mean(rr),
            "refusal_accuracy": mean(refusal_ok),
            "keyword_accuracy": mean(keyword_ok),
            "citation_coverage": mean(coverage),
            "groundedness": mean(grounded),
            "faithfulness": mean(faith),
            "faithfulness_judge": mean(faith_judge),
            "leaks": leaks,
            "unauthorized": unauthorized,
            "unauthorized_rate": round(unauthorized / len(questions), 4) if questions else None,
            "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
            "p95_ms": round(percentile(latencies, 95), 1) if latencies else None,
            "answered": answering,
            "success_rate": mean([r["success"] for r in per_q]) if answering else None,
            "cost_per_success": round(cost / successes, 8) if successes else None,
            "per_question": per_q,
        }

    report["cache_poisoning_leaks"] = cache_poisoning_test(service, questions, all_chunks)
    return report


def cache_poisoning_test(
    service: CopilotService, questions: list[Question], all_chunks: list[Chunk]
) -> int:
    """Plant an admin answer full of restricted canaries, then look it up as the real role.

    For every lower-role question, an answer holding every canary that role may not see is
    cached under an admin, and the cache is then asked for the same question as the lower role.
    Any hit is a leak. This exercises the configured cache backend (memory or Redis) without
    calling the LLM. Entries go into a separate eval partition, so real users never see them.
    """
    cache = make_cache(service.settings, f"{service.corpus_version}:eval-poisoning")
    leaks = 0
    for q in questions:
        if q.role == Role.ADMIN:
            continue
        admin = Principal(user_id="eval-admin", role=Role.ADMIN, tenant=q.tenant)
        user = Principal(user_id=f"eval-{q.role.label}", role=q.role, tenant=q.tenant)
        forbidden = forbidden_canaries(all_chunks, user)
        if not forbidden:
            continue
        qvec = service.retriever.embedder.embed_query(q.question)
        cache.put(admin, qvec, Answer(text=" ".join(sorted(forbidden)), citations=[]))
        hit = cache.get(user, qvec)
        if hit is not None:
            leaks += max(1, len(_leaks(hit.text, forbidden)))
    return leaks


def format_table(report: dict) -> str:
    cols = [
        "recall@5",
        "mrr",
        "groundedness",
        "faithfulness",
        "unauthorized_rate",
        "refusal_accuracy",
        "keyword_accuracy",
        "citation_coverage",
        "leaks",
        "p95_ms",
        "cost_per_success",
    ]
    lines = ["| mode | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for mode, m in report["modes"].items():
        if m.get("answered") is False:
            mode += " (retrieval only)"
        cells = ["–" if m.get(c) is None else str(m[c]) for c in cols]
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    if any(m.get("faithfulness_judge") is not None for m in report["modes"].values()):
        judged = ", ".join(
            f"{mode} {m['faithfulness_judge']}" for mode, m in report["modes"].items()
        )
        lines.append(f"\nFaithfulness judged by the LLM: {judged}")
    if "cache_poisoning_leaks" in report:
        lines.append(f"\nCache-poisoning leaks: {report['cache_poisoning_leaks']}")
    return "\n".join(lines)
