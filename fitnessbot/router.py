"""Classify freetext messages: meal | metric | query | goal | data-answer."""

import json
import logging
import re
import time

from fitnessbot import db
from fitnessbot.ai.prompts import ROUTER_SYSTEM, ROUTER_USER
from fitnessbot.inference.base import InferenceError

logger = logging.getLogger(__name__)

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

# Personal best / PR patterns: "PR bench 225 lbs", "new PB 5K 24:30", "personal best deadlift 405"
_PB_PATTERN = re.compile(
    r"^(?:new\s+)?(?:PR|PB|personal\s+(?:best|record))\s+(.+?)\s+([\d]+(?:[:.]\d+)?)\s*(.*)$",
    re.IGNORECASE,
)


def classify_message(text: str, has_pending_question: bool = False, user_id: int | None = None) -> dict:
    text_stripped = text.strip()

    if has_pending_question and len(text_stripped.split()) <= 5:
        return {"intent": "data_answer", "extracted": {"value": text_stripped}}

    # Personal best fast path
    pb_match = _PB_PATTERN.match(text_stripped)
    if pb_match:
        exercise = pb_match.group(1).strip()
        value = pb_match.group(2).strip()
        unit = pb_match.group(3).strip()
        return {
            "intent": "personal_best",
            "extracted": {"exercise_name": exercise, "value": value, "unit": unit},
        }

    if _METRIC_PATTERN.match(text_stripped):
        parts = text_stripped.split(maxsplit=1)
        return {
            "intent": "metric",
            "extracted": {"metric_type": parts[0].lower(), "value": parts[1] if len(parts) > 1 else ""},
        }

    lower = text_stripped.lower()
    if lower.startswith(("i ate ", "i had ", "just had ", "just ate ", "for breakfast ", "for lunch ", "for dinner ")):
        return {"intent": "meal", "extracted": {"raw_text": text_stripped}}

    if text_stripped.endswith("?"):
        return {"intent": "query", "extracted": {"question": text_stripped}}

    meal_matches = len(_MEAL_KEYWORDS.findall(text_stripped))
    metric_matches = len(_METRIC_KEYWORDS.findall(text_stripped))

    if meal_matches >= 2 and metric_matches == 0:
        return {"intent": "meal", "extracted": {"raw_text": text_stripped}}

    return _classify_with_llm(text_stripped, has_pending_question, user_id)


def _classify_with_llm(text: str, has_pending_question: bool, user_id: int | None = None) -> dict:
    from fitnessbot.inference.factory import get_inference, get_inference_for_system

    context = "There is a pending question awaiting the user's answer." if has_pending_question else "No pending questions."
    user_prompt = ROUTER_USER.format(text=text, context=context)

    try:
        infer = get_inference(user_id) if user_id else get_inference_for_system()
    except InferenceError:
        logger.warning("No inference available for routing, defaulting to meal")
        return {"intent": "meal", "extracted": {"raw_text": text}}

    start = time.time()
    try:
        result = infer(
            system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=300,
            json_mode=True,
        )
        latency_ms = (time.time() - start) * 1000
        raw = result["text"]

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        parsed = json.loads(raw)

        try:
            db.insert_llm_analysis(
                kind="router",
                model="provider",
                input_digest=text[:200],
                output_text=raw[:500],
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                latency_ms=latency_ms,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("Failed to log router LLM call: %s", e)

        return parsed

    except Exception as e:
        logger.error("LLM router failed: %s", e)
        return {"intent": "meal", "extracted": {"raw_text": text}}
