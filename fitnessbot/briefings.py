"""Scheduled Telegram briefings: morning, midday, evening + nudges + rollups."""

import logging
from datetime import datetime, timezone, timedelta

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.ai.prompts import compose_prompt
from fitnessbot.metrics import get_weight_summary, build_weight_analysis
from fitnessbot.nutrition import get_nutrition_targets
from fitnessbot.web.connections import decrypt_token
from fitnessbot.tz import user_today

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


def _get_yesterday_performance(user_id: int) -> dict | None:
    """Get yesterday's totals for coaching tone in morning brief."""
    from fitnessbot.tz import day_utc_range, user_today as _ut
    from datetime import datetime as _dt, timedelta
    today_str = _ut(user_id)
    yesterday = (_dt.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    urange = day_utc_range(yesterday, user_id)
    totals = db.get_today_totals(user_id, yesterday, utc_range=urange)
    if totals["calories"] == 0:
        return None
    targets = _get_user_targets(user_id)
    return {
        "cal_pct": totals["calories"] / targets["calories"] if targets["calories"] else 0,
        "protein_pct": totals["protein"] / targets["protein"] if targets["protein"] else 0,
        "fat_over": totals["fat"] > targets["fat"] * 1.1 if targets["fat"] else False,
        "calories": totals["calories"],
        "protein": totals["protein"],
    }


def build_morning_brief(user_id: int) -> str:
    weight = get_weight_summary(user_id)
    targets = _get_user_targets(user_id)

    lines = ["*Morning brief*", ""]

    # Coaching tone based on yesterday's performance
    yesterday = _get_yesterday_performance(user_id)
    if yesterday:
        if yesterday["cal_pct"] > 1.15:
            lines.append(f"Yesterday: {yesterday['calories']:.0f} cal — over target. Time to tighten up today. No excuses.")
        elif yesterday["protein_pct"] < 0.7:
            lines.append(f"Yesterday: only {yesterday['protein']:.0f}g protein. That's not enough. Prioritize it from meal one today.")
        elif yesterday["cal_pct"] > 0.9 and yesterday["protein_pct"] > 0.85:
            lines.append("Solid day yesterday. Keep that energy going.")
        lines.append("")

    # Weight with trend analysis
    w_analysis = build_weight_analysis(user_id)
    if w_analysis.get("has_data"):
        lines.append(f"Weight: {w_analysis['current_smoothed']} lbs (smoothed)")
        if w_analysis.get("trend_7d") is not None:
            direction = "↓" if w_analysis["trend_7d"] < 0 else "↑"
            lines.append(f"  7d: {direction} {abs(w_analysis['trend_7d']):.1f} lbs")
        if w_analysis.get("trend_30d") is not None:
            direction = "↓" if w_analysis["trend_30d"] < 0 else "↑"
            lines.append(f"  30d: {direction} {abs(w_analysis['trend_30d']):.1f} lbs")
        if w_analysis.get("goal_status") and w_analysis["goal_status"] != "no_goal":
            lines.append(f"  {w_analysis['goal_message']}")
        if w_analysis.get("weekly_rate") is not None:
            rate = w_analysis["weekly_rate"]
            if abs(rate) >= 0.2:
                lines.append(f"  Rate: {'losing' if rate < 0 else 'gaining'} ~{abs(rate):.1f} lbs/week")
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
    from fitnessbot.tz import day_utc_range
    today = user_today(user_id)
    urange = day_utc_range(today, user_id)
    totals = db.get_today_totals(user_id, today, utc_range=urange)
    targets = _get_user_targets(user_id)
    meal_count = db.get_meal_count_today(user_id, today, utc_range=urange)

    lines = ["*Midday check*", ""]

    if totals["calories"] == 0:
        lines.append("Nothing logged yet. It's past noon. Don't let the day slip away — log what you've eaten.")
        lines.append("")
        lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
        return "\n".join(lines)

    remaining_cal = targets["calories"] - totals["calories"]
    remaining_prot = targets["protein"] - totals["protein"]

    if remaining_cal > targets["calories"] * 0.6:
        pace = "light so far"
    elif remaining_cal > targets["calories"] * 0.3:
        pace = "on track"
    else:
        pace = "ahead"

    lines.append(f"Intake so far: {totals['calories']:.0f} / {targets['calories']} cal ({pace})")
    lines.append(f"  P: {totals['protein']:.0f}/{targets['protein']}g · C: {totals['carbs']:.0f}/{targets['carbs']}g · F: {totals['fat']:.0f}/{targets['fat']}g")

    # Coaching nudge based on current state
    if totals["fat"] > targets["fat"] * 0.9 and remaining_cal > targets["calories"] * 0.3:
        lines.append("")
        lines.append(f"Fat's already at {totals['fat']:.0f}g — close to your {targets['fat']}g limit. Go lean the rest of the day.")
    elif remaining_prot > targets["protein"] * 0.6:
        lines.append("")
        lines.append(f"Only {totals['protein']:.0f}g protein so far. You need {remaining_prot:.0f}g more — that's a lot to cram into dinner. Start stacking now.")
    elif meal_count < 2:
        lines.append("")
        lines.append("Only one meal so far — log lunch when you eat.")

    lines.append("")
    lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
    return "\n".join(lines)


def build_evening_wrap(user_id: int) -> str:
    from fitnessbot.tz import day_utc_range
    today = user_today(user_id)
    urange = day_utc_range(today, user_id)
    totals = db.get_today_totals(user_id, today, utc_range=urange)
    targets = _get_user_targets(user_id)
    weight = get_weight_summary(user_id)
    meal_count = db.get_meal_count_today(user_id, today, utc_range=urange)

    lines = ["*Evening wrap*", ""]

    if totals["calories"] == 0:
        lines.append("No meals logged today — I can't show your macros without them.")
        lines.append("Send what you ate (type or voice) and I'll catch you up.")
        return "\n".join(lines)

    def _stat(label, actual, target, unit=""):
        diff = target - actual
        pct = abs(diff) / target if target else 0
        if pct < 0.05:
            status = "on target"
        elif diff > 0:
            status = f"{abs(diff):.0f}{unit} short"
        else:
            status = f"exceeded by {abs(diff):.0f}{unit}"
        return f"{label}  {actual:.0f} / {target}{unit}   {status}"

    lines.append(_stat("Calories", totals["calories"], targets["calories"]))
    lines.append(_stat("Protein", totals["protein"], targets["protein"], "g"))
    lines.append(_stat("Carbs", totals["carbs"], targets["carbs"], "g"))
    lines.append(_stat("Fat", totals["fat"], targets["fat"], "g"))

    # Weight with trend analysis
    w_analysis = build_weight_analysis(user_id)
    if w_analysis.get("has_data"):
        lines.append("")
        w_line = f"Weight  {w_analysis['current_smoothed']} lbs"
        if w_analysis.get("trend_7d") is not None:
            direction = "↑" if w_analysis["trend_7d"] >= 0 else "↓"
            w_line += f"  ({direction} {abs(w_analysis['trend_7d']):.1f} over 7d)"
        lines.append(w_line)
        if w_analysis.get("goal_status") and w_analysis["goal_status"] != "no_goal":
            lines.append(f"  {w_analysis['goal_message']}")

    from fitnessbot import training_plan
    adherence_text = training_plan.format_day_adherence(user_id, today)
    if adherence_text:
        lines.append("")
        lines.append(adherence_text)

    # Health benefits summary for today's workouts
    try:
        from fitnessbot.health_benefits import get_daily_benefits
        daily_benefits = get_daily_benefits(user_id, today)
        if daily_benefits["session_count"] > 0:
            lines.append("")
            lines.append(f"\U0001f3cb Activity: {daily_benefits['session_count']} session{'s' if daily_benefits['session_count'] > 1 else ''}, {daily_benefits['total_duration_min']} min, ~{daily_benefits['total_calories_burned']} cal burned")
            if daily_benefits.get("primary_benefit_label"):
                lines.append(f"  {daily_benefits['primary_benefit_icon']} {daily_benefits['primary_benefit_label']}")
            muscles = [m for m in daily_benefits.get("muscle_groups_worked", []) if m != "full body"]
            if muscles:
                lines.append(f"  Muscles: {', '.join(muscles)}")
    except Exception:
        pass

    # Coaching tone based on how the day went — adapted to user's tone preference
    cal_pct = totals["calories"] / targets["calories"] if targets["calories"] else 0
    prot_pct = totals["protein"] / targets["protein"] if targets["protein"] else 0
    fat_pct = totals["fat"] / targets["fat"] if targets["fat"] else 0

    user = db.get_user_by_id(user_id)
    tone = (user.get("feedback_tone_preference") or "neutral") if user else "neutral"

    lines.append("")
    if cal_pct > 1.15 and fat_pct > 1.2:
        if tone == "supportive":
            lines.append("Went over on calories and fat today. Not ideal, but tomorrow's a clean slate. Plan your first two meals before bed.")
        else:
            lines.append("Tough day. Over on calories and fat. Don't spiral — just reset tomorrow. Clean meals, lean protein, move on.")
    elif cal_pct > 1.1:
        lines.append(f"Calories exceeded by {totals['calories'] - targets['calories']:.0f}. It happens. Tomorrow: be intentional from breakfast.")
    elif prot_pct < 0.7:
        prot_gap = targets["protein"] - totals["protein"]
        if tone == "blunt":
            lines.append(f"Only {totals['protein']:.0f}g protein today — {prot_gap:.0f}g short. That's unacceptable for your goals. Fix it tomorrow from meal one.")
        else:
            lines.append(f"Only {totals['protein']:.0f}g protein today — {prot_gap:.0f}g short. That's hurting your recovery. A scoop of whey or Greek yogurt before bed helps, but plan better tomorrow.")
    elif cal_pct > 0.9 and prot_pct > 0.85:
        lines.append("Good day. Targets met, protein solid. This is what consistency looks like.")
    else:
        nudges = []
        if meal_count < 3:
            nudges.append("No dinner logged yet — want to add it?")
        prot_gap = targets["protein"] - totals["protein"]
        if prot_gap > 10:
            nudges.append(f"~{prot_gap:.0f}g protein short. A scoop of whey or Greek yogurt before bed closes the gap.")
        for n in nudges:
            lines.append(n)

    lines.append("")
    lines.append(f"[View dashboard]({Config.BASE_URL}/dashboard)")
    return "\n".join(lines)


def build_weekly_rollup(user_id: int) -> str:
    today = datetime.now(timezone.utc)
    week_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    week_end = today.strftime("%Y-%m-%d")

    from fitnessbot.tz import utc_offset_hours as _utc_off, user_today as _user_today
    today_str = _user_today(user_id)
    cal_hist = db.get_calorie_history(user_id, 7, utc_offset_hours=_utc_off(user_id))
    # Exclude today (in-progress) from completed-day averages
    completed = [d for d in cal_hist if d["date"] != today_str]
    today_entry = next((d for d in cal_hist if d["date"] == today_str), None)
    avg_cal = sum(d["calories"] for d in completed) / max(len(completed), 1) if completed else 0
    targets = _get_user_targets(user_id)
    weight = get_weight_summary(user_id)

    lines = [f"*Week of {week_start} to {week_end}*", ""]
    lines.append(f"Intake  avg {avg_cal:.0f} kcal/day over {len(completed)} completed day{'s' if len(completed) != 1 else ''} (profile target {targets['calories']})")
    if today_entry:
        lines.append(f"Today (in progress)  {today_entry['calories']:.0f} kcal so far")
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

    # Weekly health benefits summary
    try:
        from fitnessbot.health_benefits import get_weekly_benefits
        weekly = get_weekly_benefits(user_id, ws)
        if weekly["total_sessions"] > 0:
            lines.append("")
            lines.append(f"\U0001f3cb Weekly activity: {weekly['active_days']}/7 active days, {weekly['total_sessions']} sessions, ~{weekly['total_calories_burned']:,} cal burned")
            for bt, bd in sorted(weekly["benefit_breakdown"].items(), key=lambda x: -x[1]["duration"]):
                from fitnessbot.health_benefits import BENEFIT_ICONS, BENEFIT_LABELS
                icon = BENEFIT_ICONS.get(bt, "")
                label = BENEFIT_LABELS.get(bt, bt)
                lines.append(f"  {icon} {label}: {bd['count']} session{'s' if bd['count'] > 1 else ''}, ~{bd['calories']} cal")
            if weekly.get("insight"):
                lines.append(f"\n{weekly['insight']}")
    except Exception:
        pass

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
