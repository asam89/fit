"""APScheduler-based job scheduler for briefings, nudges, and rollups.

Runs a per-minute dispatcher that checks each user's notification preferences
and sends briefings at their configured times.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from fitnessbot.config import Config

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _user_now(tz_str: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_str))
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Toronto"))


async def _dispatch_briefings():
    """Run every minute. For each connected user, check if it's time for any briefing."""
    from fitnessbot import db
    from fitnessbot.briefings import (
        build_morning_brief, build_midday_check, build_evening_wrap,
        build_weekly_rollup, _send_telegram,
    )

    connections = db.get_all_active_connections()
    if not connections:
        return
    for conn in connections:
        uid = conn["user_id"]
        try:
            user = db.get_user_by_id(uid)
            if not user:
                continue
            tz_str = user.get("timezone", "America/Toronto")
            now = _user_now(tz_str)
            current_time = now.strftime("%H:%M")
            prefs = db.get_notification_preferences(uid)
            logger.debug("User %s tz=%s local=%s prefs_morning=%s/%s midday=%s/%s evening=%s/%s",
                         uid, tz_str, current_time,
                         prefs["morning_brief_enabled"], prefs["morning_brief_time"],
                         prefs["midday_check_enabled"], prefs["midday_check_time"],
                         prefs["evening_wrap_enabled"], prefs["evening_wrap_time"])

            # Morning brief
            if (prefs["morning_brief_enabled"]
                    and current_time == prefs["morning_brief_time"]
                    and db.get_briefings_sent_today(uid, "morning") == 0):
                text = build_morning_brief(uid)
                if prefs.get("activity_prompts_enabled", 1):
                    prompt = _build_activity_prompt(uid, now)
                    if prompt:
                        text += "\n\n" + prompt
                sent = await _send_telegram(uid, text)
                if sent:
                    db.insert_briefing_log(uid, "morning", text[:200])

            # Midday check
            if (prefs["midday_check_enabled"]
                    and current_time == prefs["midday_check_time"]
                    and db.get_briefings_sent_today(uid, "midday") == 0):
                text = build_midday_check(uid)
                sent = await _send_telegram(uid, text)
                if sent:
                    db.insert_briefing_log(uid, "midday", text[:200])

            # Evening wrap
            if (prefs["evening_wrap_enabled"]
                    and current_time == prefs["evening_wrap_time"]
                    and db.get_briefings_sent_today(uid, "evening") == 0):
                text = build_evening_wrap(uid)
                if prefs.get("activity_prompts_enabled", 1):
                    stale = _build_stale_suggestion(uid)
                    if stale:
                        text += "\n\n" + stale
                if prefs["weekly_rollup_enabled"] and now.weekday() == prefs["weekly_rollup_day"]:
                    text += "\n\n" + build_weekly_rollup(uid)
                sent = await _send_telegram(uid, text)
                if sent:
                    db.insert_briefing_log(uid, "evening", text[:200], had_nudge=True)

        except Exception as e:
            logger.error("Dispatch failed for user %s: %s", uid, e, exc_info=True)


def _build_activity_prompt(user_id: int, now: datetime) -> str | None:
    """Build an activity-aware prompt based on today's plan and patterns."""
    from fitnessbot import db, training_plan

    today_str = now.date().isoformat()
    today_items = training_plan.get_items_for_date(user_id, today_str)

    if today_items:
        pending = [i for i in today_items if i["status"] == "planned" and i["activity_type"] != "rest"]
        if pending:
            names = ", ".join(i["title"] for i in pending)
            return f"\U0001f3af Today's plan: {names} — are you in?"
        rest = [i for i in today_items if i["activity_type"] == "rest"]
        if rest:
            return "\U0001f4a4 Rest day today — recovery is training too."
        return None

    # No plan today — check patterns for this day of week
    dow = now.weekday()
    patterns = db.get_activity_patterns(user_id)
    day_patterns = [p for p in patterns if p["day_of_week"] == dow and p["freq"] >= 2]
    if day_patterns:
        top = day_patterns[0]
        return f"\U0001f4ac You usually do {top['title']} on {DAY_NAMES[dow]}s — planning one today?"

    return None


def _build_stale_suggestion(user_id: int) -> str | None:
    """Suggest an activity the user hasn't done in a while."""
    from fitnessbot import db
    stale = db.get_stale_activities(user_id)
    if stale:
        s = stale[0]
        days = int(s["days_since"])
        return f"\U0001f50D You haven't done {s['title']} in {days} days — want to add one this week?"
    return None


def setup_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone=Config.TIMEZONE)

    async def _safe_dispatch():
        try:
            await _dispatch_briefings()
        except Exception as e:
            logger.error("Scheduler dispatch_briefings failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _safe_dispatch,
        CronTrigger(minute="*"),
        id="dispatch_briefings",
        replace_existing=True,
    )

    logger.info("Scheduler configured: per-minute dispatcher, tz=%s", Config.TIMEZONE)
    return _scheduler


def start_scheduler() -> None:
    scheduler = setup_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")
    _scheduler = None
