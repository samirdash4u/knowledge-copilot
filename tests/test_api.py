from fastapi.testclient import TestClient

from copilot.api import create_app


def _client(settings, service):
    return TestClient(create_app(settings, service))


def _token(client, role, tenant="policy"):
    r = client.post("/auth/dev-token", json={"role": role, "tenant": tenant})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_requires_token(settings, service):
    client = _client(settings, service)
    assert client.post("/ask", json={"question": "hi"}).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert client.post("/ask", json={"question": "hi"}, headers=bad).status_code == 401


def test_ask_returns_cited_answer(settings, service):
    client = _client(settings, service)
    r = client.post(
        "/ask",
        json={"question": "How many days of paid annual leave do I get?", "mode": "bm25"},
        headers=_token(client, "employee"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["citations"]
    assert all(c["doc_id"] for c in body["citations"])


def test_role_in_body_is_ignored(settings, service):
    client = _client(settings, service)
    r = client.post(
        "/ask",
        json={"question": "What is the salary range for level L4?", "role": "admin"},
        headers=_token(client, "employee"),
    )
    assert r.status_code == 200
    assert "CANARY-MGR-SAL" not in r.text


def test_whoami_reflects_token(settings, service):
    client = _client(settings, service)
    r = client.get("/whoami", headers=_token(client, "manager", "engineering"))
    assert r.json() == {"user_id": "demo", "role": "manager", "tenant": "engineering"}


def test_dev_token_endpoint_off_by_default(settings, service):
    client = _client(settings.model_copy(update={"dev_token_endpoint": False}), service)
    assert client.post("/auth/dev-token", json={"role": "admin"}).status_code == 404
