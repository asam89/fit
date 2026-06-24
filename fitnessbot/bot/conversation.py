"""Conversational engine: understand -> act -> respond loop for Telegram messages."""

import json
import logging
import re
import time
from datetime import datetime, timezone

from fitnessbot import db
from fitnessbot.ai.food_parser import parse_meal, log_meal_from_parsed
from fitnessbot.event_coaching import (
    is_event_goal_message, is_readiness_check, parse_event_date,
    detect_sport_type, build_prep_plan, build_readiness_assessment,
    format_prep_plan_summary,
)
from fitnessbot.metrics import log_weight, get_weight_summary
from fitnessbot.inference.base import InferenceError

logger = logging.getLogger(__name__)

# --- NLU prompts ---

NLU_SYSTEM = """You are an intent classifier for a fitness tracking bot. Given a user message, extract ALL intents and structured data.

Return ONLY a JSON object with key "intents" — an array of objects, each with:
- "type": one of meal_log, health_metric, workout_log, profile_update, goal_update, plan_set, plan_complete, event_goal, readiness_check, query, correction, general
- "confidence": 0.0-1.0
- Fields specific to the type:
  - meal_log: "items" (array of {name, qty, unit}), "meal_type" (breakfast/lunch/dinner/snack), "when" (now/this_morning/last_night)
  - health_metric: "metric" (weight/sleep_hours/sleep_quality/resting_hr/hrv/bp/spo2/body_fat/mood/energy/hydration), "value", "unit", "when"
  - workout_log: "activity" (strength/cardio/mixed/yoga/etc), "duration_min", "notes"
  - profile_update: "field" (age/height/sex/units/activity_level), "value"
  - goal_update: "goal_type" (lose/gain/maintain), "target_weight", "target_date"
  - plan_set: "activities" (array of {day: "monday"..."sunday", title: str, type: "strength"/"run"/"cardio"/"mobility"/"sport"/"rest"/"other", duration: int|null})
  - plan_complete: "title_hint" (what activity to mark done, e.g. "basketball", "legs"), "actual_duration": int|null
  - event_goal: "title" (event name, e.g. "basketball tournament", "half marathon"), "date_text" (raw date mention, e.g. "July 17th", "in 30 days"), "description" (what the user wants help with)
  - readiness_check: "event_hint" (which event they're asking about, or empty for most recent)
  - query: "question"
  - correction: "what" (description of what to fix), "new_value"
  - general: "text"

Use event_goal when the user mentions an upcoming event, competition, race, or challenge with a date and wants preparation help, motivation, or a plan.
Use readiness_check when the user asks if they're ready/prepared for an upcoming event.

Multi-data messages should produce multiple intents. Be concise.
If confidence < 0.6, set "ambiguous": true and "clarification": "short question to ask".
"""

RESPOND_SYSTEM = """You are a fitness coaching assistant. Given the user's context (targets, today's totals, what was just logged, weight trend), write a SHORT reply (2-4 lines max).

Rules:
- First confirm what was logged (use the EXACT numbers provided in context, never invent numbers)
- Then add ONE practical, specific focus point about diet or training
- For meals: reference remaining macros and what to prioritize next
- When the user asks what to eat, or when there's a significant protein/macro gap (>20g protein remaining), suggest 2-3 specific foods with approximate amounts that would fill the gap. Example: "40g protein to go — try: grilled chicken breast (4oz = 35g), Greek yogurt (1 cup = 17g), or a protein shake (25g)."
- Food suggestions should be common, practical foods. Include approximate portion and protein/macro content.
- Voice: plain, specific, honest, lightly motivating. Never robotic stat-dumps, never alarmist
- No medical claims. On distress signals, respond with care
- Keep it tight — this costs the user tokens on their own key
- Numbers in the context block are ground truth — NEVER hallucinate different numbers"""

# --- fast-path patterns ---

_WEIGHT_PAT = re.compile(r"^(?:weight|weigh)\s+([\d.]+)\s*(lbs?|kg|pounds?)?(?:\s+this\s+morning)?$", re.I)
_SLEEP_PAT = re.compile(r"^(?:slept?|sleep)\s+([\d.]+)\s*(?:h(?:ours?)?|hrs?)?$", re.I)
_RHR_PAT = re.compile(r"^(?:rhr|resting\s*(?:hr|heart\s*rate))\s+([\d]+)$", re.I)
_HYDRATION_PAT = re.compile(r"^(?:drank|water|hydration)\s+([\d.]+)\s*(?:glasses?|liters?|litres?|cups?|oz)?$", re.I)
_BARE_NUMBER = re.compile(r"^[\d.]+$")
_QUERY_PAT = re.compile(
    r"(?:how(?:\'s| is| has| have| am| was| were| did))|(?:what(?:\'s| is| are| was| were))|(?:show me|tell me|give me|summary|report|recap|review)",
    re.I,
)


def _fast_path_intents(text: str, pending: dict | None) -> list[dict] | None:
    """Try deterministic matches before LLM. Returns list of intents or None to fall through."""
    stripped = text.strip()

    if pending and _BARE_NUMBER.match(stripped):
        cat = pending.get("category", "")
        if cat == "weight":
            return [{"type": "health_metric", "metric": "weight", "value": stripped, "unit": "lbs", "confidence": 0.95}]
        elif cat == "sleep":
            return [{"type": "health_metric", "metric": "sleep_hours", "value": stripped, "unit": "hours", "confidence": 0.95}]
        return None

    m = _WEIGHT_PAT.match(stripped)
    if m:
        unit = (m.group(2) or "lbs").lower()
        if unit.startswith("pound"):
            unit = "lbs"
        return [{"type": "health_metric", "metric": "weight", "value": m.group(1), "unit": unit, "confidence": 0.99}]

    m = _SLEEP_PAT.match(stripped)
    if m:
        return [{"type": "health_metric", "metric": "sleep_hours", "value": m.group(1), "unit": "hours", "confidence": 0.99}]

    m = _RHR_PAT.match(stripped)
    if m:
        return [{"type": "health_metric", "metric": "resting_hr", "value": m.group(1), "unit": "bpm", "confidence": 0.99}]

    m = _HYDRATION_PAT.match(stripped)
    if m:
        return [{"type": "health_metric", "metric": "hydration", "value": m.group(1), "unit": "glasses", "confidence": 0.99}]

    lower = stripped.lower()
    if lower.startswith(("i ate ", "i had ", "just had ", "just ate ", "for breakfast ", "for lunch ", "for dinner ", "for snack ")):
        return [{"type": "meal_log", "items": [], "raw_text": stripped, "confidence": 0.95}]

    if _QUERY_PAT.search(stripped) and any(kw in lower for kw in (
        "fitness", "diet", "weight", "calories", "macro", "protein", "training",
        "workout", "sleep", "progress", "doing", "going", "week", "month",
        "today", "nutrition", "eating", "health", "plan", "adherence",
    )):
        return [{"type": "query", "question": stripped, "confidence": 0.95}]

    # Personal best fast path
    from fitnessbot.router import _PB_PATTERN
    pb_m = _PB_PATTERN.match(stripped)
    if pb_m:
        return [{"type": "personal_best", "exercise_name": pb_m.group(1).strip(),
                 "value": pb_m.group(2).strip(), "unit": pb_m.group(3).strip(), "confidence": 0.95}]

    # Event goal fast path
    if is_event_goal_message(stripped):
        return [{"type": "event_goal", "title": stripped, "date_text": stripped, "description": stripped, "confidence": 0.9}]

    # Readiness check fast path
    if is_readiness_check(stripped):
        return [{"type": "readiness_check", "event_hint": stripped, "confidence": 0.95}]

    return None


def _nlu_via_llm(text: str, user_id: int, pending: dict | None) -> tuple[list[dict], dict]:
    """Classify via LLM. Returns (intents, token_usage)."""
    from fitnessbot.inference.factory import get_inference

    context_parts = []
    if pending:
        context_parts.append(f"PENDING QUESTION: \"{pending['question_text']}\" (category: {pending.get('category', 'unknown')})")
    context_parts.append(f"Message: {text}")

    try:
        infer = get_inference(user_id)
        result = infer(
            system=NLU_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(context_parts)}],
            max_tokens=400,
            json_mode=True,
        )
        raw = result["text"]
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        parsed = json.loads(raw)
        intents = parsed.get("intents", [parsed] if "type" in parsed else [])
        usage = {"input_tokens": result.get("input_tokens", 0), "output_tokens": result.get("output_tokens", 0)}
        return intents, usage
    except InferenceError:
        logger.warning("No inference for NLU (user %s), falling back to meal", user_id)
        return [{"type": "meal_log", "raw_text": text, "confidence": 0.5}], {"input_tokens": 0, "output_tokens": 0}
    except Exception as e:
        logger.error("NLU LLM failed: %s", e)
        return [{"type": "meal_log", "raw_text": text, "confidence": 0.5}], {"input_tokens": 0, "output_tokens": 0}


# --- ACT layer ---

def _act_on_intents(intents: list[dict], user_id: int, raw_text: str) -> list[dict]:
    """Execute writes for each intent. Returns list of {intent_type, result, ...} for each."""
    user = db.get_user_by_id(user_id)
    units_pref = user.get("units_pref", "imperial") if user else "imperial"
    results = []

    for intent in intents:
        itype = intent.get("type", "general")
        try:
            if itype == "meal_log":
                r = _act_meal(intent, user_id, raw_text, units_pref)
            elif itype == "health_metric":
                r = _act_metric(intent, user_id)
            elif itype == "workout_log":
                r = _act_workout(intent, user_id)
            elif itype == "profile_update":
                r = _act_profile(intent, user_id)
            elif itype == "goal_update":
                r = _act_goal(intent, user_id)
            elif itype == "plan_set":
                r = _act_plan_set(intent, user_id)
            elif itype == "plan_complete":
                r = _act_plan_complete(intent, user_id)
            elif itype == "personal_best":
                r = _act_personal_best(intent, user_id)
            elif itype == "event_goal":
                r = _act_event_goal(intent, user_id)
            elif itype == "readiness_check":
                r = _act_readiness_check(intent, user_id)
            elif itype == "query":
                r = _act_query(user_id, intent.get("question", ""))
            elif itype == "correction":
                r = _act_correction(intent, user_id, units_pref)
            else:
                r = {"action": "none", "note": "general message"}
            r["intent_type"] = itype
            results.append(r)
        except Exception as e:
            logger.error("ACT failed for intent %s: %s", itype, e)
            results.append({"intent_type": itype, "action": "error", "error": str(e)})
    return results


def _act_meal(intent: dict, user_id: int, raw_text: str, units_pref: str) -> dict:
    meal_text = intent.get("raw_text") or raw_text
    items = parse_meal(meal_text, units_pref=units_pref, user_id=user_id)
    if not items:
        return {"action": "parse_failed", "text": meal_text}
    result = log_meal_from_parsed(user_id, meal_text, items, source="telegram")
    return {
        "action": "meal_logged",
        "items": result["items"],
        "total_calories": result["total_calories"],
        "total_protein": result["total_protein"],
        "total_carbs": result["total_carbs"],
        "total_fat": result["total_fat"],
        "meal_id": result.get("meal_id"),
    }


def _act_metric(intent: dict, user_id: int) -> dict:
    metric = intent.get("metric", "")
    value_str = str(intent.get("value", ""))
    unit = intent.get("unit", "")

    if metric == "weight":
        try:
            w = float(value_str)
            info = log_weight(user_id, w)
            return {"action": "weight_logged", "raw": info["raw"], "smoothed": info["smoothed"], "unit": unit or "lbs"}
        except (ValueError, TypeError):
            return {"action": "error", "error": f"Invalid weight: {value_str}"}

    elif metric == "sleep_hours":
        hours = float(value_str)
        db.insert_health_data(user_id, "sleep", json.dumps({"hours": hours}), notes=f"Sleep: {hours}h")
        return {"action": "sleep_logged", "hours": hours}

    elif metric == "sleep_quality":
        db.insert_health_data(user_id, "sleep", json.dumps({"quality": value_str}), notes=f"Sleep quality: {value_str}")
        return {"action": "sleep_quality_logged", "quality": value_str}

    elif metric == "resting_hr":
        hr = int(float(value_str))
        db.insert_health_data(user_id, "vitals", json.dumps({"resting_hr": hr}), notes=f"RHR: {hr} bpm")
        return {"action": "rhr_logged", "value": hr}

    elif metric in ("mood", "energy"):
        db.insert_health_data(user_id, "wellness", json.dumps({metric: value_str}), notes=f"{metric}: {value_str}")
        return {"action": f"{metric}_logged", "value": value_str}

    elif metric == "hydration":
        glasses = float(value_str)
        db.insert_health_data(user_id, "wellness", json.dumps({"hydration_glasses": glasses}), notes=f"Water: {glasses}")
        return {"action": "hydration_logged", "value": glasses}

    elif metric == "body_fat":
        bf = float(value_str)
        db.insert_health_data(user_id, "body_comp", json.dumps({"body_fat_pct": bf}), notes=f"Body fat: {bf}%")
        return {"action": "body_fat_logged", "value": bf}

    elif metric == "bp":
        db.insert_health_data(user_id, "vitals", json.dumps({"blood_pressure": value_str}), notes=f"BP: {value_str}")
        return {"action": "bp_logged", "value": value_str}

    else:
        db.insert_health_data(user_id, "other", json.dumps({metric: value_str}), notes=f"{metric}: {value_str}")
        return {"action": "metric_logged", "metric": metric, "value": value_str}


def _act_workout(intent: dict, user_id: int) -> dict:
    activity = intent.get("activity", "workout")
    duration = intent.get("duration_min")
    notes = intent.get("notes", "")
    data = {"type": activity}
    if duration:
        data["duration_min"] = int(float(duration))
    if notes:
        data["notes"] = notes
    db.insert_health_data(user_id, "workout", json.dumps(data), notes=f"Workout: {activity} {duration or ''}min")
    return {"action": "workout_logged", "activity": activity, "duration_min": duration}


def _act_profile(intent: dict, user_id: int) -> dict:
    field = intent.get("field", "")
    value = intent.get("value", "")
    field_map = {
        "age": None, "height": "height", "sex": "sex",
        "units": "units_pref", "activity_level": "activity_level",
    }
    db_field = field_map.get(field)
    if db_field and value:
        db.update_user(user_id, **{db_field: value})
        return {"action": "profile_updated", "field": field, "value": value}
    return {"action": "profile_noted", "field": field, "value": value}


def _act_goal(intent: dict, user_id: int) -> dict:
    return {"action": "goal_noted", "details": intent}


def _act_plan_set(intent: dict, user_id: int) -> dict:
    from fitnessbot import training_plan
    activities = intent.get("activities", [])
    if not activities:
        return {"action": "plan_empty", "note": "No activities parsed"}
    result = training_plan.set_plan_from_text(user_id, activities)
    return {
        "action": "plan_set",
        "added": result["added"],
        "week_start": result["week_start"],
    }


def _act_plan_complete(intent: dict, user_id: int) -> dict:
    from fitnessbot import training_plan
    title_hint = intent.get("title_hint", "")
    actual_duration = intent.get("actual_duration")
    if not title_hint:
        return {"action": "plan_complete_failed", "note": "No activity specified"}
    result = training_plan.complete_by_title(user_id, title_hint, actual_duration)
    if result:
        return {
            "action": "plan_completed",
            "title": result["title"],
            "activity_type": result["activity_type"],
            "item_id": result["item_id"],
        }
    # No matching planned item — log as a workout anyway
    from fitnessbot import db as _db
    data = json.dumps({"type": "other", "activity": title_hint, "duration_min": actual_duration, "source": "voice"})
    _db.insert_health_data(user_id, "workout", data, notes=f"Workout: {title_hint}")
    return {
        "action": "workout_logged_no_plan",
        "activity": title_hint,
        "duration_min": actual_duration,
    }


def _act_personal_best(intent: dict, user_id: int) -> dict:
    """Log a new personal best / PR."""
    exercise = intent.get("exercise_name", "").strip()
    value_str = str(intent.get("value", ""))
    unit = intent.get("unit", "")
    if not exercise or not value_str:
        return {"action": "pb_failed", "note": "Missing exercise or value"}
    try:
        value = float(value_str.replace(":", "."))  # handles time-like "24:30" → 24.30
    except (ValueError, TypeError):
        return {"action": "pb_failed", "note": f"Invalid value: {value_str}"}

    # Check if this is actually a new PR (beats previous best)
    prev = db.get_personal_bests_by_exercise(user_id, exercise)
    is_new_record = not prev or value > max(p["value"] for p in prev)

    pb_id = db.insert_personal_best(user_id, exercise, value, unit)
    return {
        "action": "pb_logged",
        "exercise_name": exercise,
        "value": value,
        "unit": unit,
        "pb_id": pb_id,
        "is_new_record": is_new_record,
        "previous_best": max(p["value"] for p in prev) if prev else None,
    }


def _act_event_goal(intent: dict, user_id: int) -> dict:
    """Create an event goal and generate a prep plan."""
    title_raw = intent.get("title", "")
    date_text = intent.get("date_text", title_raw)
    description = intent.get("description", title_raw)

    event_date = parse_event_date(date_text)
    if not event_date:
        event_date = parse_event_date(title_raw)
    if not event_date:
        return {"action": "event_goal_no_date", "note": "Could not determine event date"}

    sport_type = detect_sport_type(title_raw) or detect_sport_type(description)

    # Extract a clean title from the raw text
    from fitnessbot.event_coaching import _EVENT_KEYWORDS
    m = _EVENT_KEYWORDS.search(title_raw)
    clean_title = title_raw[:80] if not m else f"{sport_type or ''} {m.group(0)}".strip().title()
    if not clean_title:
        clean_title = title_raw[:80]

    now = datetime.now(timezone.utc)
    target = datetime.strptime(event_date, "%Y-%m-%d")
    days_out = (target.date() - now.date()).days

    plan_data = build_prep_plan(user_id, clean_title, event_date, sport_type, description)

    eg_id = db.insert_event_goal(
        user_id=user_id,
        title=clean_title,
        event_date=event_date,
        sport_type=sport_type,
        description=description,
        days_out=days_out,
        prep_plan_json=json.dumps(plan_data.get("prep_plan", {})),
        science_notes=plan_data.get("science_notes", ""),
        readiness_markers=json.dumps(plan_data.get("readiness_markers", [])),
        motivation_frequency="daily" if days_out <= 30 else "every_other_day",
    )

    return {
        "action": "event_goal_created",
        "eg_id": eg_id,
        "title": clean_title,
        "event_date": event_date,
        "days_out": days_out,
        "sport_type": sport_type,
        "plan_data": plan_data,
    }


def _act_readiness_check(intent: dict, user_id: int) -> dict:
    """Assess readiness for the user's active event goal."""
    active_goals = db.get_active_event_goals(user_id)
    if not active_goals:
        return {"action": "readiness_no_event", "note": "No active event goals"}

    event_hint = intent.get("event_hint", "")
    goal = active_goals[0]
    if event_hint:
        for g in active_goals:
            if g["title"].lower() in event_hint.lower() or event_hint.lower() in g["title"].lower():
                goal = g
                break

    assessment = build_readiness_assessment(user_id, goal)
    return {
        "action": "readiness_assessed",
        "title": goal["title"],
        "event_date": goal["event_date"],
        "assessment": assessment,
    }


def _act_query(user_id: int, question: str = "") -> dict:
    from fitnessbot.nutrition import get_nutrition_targets
    from fitnessbot import training_plan

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    targets = get_nutrition_targets(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    lower_q = question.lower()
    is_week = any(w in lower_q for w in ("week", "7 day", "last 7", "this week", "past week"))
    is_month = any(w in lower_q for w in ("month", "30 day", "last 30", "this month", "past month"))
    lookback = 30 if is_month else 7

    macro_hist = db.get_macro_history(user_id, lookback)
    sleep_hist = db.get_sleep_history(user_id, lookback)
    workout_hist = db.get_workout_history(user_id, lookback)
    weight_hist = db.get_weight_history(user_id, limit=lookback)

    ws = training_plan._monday_of_week(datetime.now(timezone.utc).date())
    plan_items = training_plan.get_plan_items(user_id, ws)
    adherence = training_plan.compute_adherence(plan_items) if plan_items else None

    # Personal bests context for exercise-related queries
    personal_bests = db.get_top_personal_bests(user_id)

    return {
        "action": "query",
        "question": question,
        "totals": totals,
        "targets": targets,
        "weight": weight,
        "meal_count": meal_count,
        "macro_history": macro_hist,
        "sleep_history": sleep_hist,
        "workout_history": workout_hist,
        "weight_history": weight_hist,
        "plan_items": plan_items,
        "adherence": adherence,
        "lookback_days": lookback,
        "personal_bests": personal_bests,
    }


def _act_correction(intent: dict, user_id: int, units_pref: str) -> dict:
    correction_text = intent.get("what", "") or intent.get("new_value", "")
    last_meal = db.get_last_meal(user_id)
    if not last_meal:
        return {"action": "correction_no_meal", "note": "No recent meal to correct"}

    new_items = parse_meal(correction_text, units_pref=units_pref, user_id=user_id)
    if not new_items:
        return {"action": "correction_failed", "note": f"Could not parse correction: {correction_text}"}

    db.update_meal_items(last_meal["meal_id"], new_items)
    total_cal = sum(i.get("calories", 0) for i in new_items)
    total_pro = sum(i.get("protein", 0) for i in new_items)
    return {
        "action": "correction_applied",
        "meal_id": last_meal["meal_id"],
        "original_text": last_meal.get("raw_text", ""),
        "new_calories": total_cal,
        "new_protein": total_pro,
    }


# --- RESPOND layer ---

FOOD_SUGGESTIONS_DB = {
    "protein": [
        ("grilled chicken breast", "4oz", 35, 170),
        ("Greek yogurt", "1 cup", 17, 100),
        ("protein shake", "1 scoop", 25, 130),
        ("cottage cheese", "1 cup", 28, 220),
        ("eggs", "3 large", 18, 210),
        ("canned tuna", "1 can", 30, 130),
        ("turkey breast", "4oz", 28, 120),
        ("edamame", "1 cup", 17, 190),
        ("beef jerky", "1oz", 9, 80),
        ("whey protein", "1 scoop", 25, 120),
    ],
    "fat": [
        ("avocado", "1/2", 15, 160),
        ("almonds", "1oz", 14, 160),
        ("peanut butter", "2 tbsp", 16, 190),
        ("olive oil", "1 tbsp", 14, 120),
        ("cheese", "1oz", 9, 110),
    ],
    "carbs": [
        ("oatmeal", "1 cup cooked", 27, 150),
        ("banana", "1 medium", 27, 105),
        ("rice", "1 cup cooked", 45, 200),
        ("sweet potato", "1 medium", 26, 110),
        ("whole wheat bread", "2 slices", 24, 140),
    ],
}


def _get_food_suggestions(targets: dict, totals: dict) -> str:
    """Generate food suggestion lines based on macro gaps."""
    remaining_pro = targets["protein"] - totals.get("protein", 0)
    remaining_fat = targets["fat"] - totals.get("fat", 0)
    remaining_carbs = targets["carbs"] - totals.get("carbs", 0)
    remaining_cal = targets["calories"] - totals.get("calories", 0)

    suggestions = []

    if remaining_pro > 15 and remaining_cal > 100:
        foods = FOOD_SUGGESTIONS_DB["protein"]
        picks = [f"{name} ({portion} = {pro}g P)" for name, portion, pro, cal in foods if cal <= remaining_cal][:3]
        if picks:
            suggestions.append(f"PROTEIN GAP ({remaining_pro:.0f}g to go). Options: {', '.join(picks)}")

    if remaining_fat > 10 and remaining_cal > 100 and not suggestions:
        foods = FOOD_SUGGESTIONS_DB["fat"]
        picks = [f"{name} ({portion} = {fat}g F)" for name, portion, fat, cal in foods if cal <= remaining_cal][:3]
        if picks:
            suggestions.append(f"FAT GAP ({remaining_fat:.0f}g to go). Options: {', '.join(picks)}")

    if remaining_carbs > 30 and remaining_cal > 100 and not suggestions:
        foods = FOOD_SUGGESTIONS_DB["carbs"]
        picks = [f"{name} ({portion} = {carb}g C)" for name, portion, carb, cal in foods if cal <= remaining_cal][:3]
        if picks:
            suggestions.append(f"CARBS GAP ({remaining_carbs:.0f}g to go). Options: {', '.join(picks)}")

    return "\n".join(suggestions)


QUERY_RESPOND_SYSTEM = """You are a fitness coaching assistant answering a question about the user's data. You have their actual logged data below.

Rules:
- Answer based ONLY on the data provided — never invent numbers
- Be specific: reference actual numbers, dates, and trends from the data
- Plain, honest, lightly motivating tone
- If data is missing, say so ("no meals logged on Tuesday" rather than making up numbers)
- Keep it concise: 4-8 lines max
- Include a practical insight or suggestion based on what you see
- For diet questions: cover calories, protein, consistency
- For fitness questions: cover workouts, training plan adherence, activity
- For general "how am I doing": cover both diet + fitness
- Numbers from the data context are ground truth — NEVER hallucinate different numbers"""


def _build_query_context(act_result: dict) -> str:
    """Build a rich data context for answering user queries."""
    lines = []
    targets = act_result.get("targets", {})
    lookback = act_result.get("lookback_days", 7)
    period = f"last {lookback} days"

    lines.append(f"PERIOD: {period}")
    lines.append(f"TARGETS: {targets.get('calories', 0)} cal, {targets.get('protein', 0)}g P, {targets.get('carbs', 0)}g C, {targets.get('fat', 0)}g F")

    # Today
    totals = act_result.get("totals", {})
    lines.append(f"\nTODAY: {totals.get('calories', 0):.0f} cal, {totals.get('protein', 0):.0f}g P, {totals.get('carbs', 0):.0f}g C, {totals.get('fat', 0):.0f}g F | {act_result.get('meal_count', 0)} meals")

    # Macro history
    macro_hist = act_result.get("macro_history", [])
    if macro_hist:
        lines.append(f"\nDIET HISTORY ({len(macro_hist)} days logged):")
        total_cal = sum(d.get("calories", 0) or 0 for d in macro_hist)
        total_pro = sum(d.get("protein", 0) or 0 for d in macro_hist)
        avg_cal = total_cal / len(macro_hist) if macro_hist else 0
        avg_pro = total_pro / len(macro_hist) if macro_hist else 0
        lines.append(f"  Avg: {avg_cal:.0f} cal/day, {avg_pro:.0f}g protein/day")
        on_target_cal = sum(1 for d in macro_hist if abs((d.get("calories", 0) or 0) - targets.get("calories", 0)) < targets.get("calories", 2000) * 0.1)
        on_target_pro = sum(1 for d in macro_hist if (d.get("protein", 0) or 0) >= targets.get("protein", 0) * 0.9)
        lines.append(f"  Calories on target: {on_target_cal}/{len(macro_hist)} days")
        lines.append(f"  Protein hit: {on_target_pro}/{len(macro_hist)} days")
        for d in macro_hist[-7:]:
            lines.append(f"  {d['date']}: {(d.get('calories') or 0):.0f} cal, {(d.get('protein') or 0):.0f}g P, {d.get('meal_count', 0)} meals")
    else:
        lines.append(f"\nDIET HISTORY: No meals logged in the {period}")

    # Weight
    weight = act_result.get("weight", {})
    if weight.get("has_data"):
        lines.append(f"\nWEIGHT: {weight.get('current_smoothed', '?')} lbs (smoothed)")
        if weight.get("trend_7d") is not None:
            direction = "down" if weight["trend_7d"] < 0 else "up"
            lines.append(f"  7d trend: {abs(weight['trend_7d']):.1f} lbs {direction}")
        if weight.get("trend_30d") is not None:
            direction = "down" if weight["trend_30d"] < 0 else "up"
            lines.append(f"  30d trend: {abs(weight['trend_30d']):.1f} lbs {direction}")
    else:
        lines.append("\nWEIGHT: No weight data logged")

    # Workouts
    workout_hist = act_result.get("workout_history", [])
    if workout_hist:
        lines.append(f"\nWORKOUTS ({len(workout_hist)} sessions):")
        for w in workout_hist[-7:]:
            dur = w.get("duration_min", "?")
            lines.append(f"  {w.get('date', '?')}: {w.get('type', w.get('activity', 'workout'))} {dur}min")
    else:
        lines.append(f"\nWORKOUTS: None logged in {period}")

    # Training plan adherence
    adherence = act_result.get("adherence")
    plan_items = act_result.get("plan_items", [])
    if adherence:
        lines.append(f"\nTRAINING PLAN: {adherence.get('label', '?')}")
        completed = [i for i in plan_items if i.get("status") == "completed" or i.get("display_status") == "completed"]
        missed = [i for i in plan_items if i.get("display_status") == "missed"]
        if completed:
            lines.append(f"  Completed: {', '.join(i['title'] for i in completed)}")
        if missed:
            lines.append(f"  Missed: {', '.join(i['title'] for i in missed)}")

    # Sleep
    sleep_hist = act_result.get("sleep_history", [])
    if sleep_hist:
        sleep_hours = [s.get("hours", 0) for s in sleep_hist if s.get("hours")]
        if sleep_hours:
            avg_sleep = sum(sleep_hours) / len(sleep_hours)
            lines.append(f"\nSLEEP: avg {avg_sleep:.1f}h ({len(sleep_hours)} nights logged)")
    else:
        lines.append(f"\nSLEEP: Not tracked in {period}")

    # Personal bests
    personal_bests = act_result.get("personal_bests", [])
    if personal_bests:
        lines.append(f"\nPERSONAL BESTS:")
        for pb in personal_bests[:10]:
            ex = pb.get("exercise_name", "").title()
            val = pb.get("value", "")
            u = pb.get("unit", "")
            date = pb.get("recorded_at", "")[:10] if pb.get("recorded_at") else ""
            lines.append(f"  {ex}: {val}{' ' + u if u else ''} ({date})")

    return "\n".join(lines)


def _deterministic_query_response(act_result: dict) -> str:
    """Build a deterministic query response without LLM."""
    lines = []
    targets = act_result.get("targets", {})
    lookback = act_result.get("lookback_days", 7)
    period_label = "this week" if lookback <= 7 else "this month"

    macro_hist = act_result.get("macro_history", [])
    if macro_hist:
        total_cal = sum(d.get("calories", 0) or 0 for d in macro_hist)
        total_pro = sum(d.get("protein", 0) or 0 for d in macro_hist)
        avg_cal = total_cal / len(macro_hist)
        avg_pro = total_pro / len(macro_hist)
        on_target_pro = sum(1 for d in macro_hist if (d.get("protein", 0) or 0) >= targets.get("protein", 0) * 0.9)
        lines.append(f"Diet ({period_label}): avg {avg_cal:.0f} cal/day (target {targets.get('calories', 0)}), {avg_pro:.0f}g protein/day. Hit protein {on_target_pro}/{len(macro_hist)} days.")
    else:
        lines.append(f"Diet: No meals logged {period_label}.")

    weight = act_result.get("weight", {})
    if weight.get("has_data"):
        w_line = f"Weight: {weight.get('current_smoothed', '?')} lbs"
        if weight.get("trend_7d") is not None:
            direction = "down" if weight["trend_7d"] < 0 else "up"
            w_line += f" ({abs(weight['trend_7d']):.1f} {direction} over 7d)"
        lines.append(w_line)

    workout_hist = act_result.get("workout_history", [])
    lines.append(f"Workouts: {len(workout_hist)} sessions {period_label}.")

    adherence = act_result.get("adherence")
    if adherence:
        lines.append(f"Training plan: {adherence.get('label', '?')}.")

    sleep_hist = act_result.get("sleep_history", [])
    sleep_hours = [s.get("hours", 0) for s in sleep_hist if s.get("hours")]
    if sleep_hours:
        lines.append(f"Sleep: avg {sum(sleep_hours)/len(sleep_hours):.1f}h over {len(sleep_hours)} nights.")

    return "\n".join(lines) if lines else "I don't have enough data to answer that yet. Log some meals, workouts, or weight and I'll be able to tell you more."


def _build_context_digest(user_id: int, act_results: list[dict]) -> str:
    from fitnessbot.nutrition import get_nutrition_targets
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    targets = get_nutrition_targets(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    remaining_cal = targets["calories"] - totals["calories"]
    remaining_pro = targets["protein"] - totals["protein"]

    lines = [
        f"TARGETS: {targets['calories']} cal, {targets['protein']}g P, {targets['carbs']}g C, {targets['fat']}g F",
        f"TODAY SO FAR: {totals['calories']:.0f} cal, {totals['protein']:.0f}g P, {totals['carbs']:.0f}g C, {totals['fat']:.0f}g F | {meal_count} meals",
        f"REMAINING: {remaining_cal:.0f} cal, {remaining_pro:.0f}g protein",
    ]

    if weight.get("has_data"):
        w_line = f"WEIGHT: {weight['current_smoothed']} lbs"
        if weight.get("trend_7d") is not None:
            direction = "down" if weight["trend_7d"] < 0 else "up"
            w_line += f" ({abs(weight['trend_7d']):.1f} {direction} over 7d)"
        lines.append(w_line)

    # Add food suggestions for macro gaps
    food_hints = _get_food_suggestions(targets, totals)
    if food_hints:
        lines.append("")
        lines.append("FOOD OPTIONS TO FILL GAPS:")
        lines.append(food_hints)

    lines.append("")
    lines.append("JUST LOGGED:")
    for r in act_results:
        lines.append(f"  {json.dumps(r, default=str)}")

    return "\n".join(lines)


def _deterministic_confirmation(act_results: list[dict], user_id: int) -> str:
    """Build a fallback confirmation without LLM."""
    from fitnessbot.nutrition import get_nutrition_targets
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    targets_data = get_nutrition_targets(user_id)
    target_pro = targets_data["protein"]

    parts = []
    for r in act_results:
        itype = r.get("intent_type", "")
        action = r.get("action", "")
        if action == "meal_logged":
            item_names = ", ".join(i.get("name", "?") for i in r.get("items", [])[:5])
            parts.append(f"Logged: {item_names} \u2014 {r['total_calories']:.0f} cal, {r['total_protein']:.0f}g protein.")
        elif action == "weight_logged":
            parts.append(f"Logged weight: {r['raw']} {r.get('unit', 'lbs')}. Smoothed: {r['smoothed']} lbs.")
        elif action == "sleep_logged":
            parts.append(f"Logged sleep: {r['hours']}h.")
        elif action == "rhr_logged":
            parts.append(f"Logged resting HR: {r['value']} bpm.")
        elif action == "workout_logged":
            parts.append(f"Logged workout: {r['activity']}" + (f" {r['duration_min']}min." if r.get('duration_min') else "."))
        elif action == "hydration_logged":
            parts.append(f"Logged water: {r['value']} glasses.")
        elif action == "body_fat_logged":
            parts.append(f"Logged body fat: {r['value']}%.")
        elif action == "profile_updated":
            parts.append(f"Updated {r['field']} to {r['value']}.")
        elif action == "correction_applied":
            parts.append(f"Corrected last meal \u2014 now {r['new_calories']:.0f} cal, {r['new_protein']:.0f}g protein.")
        elif action == "plan_set":
            parts.append(f"Added {r['added']} activities to this week's plan.")
        elif action == "plan_completed":
            parts.append(f"\u2713 {r['title']} marked done!")
        elif action == "pb_logged":
            ex = r.get("exercise_name", "").title()
            val = r.get("value", "")
            u = r.get("unit", "")
            if r.get("is_new_record"):
                prev = r.get("previous_best")
                prev_text = f" (previous: {prev}{' ' + u if u else ''})" if prev else ""
                parts.append(f"\U0001F3C6 New PR! {ex}: {val}{' ' + u if u else ''}{prev_text}")
            else:
                parts.append(f"\u2705 Logged {ex}: {val}{' ' + u if u else ''}")
        elif action == "pb_failed":
            parts.append(f"Couldn't log PR: {r.get('note', 'try again')}")
        elif action == "workout_logged_no_plan":
            parts.append(f"Logged {r['activity']}" + (f" ({r['duration_min']}min)" if r.get('duration_min') else "") + ". No matching plan item — want me to add it?")
        elif action == "event_goal_created":
            plan_data = r.get("plan_data", {})
            parts.append(f"🎯 *{r['title']}* — {r['event_date']} ({r['days_out']} days out)")
            plan_summary = format_prep_plan_summary(plan_data)
            if plan_summary:
                parts.append(f"\n{plan_summary}")
            science = plan_data.get("science_notes", "")
            if science:
                parts.append(f"\n📖 The science:\n{science[:500]}")
            parts.append("\nI'll send you daily motivation + check-ins as your event approaches. Ask me \"am I ready?\" anytime for a readiness assessment.")
        elif action == "event_goal_no_date":
            parts.append("I'd love to help you prepare! When is the event? (e.g., 'July 17th' or 'in 30 days')")
        elif action == "readiness_assessed":
            parts.append(r.get("assessment", "Could not assess readiness — need more logged data."))
        elif action == "readiness_no_event":
            parts.append("No active event goals. Tell me about an upcoming event (e.g., 'I have a basketball tournament on July 17th') and I'll build a prep plan.")
        elif action == "parse_failed":
            parts.append("Couldn't parse that meal. Try being more specific.")
        elif action == "query":
            parts.append(_deterministic_query_response(r))
        elif action == "error":
            parts.append(f"Error: {r.get('error', 'unknown')}")
        else:
            parts.append(f"Noted: {itype}.")

    if parts:
        parts.append(f"\nToday: {totals['calories']:.0f} cal | {totals['protein']:.0f}/{target_pro}g protein.")

    return "\n".join(parts) if parts else "Got it."


def _generate_coaching_reply(user_id: int, raw_text: str, act_results: list[dict]) -> tuple[str, dict]:
    """Generate an LLM coaching reply. Returns (reply_text, token_usage)."""
    from fitnessbot.inference.factory import get_inference

    # Use dedicated query path for data questions
    query_results = [r for r in act_results if r.get("action") == "query"]
    if query_results:
        qr = query_results[0]
        digest = _build_query_context(qr)
        system = QUERY_RESPOND_SYSTEM
        prompt = f"User asked: \"{qr.get('question', raw_text)}\"\n\n{digest}"
        fallback_fn = lambda: _deterministic_query_response(qr)
    else:
        digest = _build_context_digest(user_id, act_results)
        system = RESPOND_SYSTEM
        prompt = f"User said: \"{raw_text}\"\n\n{digest}"
        fallback_fn = lambda: _deterministic_confirmation(act_results, user_id)

    try:
        infer = get_inference(user_id)
        result = infer(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400 if query_results else 250,
        )
        return result["text"].strip(), {"input_tokens": result.get("input_tokens", 0), "output_tokens": result.get("output_tokens", 0)}
    except InferenceError:
        return fallback_fn(), {"input_tokens": 0, "output_tokens": 0}
    except Exception as e:
        logger.error("Coaching reply failed: %s", e)
        return fallback_fn(), {"input_tokens": 0, "output_tokens": 0}


# --- main loop ---

async def process_message(user_id: int, text: str, channel: str = "text") -> str:
    """Run the full understand -> act -> respond loop. Returns the reply text."""
    pending = db.get_pending_data_request(user_id)

    # 1. UNDERSTAND
    fast = _fast_path_intents(text, pending)
    total_tokens = {"input_tokens": 0, "output_tokens": 0}

    if fast is not None:
        intents = fast
    else:
        intents, nlu_tokens = _nlu_via_llm(text, user_id, pending)
        total_tokens["input_tokens"] += nlu_tokens.get("input_tokens", 0)
        total_tokens["output_tokens"] += nlu_tokens.get("output_tokens", 0)

    if not intents:
        intents = [{"type": "meal_log", "raw_text": text, "confidence": 0.5}]

    # Check for ambiguity
    if len(intents) == 1 and intents[0].get("ambiguous"):
        clarification = intents[0].get("clarification", "Could you clarify what you mean?")
        category = intents[0].get("metric", intents[0].get("type", "unknown"))
        db.insert_data_request(user_id, category, clarification)
        db.insert_message_log(user_id, channel, transcript=text, detected_intents=json.dumps(intents), response_text=clarification)
        return clarification

    # Handle pending answer
    if pending and intents:
        first = intents[0]
        if first.get("type") in ("health_metric", "meal_log") or _BARE_NUMBER.match(text.strip()):
            db.resolve_data_request(pending["req_id"], text.strip())

    # 2. ACT
    act_results = _act_on_intents(intents, user_id, text)
    writes_json = json.dumps([{"type": r.get("intent_type"), "action": r.get("action")} for r in act_results])

    # 3. RESPOND
    has_real_writes = any(r.get("action", "").endswith("logged") or r.get("action") in ("correction_applied", "profile_updated", "query", "event_goal_created", "readiness_assessed") for r in act_results)

    if has_real_writes:
        reply, resp_tokens = _generate_coaching_reply(user_id, text, act_results)
        total_tokens["input_tokens"] += resp_tokens.get("input_tokens", 0)
        total_tokens["output_tokens"] += resp_tokens.get("output_tokens", 0)
    else:
        reply = _deterministic_confirmation(act_results, user_id)

    # Append dashboard link
    from fitnessbot.config import Config
    reply += f"\n\n[View dashboard]({Config.BASE_URL}/dashboard)"

    # 4. LOG
    try:
        db.insert_message_log(
            user_id, channel,
            transcript=text,
            detected_intents=json.dumps(intents, default=str),
            writes=writes_json,
            response_text=reply[:500],
            tokens_in=total_tokens["input_tokens"],
            tokens_out=total_tokens["output_tokens"],
        )
    except Exception as e:
        logger.warning("Failed to log message: %s", e)

    return reply
