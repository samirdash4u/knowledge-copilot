from pathlib import Path

import pytest

from copilot.config import Settings
from copilot.service import CopilotService, ingest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def settings(tmp_path_factory) -> Settings:
    """Offline settings: hash embeddings, in-memory store and cache, extractive fake LLM."""
    return Settings(
        corpus_dir=ROOT / "data" / "corpus",
        index_dir=tmp_path_factory.mktemp("index"),
        embedder="hash",
        reranker="none",
        vector_store="memory",
        llm="echo",
        cache="memory",
        dev_token_endpoint=True,
        jwt_secret="test-secret-that-is-at-least-32-bytes-long",
        users_db=tmp_path_factory.mktemp("users") / "users.db",
        password_hash_iterations=1_000,
    )


@pytest.fixture(scope="session")
def manifest(settings):
    return ingest(settings)


@pytest.fixture
def service(settings, manifest) -> CopilotService:
    # fresh instance per test so the cache starts empty
    return CopilotService.from_settings(settings)
