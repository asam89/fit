"""Tests for food-suggestion variety and the indulgent-meal coaching nudge.

Covers:
- meal quality scoring (indulgent vs solid) from names + macros
- movement options framing
- varied fallback nudges
- food suggestions rotate, respect calorie budget, meal time, and recent meals
"""
from unittest.mock import patch

from fitnessbot import meal_quality
from fitnessbot.bot import conversation


def _item(name, **kw):
    base = {"name": name, "calories": 0, "protein": 0, "carbs": 0,
            "fat": 0, "fiber": 0, "sugar": 0, "sodium": 0}
    base.update(kw)
    return base


class TestScoreMeal:
    def test_fast_food_combo_is_indulgent(self):
        items = [_item("Big Mac", calories=560, protein=25, fat=33, sodium=1010),
                 _item("large fries", calories=490, fat=23, sodium=400),
                 _item("Coke", calories=210, sugar=58)]
        q = meal_quality.score_meal(items)
        assert q["is_indulgent"]
        assert q["label"] == "indulgent"
        assert "fast food" in q["flags"]
        assert any("sugar" in f for f in q["flags"])

    def test_grilled_chicken_and_veg_is_solid(self):
        items = [_item("grilled chicken breast", calories=280, protein=52, fat=6),
                 _item("broccoli and quinoa", calories=220, protein=8, carbs=38, fiber=9)]
        q = meal_quality.score_meal(items)
        assert q["label"] == "solid"
        assert not q["is_indulgent"]
        assert "solid protein source" in q["positives"]
        assert "vegetables on the plate" in q["positives"]

    def test_macro_only_flags_without_keywords(self):
        items = [_item("mystery platter", calories=1100, protein=8, fat=60, fiber=1)]
        q = meal_quality.score_meal(items)
        assert q["is_indulgent"]
        assert any("only 8g protein" in f for f in q["flags"])
        assert "fat-heavy with almost no fiber" in q["flags"]

    def test_empty_meal_is_neutral(self):
        q = meal_quality.score_meal([])
        assert not q["is_indulgent"]
        assert q["flags"] == []

    def test_score_is_clamped(self):
        items = [_item("fried donut soda burger candy beer", calories=2000,
                       sugar=200, sodium=5000, fat=120)]
        q = meal_quality.score_meal(items)
        assert 0 <= q["score"] <= 100


class TestMovementOptions:
    def test_returns_achievable_bouts_with_burn(self):
        opts = meal_quality.movement_options(weight_kg=80)
        assert len(opts) == 3
        assert all(o.startswith("~") and "cal)" in o for o in opts)

    def test_never_suggests_punishing_durations(self):
        """Bouts are fixed and achievable, not 'burn off what you ate' math."""
        for _ in range(30):
            for opt in meal_quality.movement_options(weight_kg=120):
                minutes = int(opt.split(" ")[0].lstrip("~"))
                assert 10 <= minutes <= 45, opt

    def test_options_rotate(self):
        seen = {tuple(meal_quality.movement_options()) for _ in range(40)}
        assert len(seen) > 1


class TestQualitySignal:
    def test_indulgent_without_workout_includes_movement(self):
        items = [_item("cheeseburger and fries", calories=900, protein=25, fat=50, sodium=1500)]
        sig = meal_quality.build_meal_quality_signal(items, weight_kg=80, worked_out_today=False)
        assert "MEAL QUALITY: indulgent" in sig
        assert "no workout logged today" in sig
        assert "suggest getting moving" in sig

    def test_indulgent_with_workout_skips_movement_nudge(self):
        items = [_item("cheeseburger and fries", calories=900, protein=25, fat=50, sodium=1500)]
        sig = meal_quality.build_meal_quality_signal(items, weight_kg=80, worked_out_today=True)
        assert "no workout logged today" not in sig


class TestFallbackNudge:
    def test_indulgent_nudge_varies_and_pushes_movement(self):
        items = [_item("pizza", calories=800, fat=45, sodium=1400)]
        seen = {meal_quality.fallback_nudge(items, worked_out_today=False) for _ in range(60)}
        assert len(seen) > 1, "nudges should not be a single fixed sentence"
        assert any("min" in s for s in seen)

    def test_solid_meal_gets_praise(self):
        items = [_item("salmon and spinach salad", calories=450, protein=40, fiber=9)]
        assert meal_quality.fallback_nudge(items) in meal_quality._SOLID_PRAISE

    def test_middling_meal_gets_no_nudge(self):
        items = [_item("plain bagel", calories=250, carbs=48)]
        assert meal_quality.score_meal(items)["label"] == "mixed"
        assert meal_quality.fallback_nudge(items) == ""


class TestFoodSuggestionVariety:
    TARGETS = {"calories": 2500, "protein": 180, "carbs": 250, "fat": 70}
    TOTALS = {"calories": 800, "protein": 40, "carbs": 80, "fat": 20}

    def test_does_not_always_suggest_chicken_breast(self):
        with patch("fitnessbot.bot.conversation._current_meal_time", return_value="any"), \
             patch("fitnessbot.bot.conversation._recently_eaten_names", return_value=""):
            firsts = set()
            for _ in range(40):
                out = conversation._get_food_suggestions(self.TARGETS, self.TOTALS, user_id=1)
                firsts.add(out.split("Options: ")[1].split(",")[0])
        assert len(firsts) > 3, f"suggestions not rotating: {firsts}"

    def test_excludes_recently_eaten_foods(self):
        recent = "grilled chicken breast greek yogurt eggs canned tuna protein shake"
        with patch("fitnessbot.bot.conversation._current_meal_time", return_value="any"), \
             patch("fitnessbot.bot.conversation._recently_eaten_names", return_value=recent):
            for _ in range(25):
                out = conversation._get_food_suggestions(self.TARGETS, self.TOTALS, user_id=1)
                opts = out.split("Options: ")[1]
                assert "grilled chicken breast" not in opts
                assert "canned tuna" not in opts

    def test_respects_remaining_calorie_budget(self):
        totals = {"calories": 2380, "protein": 40, "carbs": 80, "fat": 20}
        with patch("fitnessbot.bot.conversation._current_meal_time", return_value="any"), \
             patch("fitnessbot.bot.conversation._recently_eaten_names", return_value=""):
            out = conversation._get_food_suggestions(self.TARGETS, totals, user_id=1)
        # only foods under the 120 remaining calories may appear
        for name, _portion, _g, cal, _t in conversation.FOOD_SUGGESTIONS_DB["protein"]:
            if cal > 120:
                assert name not in out

    def test_prefers_meal_time_appropriate_foods(self):
        with patch("fitnessbot.bot.conversation._current_meal_time", return_value="breakfast"), \
             patch("fitnessbot.bot.conversation._recently_eaten_names", return_value=""):
            hits = 0
            for _ in range(20):
                out = conversation._get_food_suggestions(self.TARGETS, self.TOTALS, user_id=1)
                if "egg whites" in out or "Greek yogurt" in out or "skyr" in out or "smoked salmon" in out:
                    hits += 1
        assert hits > 0

    def test_no_gap_yields_no_suggestions(self):
        totals = {"calories": 2400, "protein": 179, "carbs": 249, "fat": 69}
        out = conversation._get_food_suggestions(self.TARGETS, totals, user_id=1)
        assert out == ""

    def test_backward_compatible_without_user_id(self):
        out = conversation._get_food_suggestions(self.TARGETS, self.TOTALS)
        assert "PROTEIN GAP" in out


class TestMealTimeSlot:
    def test_slots_by_hour(self):
        class FakeNow:
            def __init__(self, h):
                self.hour = h
        for hour, expected in ((8, "breakfast"), (13, "lunch"), (19, "dinner"), (22, "snack")):
            with patch("fitnessbot.bot.conversation.user_now", return_value=FakeNow(hour)):
                assert conversation._current_meal_time(1) == expected

    def test_no_user_returns_any(self):
        assert conversation._current_meal_time(None) == "any"


class TestPerformanceSignalIntegration:
    def test_indulgent_meal_adds_nudge_signal(self):
        act = [{"action": "meal_logged", "items": [
            _item("large fries and a coke", calories=700, sugar=60, fat=30)]}]
        with patch("fitnessbot.bot.conversation.db") as mdb, \
             patch("fitnessbot.nutrition.get_nutrition_targets",
                   return_value={"calories": 2500, "protein": 180, "carbs": 250, "fat": 70}), \
             patch("fitnessbot.bot.conversation.get_weight_summary", return_value={"has_data": False}), \
             patch("fitnessbot.bot.conversation.user_today", return_value="2026-06-22"), \
             patch("fitnessbot.tz.day_utc_range", return_value=("a", "b")):
            mdb.get_today_totals.return_value = {"calories": 700, "protein": 10, "carbs": 90, "fat": 30}
            sig = conversation._get_performance_signal(1, act)
        assert "indulgent meal" in sig
        assert "never shame" in sig
