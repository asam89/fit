"""Tests for ad-hoc activity → dashboard calendar sync and the day picker.

Covers:
- infer_activity_type keyword mapping
- log_completed_activity creates a completed plan item (visible on the calendar)
- reassign_item_date moves the item (and its linked workout) to another day
- _day_picker_for_actions only builds a keyboard for a freshly logged workout
"""
import sqlite3
from datetime import date, timedelta
from unittest.mock import patch

import pytest


def _build_db(db_mod, db_path):
    """Build a real DB with the migration-era training-plan schema.

    init_db() stamps the latest schema_version and installs the legacy
    training_plans table; resetting the version then running migrations
    rebuilds the weekly-plan tables (training_plan_items) as in production.
    """
    db_mod.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM schema_version")
    conn.commit()
    conn.close()
    db_mod.run_migrations()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
        ("t@t.com", "hash", "T", "America/Toronto"),
    )
    conn.commit()
    conn.close()
    return 1


class TestInferActivityType:
    def test_maps_common_activities(self):
        from fitnessbot.training_plan import infer_activity_type
        assert infer_activity_type("leg day weights") == "strength"
        assert infer_activity_type("Basketball") == "sport"
        assert infer_activity_type("5k run") == "run"
        assert infer_activity_type("yoga flow") == "mobility"
        assert infer_activity_type("spin class") == "cardio"
        assert infer_activity_type("underwater basket weaving") == "other"
        assert infer_activity_type("") == "other"


class TestLogCompletedActivity:
    def test_creates_completed_calendar_item(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            from fitnessbot import training_plan
            uid = _build_db(db_mod, db_path)

            item = training_plan.log_completed_activity(uid, "Basketball", 60)
            assert item["item_id"]
            assert item["status"] == "completed"

            ws = training_plan.get_current_week_start(uid)
            items = training_plan.get_plan_items(uid, ws)
            titles = [(i["title"], i["status"], i["activity_type"]) for i in items]
            assert ("Basketball", "completed", "sport") in titles

    def test_assign_specific_day(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            from fitnessbot import training_plan
            uid = _build_db(db_mod, db_path)

            ws = training_plan.get_current_week_start(uid)
            wednesday = (date.fromisoformat(ws) + timedelta(days=2)).isoformat()
            item = training_plan.log_completed_activity(uid, "Legs", 45, date_str=wednesday)
            assert item["date"] == wednesday

            items = training_plan.get_plan_items(uid, ws)
            wed_items = [i for i in items if i["date"] == wednesday]
            assert any(i["title"] == "Legs" and i["status"] == "completed" for i in wed_items)


class TestReassignItemDate:
    def test_moves_item_to_new_day(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            from fitnessbot import training_plan
            uid = _build_db(db_mod, db_path)

            ws = training_plan.get_current_week_start(uid)
            item = training_plan.log_completed_activity(uid, "Run", 30)
            original_date = item["date"]
            friday = (date.fromisoformat(ws) + timedelta(days=4)).isoformat()

            moved = training_plan.reassign_item_date(item["item_id"], uid, friday)
            assert moved is not None
            assert moved["date"] == friday

            items = training_plan.get_plan_items(uid, ws)
            found = [i for i in items if i["item_id"] == item["item_id"]]
            assert found and found[0]["date"] == friday
            # No leftover on the original day
            assert not any(i["item_id"] == item["item_id"] and i["date"] == original_date
                           for i in items if i["date"] != friday)

    def test_reassign_missing_item_returns_none(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            from fitnessbot import training_plan
            uid = _build_db(db_mod, db_path)
            assert training_plan.reassign_item_date(9999, uid, "2026-06-22") is None


class TestDayPicker:
    def test_builds_keyboard_for_logged_workout(self):
        from fitnessbot.bot.handlers import _day_picker_for_actions
        with patch("fitnessbot.bot.handlers.user_today", return_value="2026-06-22"):
            markup = _day_picker_for_actions(
                1, [{"action": "workout_logged", "item_id": 7, "date": "2026-06-22"}]
            )
        assert markup is not None
        buttons = [b for row in markup.inline_keyboard for b in row]
        assert len(buttons) == 7
        assert all(b.callback_data.startswith("wkday:7:") for b in buttons)

    def test_no_keyboard_when_no_workout(self):
        from fitnessbot.bot.handlers import _day_picker_for_actions
        assert _day_picker_for_actions(1, [{"action": "meal_logged"}]) is None
        assert _day_picker_for_actions(1, []) is None

    def test_no_keyboard_for_matched_plan(self):
        from fitnessbot.bot.handlers import _day_picker_for_actions
        # plan_completed means it already matched an existing calendar item
        assert _day_picker_for_actions(1, [{"action": "plan_completed", "item_id": 3}]) is None
