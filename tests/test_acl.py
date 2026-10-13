"""ACL leak tests: restricted content must never reach a caller who may not see it."""

import pytest

from copilot.eval.runner import CANARY, forbidden_canaries
from copilot.models import Principal, Role
from copilot.retrieval.hybrid import MODES
from copilot.retrieval.vector import load_chunks

QUESTIONS = [
    "What is the salary range for level L4?",
    "Up to what amount can a manager approve an expense claim?",
    "What is the cap on the executive bonus pool?",
    "How do I fail over the payments Postgres cluster?",
    "When can break-glass accounts be used?",
    "Internal reference CANARY",
]


@pytest.fixture(scope="module")
def chunks(settings, manifest):
    return load_chunks(settings.index_dir / "chunks.jsonl")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("role", [Role.EMPLOYEE, Role.MANAGER])
@pytest.mark.parametrize("tenant", ["policy", "engineering"])
def test_retrieval_never_returns_forbidden_chunks(service, chunks, mode, role, tenant):
    p = Principal("u", role, tenant)
    forbidden = forbidden_canaries(chunks, p)
    assert forbidden, "test corpus should contain restricted canaries"
    for q in QUESTIONS:
        for r in service.retriever.retrieve(p, q, 20, mode=mode):
            assert r.chunk.tenant == tenant
            assert r.chunk.min_role <= role
            assert not set(CANARY.findall(r.chunk.text)) & forbidden


def test_higher_role_does_see_restricted_content(service):
    p = Principal("u", Role.MANAGER, "policy")
    results = service.retriever.retrieve(p, "salary range for level L4", 6, mode="bm25")
    assert any(r.chunk.doc_id == "salary-bands" for r in results)


def test_employee_gets_refusal_when_nothing_visible(service):
    p = Principal("u", Role.EMPLOYEE, "policy")
    ans = service.answer(p, "executive bonus pool remuneration committee cap", mode="bm25")
    assert "CANARY-ADM" not in ans.text


def test_cache_is_partitioned_by_role(service, chunks):
    """The classic trap: a manager's cached answer must not be served to an employee."""
    q = "What is the salary range for level L4?"
    manager = Principal("m", Role.MANAGER, "policy")
    employee = Principal("e", Role.EMPLOYEE, "policy")

    first = service.answer(manager, q)
    assert not first.cached
    assert service.answer(manager, q).cached  # cache works for the same scope

    ans = service.answer(employee, q)
    assert not ans.cached
    forbidden = forbidden_canaries(chunks, employee)
    assert not set(CANARY.findall(ans.text)) & forbidden
    assert not any(c.doc_id == "salary-bands" for c in ans.citations)


def test_cache_is_partitioned_by_tenant(service):
    q = "How many approvals does a pull request need?"
    eng = Principal("a", Role.EMPLOYEE, "engineering")
    pol = Principal("b", Role.EMPLOYEE, "policy")
    service.answer(eng, q)
    assert not service.answer(pol, q).cached


def test_post_retrieval_check_fails_closed(service):
    from copilot.acl import AccessViolation, assert_all_allowed
    from copilot.models import Chunk, ScoredChunk

    secret = Chunk("x#0", "x", "policy", "X", "X", "secret", Role.ADMIN)
    with pytest.raises(AccessViolation):
        assert_all_allowed(Principal("u", Role.EMPLOYEE, "policy"), [ScoredChunk(secret, 1.0)])
