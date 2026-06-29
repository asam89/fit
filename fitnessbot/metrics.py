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
