"""Login, password change and session handling for the web UI."""

import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.users import PasswordPolicyError, UserStore, hash_password, verify_password

DEMO = "ChangeMe123"
NEW = "BetterPass2026"


@pytest.fixture
def users(tmp_path):
    return UserStore(tmp_path / "users.db", iterations=1_000)


@pytest.fixture
def client(settings, service, users):
    return TestClient(create_app(settings, service, users))


def _login(client, username, password, tenant="policy"):
    return client.post(
        "/auth/login", json={"username": username, "password": password, "tenant": tenant}
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _ready(client, username):
    """Log in with the demo password and complete the forced change."""
    token = _login(client, username, DEMO).json()["access_token"]
    r = client.post(
        "/auth/change-password",
        json={"current_password": DEMO, "new_password": NEW},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_password_hash_roundtrip():
    h = hash_password("s3cret-pass", iterations=1_000)
    assert verify_password("s3cret-pass", h)
    assert not verify_password("wrong", h)
    assert "s3cret" not in h


def test_demo_users_seeded_once(users):
    assert users.seed_demo_users(DEMO) == ["alice", "bob", "carol"]
    assert users.seed_demo_users(DEMO) == []
    assert {u.username: u.role.label for u in users.list()} == {
        "alice": "employee",
        "bob": "manager",
        "carol": "admin",
    }


def test_demo_password_change_reaches_unused_demo_accounts(users):
    users.seed_demo_users(DEMO)
    users.change_password("bob", DEMO, "Summer2026Pass")
    assert users.sync_demo_passwords(DEMO) == []
    assert sorted(users.sync_demo_passwords("admin123")) == ["alice", "carol"]
    assert users.authenticate("alice", "admin123") is not None
    assert users.authenticate("alice", DEMO) is None
    assert users.get("alice").must_change
    # bob set his own password, so it is kept
    assert users.authenticate("bob", "Summer2026Pass") is not None


def test_password_policy(users):
    with pytest.raises(PasswordPolicyError):
        users.create("dave", "short1", "employee", ["policy"])
    with pytest.raises(PasswordPolicyError):
        users.create("dave", "onlyletterslong", "employee", ["policy"])
    with pytest.raises(PasswordPolicyError):
        users.create("dave", "dave12345678", "employee", ["policy"])


def test_login_rejects_bad_credentials(client):
    assert _login(client, "alice", "wrong-password").status_code == 401
    assert _login(client, "nobody", DEMO).status_code == 401
    assert _login(client, "alice", DEMO).status_code == 200


def test_login_locks_out_after_repeated_failures(client):
    for _ in range(5):
        assert _login(client, "bob", "nope").status_code == 401
    assert _login(client, "bob", DEMO).status_code == 429


def test_first_login_requires_password_change(client):
    body = _login(client, "alice", DEMO).json()
    assert body["user"]["must_change_password"] is True
    r = client.post("/ask", json={"question": "leave?"}, headers=_auth(body["access_token"]))
    assert r.status_code == 403


def test_password_change_flow(client):
    token = _ready(client, "alice")
    me = client.get("/me", headers=_auth(token)).json()
    assert me == {
        "username": "alice",
        "role": "employee",
        "tenant": "policy",
        "tenants": ["policy", "engineering"],
        "must_change_password": False,
    }
    assert _login(client, "alice", DEMO).status_code == 401
    assert _login(client, "alice", NEW).status_code == 200


def test_password_change_invalidates_old_tokens(client):
    old = _login(client, "alice", DEMO).json()["access_token"]
    new = client.post(
        "/auth/change-password",
        json={"current_password": DEMO, "new_password": NEW},
        headers=_auth(old),
    ).json()["access_token"]
    assert client.get("/me", headers=_auth(old)).status_code == 401
    assert client.get("/me", headers=_auth(new)).status_code == 200


def test_password_change_checks_current_and_policy(client):
    token = _login(client, "alice", DEMO).json()["access_token"]
    bad_current = client.post(
        "/auth/change-password",
        json={"current_password": "wrong", "new_password": NEW},
        headers=_auth(token),
    )
    assert bad_current.status_code == 403
    weak = client.post(
        "/auth/change-password",
        json={"current_password": DEMO, "new_password": "weak"},
        headers=_auth(token),
    )
    assert weak.status_code == 422


def test_logged_in_user_role_controls_answers(client):
    employee = _ready(client, "alice")
    manager = _ready(client, "bob")
    q = {"question": "What is the salary range for level L4?", "mode": "bm25"}
    emp = client.post("/ask", json=q, headers=_auth(employee)).json()
    mgr = client.post("/ask", json=q, headers=_auth(manager)).json()
    assert not any(c["doc_id"] == "salary-bands" for c in emp["citations"])
    assert any(c["doc_id"] == "salary-bands" for c in mgr["citations"])


def test_switch_tenant(client, users):
    token = _ready(client, "alice")
    r = client.post("/auth/switch-tenant", json={"tenant": "engineering"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["user"]["tenant"] == "engineering"
    r = client.post("/auth/switch-tenant", json={"tenant": "hr"}, headers=_auth(token))
    assert r.status_code == 403


def test_login_to_unassigned_tenant_is_refused(client, users):
    users.create("erin", "Summer2026Pass", "employee", ["policy"], must_change=False)
    assert _login(client, "erin", "Summer2026Pass", tenant="engineering").status_code == 403


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Knowledge Copilot" in r.text
