.PHONY: install test lint up pull-model ingest eval eval-offline

install:
	uv venv && uv pip install -e ".[ml,dev]"

test:
	pytest -q

lint:
	ruff check src tests && ruff format --check src tests

up:
	cp -n .env.example .env || true
	docker compose up -d --build

pull-model:
	docker compose exec ollama ollama pull $${COPILOT_OLLAMA_MODEL:-qwen2.5:7b-instruct-q4_K_M}

ingest:
	docker compose run --rm api copilot ingest

eval:
	docker compose run --rm -v ./reports:/app/reports api copilot eval

eval-offline:
	COPILOT_EMBEDDER=hash COPILOT_RERANKER=none COPILOT_VECTOR_STORE=memory COPILOT_CACHE=memory \
	COPILOT_LLM=echo copilot ingest && \
	COPILOT_EMBEDDER=hash COPILOT_RERANKER=none COPILOT_VECTOR_STORE=memory COPILOT_CACHE=memory \
	COPILOT_LLM=echo copilot eval --modes bm25,vector,hybrid
