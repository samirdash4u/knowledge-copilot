"""LLM backends. ``OllamaLLM`` talks to a local Ollama server; ``EchoLLM`` is a deterministic
extractive stand-in used by tests and CI."""

from __future__ import annotations

import re
from typing import Protocol

import httpx

from copilot.generation.prompt import REFUSAL


class LLM(Protocol):
    def chat(self, messages: list[dict]) -> str: ...


class OllamaLLM:
    def __init__(self, url: str, model: str, temperature: float = 0.1, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.client = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict]) -> str:
        resp = self.client.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": self.temperature, "num_ctx": 8192},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()


_SOURCE = re.compile(r"^\[(\d+)\] .*?\n(.*?)(?=\n\n\[\d+\] |\n\nQuestion: )", re.S | re.M)


class EchoLLM:
    """Answers with the first sentence of the top sources, each cited. No model required."""

    def __init__(self, max_sources: int = 2) -> None:
        self.max_sources = max_sources

    def chat(self, messages: list[dict]) -> str:
        user = messages[-1]["content"]
        sources = _SOURCE.findall(user)
        if not sources:
            return REFUSAL
        parts = []
        for num, text in sources[: self.max_sources]:
            first = re.split(r"(?<=[.!?])\s+", text.strip())[0].rstrip(".")
            parts.append(f"{first} [{num}].")
        return " ".join(parts)
