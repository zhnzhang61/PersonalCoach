"""
LLM Provider — The ONLY module in the project allowed to call LLM APIs.

Public API (three functions, that's it):
    call_llm(messages, role="creative", provider=None, fallback_chain=None)
        -> tuple[AIMessage, str]
    call_embedding(texts, provider="gemini")
        -> list[list[float]]
    cosine_similarity(a, b)
        -> float

Everything else in the project goes through these. No direct `llm.invoke()`,
no direct `ChatGoogleGenerativeAI()`, no direct `GoogleGenerativeAIEmbeddings()`,
no direct `genai.Client`.

Chat providers (default fallback: Gemini → Groq → oMLX):
    gemini — Google Gemini 2.5 Flash (20 free req/day)
    groq   — Llama 3.3 70B on Groq cloud (free tier, very fast)
    omlx   — Local Qwen via oMLX at http://127.0.0.1:8000/v1 (last resort)

Embedding providers (NO FALLBACK — embeddings from different models
are NOT interchangeable; stored vectors would become incompatible):
    gemini — text-embedding-004 via GEMINI_KEY (3072-dim by default, we clip to 768)

Multimodal note: the Gemini API key path supports text-only embeddings.
True multimodal (image+text) embeddings need Vertex AI (multimodalembedding@001)
which requires a GCP service account, not just an API key.

Example:
    from llm_provider import call_llm, call_embedding, cosine_similarity
    msg, provider = call_llm([HumanMessage(content="hi")], role="creative")
    vecs = call_embedding(["下雨天跑步", "rainy day running"])
    print(cosine_similarity(vecs[0], vecs[1]))  # should be ~0.9+
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage

# Load .env from project root (handles worktree symlinks)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=True)

# ---------------------------------------------------------------------------
# Model Definitions
# ---------------------------------------------------------------------------

RoleType = Literal["creative", "precise", "structured"]

# Provider configs: each entry defines how to construct a LangChain LLM.
# To add a new OpenAI-compatible provider, just drop another entry here.
_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "class": "langchain_google_genai.ChatGoogleGenerativeAI",
        "params": {
            "model": "gemini-2.5-flash",
            "api_key_env": "GEMINI_KEY",
        },
    },
    "groq": {
        "class": "langchain_openai.ChatOpenAI",
        "params": {
            "model": "llama-3.3-70b-versatile",
            "base_url": "https://api.groq.com/openai/v1",
            "api_key_env": "GROQ_API_KEY",
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

# Default fallback chain: try providers in this sequence
DEFAULT_FALLBACK_ORDER: list[str] = ["gemini", "groq", "omlx"]


# ---------------------------------------------------------------------------
# Private helpers — do NOT import these from outside this module.
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
            raise ValueError(
                f"API key not found: env var {api_key_env} is not set"
            )
        params["api_key"] = key

    params["temperature"] = temperature

    # Dynamic import so unused providers don't force dependencies
    module_path, class_name = class_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    return cls(**params)


def _get_llm(role: RoleType, provider: str):
    """PRIVATE — return a cached LangChain model for (provider, role)."""
    temperature = _ROLE_TEMPS.get(role, 0.4)
    cache_key = f"{provider}:{temperature}"
    if cache_key not in _llm_cache:
        _llm_cache[cache_key] = _build_llm(provider, temperature)
    return _llm_cache[cache_key]


def _provider_model_name(provider: str) -> str:
    """Return the human-readable model name for attribution (e.g., 'gemini-2.5-flash')."""
    config = _PROVIDERS.get(provider, {})
    return config.get("params", {}).get("model", provider)


def _coerce_to_aimessage(response, provider: str) -> AIMessage:
    """
    Normalize whatever a LangChain chat model returned into an AIMessage.

    Some integrations already return an AIMessage; others return a BaseMessage
    subclass with `.content`. We unify on AIMessage so LangGraph nodes can
    append it directly to state.
    """
    if isinstance(response, AIMessage):
        return response

    content = getattr(response, "content", response)
    # LangChain multi-modal content can be a list of dicts; flatten to text.
    if isinstance(content, list):
        content = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and "text" in block
        )
    return AIMessage(
        content=str(content),
        response_metadata={"provider": provider},
    )


# ---------------------------------------------------------------------------
# Public API — the ONLY function allowed to call an LLM
# ---------------------------------------------------------------------------


def call_llm(
    messages: list[BaseMessage],
    role: RoleType = "creative",
    provider: str | None = None,
    fallback_chain: list[str] | None = None,
) -> tuple[AIMessage, str]:
    """
    The single entry point for all LLM calls in the project.

    Args:
        messages: list of LangChain BaseMessage objects (SystemMessage, HumanMessage, ...)
        role: temperature preset — "creative" (0.4), "precise" (0.0), "structured" (0.1)
        provider: if set, call ONLY this provider and do NOT fall back on failure.
                  If None, walk `fallback_chain`.
        fallback_chain: override the default provider sequence. Defaults to
                        DEFAULT_FALLBACK_ORDER ("gemini" → "groq" → "omlx").

    Returns:
        (AIMessage, provider_name_that_produced_it)

    Raises:
        RuntimeError: if provider is pinned and fails, or every provider in the
                      fallback chain fails.
    """
    if provider is not None:
        # Pinned: one shot, no fallback
        llm = _get_llm(role, provider)
        response = llm.invoke(messages)
        return _coerce_to_aimessage(response, provider), provider

    chain = fallback_chain if fallback_chain is not None else DEFAULT_FALLBACK_ORDER
    last_error: Exception | None = None

    for p in chain:
        try:
            llm = _get_llm(role, p)
            response = llm.invoke(messages)
            return _coerce_to_aimessage(response, p), p
        except Exception as e:  # noqa: BLE001
            last_error = e
            # Drop cache entry for this provider so a later transient success can refresh it
            temperature = _ROLE_TEMPS.get(role, 0.4)
            _llm_cache.pop(f"{p}:{temperature}", None)
            print(f"[LLM Provider] {p} failed: {e}. Trying next in chain...")
            continue

    raise RuntimeError(
        f"All LLM providers in chain {chain} failed. Last error: {last_error}"
    )


def get_provider_model_name(provider: str) -> str:
    """
    Helper for attribution tags — e.g. `get_provider_model_name("gemini")`
    returns `"gemini-2.5-flash"`.
    """
    return _provider_model_name(provider)


# ---------------------------------------------------------------------------
# Embedding API
# ---------------------------------------------------------------------------

_EMBEDDING_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "class": "langchain_google_genai.GoogleGenerativeAIEmbeddings",
        "params": {
            # Google Gemini embedding, 3072-dim. text-embedding-004 / embedding-001
            # return 404 NOT_FOUND on the v1beta API as of April 2026.
            "model": "models/gemini-embedding-001",
            "api_key_env": "GEMINI_KEY",
        },
    },
}

_embedding_cache: dict[str, object] = {}


def _build_embedder(provider: str):
    config = _EMBEDDING_PROVIDERS.get(provider)
    if not config:
        raise ValueError(f"Unknown embedding provider: {provider}")

    class_path = config["class"]
    params = dict(config["params"])

    api_key_env = params.pop("api_key_env", None)
    if api_key_env:
        key = os.getenv(api_key_env)
        if not key:
            raise RuntimeError(
                f"Embedding API key not found: env var {api_key_env} is not set"
            )
        # langchain_google_genai uses `google_api_key`, not `api_key`
        params["google_api_key"] = key

    module_path, class_name = class_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(**params)


def call_embedding(
    texts: list[str],
    provider: str = "gemini",
) -> list[list[float]]:
    """
    Embed text(s) for semantic similarity search.

    Args:
        texts: list of strings to embed. `embed_documents` is used so this is
               suitable for topic/episode content (not query-optimized).
        provider: embedding provider. Only "gemini" is registered today.
                  NOTE: no fallback chain — vectors from different models live
                  in different spaces and cannot be mixed with stored data.

    Returns:
        list of embedding vectors (each a list of floats).

    Raises:
        RuntimeError if the provider is unavailable or the API call fails.
    """
    if provider not in _EMBEDDING_PROVIDERS:
        raise ValueError(
            f"Embedding provider '{provider}' not registered. Available: {list(_EMBEDDING_PROVIDERS)}"
        )
    if not texts:
        return []

    if provider not in _embedding_cache:
        _embedding_cache[provider] = _build_embedder(provider)
    embedder = _embedding_cache[provider]

    try:
        return embedder.embed_documents(texts)  # type: ignore[attr-defined]
    except Exception as e:
        # Drop the cached client so a transient-fail provider can recover next call
        _embedding_cache.pop(provider, None)
        raise RuntimeError(f"Embedding call failed on provider {provider}: {e}") from e


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity in [-1, 1]; returns 0.0 if either vector is zero.

    Kept here (not in a math util) so callers touching embeddings have a
    single import path: `from llm_provider import call_embedding, cosine_similarity`.
    """
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
