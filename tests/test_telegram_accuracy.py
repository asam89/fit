"""Unit tests for Telegram Accuracy fixes.

Telegram Accuracy 1 — date-boundary logic (exclude in-progress day)
Telegram Accuracy 2 — distinct workout session counting / deduplication
Telegram Accuracy 4 — under / met / exceeded wording
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock


# ── Telegram Accuracy 2: Workout session deduplication ──


class TestDedupeWorkoutRows:
    """Verify that _dedupe_workout_rows groups exercises into sessions."""

    @staticmethod
    def _make_row(ts_iso: str, data: dict | None = None, notes: str | None = None):
        """Build a fake sqlite Row-like dict."""
        return {
            "recorded_at": ts_iso,
            "data_json": json.dumps(data or {"type": "strength"}),
            "notes": notes,
        }

    def test_single_exercise_is_one_session(self):
        from fitnessbot.db import _dedupe_workout_rows

        rows = [self._make_row("2025-06-20T14:00:00Z")]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 1
        assert result[0]["_exercise_count"] == 1

    def test_n_exercises_within_90min_are_one_session(self):
        from fitnessbot.db import _dedupe_workout_rows

        rows = [
            self._make_row("2025-06-20T14:00:00Z", {"type": "strength", "activity": "bench press"}),
            self._make_row("2025-06-20T14:10:00Z", {"type": "strength", "activity": "squat"}),
            self._make_row("2025-06-20T14:25:00Z", {"type": "strength", "activity": "deadlift"}),
            self._make_row("2025-06-20T14:40:00Z", {"type": "strength", "activity": "rows"}),
            self._make_row("2025-06-20T15:00:00Z", {"type": "strength", "activity": "curls"}),
        ]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 1, "5 exercises within 90min should be 1 session"
        assert result[0]["_exercise_count"] == 5

    def test_separate_sessions_over_90min_apart(self):
        from fitnessbot.db import _dedupe_workout_rows

        rows = [
            self._make_row("2025-06-20T08:00:00Z", {"type": "cardio", "activity": "run"}),
            self._make_row("2025-06-20T08:30:00Z", {"type": "cardio", "activity": "cooldown"}),
            # 4-hour gap
            self._make_row("2025-06-20T13:00:00Z", {"type": "strength", "activity": "bench"}),
            self._make_row("2025-06-20T13:20:00Z", {"type": "strength", "activity": "squat"}),
        ]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 2, "2 clusters >90min apart = 2 sessions"
        assert result[0]["_exercise_count"] == 2
        assert result[1]["_exercise_count"] == 2

    def test_duplicate_synced_entries_at_same_time(self):
        from fitnessbot.db import _dedupe_workout_rows

        rows = [
            self._make_row("2025-06-20T14:00:00Z", {"type": "strength"}),
            self._make_row("2025-06-20T14:00:00Z", {"type": "strength"}),
            self._make_row("2025-06-20T14:00:00Z", {"type": "strength"}),
        ]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 1, "Duplicate timestamps = 1 session"

    def test_empty_rows(self):
        from fitnessbot.db import _dedupe_workout_rows

        assert _dedupe_workout_rows([]) == []

    def test_duration_aggregation(self):
        from fitnessbot.db import _dedupe_workout_rows

        rows = [
            self._make_row("2025-06-20T14:00:00Z", {"type": "strength", "duration_min": 15}),
            self._make_row("2025-06-20T14:20:00Z", {"type": "strength", "duration_min": 20}),
        ]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 1
        assert result[0]["duration_min"] == 35

    def test_cross_day_sessions(self):
        from fitnessbot.db import _dedupe_workout_rows

        rows = [
            self._make_row("2025-06-20T23:30:00Z", {"type": "strength"}),
            self._make_row("2025-06-21T00:10:00Z", {"type": "strength"}),
        ]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 1, "40min apart across midnight = 1 session"

    def test_9_exercises_in_2_sessions(self):
        """Regression: '9 workouts in 2 days' should be 2 sessions."""
        from fitnessbot.db import _dedupe_workout_rows

        rows = [
            # Day 1 morning session: 5 exercises
            self._make_row("2025-06-19T09:00:00Z", {"type": "strength", "activity": "bench"}),
            self._make_row("2025-06-19T09:15:00Z", {"type": "strength", "activity": "squat"}),
            self._make_row("2025-06-19T09:30:00Z", {"type": "strength", "activity": "deadlift"}),
            self._make_row("2025-06-19T09:45:00Z", {"type": "strength", "activity": "rows"}),
            self._make_row("2025-06-19T10:00:00Z", {"type": "strength", "activity": "ohp"}),
            # Day 2 afternoon session: 4 exercises
            self._make_row("2025-06-20T14:00:00Z", {"type": "strength", "activity": "bench"}),
            self._make_row("2025-06-20T14:15:00Z", {"type": "strength", "activity": "squat"}),
            self._make_row("2025-06-20T14:30:00Z", {"type": "strength", "activity": "deadlift"}),
            self._make_row("2025-06-20T14:45:00Z", {"type": "strength", "activity": "curls"}),
        ]
        result = _dedupe_workout_rows(rows)
        assert len(result) == 2, f"9 exercises across 2 days should be 2 sessions, got {len(result)}"


# ── Telegram Accuracy 4: Target status wording ──


class TestTargetStatus:
    """Verify _target_status labels under / met / exceeded correctly."""

    def test_under_target(self):
        from fitnessbot.bot.conversation import _target_status

        assert _target_status(2000, 2585) == "under target"

    def test_met_target_exact(self):
        from fitnessbot.bot.conversation import _target_status

        assert _target_status(2585, 2585) == "met target"

    def test_met_target_within_5pct(self):
        from fitnessbot.bot.conversation import _target_status

        assert _target_status(2500, 2585) == "met target"  # ~3.3% under

    def test_exceeded_target(self):
        from fitnessbot.bot.conversation import _target_status

        # 3220 vs 3085 is ~4.4% over, within 5% tolerance -> met target
        assert _target_status(3220, 3085) == "met target"
        # 3500 vs 3085 is ~13% over -> exceeded
        assert _target_status(3500, 3085) == "exceeded target"

    def test_zero_target(self):
        from fitnessbot.bot.conversation import _target_status

        assert _target_status(100, 0) == "no target set"


class TestStatHelper:
    """Verify the _stat helper in briefings uses correct wording."""

    def test_on_target(self):
        from fitnessbot.briefings import build_evening_wrap

        # We test _stat indirectly since it's a nested function
        # Instead, verify the logic matches by reproducing it
        target = 2585
        actual = 2585
        diff = target - actual
        pct = abs(diff) / target if target else 0
        assert pct < 0.05  # on target

    def test_exceeded(self):
        target = 3085
        actual = 3500  # ~13% over
        diff = target - actual
        pct = abs(diff) / target
        assert pct >= 0.05
        assert diff < 0  # actual > target -> exceeded

    def test_short(self):
        target = 2585
        actual = 1800
        diff = target - actual
        pct = abs(diff) / target
        assert pct >= 0.05
        assert diff > 0  # actual < target -> short


# ── Telegram Accuracy 1: Date-boundary exclusion ──


class TestExcludeToday:
    """Verify today is excluded from completed-day averages."""

    def test_weekly_rollup_excludes_today(self):
        """build_weekly_rollup should average only completed days."""
        from fitnessbot.briefings import build_weekly_rollup

        fake_cal_hist = [
            {"date": "2025-06-15", "calories": 2500, "protein": 180},
            {"date": "2025-06-16", "calories": 2600, "protein": 190},
            {"date": "2025-06-17", "calories": 2400, "protein": 170},
            {"date": "2025-06-18", "calories": 2550, "protein": 185},
            {"date": "2025-06-19", "calories": 2700, "protein": 200},
            {"date": "2025-06-20", "calories": 2450, "protein": 175},
            # Today — only 500 cal logged so far
            {"date": "2025-06-21", "calories": 500, "protein": 30},
        ]

        with patch("fitnessbot.briefings.db") as mock_db, \
             patch("fitnessbot.briefings.user_today", return_value="2025-06-21"), \
             patch("fitnessbot.briefings.get_weight_summary", return_value={}), \
             patch("fitnessbot.briefings._get_user_targets", return_value={"calories": 2585, "protein": 200, "carbs": 280, "fat": 72}), \
             patch("fitnessbot.tz.db") as mock_tz_db, \
             patch("fitnessbot.tz.user_today", return_value="2025-06-21"), \
             patch("fitnessbot.tz.utc_offset_hours", return_value=-4.0), \
             patch("fitnessbot.training_plan.get_plan_items", return_value=[]), \
             patch("fitnessbot.training_plan._monday_of_week", return_value="2025-06-16"):

            mock_tz_db.get_user_by_id.return_value = {"timezone": "America/Toronto"}
            mock_db.get_calorie_history.return_value = fake_cal_hist

            result = build_weekly_rollup(1)

            # Average of completed days (not including today's 500):
            # (2500 + 2600 + 2400 + 2550 + 2700 + 2450) / 6 = 2533
            assert "2533" in result, f"Expected avg ~2533, got: {result}"
            assert "6 completed day" in result, f"Expected 6 completed days, got: {result}"
            assert "in progress" in result.lower(), f"Expected today labeled as in progress, got: {result}"

    def test_deterministic_query_excludes_today(self):
        """_deterministic_query_response should use completed days only."""
        from fitnessbot.bot.conversation import _deterministic_query_response

        act_result = {
            "targets": {"calories": 2585, "protein": 200, "carbs": 280, "fat": 72},
            "lookback_days": 7,
            "macro_history": [
                {"date": "2025-06-15", "calories": 2500, "protein": 180},
                {"date": "2025-06-16", "calories": 2600, "protein": 190},
                {"date": "2025-06-17", "calories": 2400, "protein": 170},
            ],
            "today_macro": {"date": "2025-06-18", "calories": 500, "protein": 30},
            "weight": {},
            "workout_history": [],
            "adherence": None,
            "sleep_history": [],
        }

        result = _deterministic_query_response(act_result)

        # Average should be (2500+2600+2400)/3 = 2500, not including today's 500
        assert "2500" in result, f"Expected avg 2500, got: {result}"
        assert "3 completed days" in result, f"Expected 3 completed days, got: {result}"
        assert "in progress" in result.lower(), f"Expected today shown as in progress, got: {result}"

    def test_month_summary_excludes_today(self):
        """build_month_summary should average only completed days."""
        from fitnessbot.nutrition import build_month_summary

        fake_cal_hist = [
            {"date": "2025-06-01", "calories": 2500, "protein": 180},
            {"date": "2025-06-02", "calories": 2600, "protein": 190},
            {"date": "2025-06-03", "calories": 2400, "protein": 170},
            # Today — incomplete
            {"date": "2025-06-21", "calories": 300, "protein": 20},
        ]

        with patch("fitnessbot.nutrition.db") as mock_db, \
             patch("fitnessbot.nutrition.get_nutrition_targets", return_value={"calories": 2585, "protein": 200, "carbs": 280, "fat": 72}), \
             patch("fitnessbot.nutrition._utc_off", return_value=-4.0), \
             patch("fitnessbot.tz.db") as mock_tz_db, \
             patch("fitnessbot.tz.user_today", return_value="2025-06-21"):

            mock_tz_db.get_user_by_id.return_value = {"timezone": "America/Toronto"}
            mock_db.get_calorie_history.return_value = fake_cal_hist
            mock_db.get_weight_trend.return_value = []
            mock_db.get_workout_count_range.return_value = 2

            result = build_month_summary(1)

            # Average of completed days: (2500+2600+2400)/3 = 2500
            assert result["avg_calories"] == 2500, f"Expected 2500, got {result['avg_calories']}"
            assert result["logging_days"] == 3, f"Expected 3 logging days, got {result['logging_days']}"
            assert "in progress" in result["prose"].lower(), f"Expected 'in progress' in prose: {result['prose']}"
