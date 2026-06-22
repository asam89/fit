"""Conversational engine: understand -> act -> respond loop for Telegram messages."""

import json
import logging
import re
import time
from datetime import datetime, timezone

from fitnessbot import db
from fitnessbot.ai.food_parser import parse_meal, log_meal_from_parsed
from fitnessbot.metrics import log_weight, get_weight_summary
from fitnessbot.inference.base import InferenceError

logger = logging.getLogger(__name__)

# --- NLU prompts ---

NLU_SYSTEM = """You are an intent classifier for a fitness tracking bot. Given a user message, extract ALL intents and structured data.

Return ONLY a JSON object with key "intents" — an array of objects, each with:
- "type": one of meal_log, health_metric, workout_log, profile_update, goal_update, query, correction, general
- "confidence": 0.0-1.0
- Fields specific to the type:
  - meal_log: "items" (array of {name, qty, unit}), "meal_type" (breakfast/lunch/dinner/snack), "when" (now/this_morning/last_night)
  - health_metric: "metric" (weight/sleep_hours/sleep_quality/resting_hr/hrv/bp/spo2/body_fat/mood/energy/hydration), "value", "unit", "when"
  - workout_log: "activity" (strength/cardio/mixed/yoga/etc), "duration_min", "notes"
  - profile_update: "field" (age/height/sex/units/activity_level), "value"
  - goal_update: "goal_type" (lose/gain/maintain), "target_weight", "target_date"
  - query: "question"
  - correction: "what" (description of what to fix), "new_value"
  - general: "text"

Multi-data messages should produce multiple intents. Be concise.
If confidence < 0.6, set "ambiguous": true and "clarification": "short question to ask".
"""

RESPOND_SYSTEM = """You are a fitness coaching assistant. Given the user's context (targets, today's totals, what was just logged, weight trend), write a SHORT reply (2-4 lines max).

Rules:
- First confirm what was logged (use the EXACT numbers provided in context, never invent numbers)
- Then add ONE practical, specific focus point about diet or training
- For meals: reference remaining macros and what to prioritize next
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
            elif itype == "query":
                r = _act_query(user_id)
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


def _act_query(user_id: int) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    plan = db.get_active_diet_plan(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    targets = {
        "calories": plan["daily_calories"] if plan and plan.get("daily_calories") else 2000,
        "protein": plan["daily_protein"] if plan and plan.get("daily_protein") else 140,
        "carbs": plan["daily_carbs"] if plan and plan.get("daily_carbs") else 200,
        "fat": plan["daily_fat"] if plan and plan.get("daily_fat") else 60,
    }

    return {
        "action": "query",
        "totals": totals,
        "targets": targets,
        "weight": weight,
        "meal_count": meal_count,
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

def _build_context_digest(user_id: int, act_results: list[dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    plan = db.get_active_diet_plan(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    targets = {
        "calories": plan["daily_calories"] if plan and plan.get("daily_calories") else 2000,
        "protein": plan["daily_protein"] if plan and plan.get("daily_protein") else 140,
        "carbs": plan["daily_carbs"] if plan and plan.get("daily_carbs") else 200,
        "fat": plan["daily_fat"] if plan and plan.get("daily_fat") else 60,
    }

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

    lines.append("")
    lines.append("JUST LOGGED:")
    for r in act_results:
        lines.append(f"  {json.dumps(r, default=str)}")

    return "\n".join(lines)


def _deterministic_confirmation(act_results: list[dict], user_id: int) -> str:
    """Build a fallback confirmation without LLM."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    plan = db.get_active_diet_plan(user_id)
    target_pro = plan["daily_protein"] if plan and plan.get("daily_protein") else 140

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
        elif action == "parse_failed":
            parts.append("Couldn't parse that meal. Try being more specific.")
        elif action == "query":
            t = r["totals"]
            tg = r["targets"]
            parts.append(f"{t['calories']:.0f}/{tg['calories']} cal, {t['protein']:.0f}/{tg['protein']}g P, {t['carbs']:.0f}/{tg['carbs']}g C, {t['fat']:.0f}/{tg['fat']}g F.")
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

    digest = _build_context_digest(user_id, act_results)

    try:
        infer = get_inference(user_id)
        result = infer(
            system=RESPOND_SYSTEM,
            messages=[{"role": "user", "content": f"User said: \"{raw_text}\"\n\n{digest}"}],
            max_tokens=250,
        )
        return result["text"].strip(), {"input_tokens": result.get("input_tokens", 0), "output_tokens": result.get("output_tokens", 0)}
    except InferenceError:
        return _deterministic_confirmation(act_results, user_id), {"input_tokens": 0, "output_tokens": 0}
    except Exception as e:
        logger.error("Coaching reply failed: %s", e)
        return _deterministic_confirmation(act_results, user_id), {"input_tokens": 0, "output_tokens": 0}


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
    has_real_writes = any(r.get("action", "").endswith("logged") or r.get("action") in ("correction_applied", "profile_updated", "query") for r in act_results)

    if has_real_writes:
        reply, resp_tokens = _generate_coaching_reply(user_id, text, act_results)
        total_tokens["input_tokens"] += resp_tokens.get("input_tokens", 0)
        total_tokens["output_tokens"] += resp_tokens.get("output_tokens", 0)
    else:
        reply = _deterministic_confirmation(act_results, user_id)

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
