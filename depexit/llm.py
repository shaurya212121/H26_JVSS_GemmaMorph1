"""
LLM client for GemmaMorph: Gemma 4 only.

Backends:
  ollama  local Ollama, native /api/chat endpoint with "format": "json"
          (default model "gemma4:e2b")
  gemini  hosted Gemini API (default model "gemma-4-26b-a4b-it"),
          key from the GEMINI_API_KEY environment variable

Model and base URL come from CLI flags or environment variables
(GEMMA_BACKEND, GEMMA_MODEL, GEMMA_BASE_URL, OLLAMA_HOST). There is no
fallback between backends and no canned answers: an unreachable backend raises
LLMError with the real cause.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import requests
from depexit.log import log

BACKENDS = ("ollama", "gemini")
DEFAULT_MODELS = {"ollama": "gemma4:e2b", "gemini": "gemma-4-26b-a4b-it"}
DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434",
    "gemini": "https://generativelanguage.googleapis.com",
}
DEFAULT_TIMEOUT_SECONDS = 600


class LLMError(RuntimeError):
    """The model backend was unreachable or returned an unusable response."""


@dataclass(frozen=True)
class LLMConfig:
    backend: str
    model: str
    base_url: str
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    num_ctx: int = 16384  # Ollama context window; the default (4096) truncates long prompts

    @classmethod
    def from_options(
        cls,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        num_ctx: Optional[int] = None,
    ) -> "LLMConfig":
        """Resolve flags first, then environment variables, then backend defaults."""
        backend = (backend or os.getenv("GEMMA_BACKEND") or "ollama").strip().lower()
        if backend not in BACKENDS:
            raise ValueError(f"Unknown backend '{backend}'. Choose one of: {', '.join(BACKENDS)}")
        model = model or os.getenv("GEMMA_MODEL") or DEFAULT_MODELS[backend]
        env_url = os.getenv("GEMMA_BASE_URL") or (os.getenv("OLLAMA_HOST") if backend == "ollama" else None)
        base_url = (base_url or env_url or DEFAULT_BASE_URLS[backend]).rstrip("/")
        return cls(
            backend=backend,
            model=model,
            base_url=base_url,
            timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
            num_ctx=num_ctx or cls.num_ctx,
        )


class LLMClient:
    """Sends one prompt to the configured Gemma 4 backend and returns the raw text."""

    def __init__(self, config: LLMConfig):
        self.config = config

    def ask(self, prompt: str, system: str = "") -> str:
        cfg = self.config
        log(f"  LLM query ({cfg.backend}:{cfg.model}): {_preview(prompt)}")
        if cfg.backend == "ollama":
            result = self._ask_ollama(prompt, system)
        else:
            result = self._ask_gemini(prompt, system)
        log(f"  LLM response: {_preview(result)}")
        return result

    def _ask_ollama(self, prompt: str, system: str) -> str:
        cfg = self.config
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": cfg.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_ctx": cfg.num_ctx},
        }
        data = self._post(f"{cfg.base_url}/api/chat", payload, headers={})
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Unexpected Ollama response shape: {str(data)[:300]}") from exc

    def _ask_gemini(self, prompt: str, system: str) -> str:
        cfg = self.config
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (required for --backend gemini).")
        # Gemma models on the Gemini API do not accept a separate system instruction,
        # so the system text is placed at the start of the user turn.
        text = f"{system}\n\n{prompt}" if system else prompt
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"temperature": 0.1},
        }
        url = f"{cfg.base_url}/v1beta/models/{cfg.model}:generateContent"
        data = self._post(url, payload, headers={"x-goog-api-key": api_key})
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected Gemini response shape: {str(data)[:300]}") from exc

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"Cannot reach {self.config.backend} at {url}: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"{self.config.backend} returned HTTP {resp.status_code} from {url}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(f"{self.config.backend} returned non-JSON body: {resp.text[:300]}") from exc


def _preview(text: str, max_chars: int = 80) -> str:
    flat = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return flat[:max_chars] + "..." if len(flat) > max_chars else flat


def extract_json_object(raw: str) -> dict:
    """
    Return the outermost JSON object in model output (markdown fences and
    surrounding prose are ignored). Raises ValueError if there is none or it
    does not parse; a bare list or scalar is an error, not silently wrapped.
    """
    text = raw
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    candidate = text[start:end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = json.loads(re.sub(r",\s*([}\]])", lambda m: m.group(1), candidate))  # tolerate trailing commas
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
