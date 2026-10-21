import httpx
import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.generation.llm import LLMUnavailable, OllamaLLM
from copilot.generation.prompt import fit_context
from copilot.models import Chunk, Role, ScoredChunk


def _scored(text: str, n: int = 0) -> ScoredChunk:
    chunk = Chunk(
        chunk_id=f"policy/doc#00-s#{n}",
        doc_id="doc",
        tenant="policy",
        title="Doc",
        section="S",
        text=text,
        min_role=Role.EMPLOYEE,
    )
    return ScoredChunk(chunk, 1.0)


def test_fit_context_drops_lower_ranked_sources():
    results = [_scored("a" * 400, i) for i in range(5)]
    kept = fit_context(results, 1000)
    assert [r.chunk.chunk_id for r in kept] == [r.chunk.chunk_id for r in results[:2]]


def test_fit_context_cuts_an_oversized_top_source():
    kept = fit_context([_scored("a" * 5000), _scored("b")], 1000)
    assert len(kept) == 1 and len(kept[0].chunk.text) == 1000


def _llm(handler) -> OllamaLLM:
    llm = OllamaLLM("http://ollama", "m", timeout=7)
    llm.client = httpx.Client(transport=httpx.MockTransport(handler))
    return llm


def test_ollama_timeout_becomes_friendly_error():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMUnavailable) as err:
        _llm(handler).chat([{"role": "user", "content": "hi"}])
    assert err.value.status == 504
    assert "COPILOT_LLM_TIMEOUT_S" in str(err.value)


def test_ollama_request_bounds_context_and_length():
    seen = {}

    def handler(request):
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": " ok "}})

    assert _llm(handler).chat([{"role": "user", "content": "hi"}]) == "ok"
    assert seen["options"]["num_ctx"] == 4096
    assert seen["options"]["num_predict"] == 512
    assert seen["keep_alive"] == "30m"


def test_ask_returns_504_when_llm_times_out(settings, service):
    class SlowLLM:
        def chat(self, messages):
            raise LLMUnavailable("The language model took too long.", status=504)

    service.llm = SlowLLM()
    client = TestClient(create_app(settings, service))
    token = client.post("/auth/dev-token", json={"role": "employee", "tenant": "policy"})
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    r = client.post(
        "/ask",
        json={"question": "How many days of paid annual leave do I get?", "use_cache": False},
        headers=headers,
    )
    assert r.status_code == 504
    assert r.json()["detail"] == "The language model took too long."
