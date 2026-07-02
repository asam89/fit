"""Tests for the health_benefits module."""

import json
import unittest
from unittest.mock import patch, MagicMock

from fitnessbot.health_benefits import (
    _match_activity,
    calc_calories_burned,
    get_activity_benefits,
    get_daily_benefits,
    get_weekly_benefits,
    format_activity_benefit_telegram,
    format_daily_benefits_telegram,
    format_weekly_benefits_telegram,
    _intensity_label,
    _generate_weekly_insight,
    _suggest_missing_muscles,
    _get_user_weight_kg,
    ACTIVITY_MET_MAP,
    BENEFIT_LABELS,
)


class TestActivityMatching(unittest.TestCase):
    def test_exact_match(self):
        met, benefit, muscles = _match_activity("basketball")
        self.assertEqual(met, 6.5)
        self.assertEqual(benefit, "cardiovascular")
        self.assertIn("quadriceps", muscles)

    def test_case_insensitive(self):
        met, benefit, muscles = _match_activity("BASKETBALL")
        self.assertEqual(met, 6.5)

    def test_substring_match(self):
        met, benefit, muscles = _match_activity("heavy deadlift session")
        self.assertEqual(met, 6.0)
        self.assertEqual(benefit, "muscle_building")
        self.assertIn("back", muscles)

    def test_unknown_activity_defaults(self):
        met, benefit, muscles = _match_activity("underwater basket weaving")
        self.assertEqual(met, 4.0)
        self.assertEqual(benefit, "cardiovascular")
        self.assertEqual(muscles, ["full body"])

    def test_empty_activity(self):
        met, benefit, muscles = _match_activity("")
        self.assertEqual(met, 4.0)

    def test_none_activity(self):
        met, benefit, muscles = _match_activity(None)
        self.assertEqual(met, 4.0)

    def test_strength_activities(self):
        met, benefit, muscles = _match_activity("legs")
        self.assertEqual(benefit, "muscle_building")
        self.assertIn("quadriceps", muscles)
        self.assertIn("hamstrings", muscles)

    def test_cardio_activities(self):
        met, benefit, muscles = _match_activity("running")
        self.assertEqual(met, 9.8)
        self.assertEqual(benefit, "cardiovascular")

    def test_flexibility_activities(self):
        met, benefit, muscles = _match_activity("yoga")
        self.assertEqual(met, 3.0)
        self.assertEqual(benefit, "flexibility")

    def test_longest_keyword_wins(self):
        """'jump rope' should match before 'run' even though both might partially match."""
        met, benefit, muscles = _match_activity("jump rope")
        self.assertEqual(met, 10.0)


class TestCaloriesBurned(unittest.TestCase):
    def test_basic_calculation(self):
        # MET * 3.5 * weight_kg / 200 * duration
        # 5.0 * 3.5 * 80 / 200 * 30 = 210
        cal = calc_calories_burned(5.0, 80, 30)
        self.assertEqual(cal, 210)

    def test_zero_duration(self):
        cal = calc_calories_burned(5.0, 80, 0)
        self.assertEqual(cal, 0)

    def test_negative_duration(self):
        cal = calc_calories_burned(5.0, 80, -10)
        self.assertEqual(cal, 0)

    def test_high_met_activity(self):
        # Sprint: MET 12, 90kg, 20 min
        # 12 * 3.5 * 90 / 200 * 20 = 378
        cal = calc_calories_burned(12.0, 90, 20)
        self.assertEqual(cal, 378)

    def test_light_activity(self):
        # Walking: MET 3.5, 70kg, 60 min
        # 3.5 * 3.5 * 70 / 200 * 60 = 257.25 -> 257
        cal = calc_calories_burned(3.5, 70, 60)
        self.assertEqual(cal, 257)


class TestIntensityLabel(unittest.TestCase):
    def test_light(self):
        self.assertEqual(_intensity_label(2.5), "light")

    def test_moderate(self):
        self.assertEqual(_intensity_label(5.0), "moderate")

    def test_vigorous(self):
        self.assertEqual(_intensity_label(7.0), "vigorous")

    def test_high(self):
        self.assertEqual(_intensity_label(10.0), "high")

    def test_boundary_light_moderate(self):
        self.assertEqual(_intensity_label(3.0), "moderate")
        self.assertEqual(_intensity_label(2.9), "light")

    def test_boundary_moderate_vigorous(self):
        self.assertEqual(_intensity_label(6.0), "vigorous")
        self.assertEqual(_intensity_label(5.9), "moderate")


class TestActivityBenefits(unittest.TestCase):
    def test_returns_all_fields(self):
        result = get_activity_benefits("basketball", 45, 90)
        self.assertIn("calories_burned", result)
        self.assertIn("benefit_type", result)
        self.assertIn("benefit_label", result)
        self.assertIn("benefit_icon", result)
        self.assertIn("muscle_groups", result)
        self.assertIn("intensity", result)
        self.assertIn("met_value", result)
        self.assertIn("duration_min", result)

    def test_default_duration(self):
        result = get_activity_benefits("running", None, 80)
        self.assertEqual(result["duration_min"], 30)

    def test_specific_duration(self):
        result = get_activity_benefits("running", 45, 80)
        self.assertEqual(result["duration_min"], 45)

    def test_calorie_calculation_matches(self):
        result = get_activity_benefits("strength", 30, 80)
        expected = calc_calories_burned(5.0, 80, 30)
        self.assertEqual(result["calories_burned"], expected)


class TestGetUserWeightKg(unittest.TestCase):
    @patch("fitnessbot.metrics.get_weight_summary")
    def test_with_weight_data(self, mock_ws):
        mock_ws.return_value = {"has_data": True, "current_smoothed": 225.6}
        result = _get_user_weight_kg(1)
        self.assertAlmostEqual(result, 225.6 * 0.453592, places=1)

    @patch("fitnessbot.metrics.get_weight_summary")
    def test_fallback_no_data(self, mock_ws):
        mock_ws.return_value = {"has_data": False}
        result = _get_user_weight_kg(1)
        self.assertEqual(result, 80.0)


class TestDailyBenefits(unittest.TestCase):
    @patch("fitnessbot.health_benefits._get_user_weight_kg")
    @patch("fitnessbot.health_benefits.db.get_connection")
    @patch("fitnessbot.tz.day_utc_range")
    @patch("fitnessbot.tz.user_today")
    def test_empty_day(self, mock_today, mock_range, mock_conn, mock_weight):
        mock_today.return_value = "2025-01-15"
        mock_range.return_value = ("2025-01-15T05:00:00Z", "2025-01-16T04:59:59Z")
        mock_weight.return_value = 80.0

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        mock_conn.return_value = conn

        result = get_daily_benefits(1)
        self.assertEqual(result["session_count"], 0)
        self.assertEqual(result["total_calories_burned"], 0)

    @patch("fitnessbot.health_benefits._get_user_weight_kg")
    @patch("fitnessbot.health_benefits.db.get_connection")
    @patch("fitnessbot.tz.day_utc_range")
    @patch("fitnessbot.tz.user_today")
    def test_with_plan_items(self, mock_today, mock_range, mock_conn, mock_weight):
        mock_today.return_value = "2025-01-15"
        mock_range.return_value = ("2025-01-15T05:00:00Z", "2025-01-16T04:59:59Z")
        mock_weight.return_value = 80.0

        conn = MagicMock()
        # First call: training_plan_items
        plan_rows = [
            {"title": "Legs", "activity_type": "strength", "planned_duration_min": 45, "linked_exercise_id": None}
        ]
        # Second call: health_data
        hd_rows = []
        conn.execute.return_value.fetchall.side_effect = [plan_rows, hd_rows]
        mock_conn.return_value = conn

        result = get_daily_benefits(1, "2025-01-15")
        self.assertEqual(result["session_count"], 1)
        self.assertGreater(result["total_calories_burned"], 0)
        self.assertIn("quadriceps", result["muscle_groups_worked"])

    @patch("fitnessbot.health_benefits._get_user_weight_kg")
    @patch("fitnessbot.health_benefits.db.get_connection")
    @patch("fitnessbot.tz.day_utc_range")
    @patch("fitnessbot.tz.user_today")
    def test_dedup_plan_sourced_entries(self, mock_today, mock_range, mock_conn, mock_weight):
        mock_today.return_value = "2025-01-15"
        mock_range.return_value = ("2025-01-15T05:00:00Z", "2025-01-16T04:59:59Z")
        mock_weight.return_value = 80.0

        conn = MagicMock()
        plan_rows = [
            {"title": "Running", "activity_type": "run", "planned_duration_min": 30, "linked_exercise_id": 5}
        ]
        hd_rows = [
            {"data_json": json.dumps({"type": "run", "source": "training_plan"}), "recorded_at": "2025-01-15T10:00:00Z"}
        ]
        conn.execute.return_value.fetchall.side_effect = [plan_rows, hd_rows]
        mock_conn.return_value = conn

        result = get_daily_benefits(1, "2025-01-15")
        # Should only count the plan item, not the duplicate health_data entry
        self.assertEqual(result["session_count"], 1)


class TestWeeklyInsight(unittest.TestCase):
    def test_no_sessions(self):
        insight = _generate_weekly_insight(0, 0, 0, 0, {}, set())
        self.assertIn("No workouts", insight)

    def test_high_activity(self):
        insight = _generate_weekly_insight(
            6, 5, 2500, 300,
            {"cardiovascular": {"count": 3, "calories": 1200, "duration": 150},
             "muscle_building": {"count": 3, "calories": 1300, "duration": 150}},
            {"quadriceps", "hamstrings", "chest", "back"}
        )
        self.assertIn("5 active days", insight)
        self.assertIn("excellent", insight)

    def test_cardio_only(self):
        insight = _generate_weekly_insight(
            3, 3, 1500, 180,
            {"cardiovascular": {"count": 3, "calories": 1500, "duration": 180}},
            {"quadriceps", "calves"}
        )
        self.assertIn("strength", insight.lower())

    def test_strength_only(self):
        insight = _generate_weekly_insight(
            3, 3, 1000, 120,
            {"muscle_building": {"count": 3, "calories": 1000, "duration": 120}},
            {"chest", "back", "shoulders"}
        )
        self.assertIn("cardio", insight.lower())

    def test_well_rounded(self):
        insight = _generate_weekly_insight(
            4, 4, 2000, 240,
            {"cardiovascular": {"count": 2, "calories": 1000, "duration": 120},
             "muscle_building": {"count": 1, "calories": 500, "duration": 60},
             "flexibility": {"count": 1, "calories": 500, "duration": 60}},
            set()
        )
        self.assertIn("Well-rounded", insight)


class TestSuggestMissingMuscles(unittest.TestCase):
    def test_suggests_missing(self):
        worked = {"quadriceps", "chest"}
        missing = _suggest_missing_muscles(worked)
        self.assertIn("hamstrings", missing)
        self.assertIn("back", missing)
        self.assertNotIn("quadriceps", missing)

    def test_full_body_excluded(self):
        worked = {"full body", "quadriceps"}
        missing = _suggest_missing_muscles(worked)
        self.assertNotIn("full body", missing)


class TestTelegramFormatting(unittest.TestCase):
    def test_format_activity_benefit(self):
        benefit = {
            "benefit_icon": "\U0001f4aa",
            "benefit_label": "Muscle Building & Strength",
            "calories_burned": 210,
            "duration_min": 30,
            "intensity": "moderate",
            "muscle_groups": ["chest", "triceps", "shoulders"],
        }
        text = format_activity_benefit_telegram(benefit)
        self.assertIn("210 cal", text)
        self.assertIn("Muscle Building", text)
        self.assertIn("chest", text)

    def test_format_daily_empty(self):
        daily = {"session_count": 0}
        text = format_daily_benefits_telegram(daily)
        self.assertEqual(text, "")

    def test_format_daily_with_data(self):
        daily = {
            "session_count": 2,
            "date": "2025-01-15",
            "total_calories_burned": 450,
            "total_duration_min": 75,
            "activities": [
                {
                    "activity": "running",
                    "benefit_type": "cardiovascular",
                    "benefit_icon": "\u2764\ufe0f",
                    "benefit_label": "Cardio & Heart Health",
                    "calories_burned": 300,
                    "duration_min": 30,
                    "intensity": "vigorous",
                    "muscle_groups": ["quadriceps", "hamstrings", "calves"],
                    "met_value": 9.8,
                }
            ],
            "muscle_groups_worked": ["quadriceps", "hamstrings", "calves"],
        }
        text = format_daily_benefits_telegram(daily)
        self.assertIn("450 cal", text)
        self.assertIn("Health Benefits", text)

    def test_format_weekly_no_sessions(self):
        weekly = {"total_sessions": 0}
        text = format_weekly_benefits_telegram(weekly)
        self.assertIn("No workouts", text)

    def test_format_weekly_with_data(self):
        weekly = {
            "total_sessions": 5,
            "active_days": 4,
            "total_calories_burned": 2500,
            "total_duration_min": 300,
            "benefit_breakdown": {
                "cardiovascular": {"count": 3, "calories": 1500, "duration": 180},
                "muscle_building": {"count": 2, "calories": 1000, "duration": 120},
            },
            "insight": "Good mix of cardio and strength.",
        }
        text = format_weekly_benefits_telegram(weekly)
        self.assertIn("2,500", text)
        self.assertIn("4/7", text)
        self.assertIn("Cardio", text)


class TestMetDatabase(unittest.TestCase):
    """Verify MET database has reasonable values."""

    def test_running_higher_than_walking(self):
        run_met = ACTIVITY_MET_MAP["run"][0]
        walk_met = ACTIVITY_MET_MAP["walk"][0]
        self.assertGreater(run_met, walk_met)

    def test_sprint_highest_cardio(self):
        sprint_met = ACTIVITY_MET_MAP["sprint"][0]
        run_met = ACTIVITY_MET_MAP["run"][0]
        self.assertGreater(sprint_met, run_met)

    def test_all_mets_positive(self):
        for key, (met, _, _) in ACTIVITY_MET_MAP.items():
            self.assertGreater(met, 0, f"MET for '{key}' should be positive")

    def test_all_benefit_types_valid(self):
        valid_types = set(BENEFIT_LABELS.keys())
        for key, (_, benefit, _) in ACTIVITY_MET_MAP.items():
            self.assertIn(benefit, valid_types, f"Invalid benefit type for '{key}': {benefit}")


if __name__ == "__main__":
    unittest.main()
