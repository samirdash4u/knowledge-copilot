"""LLM backends. ``OllamaLLM`` talks to a local Ollama server; ``EchoLLM`` is a deterministic
extractive stand-in used by tests and CI."""

from __future__ import annotations

import re
from typing import Protocol

import httpx

from copilot.generation.prompt import REFUSAL


class LLM(Protocol):
    def chat(self, messages: list[dict]) -> str: ...


class LLMUnavailable(RuntimeError):
    """The model could not produce an answer. The message is safe to show to the user."""

    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


class OllamaLLM:
    def __init__(
        self,
        url: str,
        model: str,
        temperature: float = 0.1,
        timeout: float = 300.0,
        num_ctx: int = 4096,
        max_tokens: int = 512,
        keep_alive: str = "30m",
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.max_tokens = max_tokens
        self.keep_alive = keep_alive
        # Fail fast when Ollama isn't running; wait patiently while it generates.
        self.client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def chat(self, messages: list[dict]) -> str:
        try:
            resp = self.client.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "options": {
                        "temperature": self.temperature,
                        "num_ctx": self.num_ctx,
                        "num_predict": self.max_tokens,
                    },
                },
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise LLMUnavailable(
                f"The language model is not reachable at {self.url}. Check that Ollama is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMUnavailable(
                f"The language model took longer than {self.timeout:.0f} seconds to answer. "
                "It may still be loading, or running on the CPU. Try again in a minute, or raise "
                "COPILOT_LLM_TIMEOUT_S.",
                status=504,
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200]
            raise LLMUnavailable(
                f"The language model returned an error ({exc.response.status_code}): {detail}. "
                f"Check that the model '{self.model}' is pulled (ollama list)."
            ) from exc
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
