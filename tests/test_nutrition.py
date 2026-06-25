"""Unit tests for fitnessbot.nutrition pure calculation functions."""

import pytest

from fitnessbot.nutrition import (
    clamp,
    validate_age,
    validate_weight_kg,
    validate_height_cm,
    lbs_to_kg,
    inches_to_cm,
    compute_bmr,
    compute_tdee,
    apply_goal,
    apply_safety_floor,
    derive_macro_targets,
    ACTIVITY_MULTIPLIERS,
    GOAL_ADJUSTMENTS,
    MACRO_PRESETS,
    SAFETY_FLOOR,
)


# ── Validation helpers ──


class TestClamp:
    def test_within_range(self):
        assert clamp(50, 0, 100) == 50

    def test_below_floor(self):
        assert clamp(-5, 0, 100) == 0

    def test_above_ceiling(self):
        assert clamp(150, 0, 100) == 100

    def test_at_boundary(self):
        assert clamp(0, 0, 100) == 0
        assert clamp(100, 0, 100) == 100


class TestValidateAge:
    def test_none_returns_default(self):
        assert validate_age(None) == 30

    def test_normal_age(self):
        assert validate_age(25) == 25

    def test_below_min(self):
        assert validate_age(10) == 14

    def test_above_max(self):
        assert validate_age(120) == 100


class TestValidateWeightKg:
    def test_normal(self):
        assert validate_weight_kg(80.0) == 80.0

    def test_below_min(self):
        assert validate_weight_kg(20.0) == 30.0

    def test_above_max(self):
        assert validate_weight_kg(400.0) == 300.0


class TestValidateHeightCm:
    def test_none_returns_default(self):
        assert validate_height_cm(None) == 170.0

    def test_normal(self):
        assert validate_height_cm(180.0) == 180.0

    def test_below_min(self):
        assert validate_height_cm(100.0) == 120.0

    def test_above_max(self):
        assert validate_height_cm(250.0) == 230.0


class TestUnitConversions:
    def test_lbs_to_kg(self):
        assert abs(lbs_to_kg(176.37) - 80.0) < 0.1

    def test_inches_to_cm(self):
        assert abs(inches_to_cm(70.87) - 180.0) < 0.1


# ── BMR ──


class TestComputeBMR:
    def test_male_standard(self):
        # Male, 80 kg, 180 cm, 30 years
        # BMR = 10*80 + 6.25*180 - 5*30 + 5 = 800 + 1125 - 150 + 5 = 1780
        bmr = compute_bmr("male", 80.0, 180.0, 30)
        assert bmr == 1780.0

    def test_female_standard(self):
        # Female, 65 kg, 165 cm, 28 years
        # BMR = 10*65 + 6.25*165 - 5*28 - 161 = 650 + 1031.25 - 140 - 161 = 1380.25
        bmr = compute_bmr("female", 65.0, 165.0, 28)
        assert bmr == 1380.25

    def test_male_short_form(self):
        bmr = compute_bmr("m", 80.0, 180.0, 30)
        assert bmr == 1780.0

    def test_female_short_form(self):
        bmr = compute_bmr("f", 65.0, 165.0, 28)
        assert bmr == 1380.25

    def test_none_height_uses_default(self):
        # height defaults to 170
        bmr = compute_bmr("male", 80.0, None, 30)
        expected = 10 * 80 + 6.25 * 170 - 5 * 30 + 5
        assert bmr == expected

    def test_none_age_uses_default(self):
        # age defaults to 30
        bmr = compute_bmr("male", 80.0, 180.0, None)
        assert bmr == 1780.0

    def test_input_validation_clamps_weight(self):
        # Weight 20 kg clamped to 30
        bmr = compute_bmr("male", 20.0, 180.0, 30)
        expected = 10 * 30 + 6.25 * 180 - 5 * 30 + 5
        assert bmr == expected

    def test_input_validation_clamps_height(self):
        # Height 100 cm clamped to 120
        bmr = compute_bmr("male", 80.0, 100.0, 30)
        expected = 10 * 80 + 6.25 * 120 - 5 * 30 + 5
        assert bmr == expected

    def test_input_validation_clamps_age(self):
        # Age 5 clamped to 14
        bmr = compute_bmr("male", 80.0, 180.0, 5)
        expected = 10 * 80 + 6.25 * 180 - 5 * 14 + 5
        assert bmr == expected


# ── TDEE ──


class TestComputeTDEE:
    def test_all_activity_levels(self):
        bmr = 1780.0
        for level, mult in ACTIVITY_MULTIPLIERS.items():
            tdee = compute_tdee(bmr, level)
            assert tdee == pytest.approx(bmr * mult, abs=0.01)

    def test_unknown_activity_defaults_to_moderate(self):
        bmr = 1780.0
        tdee = compute_tdee(bmr, "unknown")
        assert tdee == pytest.approx(bmr * 1.55, abs=0.01)


# ── Goal adjustment ──


class TestApplyGoal:
    def test_all_goals(self):
        tdee = 2759.0
        for goal, delta in GOAL_ADJUSTMENTS.items():
            result = apply_goal(tdee, goal)
            assert result == tdee + delta

    def test_unknown_goal_no_change(self):
        assert apply_goal(2759.0, "nonexistent") == 2759.0

    def test_aggressive_cut_capped_at_500(self):
        assert GOAL_ADJUSTMENTS["aggressive_cut"] == -500

    def test_max_delta_magnitude(self):
        for delta in GOAL_ADJUSTMENTS.values():
            assert abs(delta) <= 500


# ── Safety floor ──


class TestApplySafetyFloor:
    def test_male_above_floor(self):
        cal, clamped = apply_safety_floor(2000, "male")
        assert cal == 2000
        assert clamped is False

    def test_male_below_floor(self):
        cal, clamped = apply_safety_floor(1200, "male")
        assert cal == 1500.0
        assert clamped is True

    def test_female_above_floor(self):
        cal, clamped = apply_safety_floor(1400, "female")
        assert cal == 1400
        assert clamped is False

    def test_female_below_floor(self):
        cal, clamped = apply_safety_floor(1000, "female")
        assert cal == 1200.0
        assert clamped is True

    def test_at_exact_floor(self):
        cal, clamped = apply_safety_floor(1500, "male")
        assert cal == 1500
        assert clamped is False

    def test_short_form_sex(self):
        cal, clamped = apply_safety_floor(1000, "m")
        assert cal == 1500.0
        assert clamped is True


# ── Macro derivation ──


class TestDeriveMacroTargets:
    def test_maintain_bodyweight_based(self):
        macros = derive_macro_targets(2760, 80.0, "male", "maintain")
        # Protein: 1.8 g/kg * 80 = 144g
        assert macros["protein"] == 144
        # Fat: max(25% of 2760 / 9, 0.6 * 80) = max(76.7, 48) = 77g
        assert macros["fat"] == 77
        # Carbs fill remainder: (2760 - 144*4 - 77*9) / 4
        expected_carbs = max(round((2760 - 144 * 4 - 77 * 9) / 4), 50)
        assert macros["carbs"] == expected_carbs
        # Fiber: max(14*2760/1000, 38) = max(38.64, 38) = 39
        assert macros["fiber"] == 39
        # Sugar: 10% of 2760 / 4 = 69
        assert macros["sugar"] == 69
        # Sodium always 2300
        assert macros["sodium"] == 2300
        # Water: 35 * 80 = 2800
        assert macros["water_ml"] == 2800

    def test_bulk_higher_protein(self):
        macros = derive_macro_targets(3000, 80.0, "male", "bulk")
        # Protein: 2.0 g/kg * 80 = 160g
        assert macros["protein"] == 160

    def test_cut_higher_protein(self):
        macros = derive_macro_targets(2200, 80.0, "male", "cut")
        assert macros["protein"] == 160  # 2.0 g/kg

    def test_fat_minimum_enforced(self):
        # Light person where 25% might be below 0.6 g/kg
        macros = derive_macro_targets(1500, 100.0, "female", "maintain")
        # Fat floor: 0.6 * 100 = 60g, 25% of 1500/9 = 41.7g
        assert macros["fat"] >= 60

    def test_carbs_minimum_50g(self):
        # Very low calorie with heavy protein/fat
        macros = derive_macro_targets(1200, 100.0, "male", "cut")
        assert macros["carbs"] >= 50

    def test_fiber_male_floor_38(self):
        macros = derive_macro_targets(2000, 80.0, "male", "maintain")
        assert macros["fiber"] >= 38

    def test_fiber_female_floor_25(self):
        macros = derive_macro_targets(1500, 60.0, "female", "maintain")
        assert macros["fiber"] >= 25

    def test_preset_balanced(self):
        macros = derive_macro_targets(2000, 80.0, "male", "maintain", preset="balanced")
        # 30/40/30 P/C/F
        assert macros["protein"] == round(2000 * 0.30 / 4)  # 150
        assert macros["carbs"] == round(2000 * 0.40 / 4)  # 200
        assert macros["fat"] == round(2000 * 0.30 / 9)  # 67

    def test_preset_high_protein(self):
        macros = derive_macro_targets(2000, 80.0, "male", "maintain", preset="high_protein")
        assert macros["protein"] == round(2000 * 0.40 / 4)  # 200
        assert macros["carbs"] == round(2000 * 0.30 / 4)  # 150
        assert macros["fat"] == round(2000 * 0.30 / 9)  # 67

    def test_preset_low_carb(self):
        macros = derive_macro_targets(2000, 80.0, "male", "maintain", preset="low_carb")
        assert macros["protein"] == round(2000 * 0.35 / 4)  # 175
        assert macros["carbs"] == round(2000 * 0.20 / 4)  # 100
        assert macros["fat"] == round(2000 * 0.45 / 9)  # 100


# ── Worked example (acceptance criteria) ──


class TestWorkedExample:
    """Regression test: male, 30y, 80 kg, 180 cm, moderately active, maintain."""

    def test_bmr(self):
        bmr = compute_bmr("male", 80.0, 180.0, 30)
        assert bmr == pytest.approx(1780.0, abs=1)

    def test_tdee(self):
        bmr = 1780.0
        tdee = compute_tdee(bmr, "moderately_active")
        assert tdee == pytest.approx(2759.0, abs=1)

    def test_target_with_maintain(self):
        tdee = 2759.0
        target = apply_goal(tdee, "maintain")
        assert target == pytest.approx(2759.0, abs=1)

    def test_no_safety_floor_triggered(self):
        cal, clamped = apply_safety_floor(2759.0, "male")
        assert clamped is False

    def test_full_pipeline(self):
        bmr = compute_bmr("male", 80.0, 180.0, 30)
        assert bmr == pytest.approx(1780, abs=1)

        tdee = compute_tdee(bmr, "moderately_active")
        assert tdee == pytest.approx(2759, abs=1)

        target = apply_goal(tdee, "maintain")
        assert target == pytest.approx(2759, abs=1)

        cal, clamped = apply_safety_floor(target, "male")
        assert clamped is False

        # Round to nearest 5
        cal_rounded = round(cal / 5) * 5
        assert cal_rounded == 2760

        macros = derive_macro_targets(cal_rounded, 80.0, "male", "maintain")
        assert macros["protein"] == 144  # 1.8 * 80
        assert macros["fat"] == 77  # max(25% of 2760/9, 0.6*80)
        assert macros["carbs"] > 0
        assert macros["fiber"] >= 38
        assert macros["sodium"] == 2300
        assert macros["water_ml"] == 2800


# ── All activity tiers × both sexes ──


class TestAllCombinations:
    @pytest.mark.parametrize("sex", ["male", "female"])
    @pytest.mark.parametrize("activity", list(ACTIVITY_MULTIPLIERS.keys()))
    @pytest.mark.parametrize("goal", list(GOAL_ADJUSTMENTS.keys()))
    def test_pipeline_produces_valid_output(self, sex, activity, goal):
        weight_kg = 75.0
        height_cm = 175.0
        age = 30

        bmr = compute_bmr(sex, weight_kg, height_cm, age)
        assert bmr > 0

        tdee = compute_tdee(bmr, activity)
        assert tdee > bmr  # activity multiplier > 1

        target = apply_goal(tdee, goal)
        cal, clamped = apply_safety_floor(target, sex)
        assert cal >= SAFETY_FLOOR.get(sex, 1200)

        macros = derive_macro_targets(cal, weight_kg, sex, goal)
        assert macros["protein"] > 0
        assert macros["fat"] > 0
        assert macros["carbs"] >= 50
        assert macros["fiber"] > 0
        assert macros["sugar"] > 0
        assert macros["sodium"] == 2300
        assert macros["water_ml"] > 0

        # Macro calories should approximately equal target
        macro_cals = macros["protein"] * 4 + macros["carbs"] * 4 + macros["fat"] * 9
        # Allow rounding tolerance
        assert abs(macro_cals - cal) < 50
