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
    trend_7d, trend_30d = compute_trends(user_id)

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
        "trend_7d": round(trend_7d, 1) if trend_7d else None,
        "trend_30d": round(trend_30d, 1) if trend_30d else None,
        "unit": weight_unit,
    }


def compute_smoothed_weight(user_id: int, new_weight: float, alpha: float = 0.1) -> float:
    """Exponentially-weighted moving average with ~7-10 day half-life.

    alpha = 0.1 gives a half-life of about 6.6 days: ln(2)/ln(1-0.1) ~ 6.58
    """
    trend = db.get_weight_trend(user_id, limit=1)
    if trend and trend[0].get("smoothed_weight"):
        prev = trend[0]["smoothed_weight"]
        return alpha * new_weight + (1 - alpha) * prev
    return new_weight


def compute_trends(user_id: int) -> tuple[float | None, float | None]:
    """Compute 7-day and 30-day weight change from smoothed trend."""
    trend_data = db.get_weight_trend(user_id, limit=90)
    if not trend_data:
        return None, None

    current = trend_data[0]["smoothed_weight"] if trend_data[0].get("smoothed_weight") else None
    if current is None:
        return None, None

    trend_7d = None
    trend_30d = None

    for entry in trend_data:
        if not entry.get("smoothed_weight"):
            continue
        days_ago = _days_between(entry["date"], trend_data[0]["date"])
        if days_ago >= 7 and trend_7d is None:
            trend_7d = current - entry["smoothed_weight"]
        if days_ago >= 30 and trend_30d is None:
            trend_30d = current - entry["smoothed_weight"]
            break

    return trend_7d, trend_30d


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
    return {
        "has_data": True,
        "current_smoothed": round(latest["smoothed_weight"], 1) if latest.get("smoothed_weight") else None,
        "current_raw": latest.get("raw_weight"),
        "trend_7d": round(latest["trend_7d"], 1) if latest.get("trend_7d") else None,
        "trend_30d": round(latest["trend_30d"], 1) if latest.get("trend_30d") else None,
        "date": latest.get("date"),
        "history_count": len(history),
    }
