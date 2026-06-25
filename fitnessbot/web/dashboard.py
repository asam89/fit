"""Dashboard routes: single-surface home + trends API + quick-log + intake + manual log."""

import json
import logging
import secrets
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
from fitnessbot.tz import user_today, user_date_fmt, user_hour, user_now, _tz, day_utc_range, utc_offset_hours

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


def _localize_meals(meals: list[dict], tz_str: str) -> list[dict]:
    """Convert logged_at from UTC to user's local timezone for display."""
    tz = _tz(tz_str)
    for meal in meals:
        ts = meal.get("logged_at")
        if ts and isinstance(ts, str) and len(ts) >= 16:
            try:
                utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if utc_dt.tzinfo is None:
                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                local_dt = utc_dt.astimezone(tz)
                meal["logged_at"] = local_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
    return meals

RANGE_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}


def _build_gaps(user_id: int, today: str, totals: dict, weight: dict, connection: dict | None, *, utc_range: tuple[str, str] | None = None) -> list[dict]:
    gaps = []
    if not connection:
        gaps.append({
            "text": "Connect Telegram to start tracking via chat.",
            "link": "/settings",
            "link_text": "Go to Settings",
            "icon": "telegram",
        })
    if totals["calories"] == 0:
        gaps.append({
            "text": "Nothing logged yet today. Tell me what you ate or use the quick-log below.",
            "link": "#log",
            "link_text": "Quick Log",
            "icon": "meal",
        })
    elif db.get_meal_count_today(user_id, today, utc_range=utc_range) < 2:
        hour = user_hour(user_id)
        if hour >= 18:
            gaps.append({
                "text": "Only one meal logged. Missed dinner?",
                "link": "#log",
                "link_text": "Log Now",
                "icon": "meal",
            })
    if not weight.get("has_data"):
        gaps.append({
            "text": "No weight data yet. Log your first weigh-in.",
            "link": "#logdata",
            "link_text": "Log Weight",
            "icon": "weight",
        })
    elif weight.get("days_since_last") and weight["days_since_last"] >= 2:
        gaps.append({
            "text": "No weigh-in in 2+ days. Hop on the scale tomorrow morning?",
            "link": "#logdata",
            "link_text": "Log Weight",
            "icon": "weight",
        })
    return gaps


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    tz_str = user.get("timezone", "America/Toronto")
    today = user_today(uid, tz_str=tz_str)
    urange = day_utc_range(today, uid, tz_str=tz_str)
    tz_offset = utc_offset_hours(uid, tz_str=tz_str)
    totals = db.get_today_totals(uid, today, utc_range=urange)
    recent_meals = _localize_meals(db.get_recent_meals(uid, limit=5), tz_str)
    today_meals = _localize_meals(db.get_meals_by_date(uid, today, utc_range=urange), tz_str)
    for meal in today_meals:
        meal["items"] = db.get_meal_items(meal["meal_id"])
    meal_dates = db.get_meal_dates_with_counts(uid, limit=7, utc_offset_hours=tz_offset)
    weight = get_weight_summary(uid)
    connection = db.get_telegram_connection(uid)
    active_goal = db.get_active_goal(uid)
    archived_goals = db.get_archived_goals(uid)
    goal_stats = db.get_goal_stats(uid)
    weight_history = db.get_weight_trend_range(uid, 30)
    calorie_history = db.get_calorie_history(uid, 30, utc_offset_hours=tz_offset)
    heatmap_data = db.get_logging_heatmap(uid, utc_offset_hours=tz_offset)
    meal_count = db.get_meal_count_today(uid, today, utc_range=urange)

    # Single source of truth: nutrition_targets
    targets = get_nutrition_targets(uid)

    pct = {}
    for k in ("calories", "protein", "carbs", "fat"):
        pct[k] = min(100, int(totals[k] / targets[k] * 100)) if targets.get(k) else 0
    # New macro percentages with sensible defaults
    fiber_target = targets.get("fiber", 25) or 25
    sugar_target = targets.get("sugar", 50) or 50
    sodium_target = targets.get("sodium", 2300) or 2300
    pct["fiber"] = min(100, int((totals.get("fiber", 0) or 0) / fiber_target * 100))
    pct["sugar"] = min(100, int((totals.get("sugar", 0) or 0) / sugar_target * 100))
    pct["sodium"] = min(100, int((totals.get("sodium", 0) or 0) / sodium_target * 100))

    remaining_cal = targets["calories"] - totals["calories"]
    gaps = _build_gaps(uid, today, totals, weight, connection, utc_range=urange)

    today_date = user_date_fmt(uid, tz_str=tz_str)

    from fitnessbot.inference.factory import get_user_credential
    cred = get_user_credential(uid)
    has_ai = cred is not None

    if not has_ai:
        gaps.insert(0, {
            "text": "Add an API key to enable AI features (meal analysis, coaching, insights).",
            "link": "/settings",
            "link_text": "Go to Settings",
            "icon": "key",
        })

    # Build rich summaries
    today_summary = build_today_summary(uid)
    month_summary = build_month_summary(uid)
    weight_goal = db.get_weight_goal(uid)

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
            "today_meals": today_meals,
            "meal_dates": meal_dates,
            "meal_count": meal_count,
            "weight": weight,
            "weight_goal": weight_goal,
            "connection": connection,
            "active_goal": active_goal,
            "archived_goals": archived_goals,
            "goal_stats": goal_stats,
            "weight_history": weight_history,
            "calorie_history": calorie_history,
            "heatmap_data": heatmap_data,
            "has_ai": has_ai,
            "user_tz": tz_str,
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


@router.post("/api/targets/set")
async def api_targets_set(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    uid = user["user_id"]
    data = await request.json()
    targets = {
        "tdee": data.get("calories", 2000),
        "calories": data.get("calories", 2000),
        "protein": data.get("protein", 150),
        "carbs": data.get("carbs", 200),
        "fat": data.get("fat", 65),
        "fiber": data.get("fiber", 30),
        "goal_type": data.get("goal_type", "maintain"),
        "method": "manual",
    }
    db.upsert_nutrition_targets(uid, targets)
    if data.get("weight_goal") is not None:
        db.set_weight_goal(uid, data["weight_goal"])
    return JSONResponse({"ok": True, **targets})


@router.get("/api/weight-goal")
async def api_weight_goal(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    goal = db.get_weight_goal(user["user_id"])
    return JSONResponse({"weight_goal": goal})


@router.get("/api/trends")
async def api_trends(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    uid = user["user_id"]
    tz_str = user.get("timezone", "America/Toronto")
    tz_offset = utc_offset_hours(uid, tz_str=tz_str)
    range_name = request.query_params.get("range", "month")
    days = RANGE_DAYS.get(range_name, 30)
    return JSONResponse({
        "weight": db.get_weight_trend_range(uid, days),
        "calories": db.get_calorie_history(uid, days, utc_offset_hours=tz_offset),
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


def _get_present_fields(uid: int, today: str, *, utc_range: tuple[str, str] | None = None) -> set:
    present = set()
    weight = get_weight_summary(uid)
    if weight.get("has_data") and weight.get("date") == today:
        present.add("weight")
    totals = db.get_today_totals(uid, today, utc_range=utc_range)
    if totals["calories"] > 0:
        present.add("meals")
    return present


@router.get("/api/intake/next")
async def intake_next(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    uid = user["user_id"]
    tz_str = user.get("timezone", "America/Toronto")
    today = user_today(uid, tz_str=tz_str)
    urange = day_utc_range(today, uid, tz_str=tz_str)
    present = _get_present_fields(uid, today, utc_range=urange)

    try:
        from fitnessbot.inference.factory import get_inference
        infer = get_inference(uid)

        gaps_text = ", ".join(f for f in ["weight", "sleep", "resting_hr", "mood", "energy", "workout", "hydration"] if f not in present)
        result = infer(
            system=INTAKE_SYSTEM,
            messages=[{"role": "user", "content": f"Missing data today: {gaps_text or 'none'}. Today's date: {today}. User timezone: {tz_str}"}],
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


@router.post("/api/send-summary")
async def send_summary_to_telegram(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    uid = user["user_id"]
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    summary_type = body.get("type", "today")

    from fitnessbot.briefings import build_morning_brief, build_evening_wrap, build_weekly_rollup, _send_telegram

    if summary_type == "today":
        text = build_evening_wrap(uid)
    elif summary_type == "morning":
        text = build_morning_brief(uid)
    elif summary_type == "weekly":
        text = build_weekly_rollup(uid)
    else:
        text = build_evening_wrap(uid)

    sent = await _send_telegram(uid, text)
    if sent:
        return JSONResponse({"ok": True, "message": "Summary sent to Telegram"})
    return JSONResponse({"ok": False, "error": "Could not send — check Telegram connection in Settings"}, status_code=400)


@router.post("/api/invite")
async def create_invite(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    code = secrets.token_urlsafe(12)
    db.create_invite_link(user["user_id"], code)
    link = f"{Config.BASE_URL}/register?invite={code}"
    return JSONResponse({"ok": True, "link": link, "code": code})


# --- Food Diary ---

@router.get("/diary", response_class=HTMLResponse)
async def food_diary(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["user_id"]
    date_str = request.query_params.get("date", "")
    if not date_str:
        date_str = user_today(uid)
    tz_str = user.get("timezone", "America/Toronto")
    urange = day_utc_range(date_str, uid, tz_str=tz_str)
    tz_offset = utc_offset_hours(uid, tz_str=tz_str)
    meals = _localize_meals(db.get_meals_by_date(uid, date_str, utc_range=urange), tz_str)
    meal_dates = db.get_meal_dates_with_counts(uid, limit=60, utc_offset_hours=tz_offset)
    for meal in meals:
        meal["items"] = db.get_meal_items(meal["meal_id"])
    return templates.TemplateResponse(
        "diary.html",
        {
            "request": request,
            "user": user,
            "current_date": date_str,
            "meals": meals,
            "meal_dates": meal_dates,
        },
    )


@router.get("/api/meals/by-date")
async def api_meals_by_date(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    uid = user["user_id"]
    date_str = request.query_params.get("date", "")
    if not date_str:
        date_str = user_today(uid)
    tz_str = user.get("timezone", "America/Toronto")
    urange = day_utc_range(date_str, uid, tz_str=tz_str)
    meals = _localize_meals(db.get_meals_by_date(uid, date_str, utc_range=urange), tz_str)
    for meal in meals:
        meal["items"] = db.get_meal_items(meal["meal_id"])
    total_cal = sum(m.get("total_calories", 0) or 0 for m in meals)
    total_protein = sum(m.get("total_protein", 0) or 0 for m in meals)
    total_carbs = sum(m.get("total_carbs", 0) or 0 for m in meals)
    total_fat = sum(m.get("total_fat", 0) or 0 for m in meals)
    return JSONResponse({
        "ok": True,
        "date": date_str,
        "meals": meals,
        "totals": {"calories": total_cal, "protein": total_protein, "carbs": total_carbs, "fat": total_fat},
    })


# --- Personal Bests ---

@router.get("/api/personal-bests")
async def get_personal_bests(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    pbs = db.get_top_personal_bests(user["user_id"])
    return JSONResponse({"ok": True, "personal_bests": pbs})


@router.post("/api/personal-bests")
async def create_personal_best(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    exercise = data.get("exercise_name", "").strip()
    value = data.get("value")
    unit = data.get("unit", "")
    notes = data.get("notes", "")
    if not exercise or value is None:
        return JSONResponse({"error": "exercise_name and value required"}, status_code=400)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return JSONResponse({"error": "value must be a number"}, status_code=400)
    pb_id = db.insert_personal_best(user["user_id"], exercise, value, unit, notes)
    return JSONResponse({"ok": True, "pb_id": pb_id})


@router.delete("/api/personal-bests/{pb_id}")
async def delete_personal_best(pb_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    deleted = db.delete_personal_best(pb_id, user["user_id"])
    if not deleted:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/meals/{meal_id}/delete")
async def api_delete_meal(meal_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    deleted = db.delete_meal_by_id(meal_id, user["user_id"])
    if not deleted:
        return JSONResponse({"error": "Meal not found"}, status_code=404)
    return JSONResponse({"ok": True, "deleted": deleted})


class MealTypeUpdate(BaseModel):
    meal_type: str


@router.patch("/api/meals/{meal_id}/type")
async def api_update_meal_type(meal_id: int, body: MealTypeUpdate, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    valid_types = {"breakfast", "lunch", "snack", "dinner", "midnight snack"}
    if body.meal_type.lower() not in valid_types:
        return JSONResponse({"error": "Invalid meal type"}, status_code=400)
    updated = db.update_meal_type(meal_id, user["user_id"], body.meal_type.lower())
    if not updated:
        return JSONResponse({"error": "Meal not found"}, status_code=404)
    return JSONResponse({"ok": True, "meal_type": body.meal_type.lower()})


@router.post("/api/timezone")
async def update_timezone(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    tz_str = data.get("timezone", "").strip()
    if not tz_str:
        return JSONResponse({"error": "timezone required"}, status_code=400)
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(tz_str)
    except Exception:
        return JSONResponse({"error": "Invalid timezone"}, status_code=400)
    db.update_user(user["user_id"], timezone=tz_str)
    now = user_now(tz_str=tz_str)
    today = now.strftime("%Y-%m-%d")
    return JSONResponse({
        "ok": True,
        "timezone": tz_str,
        "local_time": now.strftime("%I:%M %p"),
        "today": today,
    })
