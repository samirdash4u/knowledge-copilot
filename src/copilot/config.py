"""Runtime configuration, read from environment variables (prefix ``COPILOT_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    # Server: where `copilot serve` listens. 127.0.0.1 = this machine only; 0.0.0.0 = every
    # network interface (needed to open the UI from other machines).
    host: str = "127.0.0.1"
    port: int = 8000

    # Paths
    corpus_dir: Path = Path("data/corpus")
    index_dir: Path = Path("data/index")

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 60
    # Exposes POST /auth/dev-token so the local demo can mint tokens for any role. Never enable
    # this outside local development.
    dev_token_endpoint: bool = False

    # Users (web UI login)
    users_db: Path = Path("data/users.db")
    password_hash_iterations: int = 600_000
    # On first start with an empty user database, create alice (employee), bob (manager) and
    # carol (admin) with this password. Each must change it at first login.
    seed_demo_users: bool = True
    demo_password: str = "ChangeMe123"
    login_max_failures: int = 5
    login_lockout_s: int = 300

    # Chunking
    chunk_size: int = 800  # characters
    chunk_overlap: int = 120

    # Retrieval
    retrieval_mode: Literal["bm25", "vector", "hybrid", "hybrid_rerank"] = "hybrid_rerank"
    candidates_per_retriever: int = 30
    rrf_k: int = 60
    top_k: int = 6
    # Upper bound on the source text sent to the LLM per question. Lower-ranked sources are
    # dropped first, so a long document can't blow up the prompt (and the answer time).
    max_context_chars: int = 6000

    # Backends
    embedder: Literal["hash", "sentence-transformers"] = "sentence-transformers"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reranker: Literal["none", "cross-encoder"] = "cross-encoder"
    reranker_model: str = "BAAI/bge-reranker-base"
    vector_store: Literal["memory", "qdrant"] = "qdrant"
    qdrant_url: str = "http://localhost:6333"

    # LLM
    llm: Literal["ollama", "echo"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct-q4_K_M"
    llm_temperature: float = 0.1
    # How long to wait for an answer. A 7B model on the CPU, or one loading on the first question,
    # can take minutes. Raise this on slow machines.
    llm_timeout_s: float = 300.0
    llm_num_ctx: int = 4096  # context window in tokens; smaller uses less GPU memory
    llm_max_tokens: int = 512  # cap on the answer's length
    llm_keep_alive: str = "30m"  # how long Ollama keeps the model loaded after a question

    # Cache
    cache: Literal["none", "memory", "redis"] = "redis"
    redis_url: str = "redis://localhost:6379/0"
    cache_similarity_threshold: float = 0.95
    cache_ttl_s: int = 3600

    # KPI monitoring (see docs/monitoring.md). Each /ask is recorded in a SQLite file without
    # the question or answer text. /metrics serves counts for Prometheus without a token.
    metrics_enabled: bool = True
    metrics_db: Path = Path("data/metrics.db")
    metrics_retention_days: int = 30
    eval_report: Path = Path("reports/eval.json")  # offline KPIs come from the latest eval
    # Answers whose cited sources support at least this share of their content count as grounded.
    grounded_threshold: float = 0.7
    # Local cost model; there is no API bill. The default prices compute time at a ~150 W laptop
    # and 0.15 per kWh. Set the token prices to compare with a hosted model.
    cost_currency: str = "USD"
    cost_per_compute_second: float = 0.00000625
    cost_per_1k_prompt_tokens: float = 0.0
    cost_per_1k_completion_tokens: float = 0.0
    # Targets shown next to each KPI.
    target_recall_at_5: float = 0.9
    target_mrr: float = 0.75
    target_grounded_rate: float = 0.9
    target_faithfulness: float = 0.8
    target_unauthorized_rate: float = 0.0
    target_p95_ms: float = 10_000
    target_cost_per_answer: float = 0.0001


@lru_cache
def get_settings() -> Settings:
    return Settings()
