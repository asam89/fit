"""Provider factory — resolves the right provider + key + model per user."""

import logging
from typing import Callable

from cryptography.fernet import Fernet

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.inference.base import LLMProvider, InferenceError
from fitnessbot.inference.anthropic_provider import AnthropicProvider
from fitnessbot.inference.openai_provider import OpenAIProvider
from fitnessbot.inference.gemini_provider import GeminiProvider

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, LLMProvider] = {
    "anthropic": AnthropicProvider(),
    "openai": OpenAIProvider(),
    "google": GeminiProvider(),
}

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
}


def _get_fernet() -> Fernet:
    key = Config.ENCRYPTION_KEY
    if not key:
        raise InferenceError("ENCRYPTION_KEY not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _decrypt_key(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


def _encrypt_key(raw_key: str) -> str:
    return _get_fernet().encrypt(raw_key.encode()).decode()


def _mask_key(key: str) -> str:
    if len(key) < 8:
        return "****"
    return key[:4] + "..." + key[-4:]


def get_user_credential(user_id: int, provider: str | None = None) -> dict | None:
    """Get the active LLM credential for a user. If provider is None, uses user's active_provider."""
    user = db.get_user_by_id(user_id)
    if not user:
        return None
    if provider is None:
        provider = user.get("active_provider", "anthropic") or "anthropic"
    cred = db.get_llm_credential(user_id, provider)
    if not cred:
        if provider == "anthropic" and Config.ANTHROPIC_API_KEY:
            return {
                "provider": "anthropic",
                "key": Config.ANTHROPIC_API_KEY,
                "model": user.get("active_model") or Config.ANALYSIS_MODEL,
                "is_system": True,
            }
        return None
    return {
        "provider": cred["provider"],
        "key": _decrypt_key(cred["encrypted_key"]),
        "model": cred.get("model") or DEFAULT_MODELS.get(provider, ""),
        "is_system": False,
    }


def get_inference(user_id: int) -> Callable:
    """Return a callable that runs inference for the given user.

    Usage: result = get_inference(user_id)(system="...", messages=[...], max_tokens=1024)
    Returns: {"text": str, "input_tokens": int, "output_tokens": int}
    Raises InferenceError on failure.
    """
    cred = get_user_credential(user_id)
    if not cred:
        raise InferenceError("No API key configured. Add one in Settings → Connections.")

    provider = PROVIDERS.get(cred["provider"])
    if not provider:
        raise InferenceError(f"Unknown provider: {cred['provider']}")

    key = cred["key"]
    model = cred["model"]

    def _call(*, system: str, messages: list[dict], max_tokens: int = 1024, json_mode: bool = False) -> dict:
        return provider.complete(
            key=key, system=system, messages=messages,
            model=model, max_tokens=max_tokens, json_mode=json_mode,
        )

    return _call


def get_inference_for_system() -> Callable:
    """Return inference callable using the system-level Anthropic key (for background jobs, etc.)."""
    key = Config.ANTHROPIC_API_KEY
    if not key:
        raise InferenceError("No system ANTHROPIC_API_KEY configured.")

    provider = PROVIDERS["anthropic"]
    model = Config.ANALYSIS_MODEL

    def _call(*, system: str, messages: list[dict], max_tokens: int = 1024, json_mode: bool = False) -> dict:
        return provider.complete(
            key=key, system=system, messages=messages,
            model=model, max_tokens=max_tokens, json_mode=json_mode,
        )

    return _call


def get_provider(name: str) -> LLMProvider:
    p = PROVIDERS.get(name)
    if not p:
        raise InferenceError(f"Unknown provider: {name}")
    return p
