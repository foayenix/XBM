"""Pluggable text-completion backend.

XBM asks a language model for two small things: a 2-4 sentence article
summary, and 1-4 topic tags per bookmark. Neither needs a frontier model,
so both can run against something on your own machine.

Two providers:

- "anthropic" uses the official Anthropic SDK and talks to the Claude API.
- "openai" speaks the OpenAI /chat/completions shape over plain HTTP, which
  is what essentially every local runner exposes - Ollama, LM Studio,
  llama.cpp's server, vLLM, LocalAI, Jan, text-generation-webui. Nothing
  here is specific to OpenAI the company; it is just the wire format the
  local ecosystem settled on.

Note that this deliberately does NOT go through backend/fetching.py's
guard. That guard exists because article URLs come from other people's
tweets and must never reach a private address. Your model server is
somewhere you chose, and is normally on 127.0.0.1 - exactly what the guard
is built to refuse. The two cases are opposites and must not share a code
path.
"""
import logging
from typing import Protocol

import httpx

from backend import config

logger = logging.getLogger("xbm.llm")


class LLMError(Exception):
    """Raised when the model backend is unusable or returns nothing useful."""


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, prompt: str, *, max_tokens: int) -> str:
        """Return the model's text reply to a single user prompt."""


class AnthropicProvider:
    """The Claude API, via the official SDK."""

    name = "anthropic"

    def __init__(self, model: str, api_key: str, timeout: float):
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Fill it in in .env, or point "
                "LLM_BASE_URL at a local model server to run without it."
            )
        from anthropic import Anthropic

        self.model = model
        self._client = Anthropic(api_key=api_key, timeout=timeout)

    def complete(self, prompt: str, *, max_tokens: int) -> str:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API returned {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Claude API: {e}") from e

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined to answer this prompt.")

        # content is a list of typed blocks (text, thinking, tool_use, ...),
        # so pick the text ones out rather than assuming content[0] is text.
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError(f"Claude returned no text (stop_reason={response.stop_reason}).")
        return text


class OpenAICompatibleProvider:
    """Any server exposing POST {base}/chat/completions.

    Covers the local runners listed in the module docstring. The API key is
    optional: most local servers ignore it, a few want any non-empty value.
    """

    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float):
        if not base_url:
            raise LLMError(
                "LLM_BASE_URL is not set. Point it at your model server, "
                "e.g. http://localhost:11434/v1 for Ollama."
            )
        if not model:
            raise LLMError(
                "LLM_MODEL is not set. Name the model your server should "
                "load, e.g. llama3.1:8b or qwen2.5:7b-instruct."
            )
        self.model = model
        self.endpoint = self._resolve_endpoint(base_url)
        self._api_key = api_key
        self._timeout = timeout

    @staticmethod
    def _resolve_endpoint(base_url: str) -> str:
        """Accept the base URL in whichever shape the user pasted it."""
        url = base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"

    def complete(self, prompt: str, *, max_tokens: int) -> str:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            # Summarising and tagging want the boring answer, and small local
            # models wander badly at default temperature. (The Anthropic path
            # sends no temperature at all: newer Claude models reject sampling
            # parameters outright.)
            "temperature": 0.2,
            "stream": False,
        }

        try:
            response = httpx.post(
                self.endpoint, json=payload, headers=headers, timeout=self._timeout
            )
        except httpx.ConnectError as e:
            raise LLMError(
                f"Could not reach a model server at {self.endpoint}. "
                "Is it running, and is LLM_BASE_URL right? "
                f"({e})"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"Model server at {self.endpoint} did not answer within "
                f"{self._timeout:.0f}s. Local models can be slow to load - raise "
                "LLM_TIMEOUT_SECONDS, or use a smaller model."
            ) from e

        if response.status_code == 404:
            raise LLMError(
                f"Model server returned 404 for {self.endpoint}. Check LLM_BASE_URL "
                "points at the OpenAI-compatible root (usually ending in /v1)."
            )
        if response.status_code >= 400:
            raise LLMError(
                f"Model server returned {response.status_code}: {response.text[:300]}"
            )

        try:
            data = response.json()
        except ValueError as e:
            raise LLMError(f"Model server sent a non-JSON reply: {response.text[:200]}") from e

        if isinstance(data.get("error"), dict):
            raise LLMError(f"Model server reported an error: {data['error'].get('message', data['error'])}")

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(
                f"Model server sent an unexpected response shape: {str(data)[:300]}"
            ) from e

        text = (text or "").strip()
        if not text:
            raise LLMError(f"Model {self.model} returned an empty reply.")
        return text


def get_provider() -> LLMProvider:
    """Build the configured provider. Raises LLMError if it is unusable."""
    provider = config.LLM_PROVIDER
    if provider == "openai":
        return OpenAICompatibleProvider(
            base_url=config.LLM_BASE_URL,
            model=config.LLM_MODEL,
            api_key=config.LLM_API_KEY,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
    if provider == "anthropic":
        return AnthropicProvider(
            model=config.LLM_MODEL or config.ANTHROPIC_MODEL,
            api_key=config.ANTHROPIC_API_KEY,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
    raise LLMError(
        f"Unknown LLM_PROVIDER {provider!r}. Use 'anthropic' or 'openai'."
    )


def describe() -> dict:
    """What /api/health reports, without constructing a client."""
    provider = config.LLM_PROVIDER
    if provider == "openai":
        return {
            "provider": "openai",
            "model": config.LLM_MODEL or None,
            "base_url": config.LLM_BASE_URL or None,
            "configured": bool(config.LLM_BASE_URL and config.LLM_MODEL),
        }
    return {
        "provider": provider,
        "model": config.LLM_MODEL or config.ANTHROPIC_MODEL,
        "base_url": None,
        "configured": bool(config.ANTHROPIC_API_KEY),
    }
