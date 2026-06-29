"""Weight, body-composition, and vitals logging + trend math."""

import math
from datetime import datetime, timezone

from fitnessbot import db
from fitnessbot.tz import user_today


def log_weight(
    user_id: int,
    weight: float,
    weight_unit: str = "lbs",
    body_fat_pct: float | None = None,
    source: str = "manual",
) -> dict:
    """Log a weight entry and update the smoothed trend."""
    db.insert_body_composition(
        user_id=user_id,
        weight=weight,
        weight_unit=weight_unit,
        body_fat_pct=body_fat_pct,
        source=source,
    )

    today = user_today(user_id)
    smoothed = compute_smoothed_weight(user_id, weight)

    # Save today's entry FIRST so compute_trends sees the current data point
    db.upsert_weight_trend(
        user_id=user_id,
        date_str=today,
        raw_weight=weight,
        smoothed_weight=smoothed,
        trend_7d=None,
        trend_30d=None,
    )

    # Now compute trends with today's entry included
    trend_7d, trend_30d = compute_trends(user_id)

    # Update with computed trends
    db.upsert_weight_trend(
        user_id=user_id,
        date_str=today,
        raw_weight=weight,
        smoothed_weight=smoothed,
        trend_7d=trend_7d,
        trend_30d=trend_30d,
    )

    return {
        "raw": weight,
        "smoothed": round(smoothed, 1),
        "trend_7d": round(trend_7d, 1) if trend_7d is not None else None,
        "trend_30d": round(trend_30d, 1) if trend_30d is not None else None,
        "unit": weight_unit,
    }


def compute_smoothed_weight(user_id: int, new_weight: float, alpha: float = 0.2) -> float:
    """Exponentially-weighted moving average.

    alpha = 0.2 gives a half-life of ~3.1 days — responsive enough to
    reflect real changes within a week while still filtering daily noise.
    """
    trend = db.get_weight_trend(user_id, limit=1)
    if trend and trend[0].get("smoothed_weight"):
        prev = trend[0]["smoothed_weight"]
        return alpha * new_weight + (1 - alpha) * prev
    return new_weight


def compute_trends(user_id: int) -> tuple[float | None, float | None]:
    """Compute 7-day and 30-day weight change from smoothed trend.

    Uses the closest available data point to the target window (e.g. if no
    entry exists at exactly 7 days ago, uses the nearest entry within a
    +-2 day tolerance). This prevents trends from staying NULL when entries
    are sparse or don't land on exact 7/30 day boundaries.
    """
    trend_data = db.get_weight_trend(user_id, limit=90)
    if not trend_data:
        return None, None

    current = trend_data[0].get("smoothed_weight")
    if current is None:
        return None, None

    latest_date = trend_data[0]["date"]

    trend_7d = _find_trend_at(trend_data, latest_date, current, target_days=7, tolerance=3)
    trend_30d = _find_trend_at(trend_data, latest_date, current, target_days=30, tolerance=5)

    return trend_7d, trend_30d


def _find_trend_at(
    trend_data: list[dict],
    latest_date: str,
    current_weight: float,
    target_days: int,
    tolerance: int,
) -> float | None:
    """Find the weight change over approximately target_days.

    Picks the entry closest to target_days ago (within tolerance).
    Falls back to the oldest available entry if data window is shorter
    than target_days but has at least 3 days of history.
    """
    best_entry = None
    best_distance = float("inf")
    oldest_entry = None
    oldest_days = 0

    for entry in trend_data[1:]:  # skip the latest (current) entry
        if not entry.get("smoothed_weight"):
            continue
        days_ago = _days_between(entry["date"], latest_date)
        if days_ago == 0:
            continue

        # Track oldest for fallback
        if days_ago > oldest_days:
            oldest_days = days_ago
            oldest_entry = entry

        # Look for closest to target within tolerance
        distance = abs(days_ago - target_days)
        if distance <= tolerance and distance < best_distance:
            best_distance = distance
            best_entry = entry

    if best_entry:
        return current_weight - best_entry["smoothed_weight"]

    # Fallback: use oldest entry if we have at least 3 days of data
    if oldest_entry and oldest_days >= 3 and target_days <= 7:
        return current_weight - oldest_entry["smoothed_weight"]

    return None


def _days_between(date1: str, date2: str) -> int:
    d1 = datetime.strptime(date1, "%Y-%m-%d")
    d2 = datetime.strptime(date2, "%Y-%m-%d")
    return abs((d2 - d1).days)


def get_weight_summary(user_id: int) -> dict:
    """Get current weight status for display."""
    trend = db.get_weight_trend(user_id, limit=1)
    history = db.get_weight_history(user_id, limit=30)

    if not trend:
        return {"has_data": False}

    latest = trend[0]
    days_since_last = None
    if latest.get("date"):
        today = user_today(user_id)
        days_since_last = _days_between(latest["date"], today)

    return {
        "has_data": True,
        "current_smoothed": round(latest["smoothed_weight"], 1) if latest.get("smoothed_weight") else None,
        "current_raw": latest.get("raw_weight"),
        "trend_7d": round(latest["trend_7d"], 1) if latest.get("trend_7d") else None,
        "trend_30d": round(latest["trend_30d"], 1) if latest.get("trend_30d") else None,
        "date": latest.get("date"),
        "history_count": len(history),
        "days_since_last": days_since_last,
    }


def build_weight_analysis(user_id: int) -> dict:
    """Build a comprehensive weight analysis for dashboard and Telegram."""
    summary = get_weight_summary(user_id)
    if not summary.get("has_data"):
        return {"has_data": False}

    weight_goal = db.get_weight_goal(user_id)
    trend_data = db.get_weight_trend(user_id, limit=90)

    current = summary["current_smoothed"]
    raw = summary.get("current_raw")
    trend_7d = summary.get("trend_7d")
    trend_30d = summary.get("trend_30d")

    # Direction analysis
    if weight_goal and current:
        distance_to_goal = current - weight_goal
        if abs(distance_to_goal) < 1:
            goal_status = "at_goal"
            goal_message = f"You're at your goal weight ({weight_goal} lbs). Maintain."
        elif distance_to_goal > 0:
            # Need to lose
            if trend_7d is not None and trend_7d < -0.3:
                goal_status = "on_track_losing"
                weeks_est = abs(distance_to_goal / (trend_7d or -0.5))
                goal_message = f"Losing weight toward {weight_goal} lbs. ~{weeks_est:.0f} weeks at this rate."
            elif trend_7d is not None and trend_7d > 0.3:
                goal_status = "wrong_direction"
                goal_message = f"Gaining weight but your goal is {weight_goal} lbs. Diet and exercise aren't matching your target yet."
            else:
                goal_status = "stalled"
                goal_message = f"{distance_to_goal:.1f} lbs to go. Weight is flat — adjust calories or increase activity."
        else:
            # Need to gain
            if trend_7d is not None and trend_7d > 0.3:
                goal_status = "on_track_gaining"
                weeks_est = abs(distance_to_goal / (trend_7d or 0.5))
                goal_message = f"Gaining toward {weight_goal} lbs. ~{weeks_est:.0f} weeks at this rate."
            elif trend_7d is not None and trend_7d < -0.3:
                goal_status = "wrong_direction"
                goal_message = f"Losing weight but your goal is {weight_goal} lbs. Increase calories."
            else:
                goal_status = "stalled"
                goal_message = f"{abs(distance_to_goal):.1f} lbs to go. Weight is flat — eat more to gain."
    else:
        goal_status = "no_goal"
        goal_message = "Set a weight goal to track progress."

    # Volatility check (raw vs smoothed spread)
    volatility = None
    if len(trend_data) >= 3:
        recent_raws = [e["raw_weight"] for e in trend_data[:7] if e.get("raw_weight")]
        if len(recent_raws) >= 3:
            volatility = max(recent_raws) - min(recent_raws)

    # Weekly rate of change
    weekly_rate = None
    if trend_7d is not None:
        weekly_rate = trend_7d
    elif trend_30d is not None:
        weekly_rate = trend_30d / 4.3

    # Build suggestions
    suggestions = []
    if goal_status == "wrong_direction" and distance_to_goal > 0:
        suggestions.append("Cut 200-300 calories from daily intake")
        suggestions.append("Add 20-30 min of walking daily")
        suggestions.append("Track every meal — hidden calories add up")
    elif goal_status == "stalled" and weight_goal and distance_to_goal > 0:
        suggestions.append("Try a 10% calorie reduction for 2 weeks")
        suggestions.append("Increase protein to preserve muscle while cutting")
        suggestions.append("Add 2 more training sessions per week")
    elif goal_status == "on_track_losing":
        suggestions.append("Stay the course — consistency is working")
        suggestions.append("Keep protein high to preserve muscle")
    elif goal_status == "on_track_gaining":
        suggestions.append("Good progress — keep surplus steady")
        suggestions.append("Focus on strength training to ensure quality gains")
    if volatility and volatility > 4:
        suggestions.append(f"Weight swings of {volatility:.1f} lbs this week — weigh at the same time daily for accuracy")

    return {
        "has_data": True,
        "current_smoothed": current,
        "current_raw": raw,
        "trend_7d": trend_7d,
        "trend_30d": trend_30d,
        "weight_goal": weight_goal,
        "distance_to_goal": round(abs(current - weight_goal), 1) if weight_goal and current else None,
        "goal_status": goal_status,
        "goal_message": goal_message,
        "weekly_rate": round(weekly_rate, 2) if weekly_rate is not None else None,
        "volatility": round(volatility, 1) if volatility else None,
        "suggestions": suggestions,
        "history_count": summary.get("history_count", 0),
        "days_since_last": summary.get("days_since_last"),
    }


def build_weight_telegram_summary(user_id: int) -> str:
    """Build a rich weight trend message for Telegram after a weigh-in."""
    analysis = build_weight_analysis(user_id)
    if not analysis.get("has_data"):
        return ""

    lines = []

    # Current weight
    raw = analysis.get("current_raw")
    smoothed = analysis.get("current_smoothed")
    if raw and smoothed:
        lines.append(f"Weight: {raw} lbs (smoothed: {smoothed})")

    # Trend
    t7 = analysis.get("trend_7d")
    t30 = analysis.get("trend_30d")
    if t7 is not None:
        direction = "down" if t7 < 0 else "up"
        lines.append(f"7-day trend: {abs(t7):.1f} lbs {direction}")
    if t30 is not None:
        direction = "down" if t30 < 0 else "up"
        lines.append(f"30-day trend: {abs(t30):.1f} lbs {direction}")

    # Goal analysis
    goal_msg = analysis.get("goal_message")
    if goal_msg and analysis["goal_status"] != "no_goal":
        lines.append("")
        lines.append(goal_msg)

    # Weekly rate
    rate = analysis.get("weekly_rate")
    if rate is not None:
        if abs(rate) < 0.2:
            lines.append("Rate: Weight is essentially flat this week.")
        else:
            direction = "losing" if rate < 0 else "gaining"
            lines.append(f"Rate: {direction} ~{abs(rate):.1f} lbs/week")

    # Suggestions
    suggestions = analysis.get("suggestions", [])
    if suggestions:
        lines.append("")
        for s in suggestions[:2]:
            lines.append(f"→ {s}")

    return "\n".join(lines)
