FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf

WORKDIR /app

# CPU-only torch keeps the image small; the LLM itself runs on the GPU inside the Ollama container.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[ml]"

COPY data ./data
COPY eval ./eval

EXPOSE 8000
CMD ["copilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
