"""Deterministic workout engine: split, volume, intensity, load, progression.

Every number that drives a plan is computed here. The AI narrative layer
consumes the finished plan and may only add words to it — mirroring the
`nutrition.py` split between `compute_targets` (deterministic) and
`generate_eating_focus` (narrative).

Pure module: no network calls and no LLM calls. The only impure functions are
the thin `resolve_training_goal` / `build_plan` entry points, which read the
user's stored inputs and then delegate to the pure core.
"""

import hashlib
import json
import logging
from functools import lru_cache

from fitnessbot import db, training_profile
from fitnessbot.config import Config
from fitnessbot.nutrition import clamp, get_nutrition_targets

logger = logging.getLogger(__name__)

# --- Training goals ------------------------------------------------------

TRAINING_GOALS = (
    "strength",
    "hypertrophy",
    "muscle_gain",
    "endurance",
    "fat_loss",
    "recomp",
    "general",
)

DEFAULT_TRAINING_GOAL = "general"

# Checked in order, so a goal that says "lose fat and build muscle" resolves to
# recomp rather than fat_loss.
GOAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("recomp", ("recomp", "body composition", "lose fat and build", "lose fat and gain", "tone up", "toned")),
    ("strength", ("strength", "stronger", "1rm", "one rep max", "powerlift", "power lift", "heavier")),
    ("hypertrophy", ("hypertrophy", "build muscle", "muscle size", "bigger", "size", "jacked")),
    ("muscle_gain", ("muscle gain", "gain muscle", "put on muscle", "bulk", "mass", "gain weight")),
    ("endurance", ("endurance", "stamina", "conditioning", "marathon", "half marathon", "5k", "10k", "triathlon", "cardio")),
    ("fat_loss", ("fat loss", "lose fat", "lose weight", "leaner", "lean out", "cut", "slim down", "weight loss")),
)

# Nutrition goal types (goals.goal_type) -> training goal, used when the goal's
# free text says nothing about how the user wants to train.
NUTRITION_GOAL_MAP = {
    "cut": "fat_loss",
    "aggressive_cut": "fat_loss",
    "mild_loss": "fat_loss",
    "bulk": "muscle_gain",
    "lean_bulk": "muscle_gain",
    "mild_gain": "muscle_gain",
    "maintain": "general",
    # An event (tournament, game) means sport practice is the priority, so the
    # gym work stays moderate instead of competing with it for recovery.
    "event": "general",
}

GOAL_TEXT_FIELDS = ("refined_statement", "title", "description", "raw_input", "refined_metric", "event_name")

# --- Split selection ----------------------------------------------------

SPLIT_TEMPLATES: dict[str, dict] = {
    "full_body_2": {"name": "Full Body ×2", "days": ("full_body", "full_body")},
    "full_body_3": {"name": "Full Body ×3", "days": ("full_body", "full_body", "full_body")},
    "ppl_3": {"name": "Push / Pull / Legs", "days": ("push", "pull", "legs")},
    "upper_lower_4": {"name": "Upper / Lower ×2", "days": ("upper", "lower", "upper", "lower")},
    "upper_lower_ppl_5": {
        "name": "Upper / Lower + Push / Pull / Legs",
        "days": ("upper", "lower", "push", "pull", "legs"),
    },
    "ppl_6": {"name": "Push / Pull / Legs ×2", "days": ("push", "pull", "legs", "push", "pull", "legs")},
    "general_movement": {"name": "General Movement", "days": ("general_movement", "general_movement")},
}

# Pattern slots per session focus, in the order they should be trained
# (compound and highest-skill first).
FOCUS_PATTERNS: dict[str, tuple[str, ...]] = {
    "full_body": ("squat", "horizontal_push", "horizontal_pull", "hinge", "core", "carry"),
    "upper": ("horizontal_push", "horizontal_pull", "vertical_push", "vertical_pull", "core", "carry"),
    "lower": ("squat", "hinge", "lunge", "core", "carry", "core"),
    "push": ("horizontal_push", "vertical_push", "horizontal_push", "core", "core", "carry"),
    "pull": ("vertical_pull", "horizontal_pull", "horizontal_pull", "carry", "core", "core"),
    "legs": ("squat", "hinge", "lunge", "core", "carry", "core"),
    "general_movement": ("squat", "horizontal_push", "horizontal_pull", "hinge", "core", "carry"),
}

FOCUS_TITLES = {
    "full_body": "Full Body",
    "upper": "Upper Body",
    "lower": "Lower Body",
    "push": "Push",
    "pull": "Pull",
    "legs": "Legs",
    "general_movement": "General Movement",
}

# --- Volume, intensity, sets -------------------------------------------

# Sets per muscle per week (floor, cap). The cap is a hard ceiling: no
# adjustment or user request may push weekly volume past it.
VOLUME_LANDMARKS: dict[str, tuple[int, int]] = {
    "strength": (8, 12),
    "hypertrophy": (12, 20),
    "muscle_gain": (12, 20),
    "recomp": (12, 20),
    "endurance": (10, 16),
    "fat_loss": (8, 14),
    "general": (8, 14),
}

INTENSITY_SCHEMES: dict[str, dict] = {
    "strength": {"reps": (1, 6), "pct_1rm": (80, 95), "rest_s": (120, 300), "rpe": 8},
    "hypertrophy": {"reps": (6, 12), "pct_1rm": (65, 80), "rest_s": (60, 180), "rpe": 8},
    "muscle_gain": {"reps": (6, 12), "pct_1rm": (65, 80), "rest_s": (60, 180), "rpe": 8},
    "recomp": {"reps": (6, 12), "pct_1rm": (65, 80), "rest_s": (60, 180), "rpe": 8},
    "endurance": {"reps": (12, 20), "pct_1rm": (50, 64), "rest_s": (30, 90), "rpe": 7},
    "fat_loss": {"reps": (8, 15), "pct_1rm": (60, 75), "rest_s": (60, 120), "rpe": 7},
    "general": {"reps": (8, 15), "pct_1rm": (60, 75), "rest_s": (60, 120), "rpe": 7},
}

SETS_PER_EXERCISE = {
    "strength": 4,
    "hypertrophy": 3,
    "muscle_gain": 4,
    "recomp": 3,
    "endurance": 3,
    "fat_loss": 3,
    "general": 3,
}

MIN_SETS_PER_EXERCISE = 2
MAX_SETS_PER_EXERCISE = 5

# Trunk and carry work is accessory: fewer sets, short rests, higher reps, and
# its own weekly ceiling — the major-muscle landmarks don't describe it well.
ACCESSORY_PATTERNS = ("core", "carry")
ACCESSORY_SETS = 2
ACCESSORY_MIN_REPS = 8
ACCESSORY_MAX_REPS = 15
CORE_VOLUME_CAP = 25

# A muscle gets full credit as a primary mover and half credit as a secondary
# one — the usual volume-landmark convention.
SECONDARY_SET_CREDIT = 0.5

# Minutes of session time budgeted per exercise, used to size a session.
MIN_PER_EXERCISE = 12
MIN_EXERCISES_PER_SESSION = 3
MAX_EXERCISES_PER_SESSION = 6

# Working time per set (seconds), for duration estimates.
WORK_SECONDS_PER_SET = 45

# How far a session may overrun the user's stated time before trailing
# accessory work gets cut.
SESSION_OVERRUN_TOLERANCE_MIN = 5

# --- Progression and deload --------------------------------------------

DELOAD_CYCLE_WEEKS = 5  # inside the 4–6 week window
MISSED_SESSIONS_FOR_DELOAD = 2
DELOAD_SET_FACTOR = 0.5
DELOAD_LOAD_FACTOR = 0.9

LINEAR_INCREMENT_PCT = 0.025
DOUBLE_PROGRESSION_INCREMENT_PCT = 0.025
FAILED_SESSION_BACKOFF_PCT = 0.10
LOAD_ROUNDING = 2.5

PROGRESSION_RULES = {
    "beginner": "linear",
    "intermediate": "double_progression",
    "advanced": "undulating",
}

PROGRESSION_DESCRIPTIONS = {
    "linear": "Add load every session you complete all prescribed reps; repeat the load after a miss, back off 10% after two.",
    "double_progression": "Work up to the top of the rep range at a given load, then add load and restart at the bottom of the range.",
    "undulating": "Rotate heavy / moderate / light sessions inside the week, then step loads up next mesocycle.",
}

# Advanced undulating wave: fraction of the way through the goal's %1RM range.
UNDULATING_WAVE = (1.0, 0.5, 0.0)

# Brzycki's denominator degrades past ~12 reps, so estimates clamp there.
MAX_1RM_REPS = 12

# --- Cardio -------------------------------------------------------------

CARDIO_BY_GOAL: dict[str, dict] = {
    "fat_loss": {"sessions": 3, "minutes": 30, "intensity": "LISS"},
    "endurance": {"sessions": 4, "minutes": 40, "intensity": "mixed"},
    "strength": {"sessions": 2, "minutes": 20, "intensity": "LISS"},
    "muscle_gain": {"sessions": 2, "minutes": 20, "intensity": "LISS"},
    "hypertrophy": {"sessions": 2, "minutes": 20, "intensity": "LISS"},
    "recomp": {"sessions": 3, "minutes": 25, "intensity": "LISS"},
    "general": {"sessions": 3, "minutes": 25, "intensity": "LISS"},
}

# Users who already move a lot all day don't need the full prescription.
CARDIO_ACTIVITY_DISCOUNT = {"very_active": 1, "extra_active": 2}

# Movements that train the right pattern but demand strength or skill a
# beginner (or anyone awaiting medical clearance) doesn't have yet.
HIGH_SKILL_FLAG = "high_skill"

# Equipment that lets a movement be progressively loaded. Bodyweight work is
# valid programming but runs out of runway, so a user who owns a barbell should
# not have push-ups leading their pressing day.
LOADING_EQUIPMENT = ("barbell", "dumbbells", "machines", "bands", "full_gym")

DEFAULT_CARDIO_MODALITY = "brisk walk, bike, or row"
LOW_IMPACT_CARDIO_MODALITY = "bike, row, elliptical, or pool"

# --- Exercise library ---------------------------------------------------

EXERCISE_LIBRARY_PATH = Config.BASE_DIR / "data" / "exercises.json"


@lru_cache(maxsize=4)
def load_exercise_library(path: str | None = None) -> tuple[dict, ...]:
    """Load and cache the seed exercise library."""
    target = path or str(EXERCISE_LIBRARY_PATH)
    try:
        with open(target, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Could not load exercise library %s: %s", target, e)
        return ()
    return tuple(payload.get("exercises", []))


def filter_exercises(
    exercises: tuple[dict, ...],
    equipment: list[str],
    exclusions: list[str],
) -> tuple[dict, ...]:
    """Keep only movements the user owns equipment for and hasn't excluded.

    `full_gym` unlocks everything. An exercise is dropped if its pattern or any
    of its contraindication flags is excluded.
    """
    has_full_gym = "full_gym" in equipment
    available = set(equipment) | {"bodyweight"}
    excluded = set(exclusions)

    kept = []
    for ex in exercises:
        if ex.get("pattern") in excluded:
            continue
        if excluded.intersection(ex.get("flags") or []):
            continue
        if not has_full_gym and not available.intersection(ex.get("equipment") or []):
            continue
        kept.append(ex)
    return tuple(kept)


def variation_seed(user_id: int, mesocycle_index: int) -> int:
    """Stable per-user, per-mesocycle rotation seed.

    Uses sha256 rather than `hash()` because Python salts string hashing per
    process, which would hand the same user a different plan after a restart.
    """
    digest = hashlib.sha256(f"{user_id}:{mesocycle_index}".encode()).hexdigest()
    return int(digest[:8], 16)


def has_loading_equipment(equipment: list[str]) -> bool:
    """Whether the user can add external load at all."""
    return bool(set(equipment).intersection(LOADING_EQUIPMENT))


def is_loadable(ex: dict) -> bool:
    """Whether the movement can be loaded beyond bodyweight."""
    return bool(set(ex.get("equipment") or []).intersection(LOADING_EQUIPMENT))


def select_exercises(
    pattern: str,
    equipment: list[str],
    exclusions: list[str],
    seed: int,
    count: int = 1,
    exercises: tuple[dict, ...] | None = None,
    prefer_compound: bool = True,
) -> list[dict]:
    """Pick `count` movements for a pattern, rotated by the variation seed.

    Candidates are sorted by id so the choice is reproducible, then offset by
    the seed: two users with identical inputs get equally valid but different
    exercises, and the same user gets fresh ones each mesocycle. Compounds fill
    main slots first so an isolation movement never leads a session, and
    loadable movements win when the user has something to load them with.
    """
    library = exercises if exercises is not None else load_exercise_library()
    candidates = sorted(
        (ex for ex in filter_exercises(library, equipment, exclusions) if ex.get("pattern") == pattern),
        key=lambda ex: ex["id"],
    )
    if prefer_compound:
        compounds = [ex for ex in candidates if ex.get("compound")]
        candidates = compounds or candidates
        if has_loading_equipment(equipment):
            loadable = [ex for ex in candidates if is_loadable(ex)]
            candidates = loadable or candidates
    if not candidates:
        return []
    return [candidates[(seed + i) % len(candidates)] for i in range(count)]


# --- Goal resolution ----------------------------------------------------


def match_goal_text(text: str) -> str | None:
    """First training goal whose keywords appear in the user's goal text."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    for goal, keywords in GOAL_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return goal
    return None


def resolve_training_goal(user_id: int) -> str:
    """Map the user's stored goals onto one training goal.

    Free text wins over the coarse nutrition goal type, because "get stronger"
    and "lose weight" are the same `goal_type` as far as the goals table is
    concerned but very different training prescriptions.
    """
    goals = db.get_active_goals(user_id)
    for goal in goals:
        text = " ".join(str(goal.get(f) or "") for f in GOAL_TEXT_FIELDS)
        matched = match_goal_text(text)
        if matched:
            return matched
    for goal in goals:
        mapped = NUTRITION_GOAL_MAP.get(str(goal.get("goal_type") or ""))
        if mapped:
            return mapped
    return DEFAULT_TRAINING_GOAL


# --- Split / volume / intensity ----------------------------------------


def select_split(days: int, goal: str, experience: str) -> dict:
    """Choose a split template for the number of days the user actually has."""
    days = training_profile.normalize_days_available(days)
    experience = training_profile.normalize_experience(experience)

    if days == 2:
        key = "full_body_2"
    elif days == 3:
        key = "ppl_3" if experience in ("intermediate", "advanced") else "full_body_3"
    elif days == 4:
        key = "upper_lower_4"
    elif days == 5:
        key = "upper_lower_ppl_5"
    else:
        key = "ppl_6"

    template = SPLIT_TEMPLATES[key]
    return {"key": key, "name": template["name"], "days": list(template["days"])}


def weekly_volume(goal: str, experience: str) -> dict:
    """Weekly sets per muscle: beginners start at the floor, advanced mid-range."""
    goal = goal if goal in VOLUME_LANDMARKS else DEFAULT_TRAINING_GOAL
    experience = training_profile.normalize_experience(experience)
    floor, cap = VOLUME_LANDMARKS[goal]

    if experience == "advanced":
        target = (floor + cap) // 2
    elif experience == "intermediate":
        target = floor + (cap - floor) // 3
    else:
        target = floor
    return {"sets_per_muscle": target, "floor": floor, "cap": cap}


def intensity_scheme(goal: str) -> dict:
    """Reps, %1RM band, rest, and RPE anchor for a training goal."""
    scheme = INTENSITY_SCHEMES.get(goal, INTENSITY_SCHEMES[DEFAULT_TRAINING_GOAL])
    return {
        "reps": tuple(scheme["reps"]),
        "pct_1rm": tuple(scheme["pct_1rm"]),
        "rest_s": tuple(scheme["rest_s"]),
        "rpe": scheme["rpe"],
    }


def baseline_reps(scheme: dict) -> int:
    """Starting rep target: mid-range, not the bottom of the band.

    The floor of a strength range is a 1RM attempt, which is not where anyone
    without logged history should start.
    """
    lo, hi = scheme["reps"]
    return int(round((lo + hi) / 2))


def rest_seconds(scheme: dict, pattern: str, compound: bool) -> int:
    """Rest by role: full recovery between heavy compounds, short for accessories."""
    lo, hi = scheme["rest_s"]
    if pattern in ACCESSORY_PATTERNS or not compound:
        return int(lo)
    return int(round((lo + hi) / 2))


def cap_for_muscle(muscle: str, cap: int) -> int:
    """Weekly set ceiling for a muscle. Trunk work gets its own, higher cap."""
    return CORE_VOLUME_CAP if muscle == "core" else cap


def exercises_per_session(session_time_min: int, cardio_minutes: int = 0) -> int:
    """How many lifts fit in the user's session length."""
    session_time_min = training_profile.normalize_session_time(session_time_min)
    lifting_minutes = max(session_time_min - cardio_minutes, MIN_PER_EXERCISE * MIN_EXERCISES_PER_SESSION)
    return int(clamp(lifting_minutes // MIN_PER_EXERCISE, MIN_EXERCISES_PER_SESSION, MAX_EXERCISES_PER_SESSION))


# --- Load estimation ----------------------------------------------------


def epley_1rm(weight: float, reps: int) -> float:
    return weight * (1 + reps / 30.0)


def brzycki_1rm(weight: float, reps: int) -> float:
    return weight * (36.0 / (37.0 - reps))


def estimate_1rm(weight: float, reps: int) -> float | None:
    """Average of Epley and Brzycki from a logged working set.

    Reps beyond `MAX_1RM_REPS` are clamped: past ~12 the two formulas diverge
    sharply and Brzycki's denominator collapses entirely at 37.
    """
    try:
        weight = float(weight)
        reps = int(reps)
    except (TypeError, ValueError):
        return None
    if weight <= 0 or reps < 1:
        return None
    reps = min(reps, MAX_1RM_REPS)
    if reps == 1:
        return round(weight, 1)
    return round((epley_1rm(weight, reps) + brzycki_1rm(weight, reps)) / 2.0, 1)


def round_load(load: float) -> float:
    """Round to the smallest plate jump most gyms can actually make."""
    return round(load / LOAD_ROUNDING) * LOAD_ROUNDING


def load_from_1rm(one_rm: float | None, pct: float) -> float | None:
    if not one_rm or one_rm <= 0:
        return None
    return round_load(one_rm * pct / 100.0)


# --- Progression --------------------------------------------------------


def next_prescription(experience: str, history: list[dict], goal: str = DEFAULT_TRAINING_GOAL) -> dict:
    """Concrete next set/rep/load prescription — never "go heavier".

    `history` is oldest-first, each entry `{weight, reps, sets, completed}` for
    one exercise. With no history the prescription is anchored to an RPE and a
    rep target, and loads backfill once sets are logged.
    """
    experience = training_profile.normalize_experience(experience)
    scheme = intensity_scheme(goal)
    rule = PROGRESSION_RULES[experience]
    sets = SETS_PER_EXERCISE.get(goal, SETS_PER_EXERCISE[DEFAULT_TRAINING_GOAL])
    rep_lo, rep_hi = scheme["reps"]
    history = [h for h in (history or []) if h.get("weight")]

    base_reps = baseline_reps(scheme)

    if not history:
        return {
            "sets": sets,
            "reps": base_reps,
            "load": None,
            "rpe": scheme["rpe"],
            "rule": rule,
            "change": "baseline",
            "note": f"No logged history yet — find a load you can hold for {base_reps} reps at RPE {scheme['rpe']}.",
        }

    last = history[-1]
    last_load = float(last["weight"])
    last_reps = int(last.get("reps") or rep_lo)
    completed = bool(last.get("completed", True))

    if rule == "linear":
        if not completed:
            recent_misses = sum(1 for h in history[-2:] if not h.get("completed", True))
            if recent_misses >= 2:
                load = round_load(last_load * (1 - FAILED_SESSION_BACKOFF_PCT))
                change = "backoff"
                note = "Two misses in a row — drop 10% and rebuild from there."
            else:
                load = round_load(last_load)
                change = "repeat"
                note = "Repeat the same load and finish all prescribed reps before adding weight."
        else:
            load = round_load(max(last_load * (1 + LINEAR_INCREMENT_PCT), last_load + LOAD_ROUNDING))
            change = "increase"
            note = "All reps completed — take the smallest jump up."
        return {
            "sets": sets,
            "reps": base_reps,
            "load": load,
            "rpe": scheme["rpe"],
            "rule": rule,
            "change": change,
            "note": note,
        }

    if rule == "double_progression":
        if completed and last_reps >= rep_hi:
            load = round_load(max(last_load * (1 + DOUBLE_PROGRESSION_INCREMENT_PCT), last_load + LOAD_ROUNDING))
            reps = rep_lo
            change = "increase"
            note = f"Top of the range hit — add load and restart at {rep_lo} reps."
        else:
            load = round_load(last_load)
            reps = int(clamp(last_reps + 1, rep_lo, rep_hi))
            change = "add_reps"
            note = f"Same load, chase {reps} reps. Add load once you hold {rep_hi}."
        return {
            "sets": sets,
            "reps": reps,
            "load": load,
            "rpe": scheme["rpe"],
            "rule": rule,
            "change": change,
            "note": note,
        }

    # Advanced: undulate intensity across sessions, step up between mesocycles.
    wave_position = UNDULATING_WAVE[len(history) % len(UNDULATING_WAVE)]
    pct_lo, pct_hi = scheme["pct_1rm"]
    pct = pct_lo + (pct_hi - pct_lo) * wave_position
    reps = int(round(rep_hi - (rep_hi - rep_lo) * wave_position))
    one_rm = estimate_1rm(last_load, last_reps)
    load = load_from_1rm(one_rm, pct) or round_load(last_load)
    return {
        "sets": sets,
        "reps": int(clamp(reps, rep_lo, rep_hi)),
        "load": load,
        "pct_1rm": round(pct),
        "rpe": scheme["rpe"],
        "rule": rule,
        "change": "undulate",
        "note": f"{'Heavy' if wave_position > 0.75 else 'Moderate' if wave_position > 0.25 else 'Light'} session — {reps} reps at ~{round(pct)}% of an estimated {one_rm} 1RM.",
    }


# --- Deload -------------------------------------------------------------


def deload_due(week_index: int, missed_sessions: int = 0, cycle_weeks: int = DELOAD_CYCLE_WEEKS) -> bool:
    """Deload on schedule, or early when sessions keep getting missed."""
    week_index = max(int(week_index or 1), 1)
    cycle_weeks = max(int(cycle_weeks or DELOAD_CYCLE_WEEKS), 2)
    if missed_sessions >= MISSED_SESSIONS_FOR_DELOAD:
        return True
    return week_index % cycle_weeks == 0


def next_deload_week(week_index: int, cycle_weeks: int = DELOAD_CYCLE_WEEKS) -> int:
    week_index = max(int(week_index or 1), 1)
    cycle_weeks = max(int(cycle_weeks or DELOAD_CYCLE_WEEKS), 2)
    return ((week_index // cycle_weeks) + 1) * cycle_weeks


def apply_deload(sets: int, load: float | None) -> tuple[int, float | None]:
    """Halve the volume, shave the load — recovery week, not a new stimulus."""
    deloaded_sets = max(int(round(sets * DELOAD_SET_FACTOR)), MIN_SETS_PER_EXERCISE)
    deloaded_load = round_load(load * DELOAD_LOAD_FACTOR) if load else load
    return deloaded_sets, deloaded_load


# --- Cardio -------------------------------------------------------------


def prescribe_cardio(
    goal: str,
    activity_level: str | None,
    tdee: float | None = None,
    exclusions: list[str] | None = None,
    needs_clearance: bool = False,
) -> dict:
    """Cardio dose by goal and how much the user already moves.

    For fat loss the calorie deficit already lives in `nutrition.compute_targets`;
    cardio here is prescribed as minutes only and never re-applies a deficit.
    """
    exclusions = exclusions or []
    spec = CARDIO_BY_GOAL.get(goal, CARDIO_BY_GOAL[DEFAULT_TRAINING_GOAL])
    sessions = spec["sessions"] - CARDIO_ACTIVITY_DISCOUNT.get(str(activity_level or ""), 0)
    sessions = int(clamp(sessions, 1, 5))
    minutes = spec["minutes"]
    intensity = spec["intensity"]

    low_impact = needs_clearance or "running" in exclusions or "jumping" in exclusions
    modality = LOW_IMPACT_CARDIO_MODALITY if low_impact else DEFAULT_CARDIO_MODALITY
    if needs_clearance:
        intensity = "easy"
        minutes = min(minutes, 20)
        sessions = min(sessions, 3)
    elif low_impact and intensity == "mixed":
        intensity = "LISS"

    note = f"{sessions}× {minutes} min, conversational pace."
    if intensity == "mixed":
        note = f"{sessions - 1}× {minutes} min easy plus one interval session."
    if goal == "fat_loss":
        note += " Fat loss is driven by your nutrition targets — this is for heart health and recovery, not extra deficit."

    return {
        "modality": modality,
        "intensity": intensity,
        "sessions_per_week": sessions,
        "minutes_per_session": minutes,
        "weekly_minutes": sessions * minutes,
        "low_impact": low_impact,
        "deficit_source": "nutrition_targets" if goal == "fat_loss" else None,
        "tdee_reference": round(tdee) if tdee else None,
        "note": note,
    }


# --- Volume accounting --------------------------------------------------


def weekly_volume_by_muscle(days: list[dict]) -> dict[str, float]:
    """Sets per muscle across the week (primaries full, secondaries half)."""
    totals: dict[str, float] = {}
    for day in days:
        for ex in day.get("exercises", []):
            sets = ex.get("sets", 0)
            for muscle in ex.get("primary", []):
                totals[muscle] = totals.get(muscle, 0) + sets
            for muscle in ex.get("secondary", []):
                totals[muscle] = totals.get(muscle, 0) + sets * SECONDARY_SET_CREDIT
    return {m: round(v, 1) for m, v in sorted(totals.items())}


def enforce_volume_caps(days: list[dict], cap: int) -> list[str]:
    """Trim sets so no muscle exceeds its weekly cap. Hard ceiling.

    Walks the week in order and reduces (or, if there's no room left for a
    working set, removes) exercises that would push a primary muscle past the
    landmark cap. Returns the human-readable trims that were made.
    """
    spent: dict[str, float] = {}
    trims: list[str] = []
    for day in days:
        kept = []
        for ex in day.get("exercises", []):
            primaries = ex.get("primary", [])
            room = min((_sets_of_room(ex, spent, cap)), default=cap)
            if room < MIN_SETS_PER_EXERCISE:
                trims.append(f"dropped {ex['name']} ({'/'.join(primaries)} at weekly cap)")
                continue
            if ex["sets"] > room:
                trims.append(f"{ex['name']} {ex['sets']}→{int(room)} sets (weekly cap)")
                ex["sets"] = int(room)
            for muscle in primaries:
                spent[muscle] = spent.get(muscle, 0) + ex["sets"]
            for muscle in ex.get("secondary", []):
                spent[muscle] = spent.get(muscle, 0) + ex["sets"] * SECONDARY_SET_CREDIT
            kept.append(ex)
        day["exercises"] = kept
    return trims


def _sets_of_room(ex: dict, spent: dict[str, float], cap: int) -> list[float]:
    """Sets this exercise may still run before any muscle it works hits its cap.

    Secondary muscles are included: they only take half credit per set, so the
    ceiling is twice as many sets away, but ignoring them lets a week drift past
    a cap that is supposed to be hard.
    """
    room = [cap_for_muscle(m, cap) - spent.get(m, 0) for m in ex.get("primary", [])]
    room += [
        (cap_for_muscle(m, cap) - spent.get(m, 0)) / SECONDARY_SET_CREDIT
        for m in ex.get("secondary", [])
    ]
    return room


def top_up_volume(days: list[dict], target: int, cap: int, session_time_min: int) -> None:
    """Add sets to under-worked muscles until they reach the weekly target.

    Filling pattern slots gets the movements right but not necessarily the
    volume: a 4-day upper/lower week only touches chest twice, so the target is
    approached by adding sets to the movements that already train it rather than
    by bolting on extra exercises the user has no time for.
    """
    budget = session_time_min + SESSION_OVERRUN_TOLERANCE_MIN
    lifts = [
        (day, ex)
        for day in days
        for ex in day.get("exercises", [])
        if ex["pattern"] not in ACCESSORY_PATTERNS
    ]
    # Bounded: each pass must add at least one set or we stop.
    for _ in range(MAX_SETS_PER_EXERCISE):
        spent = weekly_volume_by_muscle(days)
        added = False
        for day, ex in lifts:
            if ex["sets"] >= MAX_SETS_PER_EXERCISE:
                continue
            primaries = list(ex.get("primary", []))
            if not primaries or all(spent.get(m, 0) >= target for m in primaries):
                continue
            if min(_sets_of_room(ex, spent, cap), default=cap) < 1:
                continue
            if estimate_duration_min(day) + (WORK_SECONDS_PER_SET + ex["rest_s"]) / 60 > budget:
                continue
            ex["sets"] += 1
            for muscle in primaries:
                spent[muscle] = spent.get(muscle, 0) + 1
            for muscle in ex.get("secondary", []):
                spent[muscle] = spent.get(muscle, 0) + SECONDARY_SET_CREDIT
            added = True
        if not added:
            return


def volume_shortfall(days: list[dict], target: int) -> dict[str, float]:
    """Muscles the week can't bring to target, so nothing downstream overclaims."""
    return {
        muscle: round(target - sets, 1)
        for muscle, sets in weekly_volume_by_muscle(days).items()
        if sets < target
    }


def fit_session(day: dict, session_time_min: int) -> list[str]:
    """Drop trailing accessory work until the session fits the user's time.

    Keeps at least `MIN_EXERCISES_PER_SESSION` so a short slot still trains the
    day's main patterns rather than collapsing to nothing.
    """
    dropped: list[str] = []
    budget = session_time_min + SESSION_OVERRUN_TOLERANCE_MIN
    while (
        len(day["exercises"]) > MIN_EXERCISES_PER_SESSION
        and estimate_duration_min(day) > budget
    ):
        dropped.append(day["exercises"].pop()["name"])
    return dropped


def estimate_duration_min(day: dict) -> int:
    seconds = 0
    for ex in day.get("exercises", []):
        rest = ex.get("rest_s", 90)
        seconds += ex["sets"] * (WORK_SECONDS_PER_SET + rest)
    minutes = seconds // 60
    cardio = day.get("cardio")
    if cardio:
        minutes += cardio.get("minutes_per_session", 0)
    return int(minutes)


# --- Plan generation ----------------------------------------------------


def generate_plan(
    profile: dict,
    goal: str = DEFAULT_TRAINING_GOAL,
    *,
    user_id: int = 0,
    mesocycle_index: int = 0,
    week_index: int = 1,
    missed_sessions: int = 0,
    one_rm_by_exercise: dict[str, float] | None = None,
    history_by_exercise: dict[str, list[dict]] | None = None,
    tdee: float | None = None,
    activity_level: str | None = None,
    exercises: tuple[dict, ...] | None = None,
) -> dict:
    """Build a full week of training from a normalized training profile.

    Pure: every input arrives as an argument, so the same inputs always produce
    the same plan.
    """
    goal = goal if goal in TRAINING_GOALS else DEFAULT_TRAINING_GOAL
    one_rm_by_exercise = one_rm_by_exercise or {}
    history_by_exercise = history_by_exercise or {}
    experience = training_profile.normalize_experience(profile.get("experience_level"))
    equipment = training_profile.normalize_equipment(profile.get("equipment"))
    exclusions = training_profile.normalize_exclusions(profile.get("movement_exclusions"))
    session_time = training_profile.normalize_session_time(profile.get("session_time_min"))
    days_available = training_profile.normalize_days_available(profile.get("days_available"))
    needs_clearance = bool(profile.get("needs_medical_clearance"))

    notes: list[str] = []
    if needs_clearance:
        # Red flags collapse the plan to unloaded general movement until a
        # physician clears the user. Guidance, not a diagnosis.
        goal = "general"
        experience = "beginner"
        equipment = ["bodyweight"]
        days_available = min(days_available, 3)
        session_time = min(session_time, 30)
        notes.append(training_profile.MEDICAL_CLEARANCE_NOTICE)

    if experience == "beginner":
        # Dips and pull-ups need no equipment but aren't beginner movements;
        # push-up and inverted-row progressions train the same patterns.
        exclusions = sorted(set(exclusions) | {HIGH_SKILL_FLAG})

    scheme = intensity_scheme(goal)
    volume = weekly_volume(goal, experience)
    split = (
        {"key": "general_movement", "name": SPLIT_TEMPLATES["general_movement"]["name"], "days": ["general_movement"] * days_available}
        if needs_clearance
        else select_split(days_available, goal, experience)
    )
    cardio = prescribe_cardio(goal, activity_level, tdee, exclusions, needs_clearance)
    deload = deload_due(week_index, missed_sessions)
    if deload:
        notes.append(
            "Deload week — same movements, roughly half the sets and lighter loads. Recovery is where the adaptation lands."
        )
    if missed_sessions >= MISSED_SESSIONS_FOR_DELOAD:
        notes.append(f"{missed_sessions} sessions missed recently, so this week backs off instead of pushing on.")

    seed = variation_seed(user_id, mesocycle_index)
    per_session = exercises_per_session(session_time, cardio["minutes_per_session"] if needs_clearance else 0)
    cardio_days = _cardio_day_indices(len(split["days"]), cardio["sessions_per_week"])

    days: list[dict] = []
    for day_index, focus in enumerate(split["days"]):
        patterns = FOCUS_PATTERNS.get(focus, FOCUS_PATTERNS["full_body"])[:per_session]
        day_exercises = []
        used_ids: set[str] = set()
        for slot, pattern in enumerate(patterns):
            accessory = pattern in ACCESSORY_PATTERNS
            # Ask for several candidates so a repeated pattern slot (two rows on
            # a pull day) doesn't prescribe the same movement twice.
            picks = select_exercises(
                pattern,
                equipment,
                exclusions,
                seed + day_index * len(patterns) + slot,
                count=len(patterns),
                exercises=exercises,
                prefer_compound=not accessory,
            )
            ex = next((p for p in picks if p["id"] not in used_ids), None)
            if ex is None:
                continue
            used_ids.add(ex["id"])
            prescription = next_prescription(experience, history_by_exercise.get(ex["id"], []), goal)
            load = prescription["load"]
            if load is None:
                load = load_from_1rm(one_rm_by_exercise.get(ex["id"]), scheme["pct_1rm"][0])
            sets = int(clamp(prescription["sets"], MIN_SETS_PER_EXERCISE, MAX_SETS_PER_EXERCISE))
            reps = prescription["reps"]
            if accessory:
                sets = ACCESSORY_SETS
                reps = int(clamp(reps, ACCESSORY_MIN_REPS, ACCESSORY_MAX_REPS))
                load = None
            if deload:
                sets, load = apply_deload(sets, load)
            day_exercises.append(
                {
                    "exercise_id": ex["id"],
                    "name": ex["name"],
                    "pattern": ex["pattern"],
                    "primary": list(ex.get("primary", [])),
                    "secondary": list(ex.get("secondary", [])),
                    "sets": sets,
                    "reps": reps,
                    "pct_1rm": None if accessory else (prescription.get("pct_1rm") or scheme["pct_1rm"][0]),
                    "rpe": prescription["rpe"],
                    "load": load,
                    "rest_s": rest_seconds(scheme, ex["pattern"], bool(ex.get("compound"))),
                    "progression": prescription["change"],
                    "cue": prescription["note"],
                }
            )
        day = {
            "focus": focus,
            "title": FOCUS_TITLES.get(focus, focus.replace("_", " ").title()),
            "exercises": day_exercises,
            "cardio": (
                {
                    "modality": cardio["modality"],
                    "intensity": cardio["intensity"],
                    "minutes_per_session": cardio["minutes_per_session"],
                }
                if day_index in cardio_days
                else None
            ),
        }
        days.append(day)

    notes.extend(enforce_volume_caps(days, volume["cap"]))
    for day in days:
        fit_session(day, session_time)
    if not deload:
        # After fitting, so extra sets land in the time the user actually has.
        top_up_volume(days, volume["sets_per_muscle"], volume["cap"], session_time)
    for day in days:
        day["est_duration_min"] = estimate_duration_min(day)

    by_muscle = weekly_volume_by_muscle(days)
    return {
        "goal": goal,
        "experience": experience,
        "split": split["name"],
        "split_key": split["key"],
        "mesocycle_index": mesocycle_index,
        "week_index": week_index,
        "deload": deload,
        "days": days,
        "weekly_volume_by_muscle": by_muscle,
        "volume_target": volume,
        "volume_shortfall": volume_shortfall(days, volume["sets_per_muscle"]),
        "intensity": scheme,
        "progression_rule": PROGRESSION_RULES[experience],
        "progression_note": PROGRESSION_DESCRIPTIONS[PROGRESSION_RULES[experience]],
        "next_deload_week": next_deload_week(week_index),
        "cardio": cardio,
        "session_time_min": session_time,
        "equipment": equipment,
        "exclusions": exclusions,
        "medical_clearance_required": needs_clearance,
        "variation_seed": seed,
        "notes": notes,
    }


def _cardio_day_indices(day_count: int, sessions: int) -> set[int]:
    """Spread cardio sessions across the training week."""
    if day_count <= 0 or sessions <= 0:
        return set()
    sessions = min(sessions, day_count)
    return {round(i * day_count / sessions) % day_count for i in range(sessions)}


def build_plan(
    user_id: int,
    mesocycle_index: int = 0,
    week_index: int = 1,
    missed_sessions: int = 0,
    one_rm_by_exercise: dict[str, float] | None = None,
    history_by_exercise: dict[str, list[dict]] | None = None,
) -> dict:
    """Gather a user's stored inputs and generate their week.

    Thin impure wrapper: reads the profile, goal, and nutrition TDEE, then
    hands everything to `generate_plan`.
    """
    profile = training_profile.get_training_profile(user_id)
    user = db.get_user_by_id(user_id) or {}
    goal = resolve_training_goal(user_id)

    tdee = None
    try:
        tdee = (get_nutrition_targets(user_id) or {}).get("tdee")
    except Exception as e:  # targets are advisory here; never block a plan
        logger.warning("Could not read nutrition targets for user %s: %s", user_id, e)

    return generate_plan(
        profile,
        goal,
        user_id=user_id,
        mesocycle_index=mesocycle_index,
        week_index=week_index,
        missed_sessions=missed_sessions,
        one_rm_by_exercise=one_rm_by_exercise,
        history_by_exercise=history_by_exercise,
        tdee=tdee,
        activity_level=user.get("activity_level"),
    )
