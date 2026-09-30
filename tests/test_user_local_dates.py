"""Event and prompt dates follow the user's local calendar, not the UTC host's.

The frozen instant is 02:30 UTC on Oct 1 — still 22:30 on Sep 30 in Toronto,
which is where UTC-based date math goes off by one.
"""
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

FROZEN_UTC = datetime(2026, 10, 1, 2, 30, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_UTC if tz is None else FROZEN_UTC.astimezone(tz)


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch("fitnessbot.tz.datetime", _FrozenDatetime):
        yield


def test_days_until_uses_local_today():
    from fitnessbot.tz import days_until
    assert days_until("2026-10-17", tz_str="America/Toronto") == 17
    assert days_until("2026-10-17", tz_str="UTC") == 16
    assert days_until("2026-09-29", tz_str="America/Toronto") == -1


def test_parse_event_date_relative_and_same_day():
    from fitnessbot.event_coaching import parse_event_date
    assert parse_event_date("in 3 days") == "2026-10-03"
    # Still Sep 30 locally, so "Sep 30" is today, not next year.
    assert parse_event_date("Sep 30") == "2026-09-30"


def test_compose_prompt_carries_local_clock():
    from fitnessbot.ai.prompts import compose_prompt
    from fitnessbot.tz import user_now
    system = compose_prompt("Task.", now=user_now(tz_str="America/Toronto"))
    assert "Wednesday, September 30, 2026" in system
    assert "already happened" in system
    assert "Current date and time" not in compose_prompt("Task.")


@pytest.fixture
def db_with_events(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        db.init_db()
        db.run_migrations()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
            ("t@t.com", "hash", "T", "America/Toronto"),
        )
        for title, event_date, days_out in (
            ("Past tournament", "2026-07-17", 25),
            ("Tonight's game", "2026-09-30", 0),
            ("Running 5K", "2026-10-17", 18),
        ):
            conn.execute(
                "INSERT INTO event_goals (user_id, title, event_date, days_out, status) VALUES (1, ?, ?, ?, 'active')",
                (title, event_date, days_out),
            )
        conn.commit()
        conn.close()
        yield db


def test_active_event_goals_drop_past_events_by_local_date(db_with_events):
    titles = [g["title"] for g in db_with_events.get_active_event_goals(1)]
    assert titles == ["Tonight's game", "Running 5K"]


def test_goal_context_recomputes_days_out(db_with_events):
    from fitnessbot.bot.conversation import _goals_context_lines
    with patch.object(db_with_events, "get_active_goals", return_value=[]):
        lines = _goals_context_lines(1)
    joined = "\n".join(lines)
    assert "Running 5K" in joined and "17 days out" in joined
    assert "18 days out" not in joined
    assert "Past tournament" not in joined
