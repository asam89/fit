"""Classify freetext messages: meal | metric | query | goal | data-answer."""

import json
import logging
import re
import time

import anthropic

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.ai.prompts import ROUTER_SYSTEM, ROUTER_USER

logger = logging.getLogger(__name__)

# Keyword fast-paths to avoid API calls
_MEAL_KEYWORDS = re.compile(
    r"\b(ate|eat|had|breakfast|lunch|dinner|snack|toast|eggs?|chicken|rice|salad|pizza|burger|sandwich|coffee|tea|milk|juice|water)\b",
    re.IGNORECASE,
)
_METRIC_KEYWORDS = re.compile(
    r"\b(weight|weigh|slept|sleep|heart rate|hr |resting|blood pressure|bp |steps?)\b",
    re.IGNORECASE,
)
_METRIC_PATTERN = re.compile(
    r"^(weight|weigh|slept|sleep|hr|resting hr|bp)\s+[\d.]+",
    re.IGNORECASE,
)


def classify_message(text: str, has_pending_question: bool = False) -> dict:
    """Classify a user message into an intent category.

    Returns: {"intent": str, "extracted": dict}
    """
    text_stripped = text.strip()

    # Fast path: if there's a pending question and the reply is short, it's likely an answer
    if has_pending_question and len(text_stripped.split()) <= 5:
        return {"intent": "data_answer", "extracted": {"value": text_stripped}}

    # Fast path: metric pattern ("weight 182")
    if _METRIC_PATTERN.match(text_stripped):
        parts = text_stripped.split(maxsplit=1)
        return {
            "intent": "metric",
            "extracted": {"metric_type": parts[0].lower(), "value": parts[1] if len(parts) > 1 else ""},
        }

    # Fast path: starts with meal-like patterns
    lower = text_stripped.lower()
    if lower.startswith(("i ate ", "i had ", "just had ", "just ate ", "for breakfast ", "for lunch ", "for dinner ")):
        return {"intent": "meal", "extracted": {"raw_text": text_stripped}}

    # Fast path: question marks suggest a query
    if text_stripped.endswith("?"):
        return {"intent": "query", "extracted": {"question": text_stripped}}

    # If we have strong keyword signals, use them
    meal_matches = len(_MEAL_KEYWORDS.findall(text_stripped))
    metric_matches = len(_METRIC_KEYWORDS.findall(text_stripped))

    if meal_matches >= 2 and metric_matches == 0:
        return {"intent": "meal", "extracted": {"raw_text": text_stripped}}

    # Fall back to Claude for ambiguous messages
    return _classify_with_claude(text_stripped, has_pending_question)


def _classify_with_claude(text: str, has_pending_question: bool) -> dict:
    """Use Claude to classify ambiguous messages."""
    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    context = "There is a pending question awaiting the user's answer." if has_pending_question else "No pending questions."
    user_prompt = ROUTER_USER.format(text=text, context=context)

    start = time.time()
    try:
        response = client.messages.create(
            model=Config.ROUTER_MODEL,
            max_tokens=300,
            system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        latency_ms = (time.time() - start) * 1000
        raw = response.content[0].text.strip()

        # Strip markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)

        try:
            db.insert_llm_analysis(
                kind="router",
                model=Config.ROUTER_MODEL,
                input_digest=text[:200],
                output_text=raw[:500],
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.warning("Failed to log router LLM call: %s", e)

        return result

    except Exception as e:
        logger.error("Claude router failed: %s", e)
        # Default to meal if classification fails
        return {"intent": "meal", "extracted": {"raw_text": text}}
