"""LLM clients: one OpenAI-compatible client (Gemini, Groq, OpenAI, OpenRouter, Azure-compatible, HF router ...) and one
Anthropic client. Plain `requests`, no vendor SDKs.

Built for free tiers: calls are paced (`min_interval`), 429/5xx are retried with backoff (honouring Retry-After), and a provider
that rejects `response_format: json_object` is retried once without it. Errors never contain the API key.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import requests

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """A call could not be completed (auth, exhausted quota, unusable response). Safe to show: never includes the key."""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal  # True: do not keep trying other batches (bad key, model not found)


@dataclass
class LlmResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float = 0.0


class LlmClient(Protocol):
    name: str
    model: str

    def complete(self, system: str, user: str, max_tokens: int = 3000) -> LlmResponse: ...


PRESETS: dict[str, dict] = {
    # model ids change often: override with LLM_MODEL (and check them with `python -m app.llm.check`)
    "gemini": {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.1-flash-lite"},
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b"},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1", "model": "claude-haiku-5-5"},
    "openai_compat": {"kind": "openai", "base_url": "", "model": ""},  # needs LLM_BASE_URL and LLM_MODEL
}


class _Http:
    """Pacing + retry shared by both clients."""

    def __init__(
        self,
        timeout: float = 90,
        min_interval: float = 0.0,
        max_retries: int = 3,
        post: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout, self._min_interval, self._max_retries = timeout, min_interval, max_retries
        self._post = post or requests.post
        self._sleep, self._clock = sleep, clock
        self._last_call = -1e9

    def send(self, url: str, headers: dict, payload: dict) -> requests.Response:
        """POST with pacing; retries 429/5xx/timeouts. Returns the last response (callers inspect 4xx)."""
        response = None
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()
            try:
                response = self._post(url, headers=headers, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt == self._max_retries:
                    raise LlmError(f"network error: {type(exc).__name__}") from exc
                self._sleep(min(30, 2 ** (attempt + 1)))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self._max_retries:
                    return response
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2 ** (attempt + 2)
                self._sleep(min(60, delay))
                continue
            return response
        return response  # pragma: no cover


def _snippet(response: requests.Response) -> str:
    return " ".join((response.text or "")[:200].split())


def _check_status(response: requests.Response, provider: str) -> None:
    code = response.status_code
    if code in (401, 403):
        raise LlmError(f"{provider}: authentication failed ({code}); check the API key and that it may use this model", fatal=True)
    if code == 404:
        raise LlmError(f"{provider}: model or endpoint not found (404); check LLM_MODEL / LLM_BASE_URL: {_snippet(response)}", fatal=True)
    if code == 429:
        raise LlmError(f"{provider}: rate limited / quota exhausted (429) after retries: {_snippet(response)}")
    if code >= 400:
        raise LlmError(f"{provider}: HTTP {code}: {_snippet(response)}", fatal=code == 400)


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, label: str = "openai_compat", json_mode: bool = True, **http) -> None:
        self.name, self.model = label, model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._json_mode = json_mode
        self._http = _Http(**http)

    def complete(self, system: str, user: str, max_tokens: int = 3000) -> LlmResponse:
        payload = {
            "model": self.model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        started = time.monotonic()
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = self._http.send(self._url, self._headers, payload)
        if response.status_code == 400 and self._json_mode:  # provider/model without JSON mode: ask in the prompt only
            self._json_mode = False
            payload.pop("response_format", None)
            response = self._http.send(self._url, self._headers, payload)
        _check_status(response, self.name)
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"{self.name}: unexpected response shape: {_snippet(response)}") from exc
        usage = data.get("usage") or {}
        return LlmResponse(text, data.get("model") or self.model, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                           round(time.monotonic() - started, 2))


class AnthropicClient:
    VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com/v1", **http) -> None:
        self.name, self.model = "anthropic", model
        self._url = base_url.rstrip("/") + "/messages"
        self._headers = {"x-api-key": api_key, "anthropic-version": self.VERSION, "content-type": "application/json"}
        self._http = _Http(**http)

    def complete(self, system: str, user: str, max_tokens: int = 3000) -> LlmResponse:
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                   "messages": [{"role": "user", "content": user}]}
        started = time.monotonic()
        response = self._http.send(self._url, self._headers, payload)
        _check_status(response, self.name)
        try:
            data = response.json()
            text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
        except (ValueError, KeyError, TypeError) as exc:
            raise LlmError(f"anthropic: unexpected response shape: {_snippet(response)}") from exc
        usage = data.get("usage") or {}
        return LlmResponse(text, data.get("model") or self.model, usage.get("input_tokens"), usage.get("output_tokens"),
                           round(time.monotonic() - started, 2))


def build_llm_client(settings) -> tuple[LlmClient | None, str]:
    """Returns (client, note). client is None when no key is configured; note says why / what was built."""
    provider = (settings.llm_provider or "gemini").strip().lower()
    preset = PRESETS.get(provider)
    if preset is None:
        return None, f"unknown LLM_PROVIDER {provider!r} (use one of {sorted(PRESETS)})"
    key = settings.llm_api_key or getattr(settings, f"{provider}_api_key", "") or ""
    if not key:
        return None, f"no API key for provider {provider!r} (set LLM_API_KEY or {provider.upper()}_API_KEY)"
    model = settings.llm_model or preset["model"]
    base_url = settings.llm_base_url or preset["base_url"]
    if not model or (preset["kind"] == "openai" and not base_url):
        return None, "LLM_PROVIDER=openai_compat needs LLM_BASE_URL and LLM_MODEL"
    http = {"timeout": settings.llm_timeout_seconds, "min_interval": settings.llm_min_interval_seconds}
    if preset["kind"] == "anthropic":
        return AnthropicClient(key, model, base_url, **http), f"anthropic:{model}"
    return OpenAICompatClient(base_url, key, model, label=provider, **http), f"{provider}:{model}"
