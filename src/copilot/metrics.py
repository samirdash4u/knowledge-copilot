"""KPI monitoring: a record per /ask, Prometheus metrics, and the KPI summary.

Online KPIs come from live traffic. Each request is stored in a SQLite file (no question or
answer text, only numbers, tenant, role and mode):

- groundedness: share of answers whose cited sources support them (faithfulness at or above
  ``grounded_threshold``)
- faithfulness: mean support score of answers (see ``generation.citations.faithfulness``)
- unauthorized retrieval rate: share of requests where a chunk the caller may not see was
  retrieved (the post-retrieval check blocks it) or reached the answer, or a forbidden canary
  appears in the answer. The target is 0.
- p95 latency of every request that did not fail
- cost per successful answer: estimated cost of all requests divided by grounded answers. The
  model runs locally, so cost is compute time and tokens priced with configurable rates.

Offline KPIs (Recall@5, MRR, and the same quality numbers on known questions) come from the
latest eval report written by ``copilot eval``.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import astuple, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from copilot.acl import can_access, leaked_canaries
from copilot.config import Settings
from copilot.models import Answer, Principal

log = logging.getLogger(__name__)

STATUSES = ("answered", "refused", "blocked", "error")
LATENCY_BUCKETS_S = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120)


@dataclass
class RequestRecord:
    ts: float
    tenant: str
    role: str
    mode: str
    status: str  # one of STATUSES; "blocked" = the access check stopped an unauthorized chunk
    cached: bool
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    faithfulness: float | None = None
    grounded: bool = False
    unauthorized: int = 0  # chunks and canaries the caller may not see
    cost: float = 0.0


def request_cost(
    s: Settings, latency_ms: float, prompt_tokens: int, completion_tokens: int
) -> float:
    return (
        latency_ms / 1000 * s.cost_per_compute_second
        + prompt_tokens / 1000 * s.cost_per_1k_prompt_tokens
        + completion_tokens / 1000 * s.cost_per_1k_completion_tokens
    )


def is_grounded(ans: Answer, threshold: float) -> bool:
    return (
        not ans.refused
        and bool(ans.citations)
        and ans.faithfulness is not None
        and ans.faithfulness >= threshold
    )


def audit(service, principal: Principal, ans: Answer) -> int:
    """Count the chunks and canary tokens in this answer that the caller may not see."""
    bad = 0
    for chunk_id in set(ans.retrieved_chunk_ids) | {c.chunk_id for c in ans.citations}:
        chunk = service.chunks_by_id.get(chunk_id)
        if chunk is not None and not can_access(principal, chunk):
            bad += 1
    return bad + len(leaked_canaries(ans.text, service.forbidden_canaries(principal)))


def record_answer(
    s: Settings,
    principal: Principal,
    mode: str,
    ans: Answer,
    latency_ms: float,
    unauthorized: int,
) -> RequestRecord:
    prompt = ans.usage.get("prompt_tokens", 0)
    completion = ans.usage.get("completion_tokens", 0)
    return RequestRecord(
        ts=time.time(),
        tenant=principal.tenant,
        role=principal.role.label,
        mode=mode,
        status="refused" if ans.refused else "answered",
        cached=ans.cached,
        latency_ms=latency_ms,
        prompt_tokens=prompt,
        completion_tokens=completion,
        faithfulness=ans.faithfulness,
        grounded=is_grounded(ans, s.grounded_threshold),
        unauthorized=unauthorized,
        cost=request_cost(s, latency_ms, prompt, completion),
    )


def record_failure(
    s: Settings, principal: Principal, mode: str, status: str, latency_ms: float
) -> RequestRecord:
    return RequestRecord(
        ts=time.time(),
        tenant=principal.tenant,
        role=principal.role.label,
        mode=mode,
        status=status,
        cached=False,
        latency_ms=latency_ms,
        unauthorized=1 if status == "blocked" else 0,
        cost=request_cost(s, latency_ms, 0, 0),
    )


# ---------- summaries ----------


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def summarize(records: list[RequestRecord]) -> dict:
    answered = [r for r in records if r.status == "answered"]
    grounded = sum(r.grounded for r in answered)
    faith = [r.faithfulness for r in answered if r.faithfulness is not None]
    latencies = [r.latency_ms for r in records if r.status != "error"]
    cost = sum(r.cost for r in records)
    n = len(records)

    def rate(x: int, total: int) -> float | None:
        return round(x / total, 4) if total else None

    return {
        "requests": n,
        "by_status": {st: sum(r.status == st for r in records) for st in STATUSES},
        "cache_hit_rate": rate(sum(r.cached for r in records), n),
        "grounded_rate": rate(grounded, len(answered)),
        "faithfulness": round(sum(faith) / len(faith), 3) if faith else None,
        "unauthorized_rate": rate(sum(r.unauthorized > 0 for r in records), n),
        "unauthorized_events": sum(r.unauthorized for r in records),
        "p50_ms": _round(percentile(latencies, 50)),
        "p95_ms": _round(percentile(latencies, 95)),
        "prompt_tokens": sum(r.prompt_tokens for r in records),
        "completion_tokens": sum(r.completion_tokens for r in records),
        "total_cost": round(cost, 6),
        "successful_answers": grounded,
        "cost_per_success": round(cost / grounded, 8) if grounded else None,
    }


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def load_offline(path: Path, mode: str) -> dict | None:
    """The KPIs of one retrieval mode from an eval report, or None if there is no report."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    modes = report.get("modes") or {}
    if not modes:
        return None
    if mode not in modes:
        mode = list(modes)[-1]
    m = modes[mode]
    judged = m.get("faithfulness_judge")
    return {
        "mode": mode,
        "questions": report.get("questions"),
        "generated_at": report.get("generated_at")
        or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds"),
        "recall_at_5": m.get("recall@5"),
        "mrr": m.get("mrr"),
        "grounded_rate": m.get("groundedness"),
        # The LLM judge, when the eval ran with --judge; else the lexical score.
        "faithfulness": judged if judged is not None else m.get("faithfulness"),
        "unauthorized_rate": m.get("unauthorized_rate"),
        "p95_ms": m.get("p95_ms"),
        "cost_per_success": m.get("cost_per_success"),
        "cache_poisoning_leaks": report.get("cache_poisoning_leaks"),
    }


# (key, label, unit, higher is better, settings field with the target)
KPIS = [
    ("recall_at_5", "Recall@5", "ratio", True, "target_recall_at_5"),
    ("mrr", "MRR", "ratio", True, "target_mrr"),
    ("grounded_rate", "Groundedness", "percent", True, "target_grounded_rate"),
    ("faithfulness", "Faithfulness", "ratio", True, "target_faithfulness"),
    (
        "unauthorized_rate",
        "Unauthorized retrieval rate",
        "percent",
        False,
        "target_unauthorized_rate",
    ),
    ("p95_ms", "p95 latency", "ms", False, "target_p95_ms"),
    ("cost_per_success", "Cost per successful answer", "money", False, "target_cost_per_answer"),
]


def kpi_rows(s: Settings, online: dict | None, offline: dict | None) -> list[dict]:
    rows = []
    for key, label, unit, higher, target_field in KPIS:
        target = getattr(s, target_field)
        row = {"key": key, "label": label, "unit": unit, "target": target}
        row["higher_is_better"] = higher
        for source, data in (("online", online), ("offline", offline)):
            value = (data or {}).get(key)
            row[source] = value
            if value is None:
                row[f"{source}_status"] = None
            else:
                ok = value >= target if higher else value <= target
                row[f"{source}_status"] = "ok" if ok else "miss"
        rows.append(row)
    return rows


def format_value(value: float | None, unit: str, currency: str = "USD") -> str:
    if value is None:
        return "–"
    if unit == "percent":
        return f"{value * 100:.1f}%"
    if unit == "ms":
        return f"{value:,.0f} ms"
    if unit == "money":
        return f"{value:.6f} {currency}"
    return f"{value:.3f}"


def format_kpis(report: dict) -> str:
    currency = report["currency"]
    hours = report["window_hours"]
    offline = report["offline"]
    lines = [
        f"| KPI | target | live, last {hours:g} h | eval |",
        "|---|---|---|---|",
    ]
    for r in report["kpis"]:
        cells = [r["label"], ("≥ " if r["higher_is_better"] else "≤ ")]
        cells[1] += format_value(r["target"], r["unit"], currency)
        for source in ("online", "offline"):
            mark = {"ok": " ✓", "miss": " ✗"}.get(r[f"{source}_status"] or "", "")
            cells.append(format_value(r[source], r["unit"], currency) + mark)
        lines.append("| " + " | ".join(cells) + " |")
    online = report["online"]
    lines.append(
        f"\nLive: {online['requests']} requests"
        f" ({', '.join(f'{v} {k}' for k, v in online['by_status'].items() if v)})"
        if online["requests"]
        else "\nLive: no requests in this window yet"
    )
    if offline:
        lines.append(
            f"Eval: mode {offline['mode']}, {offline['questions']} questions,"
            f" run {offline['generated_at']}"
        )
    else:
        lines.append("Eval: no report yet; run 'copilot eval' (Recall@5 and MRR come from it)")
    return "\n".join(lines)


# ---------- storage ----------

_COLUMNS = [f.name for f in fields(RequestRecord)]


class KpiStore:
    """Request records in SQLite. Opens a connection per call, so any thread may use it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "ts REAL NOT NULL, tenant TEXT, role TEXT, mode TEXT, status TEXT,"
                " cached INTEGER, latency_ms REAL, prompt_tokens INTEGER,"
                " completion_tokens INTEGER, faithfulness REAL, grounded INTEGER,"
                " unauthorized INTEGER, cost REAL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def add(self, rec: RequestRecord) -> None:
        marks = ", ".join("?" for _ in _COLUMNS)
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO requests ({', '.join(_COLUMNS)}) VALUES ({marks})", astuple(rec)
            )

    def since(self, ts: float) -> list[RequestRecord]:
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM requests WHERE ts >= ? ORDER BY ts", (ts,)
            ).fetchall()
        out = []
        for row in rows:
            rec = RequestRecord(*row)
            rec.cached, rec.grounded = bool(rec.cached), bool(rec.grounded)
            out.append(rec)
        return out

    def prune(self, before: float) -> int:
        with closing(self._connect()) as db, db:
            return db.execute("DELETE FROM requests WHERE ts < ?", (before,)).rowcount


# ---------- Prometheus ----------


class PromMetrics:
    """Counters and a latency histogram in the Prometheus text format, without a client library.

    They count from process start; Prometheus handles the reset after a restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str], int] = {}
        self.grounded: dict[str, int] = {}
        self.unauthorized: dict[str, int] = {}
        self.cache_hits: dict[str, int] = {}
        self.faith_sum: dict[str, float] = {}
        self.faith_count: dict[str, int] = {}
        self.buckets: dict[str, list[int]] = {}
        self.lat_sum: dict[str, float] = {}
        self.lat_count: dict[str, int] = {}
        self.tokens = {"prompt": 0, "completion": 0}
        self.cost = 0.0

    def observe(self, r: RequestRecord) -> None:
        with self._lock:
            t = r.tenant
            self.requests[(t, r.status)] = self.requests.get((t, r.status), 0) + 1
            self.grounded[t] = self.grounded.get(t, 0) + int(r.grounded)
            self.unauthorized[t] = self.unauthorized.get(t, 0) + int(r.unauthorized > 0)
            self.cache_hits[t] = self.cache_hits.get(t, 0) + int(r.cached)
            if r.status == "answered" and r.faithfulness is not None:
                self.faith_sum[t] = self.faith_sum.get(t, 0.0) + r.faithfulness
                self.faith_count[t] = self.faith_count.get(t, 0) + 1
            if r.status != "error":
                counts = self.buckets.setdefault(t, [0] * len(LATENCY_BUCKETS_S))
                for i, le in enumerate(LATENCY_BUCKETS_S):
                    if r.latency_ms / 1000 <= le:
                        counts[i] += 1
                self.lat_sum[t] = self.lat_sum.get(t, 0.0) + r.latency_ms / 1000
                self.lat_count[t] = self.lat_count.get(t, 0) + 1
            self.tokens["prompt"] += r.prompt_tokens
            self.tokens["completion"] += r.completion_tokens
            self.cost += r.cost

    def render(self, currency: str, offline: dict | None) -> str:
        out: list[str] = []

        def family(name: str, kind: str, help_: str, samples: list[tuple[str, float]]) -> None:
            # A sample's "labels" may start with a suffix such as _sum or _bucket.
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {kind}")
            out.extend(f"{name}{labels} {_num(v)}" for labels, v in samples)

        with self._lock:
            family(
                "copilot_requests_total",
                "counter",
                "Questions asked, by tenant and outcome (answered, refused, blocked, error).",
                [(_labels(tenant=t, status=s), v) for (t, s), v in sorted(self.requests.items())],
            )
            family(
                "copilot_grounded_answers_total",
                "counter",
                "Answers whose cited sources support them (successful answers).",
                [(_labels(tenant=t), v) for t, v in sorted(self.grounded.items())],
            )
            family(
                "copilot_unauthorized_retrievals_total",
                "counter",
                "Requests where the caller's results held a document they may not see. Target 0.",
                [(_labels(tenant=t), v) for t, v in sorted(self.unauthorized.items())],
            )
            family(
                "copilot_cache_hits_total",
                "counter",
                "Answers served from the semantic cache.",
                [(_labels(tenant=t), v) for t, v in sorted(self.cache_hits.items())],
            )
            family(
                "copilot_answer_faithfulness",
                "summary",
                "Share of each answer's content supported by the sources it cites (0-1).",
                [(f"_sum{_labels(tenant=t)}", v) for t, v in sorted(self.faith_sum.items())]
                + [(f"_count{_labels(tenant=t)}", v) for t, v in sorted(self.faith_count.items())],
            )
            hist: list[tuple[str, float]] = []
            for t, counts in sorted(self.buckets.items()):
                for le, c in zip(LATENCY_BUCKETS_S, counts, strict=True):
                    hist.append((f"_bucket{_labels(tenant=t, le=_num(le))}", c))
                hist.append((f"_bucket{_labels(tenant=t, le='+Inf')}", self.lat_count[t]))
                hist.append((f"_sum{_labels(tenant=t)}", self.lat_sum[t]))
                hist.append((f"_count{_labels(tenant=t)}", self.lat_count[t]))
            family(
                "copilot_request_latency_seconds",
                "histogram",
                "Time to answer a question, including cache hits and refusals.",
                hist,
            )
            family(
                "copilot_llm_tokens_total",
                "counter",
                "Tokens the LLM read (prompt) and wrote (completion).",
                [(_labels(kind=k), v) for k, v in self.tokens.items()],
            )
            family(
                "copilot_cost_total",
                "counter",
                "Estimated cost of all requests (local compute and token prices from settings).",
                [(_labels(currency=currency), self.cost)],
            )

        if offline:
            labels = _labels(mode=offline["mode"])
            for key, label, *_ in KPIS:
                if offline.get(key) is None:
                    continue
                name = f"copilot_eval_{key}"
                out.append(f"# HELP {name} {label} from the latest eval report.")
                out.append(f"# TYPE {name} gauge")
                out.append(f"{name}{labels} {_num(offline[key])}")
        return "\n".join(out) + "\n"


def _labels(**kv: str) -> str:
    def esc(v: str) -> str:
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in kv.items()) + "}"


def _num(v: float) -> str:
    return str(v) if isinstance(v, int) else repr(float(v))


# ---------- the monitor the API uses ----------


class Monitor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = KpiStore(settings.metrics_db)
        self.prom = PromMetrics()
        cutoff = time.time() - settings.metrics_retention_days * 86400
        removed = self.store.prune(cutoff)
        if removed:
            log.info(
                "removed %d request records older than %d days",
                removed,
                settings.metrics_retention_days,
            )

    def observe(self, rec: RequestRecord) -> None:
        if rec.unauthorized:
            log.error(
                "KPI: unauthorized retrieval for role=%s tenant=%s status=%s",
                rec.role,
                rec.tenant,
                rec.status,
            )
        self.prom.observe(rec)
        try:
            self.store.add(rec)
        except sqlite3.Error:
            # Monitoring must never break answering.
            log.exception("could not record KPI sample in %s", self.store.path)

    def offline(self) -> dict | None:
        return load_offline(self.settings.eval_report, self.settings.retrieval_mode)

    def report(self, hours: float = 24) -> dict:
        online = summarize(self.store.since(time.time() - hours * 3600))
        offline = self.offline()
        return {
            "window_hours": hours,
            "currency": self.settings.cost_currency,
            "grounded_threshold": self.settings.grounded_threshold,
            "online": online,
            "offline": offline,
            "kpis": kpi_rows(self.settings, online, offline),
        }

    def prometheus(self) -> str:
        return self.prom.render(self.settings.cost_currency, self.offline())
