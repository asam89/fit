"""Dashboard routes: single-surface home + trends API + quick-log + intake + manual log."""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.metrics import get_weight_summary
from fitnessbot.nutrition import get_nutrition_targets, build_today_summary, build_month_summary
from fitnessbot.web.auth import get_current_user
from fitnessbot.inference.base import InferenceError

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))

RANGE_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}


def _build_gaps(user_id: int, today: str, totals: dict, weight: dict, connection: dict | None) -> list[str]:
    gaps = []
    if not connection:
        gaps.append("Connect Telegram in Settings to start tracking via chat.")
    if totals["calories"] == 0:
        gaps.append("Nothing logged yet today. Tell me what you ate or use the quick-log below.")
    elif db.get_meal_count_today(user_id, today) < 2:
        hour = datetime.now(timezone.utc).hour
        if hour >= 18:
            gaps.append("Only one meal logged. Missed dinner?")
    if not weight.get("has_data"):
        gaps.append("No weight data yet. Log your weight: \"weight 182\"")
    elif weight.get("days_since_last") and weight["days_since_last"] >= 2:
        gaps.append("No weigh-in in 2+ days. Hop on the scale tomorrow morning?")
    return gaps


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(uid, today)
    recent_meals = db.get_recent_meals(uid, limit=5)
    weight = get_weight_summary(uid)
    connection = db.get_telegram_connection(uid)
    active_goal = db.get_active_goal(uid)
    archived_goals = db.get_archived_goals(uid)
    goal_stats = db.get_goal_stats(uid)
    weight_history = db.get_weight_trend_range(uid, 30)
    calorie_history = db.get_calorie_history(uid, 30)
    heatmap_data = db.get_logging_heatmap(uid)
    meal_count = db.get_meal_count_today(uid, today)

    # Single source of truth: nutrition_targets
    targets = get_nutrition_targets(uid)

    pct = {}
    for k in ("calories", "protein", "carbs", "fat"):
        pct[k] = min(100, int(totals[k] / targets[k] * 100)) if targets.get(k) else 0

    remaining_cal = targets["calories"] - totals["calories"]
    gaps = _build_gaps(uid, today, totals, weight, connection)

    today_date = datetime.now(timezone.utc).strftime("%A, %b %d")

    from fitnessbot.inference.factory import get_user_credential
    cred = get_user_credential(uid)
    has_ai = cred is not None

    if not has_ai:
        gaps.insert(0, "Add an API key in Settings \u2192 Connections to enable AI features.")

    # Build rich summaries
    today_summary = build_today_summary(uid)
    month_summary = build_month_summary(uid)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "today_date": today_date,
            "today_summary": today_summary,
            "month_summary": month_summary,
            "totals": totals,
            "targets": targets,
            "pct": pct,
            "remaining_cal": remaining_cal,
            "gaps": gaps,
            "recent_meals": recent_meals,
            "meal_count": meal_count,
            "weight": weight,
            "connection": connection,
            "active_goal": active_goal,
            "archived_goals": archived_goals,
            "goal_stats": goal_stats,
            "weight_history": weight_history,
            "calorie_history": calorie_history,
            "heatmap_data": heatmap_data,
            "has_ai": has_ai,
        },
    )


@router.get("/api/targets")
async def api_targets(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    targets = get_nutrition_targets(user["user_id"])
    return JSONResponse(targets)


@router.post("/api/targets/refresh")
async def api_targets_refresh(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    from fitnessbot.nutrition import compute_targets
    uid = user["user_id"]
    targets = compute_targets(uid)
    db.upsert_nutrition_targets(uid, targets)
    return JSONResponse(targets)


@router.get("/api/trends")
async def api_trends(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    uid = user["user_id"]
    range_name = request.query_params.get("range", "month")
    days = RANGE_DAYS.get(range_name, 30)
    return JSONResponse({
        "weight": db.get_weight_trend_range(uid, days),
        "calories": db.get_calorie_history(uid, days),
    })


@router.post("/dashboard/log")
async def dashboard_quick_log(request: Request, text: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    text = text.strip()
    if not text:
        return RedirectResponse("/dashboard", status_code=303)

    from fitnessbot.router import classify_message
    from fitnessbot.ai.food_parser import parse_meal, log_meal_from_parsed
    from fitnessbot.metrics import log_weight

    uid = user["user_id"]
    result = classify_message(text, user_id=uid)
    intent = result.get("intent", "other")

    if intent == "metric":
        extracted = result.get("extracted", {})
        metric_type = extracted.get("metric_type", "")
        value = extracted.get("value", "")
        if metric_type in ("weight", "weigh"):
            try:
                w = float(value)
                log_weight(user["user_id"], w)
                return RedirectResponse("/dashboard?log=weight_ok", status_code=303)
            except ValueError:
                pass

    units = user.get("units_pref", "imperial")
    items = parse_meal(text, units_pref=units, user_id=uid)
    if items:
        log_meal_from_parsed(user["user_id"], text, items, source="web")
        return RedirectResponse("/dashboard?log=meal_ok", status_code=303)

    return RedirectResponse("/dashboard?log=error", status_code=303)


INTAKE_SYSTEM = """You are a health data intake assistant. Given the user's current data gaps, generate the single most important question to ask next.
Return ONLY a JSON object with these keys:
"question": short, friendly question text
"field": the data field this captures (weight, sleep_hours, sleep_quality, resting_hr, mood, energy, workout_type, workout_duration, hydration)
"input_type": one of "number", "select", "chips"
"options": array of option strings (for select/chips), empty array for number
"unit": unit label (e.g. "lbs", "hours", "bpm"), empty string if none
"placeholder": placeholder text for number inputs
Do not ask about data that is already present."""

INTAKE_FALLBACK = [
    {"question": "What's your weight this morning?", "field": "weight", "input_type": "number", "options": [], "unit": "lbs", "placeholder": "e.g. 182"},
    {"question": "How did you sleep last night?", "field": "sleep_quality", "input_type": "chips", "options": ["Great", "OK", "Poor"], "unit": "", "placeholder": ""},
    {"question": "How many hours did you sleep?", "field": "sleep_hours", "input_type": "number", "options": [], "unit": "hours", "placeholder": "e.g. 7.5"},
    {"question": "Resting heart rate?", "field": "resting_hr", "input_type": "number", "options": [], "unit": "bpm", "placeholder": "e.g. 58"},
    {"question": "How's your energy today?", "field": "energy", "input_type": "chips", "options": ["High", "Normal", "Low"], "unit": "", "placeholder": ""},
    {"question": "Did you work out today?", "field": "workout_type", "input_type": "chips", "options": ["Strength", "Cardio", "Mixed", "Rest day"], "unit": "", "placeholder": ""},
    {"question": "How many glasses of water today?", "field": "hydration", "input_type": "number", "options": [], "unit": "glasses", "placeholder": "e.g. 8"},
]


def _get_present_fields(uid: int, today: str) -> set:
    present = set()
    weight = get_weight_summary(uid)
    if weight.get("has_data") and weight.get("date") == today:
        present.add("weight")
    totals = db.get_today_totals(uid, today)
    if totals["calories"] > 0:
        present.add("meals")
    return present


@router.get("/api/intake/next")
async def intake_next(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    uid = user["user_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    present = _get_present_fields(uid, today)

    try:
        from fitnessbot.inference.factory import get_inference
        infer = get_inference(uid)

        gaps_text = ", ".join(f for f in ["weight", "sleep", "resting_hr", "mood", "energy", "workout", "hydration"] if f not in present)
        result = infer(
            system=INTAKE_SYSTEM,
            messages=[{"role": "user", "content": f"Missing data today: {gaps_text or 'none'}. Today's date: {today}. User timezone: {user.get('timezone', 'America/Toronto')}"}],
            max_tokens=300,
            json_mode=True,
        )
        raw = result["text"]
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        question = json.loads(raw)
        required_keys = {"question", "field", "input_type"}
        if not required_keys.issubset(question.keys()):
            raise ValueError("Missing keys")
        return JSONResponse(question)
    except Exception:
        fallback = [q for q in INTAKE_FALLBACK if q["field"] not in present]
        if fallback:
            return JSONResponse(fallback[0])
        return JSONResponse({"done": True, "message": "All caught up for today!"})


@router.post("/api/intake/answer")
async def intake_answer(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    uid = user["user_id"]
    body = await request.json()
    field = body.get("field", "")
    value = body.get("value", "")

    if not field or not value:
        return JSONResponse({"error": "Missing field or value"}, status_code=400)

    from fitnessbot.metrics import log_weight

    try:
        if field == "weight":
            w = float(value)
            log_weight(uid, w)
        elif field == "sleep_hours":
            hours = float(value)
            db.insert_health_data(uid, "sleep", json.dumps({"hours": hours}), notes=f"Sleep: {hours}h", recorded_at=datetime.now(timezone.utc).isoformat())
        elif field == "sleep_quality":
            db.insert_health_data(uid, "sleep", json.dumps({"quality": value}), notes=f"Sleep quality: {value}", recorded_at=datetime.now(timezone.utc).isoformat())
        elif field == "resting_hr":
            hr = int(float(value))
            db.insert_health_data(uid, "vitals", json.dumps({"resting_hr": hr}), notes=f"Resting HR: {hr} bpm", recorded_at=datetime.now(timezone.utc).isoformat())
        elif field in ("mood", "energy"):
            db.insert_health_data(uid, "wellness", json.dumps({field: value}), notes=f"{field}: {value}", recorded_at=datetime.now(timezone.utc).isoformat())
        elif field == "workout_type":
            if value.lower() != "rest day":
                db.insert_health_data(uid, "workout", json.dumps({"type": value}), notes=f"Workout: {value}", recorded_at=datetime.now(timezone.utc).isoformat())
        elif field == "workout_duration":
            db.insert_health_data(uid, "workout", json.dumps({"duration_min": int(float(value))}), notes=f"Workout: {value} min", recorded_at=datetime.now(timezone.utc).isoformat())
        elif field == "hydration":
            db.insert_health_data(uid, "wellness", json.dumps({"hydration_glasses": int(float(value))}), notes=f"Water: {value} glasses", recorded_at=datetime.now(timezone.utc).isoformat())
        else:
            return JSONResponse({"error": f"Unknown field: {field}"}, status_code=400)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": f"Invalid value: {e}"}, status_code=400)

    return JSONResponse({"ok": True, "field": field, "value": value})


@router.post("/dashboard/log/weight")
async def log_weight_manual(request: Request, weight_val: str = Form(...), weight_unit: str = Form("lbs")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from fitnessbot.metrics import log_weight
    try:
        w = float(weight_val.strip())
        log_weight(user["user_id"], w, weight_unit=weight_unit)
        return RedirectResponse("/dashboard?log=weight_ok", status_code=303)
    except ValueError:
        return RedirectResponse("/dashboard?log=error", status_code=303)


@router.post("/dashboard/log/sleep")
async def log_sleep_manual(request: Request, sleep_hours: str = Form(""), sleep_quality: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["user_id"]
    data = {}
    if sleep_hours.strip():
        try:
            data["hours"] = float(sleep_hours.strip())
        except ValueError:
            pass
    if sleep_quality.strip():
        data["quality"] = sleep_quality.strip()
    if data:
        db.insert_health_data(uid, "sleep", json.dumps(data), notes=f"Sleep: {data}", recorded_at=datetime.now(timezone.utc).isoformat())
    return RedirectResponse("/dashboard?log=sleep_ok", status_code=303)


@router.post("/dashboard/log/workout")
async def log_workout_manual(request: Request, workout_type: str = Form(""), workout_duration: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["user_id"]
    data = {}
    if workout_type.strip():
        data["type"] = workout_type.strip()
    if workout_duration.strip():
        try:
            data["duration_min"] = int(float(workout_duration.strip()))
        except ValueError:
            pass
    if data:
        db.insert_health_data(uid, "workout", json.dumps(data), notes=f"Workout: {data}", recorded_at=datetime.now(timezone.utc).isoformat())
    return RedirectResponse("/dashboard?log=workout_ok", status_code=303)


@router.post("/dashboard/log/vitals")
async def log_vitals_manual(request: Request, resting_hr: str = Form(""), blood_pressure: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["user_id"]
    data = {}
    if resting_hr.strip():
        try:
            data["resting_hr"] = int(float(resting_hr.strip()))
        except ValueError:
            pass
    if blood_pressure.strip():
        data["blood_pressure"] = blood_pressure.strip()
    if data:
        db.insert_health_data(uid, "vitals", json.dumps(data), notes=f"Vitals: {data}", recorded_at=datetime.now(timezone.utc).isoformat())
    return RedirectResponse("/dashboard?log=vitals_ok", status_code=303)
