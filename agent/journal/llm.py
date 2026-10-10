"""Plain-language explanations (spec section 10): code computes every number, the LLM only explains them.

Provider from .env LLM_PROVIDER: 'anthropic' (Claude API, chosen 2026-09-29: claude-sonnet-5-5), 'ollama' (a
local model), or 'template' (no LLM). Any failure raises LLMUnavailable; callers keep a template text and retry
later. The API key is never logged or stored anywhere but .env.
"""
from __future__ import annotations

from typing import Any

import requests

from ..settings import get_secrets

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_S = 90

SYSTEM = ("You write short, plain-English notes for a swing-trading journal (Indian stocks, amounts in ₹). "
          "Use only the numbers in the data you are given; never invent a number, a price or a reason. "
          "No bullet points unless asked, no markdown headings, no investment advice beyond the one suggestion "
          "you may be asked for.")


class LLMUnavailable(RuntimeError):
    pass


def provider() -> str:
    p = (get_secrets().llm_provider or "template").strip().lower()
    return p if p in ("anthropic", "ollama", "template") else "template"


def status() -> dict[str, Any]:
    s = get_secrets()
    p = provider()
    ready = {"anthropic": bool(s.anthropic_api_key), "ollama": bool(s.ollama_model), "template": True}[p]
    model = {"anthropic": s.anthropic_model, "ollama": s.ollama_model, "template": None}[p]
    return {"provider": p, "model": model, "configured": ready}


def complete(prompt: str, max_tokens: int = 500, system: str = SYSTEM) -> tuple[str, str]:
    """Returns (text, model). Raises LLMUnavailable."""
    s = get_secrets()
    p = provider()
    try:
        if p == "anthropic":
            if not s.anthropic_api_key:
                raise LLMUnavailable("ANTHROPIC_API_KEY is not set in .env")
            r = requests.post(ANTHROPIC_URL, timeout=TIMEOUT_S, json={
                "model": s.anthropic_model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": prompt}]},
                headers={"x-api-key": s.anthropic_api_key, "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"})
            if r.status_code != 200:
                raise LLMUnavailable(f"Claude API {r.status_code}: {_error_text(r)}")
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
            return text, data.get("model", s.anthropic_model)
        if p == "ollama":
            if not s.ollama_model:
                raise LLMUnavailable("OLLAMA_MODEL is not set in .env")
            r = requests.post(s.ollama_url.rstrip("/") + "/api/generate", timeout=TIMEOUT_S * 2, json={
                "model": s.ollama_model, "system": system, "prompt": prompt, "stream": False,
                "options": {"num_predict": max_tokens}})
            if r.status_code != 200:
                raise LLMUnavailable(f"Ollama {r.status_code}: {r.text[:200]}")
            return r.json().get("response", "").strip(), s.ollama_model
    except requests.RequestException as exc:
        raise LLMUnavailable(f"{p} not reachable: {type(exc).__name__}") from exc
    raise LLMUnavailable("No LLM configured (template only)")


def _error_text(r: requests.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", ""))[:200]
    except ValueError:
        return r.text[:200]
