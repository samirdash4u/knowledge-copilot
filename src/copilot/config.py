"""Runtime configuration, read from environment variables (prefix ``COPILOT_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

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
    llm_timeout_s: float = 120.0

    # Cache
    cache: Literal["none", "memory", "redis"] = "redis"
    redis_url: str = "redis://localhost:6379/0"
    cache_similarity_threshold: float = 0.95
    cache_ttl_s: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
