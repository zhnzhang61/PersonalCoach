"""
LLM Provider — Centralized model management for PersonalCoach.

Responsibilities:
  1. Model registry & selection (Gemini, oMLX local, extensible)
  2. API key management (reads from .env)
  3. Automatic fallback: Gemini → oMLX local when Gemini fails
  4. Unified invoke interface with model attribution tag
  5. Role-based model presets (creative, precise, structured)

Usage:
    from llm_provider import get_llm, invoke_llm

    # Get a LangChain LLM instance
    llm = get_llm("creative")           # warm, creative responses
    llm = get_llm("precise")            # deterministic routing/classification
    llm = get_llm("structured")         # JSON output, low temperature

    # Or use the high-level invoke with automatic fallback + attribution
    response_text = invoke_llm(messages, role="creative")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load .env from project root (handles worktree symlinks)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=True)

# ---------------------------------------------------------------------------
# Model Definitions
# ---------------------------------------------------------------------------

RoleType = Literal["creative", "precise", "structured"]

# Provider configs: each entry defines how to construct a LangChain LLM
_PROVIDERS = {
    "gemini": {
        "class": "langchain_google_genai.ChatGoogleGenerativeAI",
        "params": {
            "model": "gemini-2.5-flash",
            "api_key_env": "GEMINI_KEY",  # env var name to read
        },
    },
    "omlx": {
        "class": "langchain_openai.ChatOpenAI",
        "params": {
            "model": "Qwen3.5-35B-A3B-8bit",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "omlx-local",  # placeholder, not a real key
        },
    },
}

# Role → temperature mapping
_ROLE_TEMPS: dict[RoleType, float] = {
    "creative": 0.4,
    "precise": 0.0,
    "structured": 0.1,
}

# Fallback order: try providers in this sequence
_FALLBACK_ORDER = ["gemini", "omlx"]


# ---------------------------------------------------------------------------
# Internal: construct LLM instances
# ---------------------------------------------------------------------------
_llm_cache: dict[str, object] = {}


def _build_llm(provider: str, temperature: float):
    """Construct a LangChain chat model for the given provider."""
    config = _PROVIDERS.get(provider)
    if not config:
        raise ValueError(f"Unknown LLM provider: {provider}")

    class_path = config["class"]
    params = dict(config["params"])

    # Resolve API key from environment if specified
    api_key_env = params.pop("api_key_env", None)
    if api_key_env:
        key = os.getenv(api_key_env)
        if not key:
            raise ValueError(f"API key not found: env var {api_key_env} is not set")
        params["api_key"] = key

    # For Google models, the param name is google_api_key or api_key depending on version
    # langchain_google_genai accepts api_key directly
    params["temperature"] = temperature

    # Dynamic import
    module_path, class_name = class_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    return cls(**params)


def _get_provider_name(provider: str) -> str:
    """Human-readable model name for attribution."""
    config = _PROVIDERS.get(provider, {})
    params = config.get("params", {})
    return params.get("model", provider)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_llm(role: RoleType = "creative", provider: str | None = None):
    """
    Get a LangChain chat model instance.

    Args:
        role: Determines temperature. "creative" (0.4), "precise" (0.0), "structured" (0.1)
        provider: Force a specific provider. If None, uses first available in fallback order.

    Returns:
        A LangChain BaseChatModel instance.
    """
    temperature = _ROLE_TEMPS.get(role, 0.4)

    if provider:
        cache_key = f"{provider}:{temperature}"
        if cache_key not in _llm_cache:
            _llm_cache[cache_key] = _build_llm(provider, temperature)
        return _llm_cache[cache_key]

    # Try fallback order
    for p in _FALLBACK_ORDER:
        cache_key = f"{p}:{temperature}"
        if cache_key in _llm_cache:
            return _llm_cache[cache_key]
        try:
            llm = _build_llm(p, temperature)
            _llm_cache[cache_key] = llm
            return llm
        except ValueError:
            continue

    raise RuntimeError(
        "No LLM provider available. Set GEMINI_KEY in .env or start oMLX locally."
    )


def get_active_provider(role: RoleType = "creative") -> str:
    """Return the name of the provider that would be used for this role."""
    temperature = _ROLE_TEMPS.get(role, 0.4)
    for p in _FALLBACK_ORDER:
        cache_key = f"{p}:{temperature}"
        if cache_key in _llm_cache:
            return p
        try:
            _build_llm(p, temperature)
            return p
        except ValueError:
            continue
    return "none"


def invoke_llm(
    messages: list,
    role: RoleType = "creative",
    tag_response: bool = True,
) -> str:
    """
    High-level invoke with automatic fallback and model attribution.

    Args:
        messages: List of LangChain BaseMessage objects.
        role: Temperature preset.
        tag_response: If True, appends [Generated by xxx model] to the response.

    Returns:
        Response text string.
    """
    last_error = None
    temperature = _ROLE_TEMPS.get(role, 0.4)

    for provider in _FALLBACK_ORDER:
        try:
            llm = _build_llm(provider, temperature)
            response = llm.invoke(messages)

            # Extract text content
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and "text" in block
                )
            content = str(content).strip()

            # Attribution tag
            if tag_response:
                model_name = _get_provider_name(provider)
                content += f"\n\n[Generated by {model_name}]"

            return content

        except Exception as e:
            last_error = e
            print(f"[LLM Provider] {provider} failed: {e}, trying next...")
            continue

    raise RuntimeError(
        f"All LLM providers failed. Last error: {last_error}"
    )


def find_api_key() -> str | None:
    """Legacy helper — returns the Gemini API key if available."""
    return os.getenv("GEMINI_KEY") or os.getenv("GEMINI_API_KEY")
