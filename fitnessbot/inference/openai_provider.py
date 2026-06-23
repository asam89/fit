"""OpenAI (ChatGPT) provider implementation."""

import json
import logging

from fitnessbot.inference.base import LLMProvider, InferenceError

logger = logging.getLogger(__name__)

MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]


class OpenAIProvider(LLMProvider):
    name = "openai"

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
        try:
            from openai import OpenAI, AuthenticationError, RateLimitError
        except ImportError:
            raise InferenceError("openai package not installed. Run: pip install openai")

        client = OpenAI(api_key=key)
        all_messages = [{"role": "system", "content": system}] + messages

        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": all_messages,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            return {
                "text": choice.message.content.strip(),
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            }
        except AuthenticationError as e:
            raise InferenceError(f"Invalid OpenAI API key: {e}")
        except RateLimitError as e:
            raise InferenceError(f"OpenAI rate limit: {e}")
        except Exception as e:
            raise InferenceError(f"OpenAI API error: {e}")

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
        try:
            from openai import OpenAI, AuthenticationError
        except ImportError:
            raise InferenceError("openai package not installed")

        client = OpenAI(api_key=key)
        b64 = base64.b64encode(image_data).decode("utf-8")
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            },
        ]
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            return {
                "text": choice.message.content.strip(),
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            }
        except AuthenticationError as e:
            raise InferenceError(f"Invalid OpenAI API key: {e}")
        except Exception as e:
            raise InferenceError(f"OpenAI vision error: {e}")

    def validate_key(self, key: str) -> bool:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key)
            client.chat.completions.create(
                model="gpt-3.5-turbo",
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        return MODELS.copy()
