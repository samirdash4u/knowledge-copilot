"""Public demo mode: shared demo logins, a read-only server and limits on /ask."""

import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.demo import RateLimiter
from copilot.users import UserStore

DEMO = "ChangeMe123"


@pytest.fixture
def demo_settings(settings):
    return settings.model_copy(
        update={
            "public_demo": True,
            "dev_token_endpoint": False,
            "demo_asks_per_minute": 2,
            "demo_asks_per_day": 3,
            "client_ip_header": "CF-Connecting-IP",
        }
    )


@pytest.fixture
def users(tmp_path):
    return UserStore(tmp_path / "users.db", iterations=1_000)


@pytest.fixture
def client(demo_settings, service, users):
    return TestClient(create_app(demo_settings, service, users))


def _token(client, username="alice", ip="1.1.1.1"):
    r = client.post(
        "/auth/login",
        json={"username": username, "password": DEMO, "tenant": "policy"},
        headers={"CF-Connecting-IP": ip},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token, ip="1.1.1.1"):
    return {"Authorization": f"Bearer {token}", "CF-Connecting-IP": ip}


def test_refuses_unsafe_settings(demo_settings, service, users):
    with pytest.raises(RuntimeError, match="DEV_TOKEN"):
        create_app(demo_settings.model_copy(update={"dev_token_endpoint": True}), service, users)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app(
            demo_settings.model_copy(update={"jwt_secret": "change-me-in-production"}),
            service,
            users,
        )


def test_demo_info_lists_logins(client, settings, service, tmp_path):
    info = client.get("/demo").json()
    assert info["enabled"] is True
    assert {a["username"]: a["role"] for a in info["accounts"]} == {
        "alice": "employee",
        "bob": "manager",
        "carol": "admin",
    }
    assert all(a["password"] == DEMO for a in info["accounts"])
    normal = TestClient(create_app(settings, service, UserStore(tmp_path / "u.db", 1_000)))
    assert normal.get("/demo").json() == {"enabled": False}


def test_demo_accounts_need_no_password_change(client):
    token = _token(client)
    me = client.get("/me", headers=_auth(token)).json()
    assert me["must_change_password"] is False
    r = client.post(
        "/ask", json={"question": "How many days of annual leave?"}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text


def test_changed_demo_accounts_are_reset_on_start(demo_settings, service, users):
    users.seed_demo_users(DEMO)
    users.set_password("carol", "SomethingElse99", must_change=False)
    users.set_role("alice", "admin")
    users.delete("bob")
    create_app(demo_settings, service, users)
    for name, role in [("alice", "employee"), ("bob", "manager"), ("carol", "admin")]:
        user = users.authenticate(name, DEMO)
        assert user is not None and user.role.label == role and not user.must_change


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "post",
            "/auth/change-password",
            {"current_password": DEMO, "new_password": "x1234567890"},
        ),
        ("post", "/jobs/ingest", None),
        ("post", "/jobs/pull-model", None),
        ("post", "/documents?tenant=policy&filename=a.md", None),
        ("delete", "/documents/policy/leave", None),
        ("post", "/admin/users", {"username": "eve", "password": "x", "role": "admin"}),
        ("delete", "/admin/users/alice", None),
        ("post", "/admin/users/alice/password", {"password": "x"}),
        ("patch", "/admin/tenants/policy", {"label": "x"}),
    ],
)
def test_changes_are_refused_even_for_admin(client, method, path, body):
    token = _token(client, "carol")
    r = client.request(method.upper(), path, json=body, headers=_auth(token))
    assert r.status_code == 403
    assert "read-only public demo" in r.json()["detail"]


def test_reads_still_work_for_admin(client):
    token = _token(client, "carol")
    assert client.get("/admin/users", headers=_auth(token)).status_code == 200
    assert client.get("/documents?tenant=policy", headers=_auth(token)).status_code == 200


def test_ask_is_rate_limited_per_visitor(client):
    token = _token(client)
    ask = {"question": "What is the expense limit?"}
    for _ in range(2):
        assert client.post("/ask", json=ask, headers=_auth(token)).status_code == 200
    r = client.post("/ask", json=ask, headers=_auth(token))
    assert r.status_code == 429 and "Retry-After" in r.headers
    # Another visitor on the same shared account is not affected.
    other = client.post("/ask", json=ask, headers=_auth(token, ip="2.2.2.2"))
    assert other.status_code == 200


def test_failed_logins_lock_out_the_visitor_not_the_account(client):
    for _ in range(5):
        r = client.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong"},
            headers={"CF-Connecting-IP": "6.6.6.6"},
        )
    assert r.status_code == 401
    locked = client.post(
        "/auth/login",
        json={"username": "alice", "password": DEMO},
        headers={"CF-Connecting-IP": "6.6.6.6"},
    )
    assert locked.status_code == 429
    _token(client, "alice", ip="7.7.7.7")


def test_rate_limiter_windows():
    limiter = RateLimiter(per_minute=2, per_day=3)
    assert limiter.hit("a", now=0) == 0
    assert limiter.hit("a", now=1) == 0
    assert limiter.hit("a", now=2) == pytest.approx(58)
    assert limiter.hit("a", now=61) == 0
    assert limiter.hit("a", now=200) > 80_000  # day limit
    assert limiter.hit("b", now=200) == 0
