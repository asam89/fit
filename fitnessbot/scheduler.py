"""APScheduler-based job scheduler for briefings, nudges, and rollups."""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from fitnessbot.config import Config

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _parse_briefing_times() -> tuple[str, str, str]:
    times = getattr(Config, "BRIEFING_TIMES", "07:30,13:00,20:30")
    parts = times.split(",")
    morning = parts[0].strip() if len(parts) > 0 else "07:30"
    midday = parts[1].strip() if len(parts) > 1 else "13:00"
    evening = parts[2].strip() if len(parts) > 2 else "20:30"
    return morning, midday, evening


def setup_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone=Config.TIMEZONE)
    morning, midday, evening = _parse_briefing_times()

    async def _safe_run(fn, name):
        try:
            await fn()
        except Exception as e:
            logger.error("Scheduler job %s failed: %s", name, e, exc_info=True)

    from fitnessbot.briefings import run_morning_brief, run_midday_check, run_evening_wrap

    m_h, m_m = morning.split(":")
    _scheduler.add_job(lambda: _safe_run(run_morning_brief, "morning_brief"),
                       CronTrigger(hour=int(m_h), minute=int(m_m)), id="morning_brief", replace_existing=True)

    d_h, d_m = midday.split(":")
    _scheduler.add_job(lambda: _safe_run(run_midday_check, "midday_check"),
                       CronTrigger(hour=int(d_h), minute=int(d_m)), id="midday_check", replace_existing=True)

    e_h, e_m = evening.split(":")
    _scheduler.add_job(lambda: _safe_run(run_evening_wrap, "evening_wrap"),
                       CronTrigger(hour=int(e_h), minute=int(e_m)), id="evening_wrap", replace_existing=True)

    logger.info("Scheduler configured: morning=%s midday=%s evening=%s tz=%s", morning, midday, evening, Config.TIMEZONE)
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
