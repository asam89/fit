"""Health benefit calculations for physical activities.

Uses MET (Metabolic Equivalent of Task) values to estimate calories burned
and categorizes activities by health benefit type (cardiovascular, strength,
flexibility, etc.) with muscle group mapping.
"""

import json
import logging
from datetime import date, timedelta

from fitnessbot import db

logger = logging.getLogger(__name__)

# MET values sourced from the Compendium of Physical Activities.
# Format: activity_keyword -> (MET_value, primary_benefit, muscle_groups)
# MET is the ratio of work metabolic rate to resting metabolic rate.
# Calories/min = MET * 3.5 * body_weight_kg / 200
ACTIVITY_MET_MAP: dict[str, tuple[float, str, list[str]]] = {
    # Strength training
    "strength": (5.0, "muscle_building", ["full body"]),
    "weights": (6.0, "muscle_building", ["full body"]),
    "weightlifting": (6.0, "muscle_building", ["full body"]),
    "legs": (6.0, "muscle_building", ["quadriceps", "hamstrings", "glutes", "calves"]),
    "leg": (6.0, "muscle_building", ["quadriceps", "hamstrings", "glutes", "calves"]),
    "upper body": (5.0, "muscle_building", ["chest", "shoulders", "triceps", "biceps"]),
    "push": (5.5, "muscle_building", ["chest", "shoulders", "triceps"]),
    "pull": (5.5, "muscle_building", ["back", "biceps", "forearms"]),
    "chest": (5.0, "muscle_building", ["chest", "triceps", "shoulders"]),
    "back": (5.0, "muscle_building", ["back", "biceps", "rear delts"]),
    "shoulders": (5.0, "muscle_building", ["shoulders", "traps", "triceps"]),
    "arms": (4.5, "muscle_building", ["biceps", "triceps", "forearms"]),
    "core": (4.0, "muscle_building", ["abs", "obliques", "lower back"]),
    "abs": (4.0, "muscle_building", ["abs", "obliques"]),
    "glutes": (5.5, "muscle_building", ["glutes", "hamstrings"]),
    "deadlift": (6.0, "muscle_building", ["back", "glutes", "hamstrings", "core"]),
    "squat": (6.0, "muscle_building", ["quadriceps", "glutes", "hamstrings", "core"]),
    "bench": (5.0, "muscle_building", ["chest", "triceps", "shoulders"]),
    # Cardio / Running
    "run": (9.8, "cardiovascular", ["quadriceps", "hamstrings", "calves", "core"]),
    "running": (9.8, "cardiovascular", ["quadriceps", "hamstrings", "calves", "core"]),
    "jog": (7.0, "cardiovascular", ["quadriceps", "hamstrings", "calves"]),
    "jogging": (7.0, "cardiovascular", ["quadriceps", "hamstrings", "calves"]),
    "sprint": (12.0, "cardiovascular", ["quadriceps", "hamstrings", "glutes", "calves"]),
    "cardio": (7.0, "cardiovascular", ["full body"]),
    "hiit": (8.0, "cardiovascular", ["full body"]),
    "cycling": (7.5, "cardiovascular", ["quadriceps", "hamstrings", "calves", "glutes"]),
    "bike": (7.5, "cardiovascular", ["quadriceps", "hamstrings", "calves", "glutes"]),
    "biking": (7.5, "cardiovascular", ["quadriceps", "hamstrings", "calves", "glutes"]),
    "swimming": (7.0, "cardiovascular", ["full body"]),
    "swim": (7.0, "cardiovascular", ["full body"]),
    "rowing": (7.0, "cardiovascular", ["back", "arms", "core", "legs"]),
    "elliptical": (5.0, "cardiovascular", ["full body"]),
    "stairmaster": (9.0, "cardiovascular", ["quadriceps", "glutes", "calves"]),
    "jump rope": (10.0, "cardiovascular", ["calves", "shoulders", "core"]),
    "walking": (3.5, "cardiovascular", ["quadriceps", "calves"]),
    "walk": (3.5, "cardiovascular", ["quadriceps", "calves"]),
    "hike": (6.0, "cardiovascular", ["quadriceps", "hamstrings", "glutes", "calves"]),
    "hiking": (6.0, "cardiovascular", ["quadriceps", "hamstrings", "glutes", "calves"]),
    # Sports
    "basketball": (6.5, "cardiovascular", ["quadriceps", "calves", "core", "shoulders"]),
    "soccer": (7.0, "cardiovascular", ["quadriceps", "hamstrings", "calves", "core"]),
    "football": (8.0, "cardiovascular", ["full body"]),
    "tennis": (7.3, "cardiovascular", ["shoulders", "core", "legs"]),
    "badminton": (5.5, "cardiovascular", ["shoulders", "core", "legs"]),
    "volleyball": (4.0, "cardiovascular", ["shoulders", "core", "legs"]),
    "hockey": (8.0, "cardiovascular", ["full body"]),
    "boxing": (7.8, "cardiovascular", ["shoulders", "arms", "core"]),
    "martial arts": (10.3, "cardiovascular", ["full body"]),
    "mma": (10.3, "cardiovascular", ["full body"]),
    "wrestling": (6.0, "muscle_building", ["full body"]),
    "golf": (4.3, "flexibility", ["core", "shoulders", "arms"]),
    "cricket": (4.8, "cardiovascular", ["arms", "legs", "core"]),
    "sport": (6.0, "cardiovascular", ["full body"]),
    # Flexibility & Recovery
    "yoga": (3.0, "flexibility", ["full body"]),
    "stretching": (2.5, "flexibility", ["full body"]),
    "pilates": (3.8, "flexibility", ["core", "glutes", "back"]),
    "mobility": (2.5, "flexibility", ["full body"]),
    "foam rolling": (2.0, "recovery", ["full body"]),
    # Plyometrics
    "plyometrics": (8.0, "power", ["quadriceps", "glutes", "calves"]),
    "pylo": (8.0, "power", ["quadriceps", "glutes", "calves"]),
    "plyo": (8.0, "power", ["quadriceps", "glutes", "calves"]),
    "box jumps": (8.0, "power", ["quadriceps", "glutes", "calves"]),
    # Mixed / Other
    "crossfit": (8.0, "cardiovascular", ["full body"]),
    "circuit": (8.0, "cardiovascular", ["full body"]),
    "mixed": (6.0, "cardiovascular", ["full body"]),
    "workout": (5.0, "muscle_building", ["full body"]),
    "gym": (5.0, "muscle_building", ["full body"]),
    "training": (5.0, "muscle_building", ["full body"]),
    "exercise": (5.0, "cardiovascular", ["full body"]),
    "other": (4.0, "cardiovascular", ["full body"]),
}

BENEFIT_LABELS = {
    "cardiovascular": "Cardio & Heart Health",
    "muscle_building": "Muscle Building & Strength",
    "flexibility": "Flexibility & Balance",
    "recovery": "Recovery & Mobility",
    "power": "Explosive Power",
}

BENEFIT_ICONS = {
    "cardiovascular": "\u2764\ufe0f",
    "muscle_building": "\U0001f4aa",
    "flexibility": "\U0001f9d8",
    "recovery": "\U0001f9ca",
    "power": "\u26a1",
}

BENEFIT_DESCRIPTIONS = {
    "cardiovascular": "Improves heart efficiency, lowers resting heart rate, boosts endurance, and reduces cardiovascular disease risk.",
    "muscle_building": "Increases lean muscle mass, strengthens bones, boosts metabolism, and improves functional strength.",
    "flexibility": "Improves range of motion, reduces injury risk, enhances posture, and promotes mind-body connection.",
    "recovery": "Reduces muscle soreness, improves blood flow, enhances tissue repair, and prevents overuse injuries.",
    "power": "Develops fast-twitch muscle fibers, improves athletic explosiveness, and enhances neuromuscular coordination.",
}


def _get_user_weight_kg(user_id: int) -> float:
    """Get user's weight in kg. Falls back to 80 kg if unavailable."""
    from fitnessbot.metrics import get_weight_summary
    summary = get_weight_summary(user_id)
    if summary.get("has_data") and summary.get("current_smoothed"):
        weight_lbs = summary["current_smoothed"]
        return weight_lbs * 0.453592
    return 80.0


def _match_activity(activity_str: str) -> tuple[float, str, list[str]]:
    """Match an activity string to its MET value, benefit type, and muscle groups.

    Tries exact match first, then substring matching.
    """
    if not activity_str:
        return (4.0, "cardiovascular", ["full body"])

    lower = activity_str.lower().strip()

    # Exact match
    if lower in ACTIVITY_MET_MAP:
        return ACTIVITY_MET_MAP[lower]

    # Substring match — try longest keyword first
    best_match = None
    best_len = 0
    for keyword, data in ACTIVITY_MET_MAP.items():
        if keyword in lower and len(keyword) > best_len:
            best_match = data
            best_len = len(keyword)

    if best_match:
        return best_match

    return (4.0, "cardiovascular", ["full body"])


def calc_calories_burned(met: float, weight_kg: float, duration_min: int) -> int:
    """Calculate calories burned using the MET formula.

    Formula: Calories = MET * 3.5 * weight_kg / 200 * duration_min
    """
    if duration_min <= 0:
        return 0
    return round(met * 3.5 * weight_kg / 200 * duration_min)


def get_activity_benefits(activity_str: str, duration_min: int | None, weight_kg: float) -> dict:
    """Calculate health benefits for a single activity.

    Returns dict with calories_burned, benefit_type, muscle_groups, and descriptions.
    """
    met, benefit_type, muscles = _match_activity(activity_str)
    duration = duration_min or 30  # default 30 min if not specified

    calories = calc_calories_burned(met, weight_kg, duration)

    return {
        "activity": activity_str,
        "met_value": met,
        "duration_min": duration,
        "calories_burned": calories,
        "benefit_type": benefit_type,
        "benefit_label": BENEFIT_LABELS.get(benefit_type, "General Fitness"),
        "benefit_icon": BENEFIT_ICONS.get(benefit_type, "\U0001f3cb"),
        "benefit_description": BENEFIT_DESCRIPTIONS.get(benefit_type, ""),
        "muscle_groups": muscles,
        "intensity": _intensity_label(met),
    }


def _intensity_label(met: float) -> str:
    if met < 3.0:
        return "light"
    elif met < 6.0:
        return "moderate"
    elif met < 9.0:
        return "vigorous"
    else:
        return "high"


def get_daily_benefits(user_id: int, date_str: str | None = None) -> dict:
    """Calculate health benefits summary for a given day.

    Pulls all workouts from training_plan_items (completed) and health_data for the day,
    deduplicates, and computes aggregate benefits.
    """
    from fitnessbot.tz import user_today, day_utc_range

    if not date_str:
        date_str = user_today(user_id)

    weight_kg = _get_user_weight_kg(user_id)
    utc_start, utc_end = day_utc_range(date_str, user_id)

    # Get completed training plan items for this date
    plan_items = []
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """SELECT title, activity_type, planned_duration_min, linked_exercise_id
               FROM training_plan_items
               WHERE user_id = ? AND date = ? AND status = 'completed'
               AND activity_type != 'rest'""",
            (user_id, date_str),
        ).fetchall()
        plan_items = [dict(r) for r in rows]

        # Get standalone health_data workouts not linked to a plan item
        hd_rows = conn.execute(
            """SELECT data_json, recorded_at FROM health_data
               WHERE user_id = ? AND data_type = 'workout'
               AND recorded_at >= ? AND recorded_at <= ?""",
            (user_id, utc_start, utc_end),
        ).fetchall()
    finally:
        conn.close()

    # Collect linked exercise IDs from plan items to avoid double-counting
    linked_ids = {
        item["linked_exercise_id"] for item in plan_items
        if item.get("linked_exercise_id")
    }

    activities = []

    # From training plan items
    for item in plan_items:
        activity_name = item.get("title") or item.get("activity_type", "workout")
        duration = item.get("planned_duration_min")
        benefits = get_activity_benefits(activity_name, duration, weight_kg)
        benefits["source"] = "training_plan"
        activities.append(benefits)

    # From standalone health_data entries (not linked to plan)
    seen_hd = set()
    for row in hd_rows:
        data = json.loads(row["data_json"]) if row["data_json"] else {}
        # Skip if this is a plan-sourced entry
        if data.get("source") == "training_plan":
            continue
        # Dedup by activity + timestamp
        key = f"{data.get('type', '')}-{data.get('activity', '')}-{row['recorded_at'][:16]}"
        if key in seen_hd:
            continue
        seen_hd.add(key)

        activity_name = data.get("activity") or data.get("type", "workout")
        duration = data.get("duration_min")
        benefits = get_activity_benefits(activity_name, duration, weight_kg)
        benefits["source"] = "health_data"
        activities.append(benefits)

    # Aggregate
    total_calories = sum(a["calories_burned"] for a in activities)
    total_duration = sum(a["duration_min"] for a in activities)
    all_muscles = set()
    benefit_types = {}
    for a in activities:
        for m in a["muscle_groups"]:
            all_muscles.add(m)
        bt = a["benefit_type"]
        if bt not in benefit_types:
            benefit_types[bt] = {"count": 0, "calories": 0, "duration": 0}
        benefit_types[bt]["count"] += 1
        benefit_types[bt]["calories"] += a["calories_burned"]
        benefit_types[bt]["duration"] += a["duration_min"]

    primary_benefit = max(benefit_types, key=lambda k: benefit_types[k]["duration"]) if benefit_types else None

    return {
        "date": date_str,
        "activities": activities,
        "session_count": len(activities),
        "total_calories_burned": total_calories,
        "total_duration_min": total_duration,
        "muscle_groups_worked": sorted(all_muscles),
        "benefit_breakdown": benefit_types,
        "primary_benefit": primary_benefit,
        "primary_benefit_label": BENEFIT_LABELS.get(primary_benefit, "") if primary_benefit else "",
        "primary_benefit_icon": BENEFIT_ICONS.get(primary_benefit, "") if primary_benefit else "",
    }


def get_weekly_benefits(user_id: int, week_start: str | None = None) -> dict:
    """Calculate health benefits summary for a full week.

    If week_start is not provided, uses the current week's Monday.
    """
    from fitnessbot.training_plan import _monday_of_week, _today

    if not week_start:
        week_start = _monday_of_week(_today(user_id))

    ws = date.fromisoformat(week_start)
    daily_summaries = []
    total_cal = 0
    total_dur = 0
    total_sessions = 0
    all_muscles = set()
    benefit_totals: dict[str, dict] = {}
    active_days = 0

    for i in range(7):
        day_str = (ws + timedelta(days=i)).isoformat()
        day_data = get_daily_benefits(user_id, day_str)
        daily_summaries.append(day_data)
        total_cal += day_data["total_calories_burned"]
        total_dur += day_data["total_duration_min"]
        total_sessions += day_data["session_count"]
        if day_data["session_count"] > 0:
            active_days += 1
        for m in day_data["muscle_groups_worked"]:
            all_muscles.add(m)
        for bt, bt_data in day_data["benefit_breakdown"].items():
            if bt not in benefit_totals:
                benefit_totals[bt] = {"count": 0, "calories": 0, "duration": 0}
            benefit_totals[bt]["count"] += bt_data["count"]
            benefit_totals[bt]["calories"] += bt_data["calories"]
            benefit_totals[bt]["duration"] += bt_data["duration"]

    primary_benefit = max(benefit_totals, key=lambda k: benefit_totals[k]["duration"]) if benefit_totals else None

    # Generate weekly insight
    insight = _generate_weekly_insight(
        total_sessions, active_days, total_cal, total_dur,
        benefit_totals, all_muscles
    )

    return {
        "week_start": week_start,
        "daily_summaries": daily_summaries,
        "total_sessions": total_sessions,
        "active_days": active_days,
        "total_calories_burned": total_cal,
        "total_duration_min": total_dur,
        "muscle_groups_worked": sorted(all_muscles),
        "benefit_breakdown": benefit_totals,
        "primary_benefit": primary_benefit,
        "primary_benefit_label": BENEFIT_LABELS.get(primary_benefit, "") if primary_benefit else "",
        "primary_benefit_icon": BENEFIT_ICONS.get(primary_benefit, "") if primary_benefit else "",
        "insight": insight,
    }


def _generate_weekly_insight(
    sessions: int, active_days: int, calories: int, duration: int,
    benefit_breakdown: dict, muscles: set
) -> str:
    """Generate a human-readable weekly insight string."""
    if sessions == 0:
        return "No workouts logged this week. Start with one session to build momentum."

    parts = []

    # Volume assessment
    if active_days >= 5:
        parts.append(f"{active_days} active days this week — excellent consistency.")
    elif active_days >= 3:
        parts.append(f"{active_days} active days — solid foundation.")
    else:
        parts.append(f"Only {active_days} active day{'s' if active_days != 1 else ''} — aim for 3-4 days next week.")

    # Calorie burn
    parts.append(f"~{calories:,} cal burned across {sessions} session{'s' if sessions != 1 else ''} ({duration} min total).")

    # Balance assessment
    has_cardio = "cardiovascular" in benefit_breakdown
    has_strength = "muscle_building" in benefit_breakdown
    has_flex = "flexibility" in benefit_breakdown

    if has_cardio and has_strength and has_flex:
        parts.append("Well-rounded week — cardio, strength, and flexibility covered.")
    elif has_cardio and has_strength:
        parts.append("Good mix of cardio and strength. Consider adding a flexibility session.")
    elif has_cardio and not has_strength:
        parts.append("Cardio-heavy week. Add 2-3 strength sessions for balanced fitness.")
    elif has_strength and not has_cardio:
        parts.append("Strength-focused week. Add 2-3 cardio sessions for heart health.")
    else:
        parts.append("Try mixing different activity types for well-rounded fitness.")

    # Muscle group coverage
    if "full body" in muscles:
        pass  # Generic, no need to comment
    elif len(muscles) >= 6:
        parts.append("Great muscle group variety — hitting the body evenly.")
    elif len(muscles) >= 3:
        missing = _suggest_missing_muscles(muscles)
        if missing:
            parts.append(f"Consider targeting: {', '.join(missing[:2])} next week.")

    return " ".join(parts)


def _suggest_missing_muscles(worked: set) -> list[str]:
    """Suggest muscle groups that haven't been worked."""
    key_groups = {"quadriceps", "hamstrings", "glutes", "chest", "back", "shoulders", "core"}
    # Exclude "full body" from consideration
    specific = worked - {"full body"}
    missing = key_groups - specific
    return sorted(missing)


def format_activity_benefit_telegram(benefit: dict) -> str:
    """Format a single activity's health benefit for Telegram message."""
    icon = benefit.get("benefit_icon", "\U0001f3cb")
    label = benefit.get("benefit_label", "General")
    cal = benefit.get("calories_burned", 0)
    duration = benefit.get("duration_min", 0)
    intensity = benefit.get("intensity", "moderate")
    muscles = benefit.get("muscle_groups", [])

    line = f"{icon} *{label}* — ~{cal} cal burned ({duration} min, {intensity} intensity)"
    if muscles and muscles != ["full body"]:
        line += f"\n   Muscles: {', '.join(muscles)}"

    return line


def format_daily_benefits_telegram(daily: dict) -> str:
    """Format daily benefits summary for Telegram."""
    if daily["session_count"] == 0:
        return ""

    lines = [f"\U0001f3cb *Today's Health Benefits* ({daily['date']})"]
    lines.append(f"Sessions: {daily['session_count']} | Duration: {daily['total_duration_min']} min | ~{daily['total_calories_burned']} cal burned")
    lines.append("")

    for activity in daily["activities"]:
        lines.append(format_activity_benefit_telegram(activity))

    if daily["muscle_groups_worked"]:
        muscles = [m for m in daily["muscle_groups_worked"] if m != "full body"]
        if muscles:
            lines.append(f"\n\U0001f4aa Muscles worked: {', '.join(muscles)}")

    return "\n".join(lines)


def format_weekly_benefits_telegram(weekly: dict) -> str:
    """Format weekly benefits summary for Telegram."""
    if weekly["total_sessions"] == 0:
        return "No workouts this week. Let's get moving!"

    lines = ["\U0001f4ca *Weekly Activity Summary*"]
    lines.append(f"{weekly['active_days']}/7 active days | {weekly['total_sessions']} sessions | {weekly['total_duration_min']} min total")
    lines.append(f"~{weekly['total_calories_burned']:,} estimated calories burned")
    lines.append("")

    # Benefit type breakdown
    for bt, data in sorted(weekly["benefit_breakdown"].items(), key=lambda x: -x[1]["duration"]):
        icon = BENEFIT_ICONS.get(bt, "\U0001f3cb")
        label = BENEFIT_LABELS.get(bt, bt)
        lines.append(f"{icon} {label}: {data['count']} session{'s' if data['count'] != 1 else ''}, ~{data['calories']} cal, {data['duration']} min")

    if weekly.get("insight"):
        lines.append(f"\n{weekly['insight']}")

    return "\n".join(lines)
