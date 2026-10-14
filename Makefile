# Thin wrappers around scripts/manage.py for Linux/macOS users who have make.
# On Windows, run `python scripts/manage.py <command>` directly.
PY ?= python3

.PHONY: install test lint setup up up-gpu down pull-model ingest eval status logs eval-offline

install:
	uv venv && uv pip install -e ".[ml,dev]"

test:
	pytest -q

lint:
	ruff check src tests && ruff format --check src tests

setup up down pull-model ingest eval status logs:
	$(PY) scripts/manage.py $@

up-gpu:
	$(PY) scripts/manage.py up --gpu

eval-offline:
	COPILOT_EMBEDDER=hash COPILOT_RERANKER=none COPILOT_VECTOR_STORE=memory COPILOT_CACHE=memory \
	COPILOT_LLM=echo copilot ingest && \
	COPILOT_EMBEDDER=hash COPILOT_RERANKER=none COPILOT_VECTOR_STORE=memory COPILOT_CACHE=memory \
	COPILOT_LLM=echo copilot eval --modes bm25,vector,hybrid
