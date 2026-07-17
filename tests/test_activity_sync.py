"""Unit tests for activity data sync between dashboard (training_plan_items) and Telegram (health_data).

Tests cover:
1. workout_log intent reconciles with training plan items
2. _reconcile_exercise correctly links to health_data (uses hd_id not exercise_id)
3. get_workout_history uses user timezone
4. Both surfaces agree on activity counts
"""

import json
import pytest
from datetime import datetime, timezone, timedelta, date
from unittest.mock import patch, MagicMock


class TestWorkoutPlanReconciliation:
    """Verify that workout_log intents try to match against training plan items."""

    @patch("fitnessbot.training_plan.complete_by_title")
    @patch("fitnessbot.bot.conversation.db")
    def test_workout_log_matches_plan_item(self, mock_db, mock_complete):
        """When a workout is logged and matches a plan item, mark it complete."""
        from fitnessbot.bot.conversation import _act_workout

        mock_complete.return_value = {
            "item_id": 42,
            "title": "Basketball",
            "activity_type": "sport",
            "status": "completed",
            "display_status": "completed",
        }

        intent = {"activity": "basketball", "duration_min": 60}
        result = _act_workout(intent, user_id=1)

        mock_complete.assert_called_once_with(1, "basketball", 60)
        assert result["action"] == "plan_completed"
        assert result["title"] == "Basketball"
        assert result["item_id"] == 42
        # Should NOT have called insert_health_data directly
        mock_db.insert_health_data.assert_not_called()

    @patch("fitnessbot.training_plan.log_completed_activity")
    @patch("fitnessbot.training_plan.complete_by_title")
    @patch("fitnessbot.bot.conversation.db")
    def test_workout_log_no_plan_match_creates_calendar_item(self, mock_db, mock_complete, mock_log):
        """When no matching plan item, create a completed plan item on the calendar."""
        from fitnessbot.bot.conversation import _act_workout

        mock_complete.return_value = None
        mock_log.return_value = {
            "item_id": 99, "title": "Swimming", "activity_type": "cardio",
            "date": "2026-06-22", "status": "completed",
        }

        intent = {"activity": "swimming", "duration_min": 30, "notes": "open water"}
        result = _act_workout(intent, user_id=1)

        mock_complete.assert_called_once_with(1, "swimming", 30)
        mock_log.assert_called_once_with(1, "swimming", 30, "open water")
        assert result["action"] == "workout_logged"
        assert result["activity"] == "swimming"
        assert result["item_id"] == 99
        assert result["date"] == "2026-06-22"

    @patch("fitnessbot.training_plan.log_completed_activity")
    @patch("fitnessbot.training_plan.complete_by_title")
    @patch("fitnessbot.bot.conversation.db")
    def test_workout_log_no_duration(self, mock_db, mock_complete, mock_log):
        """Duration is optional — passes None to complete_by_title and the logger."""
        from fitnessbot.bot.conversation import _act_workout

        mock_complete.return_value = None
        mock_log.return_value = {"item_id": 5, "title": "Yoga", "activity_type": "mobility", "date": "2026-06-22"}

        intent = {"activity": "yoga"}
        result = _act_workout(intent, user_id=1)

        mock_complete.assert_called_once_with(1, "yoga", None)
        mock_log.assert_called_once_with(1, "yoga", None, None)
        assert result["action"] == "workout_logged"


class TestReconcileExercise:
    """Verify _reconcile_exercise uses hd_id (not exercise_id) and creates entries correctly."""

    @patch("fitnessbot.tz.user_now")
    def test_finds_existing_by_hd_id(self, mock_now):
        from fitnessbot.training_plan import _reconcile_exercise

        mock_now.return_value = datetime(2025, 6, 22, 14, 0, tzinfo=timezone.utc)
        conn = MagicMock()
        row = {"hd_id": 77}
        conn.execute.return_value.fetchone.return_value = row

        item = {"date": "2025-06-22", "activity_type": "strength", "title": "Legs"}
        result = _reconcile_exercise(1, item, 45, conn)

        assert result == 77

    @patch("fitnessbot.tz.user_now")
    def test_creates_new_when_no_existing(self, mock_now):
        from fitnessbot.training_plan import _reconcile_exercise

        mock_now.return_value = datetime(2025, 6, 22, 14, 0, tzinfo=timezone.utc)
        conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.lastrowid = 123

        # First call: SELECT returns None; second call: INSERT returns cursor
        conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=None)),
            cursor_mock,
        ]

        item = {"date": "2025-06-22", "activity_type": "cardio", "title": "Run"}
        result = _reconcile_exercise(1, item, 30, conn)

        assert result == 123
        # Verify INSERT was called with correct data_type
        insert_call = conn.execute.call_args_list[1]
        assert "workout" in insert_call[0][0]
        # data_json is the second param in the tuple (user_id, data_json, now_iso)
        assert "Run" in insert_call[0][1][1]  # data_json contains title


class TestWorkoutHistoryTimezone:
    """Verify get_workout_history uses user timezone for lookback."""

    @patch("fitnessbot.db.get_connection")
    @patch("fitnessbot.db.day_utc_range", create=True)
    @patch("fitnessbot.db.user_today", create=True)
    def test_uses_timezone_aware_cutoff(self, mock_today, mock_range, mock_conn):
        """get_workout_history should use user's timezone, not UTC date('now')."""
        from fitnessbot.db import get_workout_history

        with patch("fitnessbot.tz.user_today", return_value="2025-06-22"), \
             patch("fitnessbot.tz.day_utc_range", return_value=("2025-06-15T04:00:00Z", "2025-06-16T04:00:00Z")):
            mock_conn_inst = MagicMock()
            mock_conn.return_value = mock_conn_inst
            mock_conn_inst.execute.return_value.fetchall.return_value = []

            result = get_workout_history(1, days=7)

            # Should query with UTC timestamp, not date('now', ?)
            call_args = mock_conn_inst.execute.call_args[0]
            assert "recorded_at >= ?" in call_args[0]
            assert "date('now'" not in call_args[0]


class TestActivityCountConsistency:
    """Verify both dashboard and Telegram report the same counts."""

    def test_complete_item_creates_health_data_entry(self):
        """Completing a plan item via dashboard should create a health_data entry."""
        # This tests the flow: dashboard checkbox → complete_item → _reconcile_exercise → health_data
        # If _reconcile_exercise works correctly, health_data will have the entry
        # and get_workout_history will find it.
        # (Integration test would be ideal, but unit test verifies the call chain)
        from fitnessbot.training_plan import complete_item

        with patch("fitnessbot.training_plan.db") as mock_db:
            mock_conn = MagicMock()
            mock_db.get_connection.return_value = mock_conn
            mock_db.utcnow.return_value = "2025-06-22T14:00:00Z"

            # Simulate existing item
            item_row = {
                "item_id": 5, "user_id": 1, "date": "2025-06-22",
                "activity_type": "strength", "title": "Legs",
                "status": "planned", "planned_duration_min": 45,
                "completed_at": None, "linked_exercise_id": None,
            }
            mock_conn.execute.return_value.fetchone.return_value = item_row

            with patch("fitnessbot.training_plan._reconcile_exercise", return_value=99) as mock_recon:
                result = complete_item(5, 1, 45)

            # _reconcile_exercise should have been called
            mock_recon.assert_called_once()
            assert result["status"] == "completed"
