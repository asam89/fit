"""Nutrition targets: adaptive TDEE, macro computation, and AI eating focus."""

import json
import logging
import math
from datetime import datetime, timezone, timedelta

from fitnessbot import db
from fitnessbot.metrics import get_weight_summary
from fitnessbot.tz import user_today, user_date_fmt

logger = logging.getLogger(__name__)

# Activity multipliers for Mifflin-St Jeor
ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "lightly_active": 1.375,
    "moderately_active": 1.55,
    "very_active": 1.725,
    "extra_active": 1.9,
}

# Calorie adjustments per goal type
GOAL_ADJUSTMENTS = {
    "cut": -500,
    "aggressive_cut": -750,
    "maintain": 0,
    "lean_bulk": 250,
    "bulk": 500,
}

CALORIES_PER_LB = 3500


def compute_bmr(sex: str, weight_lbs: float, height_inches: float | None, age_years: int | None) -> float:
    """Mifflin-St Jeor BMR. Falls back to reasonable estimate if height/age missing."""
    weight_kg = weight_lbs * 0.453592
    height_cm = (height_inches * 2.54) if height_inches else 170.0
    age = age_years if age_years else 30

    if sex and sex.lower() in ("male", "m"):
        return 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        return 10 * weight_kg + 6.25 * height_cm - 5 * age - 161


def _get_user_age(user: dict) -> int | None:
    birthdate = user.get("birthdate")
    if not birthdate:
        return None
    try:
        bd = datetime.strptime(birthdate, "%Y-%m-%d")
        today = datetime.now(timezone.utc)
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    except (ValueError, TypeError):
        return None


def _get_user_height_inches(user: dict) -> float | None:
    h = user.get("height")
    if h is None:
        return None
    try:
        return float(h)
    except (ValueError, TypeError):
        return None


def compute_cold_start_tdee(user: dict, weight_lbs: float) -> float:
    """Cold-start TDEE using Mifflin-St Jeor × activity multiplier."""
    sex = user.get("sex", "")
    height = _get_user_height_inches(user)
    age = _get_user_age(user)
    activity = user.get("activity_level", "moderately_active")
    multiplier = ACTIVITY_MULTIPLIERS.get(activity, 1.55)

    bmr = compute_bmr(sex, weight_lbs, height, age)
    return bmr * multiplier


def compute_adaptive_tdee(user_id: int, days: int = 28) -> float | None:
    """Adaptive TDEE from logged intake vs actual weight change.

    Formula: TDEE ≈ avg_daily_intake - (weight_change_lbs × 3500 / days)
    Needs at least 14 days of data to be meaningful.
    """
    cal_history = db.get_calorie_history(user_id, days)
    if len(cal_history) < 14:
        return None

    weight_data = db.get_weight_trend(user_id, limit=days + 5)
    if len(weight_data) < 2:
        return None

    latest_weight = weight_data[0].get("smoothed_weight")
    earliest_weight = None
    actual_days = 0

    for entry in weight_data:
        if not entry.get("smoothed_weight"):
            continue
        days_ago = _days_between(entry["date"], weight_data[0]["date"])
        if days_ago >= 14:
            earliest_weight = entry["smoothed_weight"]
            actual_days = days_ago
            break

    if earliest_weight is None or actual_days < 14 or latest_weight is None:
        return None

    weight_change = latest_weight - earliest_weight
    avg_intake = sum(d["calories"] for d in cal_history) / len(cal_history)

    # TDEE = avg_intake - (change_in_lbs * 3500 / days)
    tdee = avg_intake - (weight_change * CALORIES_PER_LB / actual_days)

    # Sanity bounds
    if tdee < 1200 or tdee > 5000:
        return None

    return round(tdee)


def _days_between(date1: str, date2: str) -> int:
    d1 = datetime.strptime(date1, "%Y-%m-%d")
    d2 = datetime.strptime(date2, "%Y-%m-%d")
    return abs((d2 - d1).days)


def compute_targets(user_id: int) -> dict:
    """Compute full nutrition targets for a user. Single source of truth.

    Returns: {tdee, goal_type, calories, protein, carbs, fat, fiber, eating_focus, method}
    """
    user = db.get_user_by_id(user_id)
    if not user:
        return _default_targets()

    weight_summary = get_weight_summary(user_id)
    current_weight = weight_summary.get("current_smoothed") or weight_summary.get("current_raw")

    if not current_weight:
        return _default_targets()

    # Try adaptive TDEE first, fall back to cold-start
    adaptive = compute_adaptive_tdee(user_id)
    if adaptive:
        tdee = adaptive
        method = "adaptive"
    else:
        tdee = compute_cold_start_tdee(user, current_weight)
        method = "mifflin_st_jeor"

    # Determine goal type from active goal or diet plan
    goal_type = _resolve_goal_type(user_id)
    adjustment = GOAL_ADJUSTMENTS.get(goal_type, 0)
    calorie_target = round(tdee + adjustment)

    # Protein: ~1g per lb of target weight (or current weight if no target)
    goal = db.get_active_goal(user_id)
    target_weight = goal.get("target_weight") if goal and goal.get("target_weight") else current_weight
    protein_target = round(min(target_weight, current_weight * 1.2))

    # Fat: 25% of calories
    fat_cals = calorie_target * 0.25
    fat_target = round(fat_cals / 9)

    # Carbs: remainder
    protein_cals = protein_target * 4
    carbs_cals = calorie_target - protein_cals - fat_cals
    carbs_target = max(round(carbs_cals / 4), 50)

    # Fiber: 14g per 1000 cal
    fiber_target = round(calorie_target * 14 / 1000)

    # Sugar: ~10% of calories from sugar (WHO recommendation)
    sugar_target = round(calorie_target * 0.10 / 4)

    # Sodium: FDA daily limit 2300 mg
    sodium_target = 2300

    return {
        "tdee": round(tdee),
        "goal_type": goal_type,
        "calories": calorie_target,
        "protein": protein_target,
        "carbs": carbs_target,
        "fat": fat_target,
        "fiber": fiber_target,
        "sugar": sugar_target,
        "sodium": sodium_target,
        "method": method,
        "weight_used": current_weight,
    }


def _resolve_goal_type(user_id: int) -> str:
    """Determine goal type from active goal or diet plan."""
    goal = db.get_active_goal(user_id)
    if goal and goal.get("goal_type"):
        gt = goal["goal_type"]
        if gt in GOAL_ADJUSTMENTS:
            return gt
        if gt == "event":
            return "cut"

    plan = db.get_active_diet_plan(user_id)
    if plan:
        cal = plan.get("daily_calories", 0) or 0
        if cal > 0 and cal < 1800:
            return "cut"
        elif cal > 2500:
            return "bulk"

    return "maintain"


def _default_targets() -> dict:
    return {
        "tdee": 2200,
        "goal_type": "maintain",
        "calories": 2200,
        "protein": 140,
        "carbs": 220,
        "fat": 60,
        "fiber": 30,
        "sugar": 55,
        "sodium": 2300,
        "method": "default",
        "weight_used": None,
    }


def get_nutrition_targets(user_id: int) -> dict:
    """Get cached targets or compute fresh ones. Main entry point."""
    cached = db.get_nutrition_targets(user_id)
    if cached:
        computed_at = cached.get("computed_at", "")
        try:
            dt = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age_hours < 24:
                return cached
        except (ValueError, TypeError):
            pass

    targets = compute_targets(user_id)
    db.upsert_nutrition_targets(user_id, targets)
    return targets


def generate_eating_focus(user_id: int, targets: dict, totals: dict) -> str | None:
    """Generate a one-liner AI eating focus based on current status."""
    try:
        from fitnessbot.inference.factory import get_inference
        infer = get_inference(user_id)
    except Exception:
        return _deterministic_focus(targets, totals)

    remaining_cal = targets["calories"] - totals.get("calories", 0)
    remaining_pro = targets["protein"] - totals.get("protein", 0)
    remaining_fat = targets["fat"] - totals.get("fat", 0)

    context = (
        f"Target: {targets['calories']} cal, {targets['protein']}g protein, "
        f"{targets['carbs']}g carbs, {targets['fat']}g fat.\n"
        f"Consumed so far: {totals.get('calories', 0):.0f} cal, "
        f"{totals.get('protein', 0):.0f}g protein, "
        f"{totals.get('carbs', 0):.0f}g carbs, {totals.get('fat', 0):.0f}g fat.\n"
        f"Remaining: {remaining_cal:.0f} cal, {remaining_pro:.0f}g protein.\n"
        f"Goal: {targets['goal_type']}. TDEE: {targets['tdee']} cal ({targets['method']})."
    )

    try:
        result = infer(
            system="You are a concise nutrition coach. Given the user's targets and current intake, write ONE short sentence (max 15 words) about what to focus on for the rest of the day. Be specific and practical. Examples: 'Prioritize protein — grilled chicken or Greek yogurt for dinner.' or 'On track. Keep dinner light and veggie-forward.' Never use emojis.",
            messages=[{"role": "user", "content": context}],
            max_tokens=60,
        )
        return result["text"].strip().rstrip(".")  + "."
    except Exception:
        return _deterministic_focus(targets, totals)


def _deterministic_focus(targets: dict, totals: dict) -> str:
    """Fallback eating focus without AI."""
    remaining_pro = targets["protein"] - totals.get("protein", 0)
    remaining_cal = targets["calories"] - totals.get("calories", 0)
    fat_over = totals.get("fat", 0) - targets["fat"]

    if remaining_pro > 30:
        return f"Prioritize protein — {remaining_pro:.0f}g still to go."
    if fat_over > 10:
        return "Ease up on fats for the rest of the day."
    if remaining_cal < 200 and remaining_cal > -100:
        return "Nearly at target — keep dinner light."
    if remaining_cal < -200:
        return "Over target today. Consider a lighter dinner or extra walk."
    return "On track — keep it balanced."


def build_today_summary(user_id: int) -> dict:
    """Build a rich today summary for the dashboard."""
    today = user_today(user_id)
    totals = db.get_today_totals(user_id, today)
    targets = get_nutrition_targets(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    # Get sleep/workout data for today
    sleep_data = db.get_health_data_today(user_id, today, "sleep")
    workout_data = db.get_health_data_today(user_id, today, "workout")

    # Build prose summary
    day_name = user_date_fmt(user_id, fmt="%A")
    parts = []

    if totals["calories"] == 0 and meal_count == 0:
        prose = f"**{day_name} so far.** Nothing logged yet. Tell me what you ate or use the quick-log."
    else:
        parts.append(f"{totals['calories']:.0f} of {targets['calories']} kcal")

        prot_gap = targets["protein"] - totals["protein"]
        if prot_gap > 5:
            parts.append(f"{totals['protein']:.0f}g protein ({prot_gap:.0f}g to go)")
        else:
            parts.append(f"{totals['protein']:.0f}g protein ✓")

        if meal_count:
            parts.append(f"{meal_count} meal{'s' if meal_count != 1 else ''}")

        if workout_data:
            parts.append("1 workout")

        if sleep_data:
            hours = _extract_sleep_hours(sleep_data)
            if hours:
                parts.append(f"slept {hours:.0f}h")

        if weight.get("has_data"):
            w_part = f"weight {weight['current_smoothed']} lb"
            if weight.get("trend_7d") is not None:
                direction = "down" if weight["trend_7d"] < 0 else "up"
                w_part += f", {abs(weight['trend_7d']):.1f} {direction} this week"
            parts.append(w_part)

        prose = f"**{day_name} so far.** " + " · ".join(parts) + "."

    # Eating focus
    eating_focus = generate_eating_focus(user_id, targets, totals)

    return {
        "prose": prose,
        "totals": totals,
        "targets": targets,
        "weight": weight,
        "meal_count": meal_count,
        "sleep_data": sleep_data,
        "workout_data": workout_data,
        "eating_focus": eating_focus,
    }


def build_month_summary(user_id: int) -> dict:
    """Build a month-to-date summary for the dashboard."""
    now = datetime.now(timezone.utc)
    month_start = now.strftime("%Y-%m-01")
    days_in_month = now.day

    # Calorie history for this month
    cal_history = db.get_calorie_history(user_id, days_in_month)
    targets = get_nutrition_targets(user_id)

    # Averages
    if cal_history:
        avg_cal = sum(d["calories"] for d in cal_history) / len(cal_history)
        avg_protein = sum(d.get("protein", 0) for d in cal_history) / len(cal_history)
        logging_days = len(cal_history)
        days_on_target = sum(1 for d in cal_history if abs(d["calories"] - targets["calories"]) < targets["calories"] * 0.1)
    else:
        avg_cal = 0
        avg_protein = 0
        logging_days = 0
        days_on_target = 0

    # Weight change this month
    weight_data = db.get_weight_trend(user_id, limit=days_in_month + 5)
    weight_change = None
    weight_start = None
    weight_end = None
    if weight_data:
        weight_end = weight_data[0].get("smoothed_weight")
        for entry in weight_data:
            if entry.get("date", "") <= month_start:
                weight_start = entry.get("smoothed_weight")
                break
        if weight_start and weight_end:
            weight_change = weight_end - weight_start

    # Workouts this month
    workouts = db.get_workout_count_range(user_id, days_in_month)

    # Build prose
    month_name = now.strftime("%B")
    parts = []

    if logging_days == 0:
        prose = f"**{month_name} so far.** No data logged this month yet."
    else:
        parts.append(f"avg {avg_cal:.0f} kcal/day (target {targets['calories']})")
        if avg_protein > 0:
            parts.append(f"avg {avg_protein:.0f}g protein")
        parts.append(f"logged {logging_days} of {days_in_month} days")
        if days_on_target > 0:
            parts.append(f"on target {days_on_target} days")

        if weight_change is not None:
            direction = "down" if weight_change < 0 else "up"
            parts.append(f"{abs(weight_change):.1f} lb {direction}")

        if workouts > 0:
            parts.append(f"{workouts} workout{'s' if workouts != 1 else ''}")

        prose = f"**{month_name} so far.** " + " · ".join(parts) + "."

    return {
        "prose": prose,
        "avg_calories": round(avg_cal),
        "avg_protein": round(avg_protein),
        "logging_days": logging_days,
        "days_in_month": days_in_month,
        "days_on_target": days_on_target,
        "weight_change": round(weight_change, 1) if weight_change else None,
        "weight_start": round(weight_start, 1) if weight_start else None,
        "weight_end": round(weight_end, 1) if weight_end else None,
        "workouts": workouts,
        "targets": targets,
    }


def _extract_sleep_hours(sleep_data: list[dict]) -> float | None:
    for entry in sleep_data:
        try:
            data = json.loads(entry.get("data_json", "{}"))
            if "hours" in data:
                return float(data["hours"])
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return None
