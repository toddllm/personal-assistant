"""LLM provider abstraction and implementations.

Provides a pluggable interface for language model chat completions.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """Abstract base class for LLM chat completion providers."""

    @abstractmethod
    async def chat(self, messages: list[dict], max_tokens: int = 80, temperature: float = 0.5) -> str | None:
        """Generate a chat completion.

        Args:
            messages: OpenAI-format message list [{role, content}, ...].
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.

        Returns:
            The assistant's response text, or None on failure.
        """


class OpenAICompatibleLLM(LLMProvider):
    """LLM provider for any OpenAI-compatible chat completions API.

    Works with Groq, Ollama (/v1/chat/completions), vLLM, llama.cpp server, etc.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat(self, messages: list[dict], max_tokens: int = 80, temperature: float = 0.5) -> str | None:
        client = await self._ensure_client()

        # Build the completions URL
        # Groq: https://api.groq.com/openai/v1/chat/completions
        # Ollama: http://host:11434/v1/chat/completions
        url = f"{self._base_url}/v1/chat/completions"

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            resp = await client.post(
                url,
                headers=headers,
                json={
                    "model": self._model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("LLM API error (%s, model=%s)", self._base_url, self._model)
            return None
