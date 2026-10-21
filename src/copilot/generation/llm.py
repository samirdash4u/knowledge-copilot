"""LLM backends. ``OllamaLLM`` talks to a local Ollama server; ``EchoLLM`` is a deterministic
extractive stand-in used by tests and CI."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False  # counted from text length, not reported by the model server


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for English)."""
    return max(1, len(text) // 4) if text else 0


def chat_with_usage(llm: LLM, messages: list[dict]) -> tuple[str, Usage]:
    """Call the model and return its reply with token counts, estimating them if needed."""
    if hasattr(llm, "chat_with_usage"):
        return llm.chat_with_usage(messages)
    text = llm.chat(messages)
    prompt = sum(estimate_tokens(m.get("content", "")) for m in messages)
    return text, Usage(prompt, estimate_tokens(text), estimated=True)


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
        return self.chat_with_usage(messages)[0]

    def chat_with_usage(self, messages: list[dict]) -> tuple[str, Usage]:
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
        data = resp.json()
        text = data["message"]["content"].strip()
        if "prompt_eval_count" in data or "eval_count" in data:
            # Ollama leaves out prompt_eval_count when the whole prompt came from its KV cache.
            return text, Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0))
        prompt = sum(estimate_tokens(m.get("content", "")) for m in messages)
        return text, Usage(prompt, estimate_tokens(text), estimated=True)


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
