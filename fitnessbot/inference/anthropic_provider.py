"""Anthropic (Claude) provider implementation."""

import logging

import anthropic

from fitnessbot.inference.base import LLMProvider, InferenceError

logger = logging.getLogger(__name__)

MODELS = [
    "claude-sonnet-4-6",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-20250506",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]


class AnthropicProvider(LLMProvider):
    name = "anthropic"

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
        client = anthropic.Anthropic(api_key=key)
        sys_text = system
        if json_mode:
            sys_text += "\n\nReturn ONLY valid JSON, no markdown fences or extra text."
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=sys_text,
                messages=messages,
            )
            return {
                "text": response.content[0].text.strip(),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        except anthropic.AuthenticationError as e:
            raise InferenceError(f"Invalid Anthropic API key: {e}")
        except anthropic.RateLimitError as e:
            raise InferenceError(f"Anthropic rate limit: {e}")
        except Exception as e:
            raise InferenceError(f"Anthropic API error: {e}")

    def complete_vision(
        self,
        *,
        key: str,
        system: str,
        image_data: bytes,
        media_type: str,
        prompt: str,
        model: str,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> dict:
        import base64
        client = anthropic.Anthropic(api_key=key)
        sys_text = system
        if json_mode:
            sys_text += "\n\nReturn ONLY valid JSON, no markdown fences or extra text."
        b64 = base64.b64encode(image_data).decode("utf-8")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=sys_text, messages=messages,
            )
            return {
                "text": response.content[0].text.strip(),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        except anthropic.AuthenticationError as e:
            raise InferenceError(f"Invalid Anthropic API key: {e}")
        except Exception as e:
            raise InferenceError(f"Anthropic vision error: {e}")

    def validate_key(self, key: str) -> bool:
        try:
            client = anthropic.Anthropic(api_key=key)
            client.messages.create(
                model="claude-3-5-haiku-20241022",
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        return MODELS.copy()
