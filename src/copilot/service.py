"""The question-answering pipeline, wired from settings."""

from __future__ import annotations

import json
import time

from copilot.acl import access_scope, forbidden_canaries
from copilot.cache import AnswerCache, InMemorySemanticCache, NullCache, RedisSemanticCache
from copilot.config import Settings
from copilot.generation.citations import check_citations, faithfulness
from copilot.generation.llm import LLM, EchoLLM, OllamaLLM, chat_with_usage
from copilot.generation.prompt import REFUSAL, build_messages, fit_context
from copilot.ingest.pipeline import Progress, run_ingest
from copilot.models import Answer, Chunk, Principal
from copilot.retrieval.bm25 import BM25Index
from copilot.retrieval.embeddings import Embedder, HashEmbedder, SentenceTransformerEmbedder
from copilot.retrieval.hybrid import HybridRetriever
from copilot.retrieval.rerank import CrossEncoderReranker, PassthroughReranker, Reranker
from copilot.retrieval.vector import (
    InMemoryVectorStore,
    QdrantVectorStore,
    VectorStore,
    load_chunks,
)


def make_embedder(s: Settings) -> Embedder:
    if s.embedder == "hash":
        return HashEmbedder()
    return SentenceTransformerEmbedder(s.embedding_model)


def make_reranker(s: Settings) -> Reranker:
    if s.reranker == "none":
        return PassthroughReranker()
    return CrossEncoderReranker(s.reranker_model)


def make_store(s: Settings, collection: str = "chunks") -> VectorStore:
    if s.vector_store == "memory":
        return InMemoryVectorStore()
    return QdrantVectorStore(s.qdrant_url, collection)


def make_llm(s: Settings) -> LLM:
    if s.llm == "echo":
        return EchoLLM()
    return OllamaLLM(
        s.ollama_url,
        s.ollama_model,
        s.llm_temperature,
        s.llm_timeout_s,
        num_ctx=s.llm_num_ctx,
        max_tokens=s.llm_max_tokens,
        keep_alive=s.llm_keep_alive,
    )


def make_cache(s: Settings, corpus_version: str) -> AnswerCache:
    if s.cache == "none":
        return NullCache()
    if s.cache == "memory":
        return InMemorySemanticCache(corpus_version, s.cache_similarity_threshold, s.cache_ttl_s)
    return RedisSemanticCache(
        s.redis_url, corpus_version, s.cache_similarity_threshold, s.cache_ttl_s
    )


def ingest(
    settings: Settings, embedder: Embedder | None = None, progress: Progress | None = None
) -> dict:
    embedder = embedder or make_embedder(settings)
    return run_ingest(settings, embedder, make_store(settings), progress=progress)


class IndexMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "The documents have not been indexed yet. Run ingest (Admin or Documents page in the"
            " web UI, or 'copilot ingest')."
        )


class CopilotService:
    def __init__(
        self,
        settings: Settings,
        retriever: HybridRetriever,
        llm: LLM,
        cache: AnswerCache,
        corpus_version: str,
        chunks: list[Chunk] | None = None,
    ) -> None:
        self.settings = settings
        self.chunks = chunks or []  # every indexed chunk; filter with acl.visible before use
        self.retriever = retriever
        self.llm = llm
        self.cache = cache
        self.corpus_version = corpus_version
        self.chunks_by_id = {c.chunk_id: c for c in self.chunks}
        self._forbidden: dict[str, set[str]] = {}

    def forbidden_canaries(self, principal: Principal) -> set[str]:
        """Canary tokens this caller must never see, computed once per access scope."""
        scope = access_scope(principal)
        if scope not in self._forbidden:
            self._forbidden[scope] = forbidden_canaries(self.chunks, principal)
        return self._forbidden[scope]

    @classmethod
    def from_settings(
        cls,
        s: Settings,
        previous: CopilotService | None = None,
        embedder: Embedder | None = None,
    ) -> CopilotService:
        """Load the index. ``previous`` (or ``embedder``) lends already-loaded models, so
        reloading after an ingest doesn't load the embedding model and reranker a second time."""
        if not (s.index_dir / "manifest.json").is_file():
            raise IndexMissing()
        manifest = json.loads((s.index_dir / "manifest.json").read_text())
        chunks = load_chunks(s.index_dir / "chunks.jsonl")
        if previous is not None:
            embedder, reranker, llm = (
                previous.retriever.embedder,
                previous.retriever.reranker,
                previous.llm,
            )
        else:
            embedder, reranker, llm = embedder or make_embedder(s), make_reranker(s), make_llm(s)
        if s.vector_store == "memory":
            store: VectorStore = InMemoryVectorStore.load(s.index_dir, chunks)
        else:
            store = make_store(s, manifest.get("collection", "chunks"))
        retriever = HybridRetriever(
            BM25Index(chunks),
            store,
            embedder,
            reranker,
            candidates=s.candidates_per_retriever,
            rrf_k=s.rrf_k,
        )
        version = manifest["corpus_version"]
        return cls(s, retriever, llm, make_cache(s, version), version, chunks)

    def answer(
        self, principal: Principal, question: str, mode: str | None = None, use_cache: bool = True
    ) -> Answer:
        mode = mode or self.settings.retrieval_mode
        timings: dict[str, float] = {}
        t0 = time.perf_counter()

        qvec = self.retriever.embedder.embed_query(question)
        if use_cache:
            cached = self.cache.get(principal, qvec)
            if cached is not None:
                cached.timings_ms = {"total": (time.perf_counter() - t0) * 1000}
                cached.usage = {}  # no tokens spent this time
                return cached

        t = time.perf_counter()
        results = self.retriever.retrieve(principal, question, self.settings.top_k, mode=mode)
        timings["retrieve"] = (time.perf_counter() - t) * 1000

        if not results:
            # Nothing visible to this caller: refuse without calling the model at all.
            ans = Answer(text=REFUSAL, citations=[], refused=True)
        else:
            results = fit_context(results, self.settings.max_context_chars)
            t = time.perf_counter()
            raw, usage = chat_with_usage(self.llm, build_messages(question, results))
            timings["generate"] = (time.perf_counter() - t) * 1000
            checked = check_citations(raw, results)
            ans = Answer(
                text=checked.text,
                citations=checked.citations,
                refused=checked.text.strip() == REFUSAL,
                uncited_sentences=checked.uncited_sentences,
                retrieved_chunk_ids=[r.chunk.chunk_id for r in results],
                faithfulness=faithfulness(checked.text, results),
                usage={
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                },
            )

        timings["total"] = (time.perf_counter() - t0) * 1000
        ans.timings_ms = timings
        if use_cache and not ans.refused:
            self.cache.put(principal, qvec, ans)
        return ans
