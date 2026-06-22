"""Abstract base for LLM providers."""

from abc import ABC, abstractmethod


class InferenceError(Exception):
    """Raised when an LLM call fails (any provider)."""
    pass


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def complete(
        self,
        *,
        key: str,
        system: str,
        messages: list[dict],
        model: str,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> dict:
        """Run a completion. Returns {"text": str, "input_tokens": int, "output_tokens": int}."""
        ...

    @abstractmethod
    def validate_key(self, key: str) -> bool:
        """Cheap test call to verify the key works."""
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return selectable model IDs for this provider."""
        ...
