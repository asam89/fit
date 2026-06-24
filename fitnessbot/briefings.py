"""Scheduled Telegram briefings: morning, midday, evening + nudges + rollups."""

import logging
from datetime import datetime, timezone, timedelta

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.metrics import get_weight_summary
from fitnessbot.nutrition import get_nutrition_targets
from fitnessbot.web.connections import decrypt_token

logger = logging.getLogger(__name__)


def _in_quiet_hours(tz_str: str = "America/Toronto") -> bool:
    try:
        import pytz
        tz = pytz.timezone(tz_str)
        now = datetime.now(tz)
        quiet = Config.QUIET_HOURS.split("-")
        start_h, start_m = map(int, quiet[0].split(":"))
        end_h, end_m = map(int, quiet[1].split(":"))
        t = now.hour * 60 + now.minute
        qs = start_h * 60 + start_m
        qe = end_h * 60 + end_m
        if qs > qe:
            return t >= qs or t < qe
        return qs <= t < qe
    except Exception:
        return False


def _get_user_targets(user_id: int) -> dict:
    """Single source of truth: nutrition_targets table."""
    return get_nutrition_targets(user_id)


async def _send_telegram(user_id: int, text: str) -> bool:
    conn = db.get_telegram_connection(user_id)
    if not conn:
        return False
    try:
        import httpx
        token = decrypt_token(conn["bot_token_encrypted"])
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": conn["chat_id"], "text": text, "parse_mode": "Markdown"},
            )
            return resp.status_code == 200
    except Exception as e:
        logger.error("Failed to send briefing to user %s: %s", user_id, e)
        return False


def build_morning_brief(user_id: int) -> str:
    weight = get_weight_summary(user_id)
    targets = _get_user_targets(user_id)

    lines = ["*Morning brief*", ""]
    if weight.get("has_data"):
        lines.append(f"Weight: {weight['current_smoothed']} lbs (smoothed)")
        if weight.get("trend_7d") is not None:
            direction = "down" if weight["trend_7d"] < 0 else "up"
            lines.append(f"  7d: {abs(weight['trend_7d']):.1f} lbs {direction}")
    else:
        lines.append("No weight data — hop on the scale this morning?")

    lines.append("")
    lines.append(f"Today's targets: {targets['calories']} cal · {targets['protein']}g P · {targets['carbs']}g C · {targets['fat']}g F")

    from fitnessbot import training_plan
    plan_text = training_plan.format_today_plan(user_id)
    if plan_text:
        lines.append("")
        lines.append(plan_text)

    lines.append("")
    lines.append("Log breakfast when you eat.")
    lines.append("")
    lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
    return "\n".join(lines)


def build_midday_check(user_id: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    targets = _get_user_targets(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    lines = ["*Midday check*", ""]

    if totals["calories"] == 0:
        lines.append("No meals logged yet — what'd you have?")
        return "\n".join(lines)

    remaining = targets["calories"] - totals["calories"]
    if remaining > targets["calories"] * 0.6:
        pace = "light so far"
    elif remaining > targets["calories"] * 0.3:
        pace = "on track"
    else:
        pace = "ahead"

    lines.append(f"Intake so far: {totals['calories']:.0f} / {targets['calories']} cal ({pace})")
    lines.append(f"  P: {totals['protein']:.0f}/{targets['protein']}g · C: {totals['carbs']:.0f}/{targets['carbs']}g · F: {totals['fat']:.0f}/{targets['fat']}g")

    if meal_count < 2:
        lines.append("")
        lines.append("Only one meal so far — log lunch when you eat.")

    lines.append("")
    lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
    return "\n".join(lines)


def build_evening_wrap(user_id: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user_id, today)
    targets = _get_user_targets(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today)

    lines = ["*Evening wrap*", ""]

    if totals["calories"] == 0:
        lines.append("No meals logged today — I can't show your macros without them.")
        lines.append("Send what you ate (type or voice) and I'll catch you up.")
        return "\n".join(lines)

    def _stat(label, actual, target, unit=""):
        diff = target - actual
        status = "on target" if abs(diff) < target * 0.05 else f"{abs(diff):.0f}{unit} {'short' if diff > 0 else 'over'}"
        return f"{label}  {actual:.0f} / {target}{unit}   {status}"

    lines.append(_stat("Calories", totals["calories"], targets["calories"]))
    lines.append(_stat("Protein", totals["protein"], targets["protein"], "g"))
    lines.append(_stat("Carbs", totals["carbs"], targets["carbs"], "g"))
    lines.append(_stat("Fat", totals["fat"], targets["fat"], "g"))

    if weight.get("has_data"):
        lines.append("")
        w_line = f"Weight  {weight['current_smoothed']} lbs"
        if weight.get("trend_7d") is not None:
            direction = "up" if weight["trend_7d"] >= 0 else "down"
            w_line += f"  ({abs(weight['trend_7d']):.1f} {direction} over 7d)"
        lines.append(w_line)

    from fitnessbot import training_plan
    adherence_text = training_plan.format_day_adherence(user_id, today)
    if adherence_text:
        lines.append("")
        lines.append(adherence_text)

    nudges = []
    if meal_count < 3:
        nudges.append("No dinner logged yet — want to add it?")
    prot_gap = targets["protein"] - totals["protein"]
    if prot_gap > 10:
        nudges.append(f"~{prot_gap:.0f}g protein short. A scoop of whey or Greek yogurt before bed closes the gap.")

    if nudges:
        lines.append("")
        for n in nudges:
            lines.append(n)

    lines.append("")
    lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
    return "\n".join(lines)


def build_weekly_rollup(user_id: int) -> str:
    today = datetime.now(timezone.utc)
    week_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    week_end = today.strftime("%Y-%m-%d")

    cal_hist = db.get_calorie_history(user_id, 7)
    avg_cal = sum(d["calories"] for d in cal_hist) / max(len(cal_hist), 1) if cal_hist else 0
    targets = _get_user_targets(user_id)
    weight = get_weight_summary(user_id)

    lines = [f"*Week of {week_start} to {week_end}*", ""]
    lines.append(f"Intake  avg {avg_cal:.0f} kcal/day (target {targets['calories']})")
    lines.append(f"Logging days  {len(cal_hist)} of 7")
    if weight.get("has_data"):
        lines.append(f"Weight  {weight['current_smoothed']} lbs")
        if weight.get("trend_7d") is not None:
            lines.append(f"  7d change: {weight['trend_7d']:.1f} lbs")

    from fitnessbot import training_plan
    from fitnessbot.training_plan import _monday_of_week
    from datetime import date as date_cls
    ws = _monday_of_week((today - timedelta(days=7)).date())
    items = training_plan.get_plan_items(user_id, ws)
    if items:
        adherence = training_plan.compute_adherence(items)
        lines.append("")
        lines.append(f"Training  {adherence['label']} planned activities")

    lines.append("")
    lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
    return "\n".join(lines)


async def run_morning_brief():
    if _in_quiet_hours():
        return
    connections = db.get_all_active_connections()
    for conn in connections:
        uid = conn["user_id"]
        if db.get_briefings_sent_today(uid, "morning") > 0:
            continue
        text = build_morning_brief(uid)
        sent = await _send_telegram(uid, text)
        if sent:
            db.insert_briefing_log(uid, "morning", text[:200])


async def run_midday_check():
    if _in_quiet_hours():
        return
    connections = db.get_all_active_connections()
    for conn in connections:
        uid = conn["user_id"]
        if db.get_briefings_sent_today(uid, "midday") > 0:
            continue
        text = build_midday_check(uid)
        sent = await _send_telegram(uid, text)
        if sent:
            db.insert_briefing_log(uid, "midday", text[:200])


async def run_evening_wrap():
    if _in_quiet_hours():
        return
    connections = db.get_all_active_connections()
    for conn in connections:
        uid = conn["user_id"]
        if db.get_briefings_sent_today(uid, "evening") > 0:
            continue
        text = build_evening_wrap(uid)
        now = datetime.now(timezone.utc)
        if now.weekday() == 6:
            text += "\n\n" + build_weekly_rollup(uid)
        sent = await _send_telegram(uid, text)
        if sent:
            db.insert_briefing_log(uid, "evening", text[:200], had_nudge=True)
