"""Suggested questions under the chat box."""

from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.auth import issue_token
from copilot.models import Chunk, Principal, Role
from copilot.suggestions import configured, generated


def _get(settings, service, role, tenant):
    client = TestClient(create_app(settings, service))
    token = issue_token(settings, "u", role, tenant)
    return client.get("/suggestions", headers={"Authorization": f"Bearer {token}"})


def test_builtin_tenants_use_their_tenant_yaml(settings, service):
    r = _get(settings, service, "employee", "policy")
    assert r.status_code == 200
    assert r.json()[0] == "How many days of paid annual leave do I get?"
    assert "How do I roll back a service?" in _get(settings, service, "admin", "engineering").json()


def test_requires_sign_in(settings, service):
    assert TestClient(create_app(settings, service)).get("/suggestions").status_code == 401


def _chunk(doc, section, role, tenant="legal", title="Contracts Policy"):
    return Chunk(
        chunk_id=f"{tenant}/{doc}#{section}",
        doc_id=doc,
        tenant=tenant,
        title=title,
        section=f"{title} > {section}" if section else title,
        text="x",
        min_role=role,
    )


def test_generated_respects_role_and_tenant():
    chunks = [
        _chunk("contracts", "", Role.EMPLOYEE),
        _chunk("contracts", "Signatures", Role.EMPLOYEE),
        _chunk("contracts", "Settlement limits", Role.MANAGER),
        _chunk("nda", "Page 2", Role.EMPLOYEE, title="The NDA Template"),
        _chunk("other", "Secret", Role.EMPLOYEE, tenant="policy", title="Other"),
    ]
    employee = generated(Principal("e", Role.EMPLOYEE, "legal"), chunks)
    assert employee == [
        "What is the Contracts Policy about?",
        "What is The NDA Template about?",
        'What does the Contracts Policy say about "Signatures"?',
    ]
    manager = generated(Principal("m", Role.MANAGER, "legal"), chunks)
    # Material written for the caller's own role comes first.
    assert manager[0] == 'What does the Contracts Policy say about "Settlement limits"?'
    assert not any("Settlement" in q for q in employee)
    assert not any("Secret" in q for q in manager)


def test_generated_is_capped():
    chunks = [_chunk(f"d{i}", f"S{i}", Role.EMPLOYEE) for i in range(10)]
    assert len(generated(Principal("e", Role.EMPLOYEE, "legal"), chunks)) == 4


def test_configured_forms():
    assert configured(None, "employee") is None
    assert configured({"label": "x"}, "employee") is None
    assert configured(["A?", " ", "B?"], "admin") == ["A?", "B?"]
    per_role = {"manager": ["M?"]}
    assert configured(per_role, "manager") == ["M?"]
    assert configured(per_role, "employee") is None  # not set for this role: generated instead
