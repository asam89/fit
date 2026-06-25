"""Nutrition targets: adaptive TDEE, macro computation, and AI eating focus."""

import json
import logging
import math
from datetime import datetime, timezone, timedelta

from fitnessbot import db
from fitnessbot.metrics import get_weight_summary
from fitnessbot.tz import user_today, user_date_fmt, day_utc_range, utc_offset_hours as _utc_off

logger = logging.getLogger(__name__)

# Activity multipliers for Mifflin-St Jeor
ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "lightly_active": 1.375,
    "moderately_active": 1.55,
    "very_active": 1.725,
    "extra_active": 1.9,
}

# Calorie adjustments per goal type (capped at +/-500 per spec)
GOAL_ADJUSTMENTS = {
    "mild_loss": -250,
    "cut": -500,
    "aggressive_cut": -500,  # capped; was -750
    "maintain": 0,
    "mild_gain": 250,
    "lean_bulk": 250,
    "bulk": 500,
}

# Macro preset splits: (protein%, carbs%, fat%) of total calories
MACRO_PRESETS = {
    "balanced": (0.30, 0.40, 0.30),
    "high_protein": (0.40, 0.30, 0.30),
    "low_carb": (0.35, 0.20, 0.45),
}

# Safety floors (kcal)
SAFETY_FLOOR = {"male": 1500, "female": 1200}
DEFAULT_SAFETY_FLOOR = 1200

CALORIES_PER_LB = 3500


# --- Input validation helpers ---

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def validate_age(age: int | None) -> int:
    if age is None:
        return 30
    return int(clamp(age, 14, 100))


def validate_weight_kg(weight_kg: float) -> float:
    return clamp(weight_kg, 30.0, 300.0)


def validate_height_cm(height_cm: float | None) -> float:
    if height_cm is None:
        return 170.0
    return clamp(height_cm, 120.0, 230.0)


def lbs_to_kg(lbs: float) -> float:
    return lbs * 0.453592


def inches_to_cm(inches: float) -> float:
    return inches * 2.54


# --- Pure calculation functions ---

def compute_bmr(sex: str, weight_kg: float, height_cm: float | None, age_years: int | None) -> float:
    """Mifflin-St Jeor BMR (pure function).

    Args:
        sex: 'male'/'m' or 'female'/'f'
        weight_kg: body weight in kilograms
        height_cm: height in centimeters (None → 170 default)
        age_years: age in years (None → 30 default)
    """
    wkg = validate_weight_kg(weight_kg)
    hcm = validate_height_cm(height_cm)
    age = validate_age(age_years)

    if sex and sex.lower() in ("male", "m"):
        return 10 * wkg + 6.25 * hcm - 5 * age + 5
    else:
        return 10 * wkg + 6.25 * hcm - 5 * age - 161


def compute_tdee(bmr: float, activity_level: str) -> float:
    """TDEE = BMR * activity multiplier (pure function)."""
    multiplier = ACTIVITY_MULTIPLIERS.get(activity_level, 1.55)
    return bmr * multiplier


def apply_goal(tdee: float, goal_type: str) -> float:
    """Apply goal adjustment to TDEE (pure function)."""
    delta = GOAL_ADJUSTMENTS.get(goal_type, 0)
    return tdee + delta


def apply_safety_floor(calories: float, sex: str) -> tuple[float, bool]:
    """Clamp calories to safety floor. Returns (clamped_cal, was_clamped)."""
    sex_key = "male" if sex and sex.lower() in ("male", "m") else "female"
    floor = SAFETY_FLOOR.get(sex_key, DEFAULT_SAFETY_FLOOR)
    if calories < floor:
        return float(floor), True
    return calories, False


def derive_macro_targets(
    calorie_target: float,
    weight_kg: float,
    sex: str,
    goal_type: str = "maintain",
    preset: str | None = None,
) -> dict:
    """Derive macro targets from calorie target and body weight (pure function).

    Hierarchy: protein first (g/kg), fat second (25% or min 0.6 g/kg), carbs fill remainder.
    If preset is given, use percentage-based split instead.
    """
    wkg = validate_weight_kg(weight_kg)

    if preset and preset in MACRO_PRESETS:
        prot_pct, carb_pct, fat_pct = MACRO_PRESETS[preset]
        protein = round(calorie_target * prot_pct / 4)
        carbs = round(calorie_target * carb_pct / 4)
        fat = round(calorie_target * fat_pct / 9)
    else:
        # Protein: 1.6-2.2 g/kg based on goal
        if goal_type in ("bulk", "lean_bulk", "mild_gain"):
            prot_per_kg = 2.0
        elif goal_type in ("cut", "aggressive_cut", "mild_loss"):
            prot_per_kg = 2.0  # higher during cut to preserve muscle
        else:
            prot_per_kg = 1.8  # maintain
        protein = round(wkg * prot_per_kg)

        # Fat: 25% of calories, but at least 0.6 g/kg
        fat_from_pct = calorie_target * 0.25 / 9
        fat_floor = wkg * 0.6
        fat = round(max(fat_from_pct, fat_floor))

        # Carbs: fill remainder
        protein_cals = protein * 4
        fat_cals = fat * 9
        carbs_cals = calorie_target - protein_cals - fat_cals
        carbs = max(round(carbs_cals / 4), 50)

    # Fiber: 14g per 1000 kcal, floor 25g (women) / 38g (men)
    fiber_base = round(calorie_target * 14 / 1000)
    if sex and sex.lower() in ("male", "m"):
        fiber = max(fiber_base, 38)
    else:
        fiber = max(fiber_base, 25)

    # Sugar: ~10% of calories (WHO)
    sugar = round(calorie_target * 0.10 / 4)

    # Sodium: FDA 2300 mg
    sodium = 2300

    # Water: 35 mL/kg bodyweight
    water_ml = round(wkg * 35)

    return {
        "protein": protein,
        "carbs": carbs,
        "fat": fat,
        "fiber": fiber,
        "sugar": sugar,
        "sodium": sodium,
        "water_ml": water_ml,
    }


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


def compute_cold_start_tdee(user: dict, weight_lbs: float) -> tuple[float, float]:
    """Cold-start TDEE using Mifflin-St Jeor * activity multiplier.

    Returns (tdee, bmr).
    """
    sex = user.get("sex", "")
    height_in = _get_user_height_inches(user)
    height_cm = inches_to_cm(height_in) if height_in else None
    age = _get_user_age(user)
    weight_kg = lbs_to_kg(weight_lbs)
    activity = user.get("activity_level", "moderately_active")

    bmr = compute_bmr(sex, weight_kg, height_cm, age)
    tdee = compute_tdee(bmr, activity)
    return tdee, bmr


def compute_adaptive_tdee(user_id: int, days: int = 28) -> float | None:
    """Adaptive TDEE from logged intake vs actual weight change.

    Formula: TDEE ≈ avg_daily_intake - (weight_change_lbs × 3500 / days)
    Needs at least 14 days of data to be meaningful.
    """
    cal_history = db.get_calorie_history(user_id, days, utc_offset_hours=_utc_off(user_id))
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

    sex = user.get("sex", "")
    weight_kg = lbs_to_kg(current_weight)

    # Try adaptive TDEE first, fall back to cold-start
    adaptive = compute_adaptive_tdee(user_id)
    if adaptive:
        tdee = adaptive
        bmr_val = compute_bmr(
            sex,
            weight_kg,
            inches_to_cm(_get_user_height_inches(user)) if _get_user_height_inches(user) else None,
            _get_user_age(user),
        )
        method = "adaptive"
    else:
        tdee, bmr_val = compute_cold_start_tdee(user, current_weight)
        method = "mifflin_st_jeor"

    # Determine goal type from active goal or diet plan
    goal_type = _resolve_goal_type(user_id)
    calorie_target = round(apply_goal(tdee, goal_type))
    goal_delta = GOAL_ADJUSTMENTS.get(goal_type, 0)

    # Safety floor
    calorie_target, floor_applied = apply_safety_floor(calorie_target, sex)
    calorie_target = int(calorie_target)

    # Round calories to nearest 5 for display cleanliness
    calorie_target = round(calorie_target / 5) * 5

    # Get macro preset from user profile (if any)
    macro_preset = user.get("macro_preset")

    # Derive macros
    macros = derive_macro_targets(calorie_target, weight_kg, sex, goal_type, macro_preset)

    return {
        "tdee": round(tdee / 5) * 5,
        "bmr": round(bmr_val / 5) * 5,
        "goal_type": goal_type,
        "goal_delta": goal_delta,
        "calories": calorie_target,
        "protein": macros["protein"],
        "carbs": macros["carbs"],
        "fat": macros["fat"],
        "fiber": macros["fiber"],
        "sugar": macros["sugar"],
        "sodium": macros["sodium"],
        "water_ml": macros["water_ml"],
        "method": method,
        "weight_used": current_weight,
        "floor_applied": floor_applied,
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
        "bmr": 1700,
        "goal_type": "maintain",
        "goal_delta": 0,
        "calories": 2200,
        "protein": 140,
        "carbs": 220,
        "fat": 60,
        "fiber": 30,
        "sugar": 55,
        "sodium": 2300,
        "water_ml": 2800,
        "method": "default",
        "weight_used": None,
        "floor_applied": False,
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
    urange = day_utc_range(today, user_id)
    totals = db.get_today_totals(user_id, today, utc_range=urange)
    targets = get_nutrition_targets(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today, utc_range=urange)

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
    cal_history = db.get_calorie_history(user_id, days_in_month, utc_offset_hours=_utc_off(user_id))
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
