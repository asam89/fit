"""Tests for the deterministic workout engine.

The engine's contract is that every number in a plan is computed here and is
reproducible, so these tests assert the decisions themselves (splits, volume,
intensity, loads, progression, deload, cardio) and the invariants that must
hold for any generated plan: no excluded movement, no unavailable equipment,
nothing past a weekly cap, and a concrete prescription on every exercise.
"""
import json
from unittest.mock import patch

import pytest

from fitnessbot import workout


# --- Goal resolution ----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I want to get stronger and hit a 300lb squat", "strength"),
        ("build muscle in my arms", "hypertrophy"),
        ("bulk up over the winter", "muscle_gain"),
        ("run a half marathon", "endurance"),
        ("lose weight before the summer", "fat_loss"),
        ("lose fat and build muscle at the same time", "recomp"),
        ("just stay healthy", None),
        ("", None),
    ],
)
def test_match_goal_text(text, expected):
    assert workout.match_goal_text(text) == expected


def test_match_goal_text_prefers_recomp_over_its_parts():
    """"Lose fat and gain muscle" is recomp, not fat loss or muscle gain."""
    assert workout.match_goal_text("lose fat and gain muscle") == "recomp"


def test_resolve_training_goal_reads_goal_text():
    with patch.object(workout.db, "get_active_goals", return_value=[{"refined_statement": "get stronger"}]):
        assert workout.resolve_training_goal(1) == "strength"


def test_resolve_training_goal_falls_back_to_nutrition_goal_type():
    """Free text that says nothing about training still maps via goal_type."""
    goals = [{"refined_statement": "feel better day to day", "goal_type": "cut"}]
    with patch.object(workout.db, "get_active_goals", return_value=goals):
        assert workout.resolve_training_goal(1) == "fat_loss"


def test_resolve_training_goal_defaults_with_no_goals():
    with patch.object(workout.db, "get_active_goals", return_value=[]):
        assert workout.resolve_training_goal(1) == workout.DEFAULT_TRAINING_GOAL


def test_resolve_training_goal_event_stays_general():
    """An event means sport practice is the priority; the gym work stays moderate."""
    goals = [{"event_name": "city tournament", "goal_type": "event"}]
    with patch.object(workout.db, "get_active_goals", return_value=goals):
        assert workout.resolve_training_goal(1) == "general"


# --- Split selection ----------------------------------------------------


@pytest.mark.parametrize(
    "days,experience,expected",
    [
        (2, "beginner", "full_body_2"),
        (2, "advanced", "full_body_2"),
        (3, "beginner", "full_body_3"),
        (3, "intermediate", "ppl_3"),
        (3, "advanced", "ppl_3"),
        (4, "beginner", "upper_lower_4"),
        (5, "intermediate", "upper_lower_ppl_5"),
        (6, "advanced", "ppl_6"),
    ],
)
def test_select_split(days, experience, expected):
    split = workout.select_split(days, "hypertrophy", experience)
    assert split["key"] == expected
    assert len(split["days"]) == days


def test_select_split_clamps_out_of_range_days():
    """Nonsense day counts are clamped rather than raising."""
    assert len(workout.select_split(99, "hypertrophy", "advanced")["days"]) == workout.training_profile.MAX_DAYS_AVAILABLE
    assert len(workout.select_split(0, "hypertrophy", "beginner")["days"]) == workout.training_profile.MIN_DAYS_AVAILABLE


def test_every_split_focus_has_patterns():
    for template in workout.SPLIT_TEMPLATES.values():
        for focus in template["days"]:
            assert focus in workout.FOCUS_PATTERNS
            assert focus in workout.FOCUS_TITLES


# --- Weekly volume ------------------------------------------------------


@pytest.mark.parametrize("goal,floor,cap", [(g, f, c) for g, (f, c) in workout.VOLUME_LANDMARKS.items()])
def test_weekly_volume_stays_in_landmarks(goal, floor, cap):
    for experience in ("beginner", "intermediate", "advanced"):
        volume = workout.weekly_volume(goal, experience)
        assert floor <= volume["sets_per_muscle"] <= cap
        assert volume["floor"] == floor
        assert volume["cap"] == cap


def test_weekly_volume_scales_with_experience():
    beginner = workout.weekly_volume("hypertrophy", "beginner")["sets_per_muscle"]
    intermediate = workout.weekly_volume("hypertrophy", "intermediate")["sets_per_muscle"]
    advanced = workout.weekly_volume("hypertrophy", "advanced")["sets_per_muscle"]
    assert beginner == workout.VOLUME_LANDMARKS["hypertrophy"][0]
    assert beginner < intermediate < advanced


def test_weekly_volume_hypertrophy_above_fat_loss():
    assert (
        workout.weekly_volume("hypertrophy", "intermediate")["sets_per_muscle"]
        > workout.weekly_volume("fat_loss", "intermediate")["sets_per_muscle"]
    )


def test_weekly_volume_unknown_goal_falls_back():
    assert workout.weekly_volume("nonsense", "beginner") == workout.weekly_volume(
        workout.DEFAULT_TRAINING_GOAL, "beginner"
    )


# --- Intensity ----------------------------------------------------------


def test_intensity_scheme_strength_is_heavy_and_low_rep():
    scheme = workout.intensity_scheme("strength")
    assert scheme["reps"] == (1, 6)
    assert scheme["pct_1rm"] == (80, 95)
    assert scheme["rest_s"] == (120, 300)


def test_intensity_scheme_endurance_is_light_and_high_rep():
    scheme = workout.intensity_scheme("endurance")
    assert scheme["reps"][0] >= 12
    assert scheme["pct_1rm"][1] < 65
    assert scheme["rest_s"][0] <= 30


def test_intensity_scheme_shares_hypertrophy_across_growth_goals():
    hyper = workout.intensity_scheme("hypertrophy")
    assert workout.intensity_scheme("muscle_gain") == hyper
    assert workout.intensity_scheme("recomp") == hyper


def test_intensity_scheme_unknown_goal_falls_back():
    assert workout.intensity_scheme("nonsense") == workout.intensity_scheme(workout.DEFAULT_TRAINING_GOAL)


def test_baseline_reps_is_mid_range_not_a_1rm_attempt():
    """Nobody with no logged history should be told to do a 1-rep set."""
    assert workout.baseline_reps(workout.intensity_scheme("strength")) == 4
    assert workout.baseline_reps(workout.intensity_scheme("hypertrophy")) == 9


def test_rest_is_longer_for_heavy_compounds_than_accessories():
    scheme = workout.intensity_scheme("strength")
    compound = workout.rest_seconds(scheme, "squat", compound=True)
    accessory = workout.rest_seconds(scheme, "core", compound=False)
    assert accessory == scheme["rest_s"][0]
    assert compound > accessory


# --- 1RM estimation ----------------------------------------------------


def test_epley_and_brzycki_match_published_formulas():
    assert workout.epley_1rm(100, 10) == pytest.approx(133.33, abs=0.01)
    assert workout.brzycki_1rm(100, 10) == pytest.approx(133.33, abs=0.01)


def test_estimate_1rm_averages_the_two_formulas():
    expected = (workout.epley_1rm(100, 5) + workout.brzycki_1rm(100, 5)) / 2
    assert workout.estimate_1rm(100, 5) == pytest.approx(round(expected, 1))


def test_estimate_1rm_single_rep_is_the_load_itself():
    assert workout.estimate_1rm(140, 1) == 140.0


def test_estimate_1rm_clamps_high_reps():
    """Brzycki's denominator collapses at 37 reps, so estimates clamp at 12."""
    assert workout.estimate_1rm(50, 40) == workout.estimate_1rm(50, workout.MAX_1RM_REPS)


@pytest.mark.parametrize("weight,reps", [(0, 5), (-10, 5), (100, 0), (None, 5), ("heavy", 5)])
def test_estimate_1rm_rejects_bad_input(weight, reps):
    assert workout.estimate_1rm(weight, reps) is None


def test_round_load_uses_practical_increments():
    assert workout.round_load(101.2) == 100.0
    assert workout.round_load(103.9) == 105.0


def test_load_from_1rm():
    assert workout.load_from_1rm(200, 80) == 160.0
    assert workout.load_from_1rm(None, 80) is None
    assert workout.load_from_1rm(0, 80) is None


# --- Progression -------------------------------------------------------


def test_no_history_gives_rpe_target_and_no_invented_load():
    rx = workout.next_prescription("beginner", [], "hypertrophy")
    assert rx["load"] is None
    assert rx["rpe"] == workout.intensity_scheme("hypertrophy")["rpe"]
    assert rx["change"] == "baseline"
    assert rx["reps"] > 0


def test_progression_rule_by_experience():
    assert workout.next_prescription("beginner", [], "strength")["rule"] == "linear"
    assert workout.next_prescription("intermediate", [], "strength")["rule"] == "double_progression"
    assert workout.next_prescription("advanced", [], "strength")["rule"] == "undulating"


def test_beginner_linear_adds_load_after_a_completed_session():
    rx = workout.next_prescription("beginner", [{"weight": 100, "reps": 5, "completed": True}], "strength")
    assert rx["load"] > 100
    assert rx["change"] == "increase"


def test_beginner_linear_repeats_after_one_miss():
    rx = workout.next_prescription("beginner", [{"weight": 100, "reps": 3, "completed": False}], "strength")
    assert rx["load"] == 100
    assert rx["change"] == "repeat"


def test_beginner_linear_backs_off_after_two_misses():
    history = [
        {"weight": 100, "reps": 3, "completed": False},
        {"weight": 100, "reps": 3, "completed": False},
    ]
    rx = workout.next_prescription("beginner", history, "strength")
    assert rx["load"] == pytest.approx(90.0)
    assert rx["change"] == "backoff"


def test_double_progression_adds_reps_before_load():
    rx = workout.next_prescription("intermediate", [{"weight": 60, "reps": 8, "completed": True}], "hypertrophy")
    assert rx["load"] == 60
    assert rx["reps"] == 9
    assert rx["change"] == "add_reps"


def test_double_progression_adds_load_at_top_of_range():
    rep_hi = workout.intensity_scheme("hypertrophy")["reps"][1]
    rx = workout.next_prescription("intermediate", [{"weight": 60, "reps": rep_hi, "completed": True}], "hypertrophy")
    assert rx["load"] > 60
    assert rx["change"] == "increase"


def test_advanced_undulates_within_the_intensity_band():
    lo, hi = workout.intensity_scheme("strength")["pct_1rm"]
    percentages = set()
    for session in range(len(workout.UNDULATING_WAVE)):
        history = [{"weight": 140, "reps": 3, "completed": True}] * (session + 1)
        rx = workout.next_prescription("advanced", history, "strength")
        assert lo <= rx["pct_1rm"] <= hi
        assert rx["load"] is not None
        percentages.add(rx["pct_1rm"])
    assert len(percentages) > 1


def test_every_prescription_is_concrete():
    """"Go heavier" is not a prescription — sets and reps are always numbers."""
    for experience in ("beginner", "intermediate", "advanced"):
        for history in ([], [{"weight": 80, "reps": 8, "completed": True}]):
            rx = workout.next_prescription(experience, history, "hypertrophy")
            assert isinstance(rx["sets"], int) and rx["sets"] > 0
            assert isinstance(rx["reps"], int) and rx["reps"] > 0
            assert rx["note"]


def test_history_without_load_is_ignored():
    """Bodyweight rows with no weight can't drive a load calculation."""
    rx = workout.next_prescription("beginner", [{"reps": 10, "completed": True}], "hypertrophy")
    assert rx["change"] == "baseline"


# --- Deload ------------------------------------------------------------


def test_deload_lands_inside_the_four_to_six_week_window():
    assert 4 <= workout.DELOAD_CYCLE_WEEKS <= 6
    assert not workout.deload_due(1)
    assert not workout.deload_due(workout.DELOAD_CYCLE_WEEKS - 1)
    assert workout.deload_due(workout.DELOAD_CYCLE_WEEKS)
    assert workout.deload_due(workout.DELOAD_CYCLE_WEEKS * 2)


def test_deload_triggers_early_after_repeated_misses():
    assert workout.deload_due(2, missed_sessions=workout.MISSED_SESSIONS_FOR_DELOAD)
    assert not workout.deload_due(2, missed_sessions=workout.MISSED_SESSIONS_FOR_DELOAD - 1)


def test_next_deload_week_is_always_ahead():
    for week in range(1, 13):
        assert workout.next_deload_week(week) > week


def test_apply_deload_halves_sets_and_lightens_load():
    sets, load = workout.apply_deload(4, 100.0)
    assert sets == 2
    assert load == pytest.approx(90.0)


def test_apply_deload_keeps_a_working_set_and_tolerates_no_load():
    sets, load = workout.apply_deload(2, None)
    assert sets >= workout.MIN_SETS_PER_EXERCISE
    assert load is None


# --- Cardio ------------------------------------------------------------


def test_cardio_is_higher_for_fat_loss_than_strength():
    fat_loss = workout.prescribe_cardio("fat_loss", "moderate")
    strength = workout.prescribe_cardio("strength", "moderate")
    assert fat_loss["sessions_per_week"] > strength["sessions_per_week"]
    assert fat_loss["minutes_per_session"] >= strength["minutes_per_session"]


def test_cardio_never_prescribes_a_calorie_deficit():
    """Nutrition owns the deficit; cardio must not double-count it."""
    rx = workout.prescribe_cardio("fat_loss", "moderate", tdee=2600)
    assert "calorie" not in json.dumps(rx).lower() or "not extra deficit" in rx["note"]
    assert "nutrition targets" in rx["note"]
    assert all("deficit" not in str(v).lower() or "not extra deficit" in str(v) for v in rx.values())


def test_cardio_backs_off_for_already_active_users():
    moderate = workout.prescribe_cardio("general", "moderate")["sessions_per_week"]
    very_active = workout.prescribe_cardio("general", "very_active")["sessions_per_week"]
    assert very_active < moderate


def test_cardio_goes_low_impact_when_running_is_excluded():
    rx = workout.prescribe_cardio("fat_loss", "moderate", exclusions=["running"])
    assert rx["modality"] == workout.LOW_IMPACT_CARDIO_MODALITY


def test_cardio_is_easier_when_clearance_is_pending():
    pending = workout.prescribe_cardio("fat_loss", "moderate", needs_clearance=True)
    normal = workout.prescribe_cardio("fat_loss", "moderate")
    assert pending["intensity"] == "easy"
    assert pending["minutes_per_session"] <= normal["minutes_per_session"]


def test_cardio_unknown_goal_and_activity_still_prescribes():
    rx = workout.prescribe_cardio("nonsense", None)
    assert rx["sessions_per_week"] >= 1
    assert rx["minutes_per_session"] >= 1


# --- Exercise library --------------------------------------------------


def test_library_loads_and_every_entry_is_complete():
    library = workout.load_exercise_library()
    assert len(library) > 20
    ids = set()
    for ex in library:
        assert ex["id"] and ex["id"] not in ids
        ids.add(ex["id"])
        assert ex["name"]
        assert ex["pattern"]
        assert ex["primary"]
        assert ex["equipment"]
        assert isinstance(ex["flags"], list)
        assert isinstance(ex["compound"], bool)


def test_library_covers_every_pattern_the_splits_ask_for():
    patterns = {p for slots in workout.FOCUS_PATTERNS.values() for p in slots}
    covered = {ex["pattern"] for ex in workout.load_exercise_library()}
    assert patterns <= covered


def test_library_missing_file_degrades_to_empty():
    assert workout.load_exercise_library("/nonexistent/exercises.json") == ()


def test_filter_excludes_movement_patterns():
    kept = workout.filter_exercises(workout.load_exercise_library(), ["full_gym"], ["squat"])
    assert kept
    assert all(ex["pattern"] != "squat" for ex in kept)


def test_filter_excludes_contraindication_flags():
    kept = workout.filter_exercises(workout.load_exercise_library(), ["full_gym"], ["overhead"])
    assert all("overhead" not in ex["flags"] for ex in kept)


def test_filter_respects_available_equipment():
    kept = workout.filter_exercises(workout.load_exercise_library(), ["bodyweight"], [])
    assert kept
    for ex in kept:
        assert "bodyweight" in ex["equipment"]
    assert not any(ex["equipment"] == ["barbell"] for ex in kept)


def test_full_gym_unlocks_everything():
    library = workout.load_exercise_library()
    assert len(workout.filter_exercises(library, ["full_gym"], [])) == len(library)


# --- Variation ---------------------------------------------------------


def test_variation_seed_is_stable_and_user_specific():
    assert workout.variation_seed(7, 0) == workout.variation_seed(7, 0)
    assert workout.variation_seed(7, 0) != workout.variation_seed(8, 0)
    assert workout.variation_seed(7, 0) != workout.variation_seed(7, 1)


def test_select_exercises_is_deterministic_for_a_seed():
    first = workout.select_exercises("squat", ["full_gym"], [], 12345, count=2)
    second = workout.select_exercises("squat", ["full_gym"], [], 12345, count=2)
    assert [ex["id"] for ex in first] == [ex["id"] for ex in second]


def test_select_exercises_prefers_compounds_for_main_slots():
    picks = workout.select_exercises("squat", ["full_gym"], [], 1, count=3)
    assert all(ex["compound"] for ex in picks)


def test_select_exercises_returns_empty_when_nothing_qualifies():
    assert workout.select_exercises("squat", ["bodyweight"], ["squat"], 1) == []


# --- Plan generation ---------------------------------------------------


PROFILE = {
    "experience_level": "intermediate",
    "days_available": 4,
    "equipment": ["barbell", "dumbbells", "machines"],
    "session_time_min": 60,
    "movement_exclusions": [],
    "needs_medical_clearance": False,
}


def _all_exercises(plan):
    return [ex for day in plan["days"] for ex in day["exercises"]]


def test_plan_output_contract():
    plan = workout.generate_plan(PROFILE, "hypertrophy", user_id=7)
    for key in (
        "goal",
        "split",
        "mesocycle_index",
        "week_index",
        "deload",
        "days",
        "weekly_volume_by_muscle",
        "volume_target",
        "progression_rule",
        "next_deload_week",
        "cardio",
    ):
        assert key in plan
    assert len(plan["days"]) == 4
    for day in plan["days"]:
        assert day["focus"] and day["title"]
        assert day["exercises"]
        assert day["est_duration_min"] > 0
    for ex in _all_exercises(plan):
        assert ex["name"] and ex["pattern"]
        assert ex["sets"] >= workout.MIN_SETS_PER_EXERCISE
        assert ex["reps"] > 0
        assert ex["rest_s"] > 0
        assert ex["load"] is not None or ex["rpe"] is not None


def test_plan_is_deterministic():
    first = workout.generate_plan(PROFILE, "hypertrophy", user_id=7)
    second = workout.generate_plan(PROFILE, "hypertrophy", user_id=7)
    assert first == second


def test_two_users_with_identical_inputs_get_different_but_valid_plans():
    a = workout.generate_plan(PROFILE, "hypertrophy", user_id=7)
    b = workout.generate_plan(PROFILE, "hypertrophy", user_id=42)
    assert a["split"] == b["split"]
    assert [ex["name"] for ex in _all_exercises(a)] != [ex["name"] for ex in _all_exercises(b)]

    # Same main patterns trained, just different movements filling the slots.
    def main_patterns(plan):
        return [ex["pattern"] for ex in _all_exercises(plan) if ex["pattern"] not in workout.ACCESSORY_PATTERNS]

    assert main_patterns(a) == main_patterns(b)


def test_same_user_rotates_movements_across_mesocycles():
    first = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, mesocycle_index=0)
    second = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, mesocycle_index=1)
    assert [ex["name"] for ex in _all_exercises(first)] != [ex["name"] for ex in _all_exercises(second)]


def test_progression_continuity_within_a_mesocycle():
    """Weeks inside one mesocycle keep the same movements so loads can progress."""
    week1 = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, week_index=1)
    week2 = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, week_index=2)
    assert [ex["name"] for ex in _all_exercises(week1)] == [ex["name"] for ex in _all_exercises(week2)]


def test_no_duplicate_movement_inside_a_session():
    plan = workout.generate_plan(PROFILE, "strength", user_id=7)
    for day in plan["days"]:
        ids = [ex["exercise_id"] for ex in day["exercises"]]
        assert len(ids) == len(set(ids))


def test_plan_never_includes_an_excluded_pattern_or_flag():
    profile = dict(PROFILE, equipment=["full_gym"], movement_exclusions=["overhead", "hinge"])
    plan = workout.generate_plan(profile, "hypertrophy", user_id=7)
    library = {ex["id"]: ex for ex in workout.load_exercise_library()}
    for ex in _all_exercises(plan):
        assert ex["pattern"] != "hinge"
        assert "overhead" not in library[ex["exercise_id"]]["flags"]


def test_plan_only_uses_available_equipment():
    profile = dict(PROFILE, equipment=["bodyweight"])
    plan = workout.generate_plan(profile, "general", user_id=7)
    library = {ex["id"]: ex for ex in workout.load_exercise_library()}
    for ex in _all_exercises(plan):
        assert "bodyweight" in library[ex["exercise_id"]]["equipment"]


def test_plan_never_exceeds_the_weekly_volume_cap():
    for goal in workout.TRAINING_GOALS:
        for experience in ("beginner", "intermediate", "advanced"):
            for days in range(2, 7):
                profile = dict(PROFILE, experience_level=experience, days_available=days, equipment=["full_gym"])
                plan = workout.generate_plan(profile, goal, user_id=3)
                cap = plan["volume_target"]["cap"]
                for muscle, sets in plan["weekly_volume_by_muscle"].items():
                    assert sets <= workout.cap_for_muscle(muscle, cap), (goal, experience, days, muscle, sets)


def test_plan_respects_session_time():
    profile = dict(PROFILE, session_time_min=30, equipment=["full_gym"])
    plan = workout.generate_plan(profile, "hypertrophy", user_id=7)
    for day in plan["days"]:
        assert day["est_duration_min"] <= 30 + workout.SESSION_OVERRUN_TOLERANCE_MIN + (
            day["cardio"]["minutes_per_session"] if day["cardio"] else 0
        )


def test_longer_sessions_get_more_work():
    short = workout.generate_plan(dict(PROFILE, session_time_min=30), "hypertrophy", user_id=7)
    long = workout.generate_plan(dict(PROFILE, session_time_min=90), "hypertrophy", user_id=7)
    assert len(_all_exercises(long)) >= len(_all_exercises(short))


def test_beginner_plan_avoids_high_skill_movements():
    """Dips and pull-ups need no equipment but aren't beginner movements."""
    profile = dict(PROFILE, experience_level="beginner", equipment=["full_gym"])
    plan = workout.generate_plan(profile, "general", user_id=7)
    library = {ex["id"]: ex for ex in workout.load_exercise_library()}
    for ex in _all_exercises(plan):
        assert workout.HIGH_SKILL_FLAG not in library[ex["exercise_id"]]["flags"]


def test_deload_week_cuts_sets_and_loads():
    normal = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, week_index=1)
    deload = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, week_index=workout.DELOAD_CYCLE_WEEKS)
    assert deload["deload"] is True
    assert sum(ex["sets"] for ex in _all_exercises(deload)) < sum(ex["sets"] for ex in _all_exercises(normal))
    assert any("Deload" in note for note in deload["notes"])


def test_missed_sessions_force_an_early_deload():
    plan = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, week_index=2, missed_sessions=3)
    assert plan["deload"] is True
    assert any("missed" in note for note in plan["notes"])


def test_logged_one_rm_fills_in_concrete_loads():
    plan = workout.generate_plan(PROFILE, "strength", user_id=7)
    lifted = {ex["exercise_id"]: 150.0 for ex in _all_exercises(plan)}
    with_loads = workout.generate_plan(PROFILE, "strength", user_id=7, one_rm_by_exercise=lifted)
    assert any(ex["load"] for ex in _all_exercises(with_loads))


def test_history_drives_the_prescription_in_the_plan():
    plan = workout.generate_plan(PROFILE, "hypertrophy", user_id=7)
    first = _all_exercises(plan)[0]
    history = {first["exercise_id"]: [{"weight": 80, "reps": 8, "completed": True}]}
    updated = workout.generate_plan(PROFILE, "hypertrophy", user_id=7, history_by_exercise=history)
    assert _all_exercises(updated)[0]["load"] == 80
    assert _all_exercises(updated)[0]["progression"] == "add_reps"


def test_medical_red_flag_forces_conservative_general_movement():
    profile = dict(
        PROFILE,
        experience_level="advanced",
        days_available=6,
        equipment=["full_gym"],
        session_time_min=90,
        needs_medical_clearance=True,
    )
    plan = workout.generate_plan(profile, "strength", user_id=7)
    assert plan["goal"] == "general"
    assert plan["medical_clearance_required"] is True
    assert plan["split_key"] == "general_movement"
    assert len(plan["days"]) <= 3
    assert plan["session_time_min"] <= 30
    assert any("physician" in note for note in plan["notes"])
    library = {ex["id"]: ex for ex in workout.load_exercise_library()}
    for ex in _all_exercises(plan):
        assert ex["load"] is None
        assert "bodyweight" in library[ex["exercise_id"]]["equipment"]
        assert workout.HIGH_SKILL_FLAG not in library[ex["exercise_id"]]["flags"]


def test_empty_profile_still_produces_a_usable_plan():
    """A user who never filled in the form gets conservative defaults."""
    plan = workout.generate_plan({}, "general", user_id=7)
    assert plan["experience"] == workout.training_profile.DEFAULT_EXPERIENCE
    assert len(plan["days"]) == workout.training_profile.DEFAULT_DAYS_AVAILABLE
    assert _all_exercises(plan)


def test_unknown_goal_falls_back_to_general():
    assert workout.generate_plan(PROFILE, "nonsense", user_id=7)["goal"] == "general"


def test_volume_shortfall_reports_what_the_week_cannot_reach():
    """The engine says where it fell short instead of overclaiming."""
    profile = dict(PROFILE, days_available=2, session_time_min=30, equipment=["bodyweight"])
    plan = workout.generate_plan(profile, "hypertrophy", user_id=7)
    assert plan["volume_shortfall"]
    for muscle, gap in plan["volume_shortfall"].items():
        assert gap > 0
        assert plan["weekly_volume_by_muscle"][muscle] < plan["volume_target"]["sets_per_muscle"]


def test_cardio_appears_on_the_prescribed_number_of_days():
    plan = workout.generate_plan(PROFILE, "fat_loss", user_id=7)
    cardio_days = [day for day in plan["days"] if day["cardio"]]
    assert len(cardio_days) == min(plan["cardio"]["sessions_per_week"], len(plan["days"]))


def test_weekly_volume_by_muscle_credits_secondaries_at_half():
    days = [
        {
            "exercises": [
                {"sets": 4, "primary": ["quads"], "secondary": ["glutes"], "pattern": "squat"},
            ]
        }
    ]
    by_muscle = workout.weekly_volume_by_muscle(days)
    assert by_muscle["quads"] == 4
    assert by_muscle["glutes"] == 2


# --- build_plan (DB-backed wrapper) ------------------------------------


def test_build_plan_reads_stored_inputs():
    profile = dict(PROFILE)
    with (
        patch.object(workout.training_profile, "get_training_profile", return_value=profile),
        patch.object(workout.db, "get_user_by_id", return_value={"activity_level": "moderate"}),
        patch.object(workout.db, "get_active_goals", return_value=[{"refined_statement": "get stronger"}]),
        patch.object(workout, "get_nutrition_targets", return_value={"tdee": 2600}),
    ):
        plan = workout.build_plan(7)
    assert plan["goal"] == "strength"
    assert plan["days"]


def test_build_plan_survives_missing_nutrition_targets():
    """A missing TDEE is advisory — it must not block plan generation."""
    with (
        patch.object(workout.training_profile, "get_training_profile", return_value=dict(PROFILE)),
        patch.object(workout.db, "get_user_by_id", return_value=None),
        patch.object(workout.db, "get_active_goals", return_value=[]),
        patch.object(workout, "get_nutrition_targets", side_effect=RuntimeError("no targets")),
    ):
        plan = workout.build_plan(7)
    assert plan["days"]
