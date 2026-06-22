"""Dashboard routes: single-surface home + trends API + quick-log."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.metrics import get_weight_summary
from fitnessbot.web.auth import get_current_user

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


def _build_snapshot(totals: dict, targets: dict, meal_count: int, weight: dict) -> str:
    if totals["calories"] == 0 and meal_count == 0:
        return "Nothing logged yet today. Tell me what you ate or tap a quick-log to get your numbers moving."
    remaining = targets["calories"] - totals["calories"]
    parts = [f"{totals['calories']:.0f} of {targets['calories']} kcal"]
    prot_gap = targets["protein"] - totals["protein"]
    if prot_gap > 5:
        parts.append(f"{totals['protein']:.0f}g protein ({prot_gap:.0f}g to go)")
    else:
        parts.append(f"{totals['protein']:.0f}g protein")
    if meal_count:
        parts.append(f"{meal_count} meal{'s' if meal_count != 1 else ''} logged")
    if weight.get("has_data"):
        parts.append(f"weight {weight['current_smoothed']} lbs")
        if weight.get("trend_7d") is not None:
            direction = "down" if weight["trend_7d"] < 0 else "up"
            parts.append(f"{abs(weight['trend_7d']):.1f} {direction} this week")
    return " · ".join(parts)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(uid, today)
    plan = db.get_active_diet_plan(uid)
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

    targets = {
        "calories": plan["daily_calories"] if plan and plan.get("daily_calories") else 2000,
        "protein": plan["daily_protein"] if plan and plan.get("daily_protein") else 140,
        "carbs": plan["daily_carbs"] if plan and plan.get("daily_carbs") else 200,
        "fat": plan["daily_fat"] if plan and plan.get("daily_fat") else 60,
    }

    pct = {}
    for k in targets:
        pct[k] = min(100, int(totals[k] / targets[k] * 100)) if targets[k] else 0

    remaining_cal = targets["calories"] - totals["calories"]
    gaps = _build_gaps(uid, today, totals, weight, connection)
    snapshot_text = _build_snapshot(totals, targets, meal_count, weight)

    today_date = datetime.now(timezone.utc).strftime("%A, %b %d")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "today_date": today_date,
            "snapshot_text": snapshot_text,
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
        },
    )


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

    result = classify_message(text)
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
    items = parse_meal(text, units_pref=units)
    if items:
        log_meal_from_parsed(user["user_id"], text, items, source="web")
        return RedirectResponse("/dashboard?log=meal_ok", status_code=303)

    return RedirectResponse("/dashboard?log=error", status_code=303)
