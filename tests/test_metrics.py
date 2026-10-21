import json
from pathlib import Path

from fastapi.testclient import TestClient

from copilot.acl import AccessViolation
from copilot.api import create_app
from copilot.eval.judge import judge_faithfulness
from copilot.eval.runner import evaluate, format_table, load_questions
from copilot.generation.citations import faithfulness, support
from copilot.metrics import (
    KpiStore,
    Monitor,
    RequestRecord,
    audit,
    format_kpis,
    load_offline,
    percentile,
    summarize,
)
from copilot.models import Answer, Chunk, Principal, Role, ScoredChunk
from copilot.retrieval.vector import load_chunks

ROOT = Path(__file__).resolve().parents[1]


def _chunk(text: str) -> ScoredChunk:
    c = Chunk("d#1", "d", "policy", "Leave policy", "Annual leave", text, Role.EMPLOYEE)
    return ScoredChunk(c, 1.0)


def test_faithfulness_scores_support_from_cited_sources():
    src = [_chunk("Employees receive 25 days of paid annual leave per calendar year.")]
    assert support("You get 25 days of paid leave.", src[0].chunk.text) == 1.0
    assert faithfulness("Employees receive 25 days of paid annual leave [1].", src) == 1.0
    # an uncited sentence scores 0, an unsupported cited one scores low
    assert (
        faithfulness("Employees receive 25 days of leave [1]. Bonuses are paid in March.", src)
        == 0.5
    )
    assert faithfulness("Bonuses are paid quarterly in March [1].", src) < 0.5
    assert faithfulness("I couldn't find this in the documents available to you.", src) is None


def test_percentile_and_summary():
    assert percentile([], 95) is None
    assert percentile(list(range(1, 101)), 95) == 95
    recs = [
        RequestRecord(
            0, "policy", "employee", "bm25", "answered", False, 100, 50, 10, 0.9, True, 0, 0.01
        ),
        RequestRecord(
            0, "policy", "employee", "bm25", "answered", True, 10, 0, 0, 0.5, False, 0, 0.0
        ),
        RequestRecord(
            0, "policy", "employee", "bm25", "blocked", False, 20, unauthorized=1, cost=0.01
        ),
        RequestRecord(0, "policy", "employee", "bm25", "error", False, 9999, cost=0.0),
    ]
    s = summarize(recs)
    assert s["requests"] == 4
    assert s["grounded_rate"] == 0.5
    assert s["unauthorized_rate"] == 0.25
    assert s["p95_ms"] == 100  # errors are left out of latency
    assert s["cost_per_success"] == 0.02  # all cost over the one grounded answer
    assert s["cache_hit_rate"] == 0.25


def test_store_round_trip_and_prune(tmp_path):
    store = KpiStore(tmp_path / "m.db")
    store.add(
        RequestRecord(100.0, "policy", "admin", "hybrid", "answered", True, 5.0, grounded=True)
    )
    store.add(RequestRecord(200.0, "policy", "admin", "hybrid", "refused", False, 7.0))
    rows = store.since(150)
    assert len(rows) == 1 and rows[0].status == "refused" and rows[0].cached is False
    assert store.prune(150) == 1
    assert len(store.since(0)) == 1


def test_answer_reports_usage_and_faithfulness(service):
    p = Principal("u", Role.EMPLOYEE, "policy")
    ans = service.answer(p, "How many days of paid annual leave do I get?", mode="bm25")
    assert ans.usage["prompt_tokens"] > 0 and ans.usage["completion_tokens"] > 0
    assert ans.faithfulness is not None and ans.faithfulness >= 0.7
    again = service.answer(p, "How many days of paid annual leave do I get?", mode="bm25")
    assert again.cached and again.usage == {}


def test_audit_flags_restricted_content(service):
    employee = Principal("u", Role.EMPLOYEE, "policy")
    restricted = next(
        c for c in service.chunks if c.tenant == "policy" and c.min_role > Role.EMPLOYEE
    )
    canary = next(iter(service.forbidden_canaries(employee)))
    clean = Answer(text="Nothing to see.", citations=[])
    assert audit(service, employee, clean) == 0
    leaked = Answer(
        text=f"The code is {canary}.", citations=[], retrieved_chunk_ids=[restricted.chunk_id]
    )
    assert audit(service, employee, leaked) == 2


def _app(settings, service, tmp_path):
    s = settings.model_copy(
        update={"metrics_db": tmp_path / "m.db", "eval_report": tmp_path / "e.json"}
    )
    return TestClient(create_app(s, service)), s


def _token(client, role):
    r = client.post("/auth/dev-token", json={"role": role, "tenant": "policy"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_ask_is_recorded_and_served_as_kpis(settings, service, tmp_path):
    client, _ = _app(settings, service, tmp_path)
    emp = _token(client, "employee")
    q = {"question": "How many days of paid annual leave do I get?", "mode": "bm25"}
    assert client.post("/ask", json=q, headers=emp).status_code == 200
    assert (
        client.post(
            "/ask", json={**q, "question": "What is the salary range for level L4?"}, headers=emp
        ).status_code
        == 200
    )

    assert client.get("/kpis", headers=emp).status_code == 403
    r = client.get("/kpis?hours=1", headers=_token(client, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["online"]["requests"] == 2
    assert body["online"]["unauthorized_rate"] == 0
    assert body["online"]["p95_ms"] is not None
    keys = [k["key"] for k in body["kpis"]]
    assert keys == [
        "recall_at_5",
        "mrr",
        "grounded_rate",
        "faithfulness",
        "unauthorized_rate",
        "p95_ms",
        "cost_per_success",
    ]
    unauth = body["kpis"][4]
    assert unauth["online"] == 0 and unauth["online_status"] == "ok"
    assert body["offline"] is None  # no eval report yet
    assert "Live: 2 requests" in format_kpis(body)

    metrics = client.get("/metrics").text
    assert 'copilot_requests_total{tenant="policy",status="answered"}' in metrics
    assert 'copilot_request_latency_seconds_bucket{tenant="policy",le="+Inf"} 2' in metrics
    assert 'copilot_unauthorized_retrievals_total{tenant="policy"} 0' in metrics
    assert "copilot_cost_total" in metrics
    # aggregate counts only: no question text
    assert "annual leave" not in metrics


def test_blocked_request_counts_as_unauthorized(settings, service, tmp_path, monkeypatch):
    client, _ = _app(settings, service, tmp_path)

    def boom(*a, **k):
        raise AccessViolation("test")

    monkeypatch.setattr(service, "answer", boom)
    r = client.post("/ask", json={"question": "anything"}, headers=_token(client, "employee"))
    assert r.status_code == 500
    body = client.get("/kpis", headers=_token(client, "admin")).json()
    assert body["online"]["by_status"]["blocked"] == 1
    assert body["online"]["unauthorized_rate"] == 1.0
    assert body["kpis"][4]["online_status"] == "miss"


def test_metrics_can_be_turned_off(settings, service, tmp_path):
    s = settings.model_copy(update={"metrics_enabled": False, "metrics_db": tmp_path / "m.db"})
    client = TestClient(create_app(s, service))
    assert client.get("/metrics").status_code == 404
    r = client.post("/ask", json={"question": "hi"}, headers=_token(client, "employee"))
    assert r.status_code == 200
    assert not (tmp_path / "m.db").exists()


def test_eval_report_feeds_offline_kpis(settings, service, tmp_path):
    questions = load_questions(ROOT / "eval" / "questions.jsonl")
    chunks = load_chunks(settings.index_dir / "chunks.jsonl")
    report = evaluate(service, questions, chunks, ["bm25"])
    m = report["modes"]["bm25"]
    assert m["recall@5"] >= 0.8
    assert m["unauthorized"] == 0 and m["unauthorized_rate"] == 0
    assert m["groundedness"] is not None and m["faithfulness"] is not None
    assert m["cost_per_success"] is not None
    assert "recall@5" in format_table(report)

    path = tmp_path / "eval.json"
    path.write_text(json.dumps(report))
    offline = load_offline(path, "hybrid_rerank")  # falls back to the mode that was run
    assert offline["mode"] == "bm25"
    assert offline["recall_at_5"] == m["recall@5"]
    assert offline["unauthorized_rate"] == 0
    assert offline["faithfulness"] == m["faithfulness"]

    monitor = Monitor(
        settings.model_copy(update={"metrics_db": tmp_path / "m.db", "eval_report": path})
    )
    rows = {r["key"]: r for r in monitor.report()["kpis"]}
    assert rows["recall_at_5"]["offline"] == m["recall@5"]
    assert rows["unauthorized_rate"]["offline_status"] == "ok"
    assert 'copilot_eval_recall_at_5{mode="bm25"}' in monitor.prometheus()


class FakeJudge:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def chat(self, messages: list[dict]) -> str:
        assert "Answer sentences:" in messages[-1]["content"]
        return self.reply


def test_llm_judge_scores_unsupported_sentences(service):
    p = Principal("u", Role.EMPLOYEE, "policy")
    ans = service.answer(p, "How many days of paid annual leave do I get?", mode="bm25")
    n = len([s for s in ans.text.split("].") if s.strip()])
    assert judge_faithfulness(FakeJudge('{"unsupported": []}'), ans, service.chunks_by_id) == 1.0
    flagged = judge_faithfulness(FakeJudge('Sure: {"unsupported": [1]}'), ans, service.chunks_by_id)
    assert flagged == 1 - 1 / n
    assert judge_faithfulness(FakeJudge("no idea"), ans, service.chunks_by_id) is None
