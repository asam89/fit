"""Google Gemini provider implementation."""

import json
import logging

from fitnessbot.inference.base import LLMProvider, InferenceError

logger = logging.getLogger(__name__)

MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]


class GeminiProvider(LLMProvider):
    name = "google"

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
            import google.generativeai as genai
        except ImportError:
            raise InferenceError("google-generativeai package not installed. Run: pip install google-generativeai")

        genai.configure(api_key=key)
        gen_config = {"max_output_tokens": max_tokens}
        if json_mode:
            gen_config["response_mime_type"] = "application/json"

        try:
            gen_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=system,
                generation_config=gen_config,
            )
            contents = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": msg["content"]}]})

            response = gen_model.generate_content(contents)
            text = response.text.strip() if response.text else ""

            input_tokens = 0
            output_tokens = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

            return {
                "text": text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        except Exception as e:
            err_str = str(e).lower()
            if "api key" in err_str or "invalid" in err_str or "401" in err_str or "403" in err_str:
                raise InferenceError(f"Invalid Google API key: {e}")
            raise InferenceError(f"Gemini API error: {e}")

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
        try:
            import google.generativeai as genai
            from google.generativeai.types import content_types
        except ImportError:
            raise InferenceError("google-generativeai package not installed")

        genai.configure(api_key=key)
        gen_config = {"max_output_tokens": max_tokens}
        if json_mode:
            gen_config["response_mime_type"] = "application/json"

        try:
            gen_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=system,
                generation_config=gen_config,
            )
            import PIL.Image
            import io
            img = PIL.Image.open(io.BytesIO(image_data))
            response = gen_model.generate_content([prompt, img])
            text = response.text.strip() if response.text else ""
            input_tokens = 0
            output_tokens = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
            return {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens}
        except Exception as e:
            raise InferenceError(f"Gemini vision error: {e}")

    def validate_key(self, key: str) -> bool:
        try:
            import google.generativeai as genai
            genai.configure(api_key=key)
            m = genai.GenerativeModel("gemini-1.5-flash")
            m.generate_content("hi")
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        return MODELS.copy()
