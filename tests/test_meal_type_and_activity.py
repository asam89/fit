"""Telegram meals keep the meal type the user named; brisk walks are moderate, not vigorous."""
import logging
import sqlite3
from unittest.mock import patch

import pytest

from fitnessbot.bot.conversation import _intent_meal_type
from fitnessbot.health_benefits import get_activity_benefits


@pytest.fixture
def meal_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        db.init_db()
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
        conn.close()
        db.run_migrations()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO users (user_id, email, password_hash, display_name, timezone) VALUES (1, ?, ?, ?, ?)",
            ("t@t.com", "hash", "T", "America/Toronto"),
        )
        conn.commit()
        conn.close()
        yield db_path


EGG = {"name": "egg", "qty": 2, "unit": "large", "calories": 144, "protein": 12.6}


@pytest.mark.parametrize("raw,expected", [
    ("breakfast", "breakfast"), ("Lunch", "lunch"), (" snack ", "snack"),
    ("brunch", None), ("", None), (None, None),
])
def test_intent_meal_type(raw, expected):
    assert _intent_meal_type({"meal_type": raw}) == expected


def test_named_breakfast_is_not_relabelled_by_clock(meal_db):
    from fitnessbot.bot.conversation import _act_meal
    with patch("fitnessbot.bot.conversation.parse_meal", return_value=[dict(EGG)]), \
         patch("fitnessbot.tz.user_hour", return_value=13):
        res = _act_meal({"meal_type": "breakfast"}, 1, "2 eggs for breakfast", "metric")
    conn = sqlite3.connect(meal_db)
    mt = conn.execute("SELECT meal_type FROM meals WHERE meal_id = ?", (res["meal_id"],)).fetchone()[0]
    conn.close()
    assert mt == "breakfast"


def test_unnamed_meal_still_inferred_from_local_hour(meal_db):
    from fitnessbot.bot.conversation import _act_meal
    with patch("fitnessbot.bot.conversation.parse_meal", return_value=[dict(EGG)]), \
         patch("fitnessbot.tz.user_hour", return_value=13):
        res = _act_meal({}, 1, "2 eggs", "metric")
    conn = sqlite3.connect(meal_db)
    mt = conn.execute("SELECT meal_type FROM meals WHERE meal_id = ?", (res["meal_id"],)).fetchone()[0]
    conn.close()
    assert mt == "lunch"


def test_brisk_walk_is_moderate():
    b = get_activity_benefits("brisk walk", 30, 80)
    assert b["intensity"] == "moderate"
    assert b["met_value"] == 4.3


def test_httpx_info_logs_are_silenced():
    import fitnessbot.main  # noqa: F401
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
