"""Admin console, document upload and background jobs: role checks are enforced on the server."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.ingest.pipeline import pending_changes
from copilot.jobs import JobManager
from copilot.service import ingest
from copilot.users import UserStore

ROOT = Path(__file__).resolve().parents[1]

PW = "Sunny-day-2026"
MD = b"---\ntenant: engineering\nmin_role: employee\n---\n# Travel Rules\n\n## Booking\n\nBook trains at least 7 days ahead and keep the TRAVELCODE-42 receipt.\n"  # noqa: E501


@pytest.fixture
def env(settings, tmp_path):
    """A private copy of the corpus and index, so uploads and ingests don't touch the repo."""
    corpus = tmp_path / "corpus"
    shutil.copytree(ROOT / "data" / "corpus", corpus)
    s = settings.model_copy(
        update={
            "corpus_dir": corpus,
            "index_dir": tmp_path / "index",
            "users_db": tmp_path / "users.db",
            "metrics_db": tmp_path / "metrics.db",
            "eval_report": tmp_path / "reports" / "eval.json",
            "eval_questions": ROOT / "eval" / "questions.jsonl",
            "seed_demo_users": False,
        }
    )
    users = UserStore(s.users_db, iterations=1_000)
    for name, role in [("emma", "employee"), ("mike", "manager"), ("ada", "admin")]:
        users.create(name, PW, role, ["policy"], must_change=False)
    users.create("eng", PW, "employee", ["engineering"], must_change=False)
    return s, users


@pytest.fixture
def client(env):
    s, users = env
    ingest(s)
    return TestClient(create_app(s, users=users))


def login(client, username, tenant="policy"):
    r = client.post("/auth/login", json={"username": username, "password": PW, "tenant": tenant})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def upload(client, headers, name="travel.md", body=MD, **params):
    params = {"tenant": "policy", "min_role": "employee", "filename": name, **params}
    params.setdefault("suggest", "false")
    return client.post("/documents", params=params, content=body, headers=headers)


def wait(client, job_id):
    return client.app.state.jobs.wait(job_id, timeout=60)


# ---------------------------------------------------------------- role checks


ADMIN_ONLY = [
    ("get", "/admin/status", None),
    ("get", "/admin/users", None),
    (
        "post",
        "/admin/users",
        {"username": "x", "password": PW, "role": "admin", "tenants": ["policy"]},
    ),  # noqa: E501
    ("patch", "/admin/users/emma", {"role": "admin"}),
    ("post", "/admin/users/emma/password", {"password": PW}),
    ("delete", "/admin/users/mike", None),
    ("get", "/admin/tenants", None),
    ("post", "/admin/tenants", {"name": "legal", "roles": ["employee"]}),
    ("patch", "/admin/tenants/policy", {"roles": ["employee"]}),
    ("get", "/admin/eval", None),
    ("post", "/jobs/eval", {"limit": 2}),
    ("post", "/jobs/pull-model", None),
    ("delete", "/documents/policy/leave-policy", None),
]


@pytest.mark.parametrize("user", ["emma", "mike"])
@pytest.mark.parametrize("method,path,body", ADMIN_ONLY)
def test_admin_endpoints_reject_non_admins(client, user, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, headers=login(client, user), **kwargs)
    assert r.status_code == 403, (path, r.text)


def test_endpoints_need_a_token(client):
    for method, path, _ in ADMIN_ONLY + [("post", "/jobs/ingest", None), ("get", "/jobs", None)]:
        assert getattr(client, method)(path).status_code == 401, path
    assert upload(client, {}).status_code == 401


def test_must_change_password_blocks_admin_console(env):
    s, users = env
    users.create("newbie", PW, "admin", ["policy"], must_change=True)
    client = TestClient(create_app(s, users=users))
    headers = login(client, "newbie")
    assert client.get("/admin/status", headers=headers).status_code == 403
    assert client.post("/jobs/ingest", headers=headers).status_code == 403
    assert upload(client, headers).status_code == 403


# ---------------------------------------------------------------- uploads


def test_employee_uploads_and_indexes(client, env):
    s, _ = env
    h = login(client, "emma")
    r = upload(client, h, index="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == "travel" and body["title"] == "Travel Rules"
    assert body["min_role"] == "employee"
    # The file's own front matter can't move it to another tenant.
    text = (s.corpus_dir / "policy" / "travel.md").read_text()
    assert "tenant:" not in text and "min_role: employee" in text
    job = next(j for j in body["jobs"] if j["kind"] == "ingest")
    assert wait(client, job["id"]).status == "succeeded"
    ans = client.post("/ask", json={"question": "How early must I book trains?"}, headers=h)
    assert ans.status_code == 200
    assert any(c["doc_id"] == "travel" for c in ans.json()["citations"])
    # Not visible in the other tenant.
    eng = login(client, "eng", "engineering")
    other = client.post("/ask", json={"question": "TRAVELCODE-42 train booking"}, headers=eng)
    assert all(c["doc_id"] != "travel" for c in other.json()["citations"])


def test_upload_min_role_cannot_exceed_own_role(client):
    assert upload(client, login(client, "emma"), min_role="manager").status_code == 403
    assert upload(client, login(client, "mike"), min_role="admin").status_code == 403
    assert upload(client, login(client, "mike"), min_role="manager").status_code == 200
    assert upload(client, login(client, "ada"), name="a.md", min_role="admin").status_code == 200


def test_upload_only_to_own_tenants(client):
    h = login(client, "emma")  # policy only
    assert upload(client, h, tenant="engineering").status_code == 403
    assert upload(client, h, tenant="nope").status_code == 403
    assert upload(client, login(client, "ada"), tenant="nope").status_code == 403


def test_upload_rejects_bad_files(client, env):
    s, _ = env
    s.max_upload_mb = 0.001
    h = login(client, "emma")
    assert upload(client, h, name="x.exe").status_code == 415
    assert upload(client, h, name="x.pdf", body=b"not a pdf").status_code == 422
    assert upload(client, h, body=b"").status_code == 422
    assert upload(client, h, body=b"x" * 2000).status_code == 413


def test_upload_filename_cannot_escape_the_tenant(client, env):
    s, _ = env
    r = upload(client, login(client, "emma"), name="../../evil.md")
    assert r.status_code == 200
    assert (s.corpus_dir / "policy" / "evil.md").exists()
    assert not (s.corpus_dir.parent / "evil.md").exists()


def test_replace_needs_overwrite_and_enough_role(client):
    mike, emma = login(client, "mike"), login(client, "emma")
    assert upload(client, mike, name="plan.md", min_role="manager").status_code == 200
    # Same document id again without 'overwrite' is refused.
    assert upload(client, mike, name="plan.md", min_role="manager").status_code == 409
    # An employee may not replace a manager-only document, even with 'overwrite'.
    assert upload(client, emma, name="plan.md", overwrite="true").status_code == 403
    # Nor one from the repo corpus they can't see (privileged access is admin-only).
    r = upload(client, mike, name="privileged-access.md", overwrite="true")
    assert r.status_code == 403
    r = upload(client, mike, name="plan.md", min_role="manager", overwrite="true")
    assert r.status_code == 200


def test_document_list_hides_documents_above_the_role(client):
    upload(client, login(client, "mike"), name="plan.md", min_role="manager")
    emma = client.get("/documents", headers=login(client, "emma")).json()
    mike = client.get("/documents", headers=login(client, "mike")).json()
    assert "plan" not in {d["doc_id"] for d in emma["documents"]}
    assert "plan" in {d["doc_id"] for d in mike["documents"]}
    assert all(d["min_role"] == "employee" for d in emma["documents"])
    assert emma["upload_roles"] == ["employee"]
    assert mike["pending"] >= 1
    plan = next(d for d in mike["documents"] if d["doc_id"] == "plan")
    assert plan["indexed"] is False
    h = login(client, "emma")
    assert client.get("/documents?tenant=engineering", headers=h).status_code == 403


def test_docx_upload_is_converted(client, tmp_path):
    from test_convert import _docx

    data = _docx(tmp_path / "Travel Policy.docx").read_bytes()
    r = upload(client, login(client, "emma"), name="Travel Policy.docx", body=data)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Travel Policy 2027" and r.json()["sections"] >= 2


def test_upload_queues_suggestions(client, env):
    s, _ = env
    r = upload(client, login(client, "emma"), suggest="true")
    job = next(j for j in r.json()["jobs"] if j["kind"] == "suggest")
    assert wait(client, job["id"]).status == "succeeded"
    assert "suggestions" in (s.corpus_dir / "policy" / "tenant.yaml").read_text()


# ---------------------------------------------------------------- jobs


def test_ingest_job_reloads_the_running_server(client, env):
    s, _ = env
    h = login(client, "emma")
    upload(client, h)
    assert pending_changes(s) == ["policy/travel.md"]
    r = client.post("/jobs/ingest", headers=h)
    assert r.status_code == 200
    job = wait(client, r.json()["id"])
    assert job.status == "succeeded", job.error
    assert job.result["documents"] >= 15
    assert pending_changes(s) == []
    assert "travel" in {c.doc_id for c in client.app.state.service.chunks}


def test_non_admins_see_ingest_jobs_but_not_others(client):
    ada, emma = login(client, "ada"), login(client, "emma")
    ev = client.post("/jobs/eval", json={"limit": 2, "retrieval_only": True}, headers=ada).json()
    ing = client.post("/jobs/ingest", headers=ada).json()
    wait(client, ev["id"])
    wait(client, ing["id"])
    seen = {j["id"] for j in client.get("/jobs", headers=emma).json()}
    assert ing["id"] in seen and ev["id"] not in seen
    assert client.get(f"/jobs/{ev['id']}", headers=emma).status_code == 404
    # Someone else's ingest: visible, but no details, and not theirs to cancel.
    assert "log" not in client.get(f"/jobs/{ing['id']}", headers=emma).json()
    assert client.post(f"/jobs/{ing['id']}/cancel", headers=emma).status_code == 403


def test_suggest_jobs_are_private(client):
    mike = login(client, "mike")
    r = upload(client, mike, name="plan.md", min_role="manager", suggest="true")
    job = r.json()["jobs"][0]
    wait(client, job["id"])
    emma = login(client, "emma")
    assert job["id"] not in {j["id"] for j in client.get("/jobs", headers=emma).json()}
    assert client.get(f"/jobs/{job['id']}", headers=mike).status_code == 200


def test_eval_job_writes_report_and_results(client, env):
    s, _ = env
    h = login(client, "ada")
    r = client.post("/jobs/eval", json={"limit": 4}, headers=h)
    assert r.status_code == 200
    job = wait(client, r.json()["id"])
    assert job.status == "succeeded", job.error
    assert job.result["passed"] is True
    assert job.done == job.total == 4
    report = json.loads(s.eval_report.read_text())
    assert report["questions"] == 4
    res = client.get("/admin/eval", headers=h).json()
    assert set(res["report"]["modes"]) == {"bm25", "vector", "hybrid", "hybrid_rerank"}
    assert "per_question" not in res["report"]["modes"]["bm25"]
    # The KPI page picks the new report up.
    assert client.get("/kpis", headers=h).json()["offline"]["questions"] == 4


def test_job_queue_runs_one_at_a_time_and_cancels():
    import threading

    jobs = JobManager()
    gate = threading.Event()
    order = []

    def first(ctx):
        order.append("first")
        gate.wait(5)
        for i in range(100):
            ctx.progress(i, 100, "working")
        return {"ok": True}

    a, _ = jobs.submit("ingest", "A", first, "u")
    while not order:  # wait until A is running, so B is the one waiting
        threading.Event().wait(0.01)
    b, _ = jobs.submit("ingest", "B", lambda ctx: order.append("second"), "u")
    c, created = jobs.submit("ingest", "C", lambda ctx: None, "u", reuse_queued=True)
    assert c.id == b.id and not created  # an ingest already waiting covers this request
    jobs.cancel(a.id)
    gate.set()
    assert jobs.wait(a.id).status == "cancelled"
    assert jobs.wait(b.id).status == "succeeded"
    assert order == ["first", "second"]


def test_no_index_yet(env):
    s, users = env
    client = TestClient(create_app(s, users=users))
    h = login(client, "ada")
    r = client.post("/ask", json={"question": "leave?"}, headers=h)
    assert r.status_code == 503 and "ingest" in r.json()["detail"]
    st = client.get("/admin/status", headers=h).json()
    assert st["index"]["built"] is False and st["index"]["pending_count"] >= 14
    job = wait(client, client.post("/jobs/ingest", headers=h).json()["id"])
    assert job.status == "succeeded", job.error
    assert client.post("/ask", json={"question": "leave?"}, headers=h).status_code == 200


def test_status_reports_components(client):
    st = client.get("/admin/status", headers=login(client, "ada")).json()
    assert st["index"]["built"] and st["index"]["pending_count"] == 0
    assert {c["name"] for c in st["components"]} == {"LLM", "Vector store", "Answer cache"}
    assert all(c["ok"] for c in st["components"])  # echo LLM, memory store and cache
    assert st["users"] == 4


# ---------------------------------------------------------------- tenants and users


def test_admin_creates_tenant_and_grants(client, env):
    s, users = env
    h = login(client, "ada")
    r = client.post(
        "/admin/tenants",
        json={"name": "legal", "label": "Legal", "roles": ["manager"], "users": ["emma"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert (s.corpus_dir / "legal" / "tenant.yaml").exists()
    assert "legal" in users.get("emma").tenants
    tenants = {t["name"]: t for t in client.get("/admin/tenants", headers=h).json()}
    assert set(tenants["legal"]["users"]) == {"emma", "mike"}  # mike by role
    assert client.post("/admin/tenants", json={"name": "legal"}, headers=h).status_code == 409
    assert client.post("/admin/tenants", json={"name": "Bad Name"}, headers=h).status_code == 422
    bad = {"name": "x", "roles": ["boss"]}
    assert client.post("/admin/tenants", json=bad, headers=h).status_code == 422
    # The new tenant is usable for uploads by those who have it.
    assert upload(client, login(client, "mike"), tenant="legal").status_code == 200
    r = client.patch("/admin/tenants/legal", json={"label": "Legal team", "roles": []}, headers=h)
    assert r.json() == {"name": "legal", "label": "Legal team", "roles": []}


def test_admin_manages_users(client, env):
    _, users = env
    h = login(client, "ada")
    new = {"username": "zoe", "password": PW, "role": "employee", "tenants": ["policy"]}
    r = client.post("/admin/users", json=new, headers=h)
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert client.post("/admin/users", json=new, headers=h).status_code == 409
    weak = {**new, "username": "zed", "password": "short"}
    assert client.post("/admin/users", json=weak, headers=h).status_code == 422
    unknown = {**new, "username": "zed", "tenants": ["nope"]}
    assert client.post("/admin/users", json=unknown, headers=h).status_code == 422

    emma = login(client, "emma")
    r = client.patch("/admin/users/emma", json={"tenants": ["policy", "engineering"]}, headers=h)
    assert r.json()["tenants"] == ["policy", "engineering"]
    assert client.get("/me", headers=emma).status_code == 200  # tenant grant keeps the session
    r = client.patch("/admin/users/emma", json={"role": "manager"}, headers=h)
    assert r.json()["role"] == "manager"
    assert client.get("/me", headers=emma).status_code == 401  # role change ends the session

    r = client.post("/admin/users/mike/password", json={"password": "Fresh-start-77"}, headers=h)
    assert r.status_code == 200 and users.get("mike").must_change

    assert client.delete("/admin/users/zoe", headers=h).status_code == 200
    assert users.get("zoe") is None


def test_admin_cannot_lock_everyone_out(client):
    h = login(client, "ada")
    assert client.patch("/admin/users/ada", json={"role": "employee"}, headers=h).status_code == 400
    assert client.delete("/admin/users/ada", headers=h).status_code == 400
    assert client.get("/admin/status", headers=h).status_code == 200


def test_admin_deletes_document(client, env):
    s, _ = env
    h = login(client, "ada")
    upload(client, h)
    assert client.delete("/documents/policy/travel", headers=h).status_code == 200
    assert not (s.corpus_dir / "policy" / "travel.md").exists()
    assert client.delete("/documents/policy/travel", headers=h).status_code == 404


def test_qdrant_ingest_writes_a_new_collection_and_keeps_the_previous(env):
    """Live questions keep reading the old collection until the server loads the new one."""
    from copilot.ingest.pipeline import run_ingest
    from copilot.retrieval.embeddings import HashEmbedder

    class FakeQdrant:
        def __init__(self):
            self.collection = "chunks"
            self.written, self.kept = [], None

        def upsert(self, chunks, vectors):
            self.written.append(self.collection)

        def drop_collections_except(self, keep):
            self.kept = keep

    s, _ = env
    store = FakeQdrant()
    first = run_ingest(s, HashEmbedder(), store)
    assert first["collection"].startswith("chunks-") and store.kept == {
        first["collection"],
        "chunks",
    }
    store2 = FakeQdrant()
    second = run_ingest(s, HashEmbedder(), store2)
    assert store2.kept == {second["collection"], first["collection"]}
