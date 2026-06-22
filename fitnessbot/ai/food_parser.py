"""Claude-powered meal text -> structured nutritional breakdown."""

import json
import logging
import time

import anthropic

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.ai.prompts import FOOD_PARSE_SYSTEM, FOOD_PARSE_USER

logger = logging.getLogger(__name__)


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)


def parse_meal(text: str, units_pref: str = "imperial") -> list[dict]:
    """Parse a natural-language meal description into structured food items via Claude."""
    client = _get_client()
    user_prompt = FOOD_PARSE_USER.format(text=text, units_pref=units_pref)

    start = time.time()
    response = client.messages.create(
        model=Config.ROUTER_MODEL,
        max_tokens=1500,
        system=FOOD_PARSE_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    latency_ms = (time.time() - start) * 1000

    raw_text = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()

    try:
        items = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Failed to parse Claude food response: %s", raw_text)
        items = []

    # Log the LLM call
    try:
        db.insert_llm_analysis(
            kind="food_parse",
            model=Config.ROUTER_MODEL,
            input_digest=text[:200],
            output_text=raw_text[:500],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
        )
    except Exception as e:
        logger.warning("Failed to log LLM analysis: %s", e)

    return items


def log_meal_from_parsed(
    user_id: int,
    raw_text: str,
    items: list[dict],
    source: str = "text",
    meal_type: str | None = None,
) -> dict:
    """Persist parsed food items as a meal and return summary."""
    total_cal = sum(item.get("calories", 0) for item in items)
    total_pro = sum(item.get("protein", 0) for item in items)
    total_carb = sum(item.get("carbs", 0) for item in items)
    total_fat = sum(item.get("fat", 0) for item in items)

    if not meal_type:
        meal_type = _infer_meal_type()

    meal_id = db.insert_meal(
        user_id=user_id,
        raw_text=raw_text,
        meal_type=meal_type,
        source=source,
        total_calories=total_cal,
        total_protein=total_pro,
        total_carbs=total_carb,
        total_fat=total_fat,
    )

    saved_items = []
    for item in items:
        food_id = db.insert_food(
            name=item.get("name", "Unknown"),
            calories=item.get("calories", 0),
            protein=item.get("protein", 0),
            carbs=item.get("carbs", 0),
            fat=item.get("fat", 0),
            fiber=item.get("fiber", 0),
            sugar=item.get("sugar", 0),
            sodium=item.get("sodium", 0),
            serving_qty=item.get("qty"),
            serving_unit=item.get("unit"),
            source="claude",
            claude_confidence=item.get("confidence"),
        )
        db.insert_meal_item(
            meal_id=meal_id,
            food_id=food_id,
            qty=item.get("qty", 1),
            unit=item.get("unit", "serving"),
            calories=item.get("calories", 0),
            protein=item.get("protein", 0),
            carbs=item.get("carbs", 0),
            fat=item.get("fat", 0),
        )
        saved_items.append(item)

    return {
        "meal_id": meal_id,
        "items": saved_items,
        "total_calories": total_cal,
        "total_protein": total_pro,
        "total_carbs": total_carb,
        "total_fat": total_fat,
    }


def _infer_meal_type() -> str:
    """Infer meal type from current time of day."""
    from datetime import datetime
    import pytz

    try:
        tz = pytz.timezone(Config.TIMEZONE)
        now = datetime.now(tz)
    except Exception:
        from datetime import timezone
        now = datetime.now(timezone.utc)

    hour = now.hour
    if hour < 11:
        return "breakfast"
    elif hour < 15:
        return "lunch"
    elif hour < 18:
        return "snack"
    else:
        return "dinner"
