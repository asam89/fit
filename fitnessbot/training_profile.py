"""Training inputs the workout engine needs — normalization and safety screen.

Pure module (no network, no LLM), mirroring nutrition.py's validator style. The
workout engine consumes get_training_profile() as its input contract, so every
value it receives is already normalized, clamped, and defaulted.

Physical traits (sex, age, height, weight, activity_level) and goals are NOT
duplicated here — they come from the users table, metrics.py, and the goals
table. This module only covers inputs that had nowhere to live.
"""

import json
import logging

from fitnessbot import db
from fitnessbot.nutrition import clamp

logger = logging.getLogger(__name__)

EXPERIENCE_LEVELS = ("beginner", "intermediate", "advanced")
DEFAULT_EXPERIENCE = "beginner"

# Training days per week. The engine's split table (2-6) defines these bounds.
MIN_DAYS_AVAILABLE = 2
MAX_DAYS_AVAILABLE = 6
DEFAULT_DAYS_AVAILABLE = 3

# A session shorter than ~20 min can't hold a warmup plus meaningful work;
# beyond ~120 min the engine would prescribe volume most people won't finish.
MIN_SESSION_MIN = 20
MAX_SESSION_MIN = 120
DEFAULT_SESSION_MIN = 60

EQUIPMENT_OPTIONS = (
    "bodyweight",
    "dumbbells",
    "barbell",
    "machines",
    "bands",
    "full_gym",
)
# Everyone always has bodyweight available, so an empty selection is not a
# dead end for the engine — it degrades to bodyweight-only programming.
DEFAULT_EQUIPMENT = ("bodyweight",)

# Structured movement patterns a user can rule out. These map 1:1 onto the
# pattern tags the exercise library filters on, so an exclusion here removes
# every contraindicated exercise rather than relying on name matching.
MOVEMENT_EXCLUSIONS = (
    "squat",
    "hinge",
    "horizontal_push",
    "vertical_push",
    "horizontal_pull",
    "vertical_pull",
    "lunge",
    "carry",
    "core",
    "overhead",
    "jumping",
    "running",
)

# Conditions where self-directed progressive overload is not appropriate
# without clearance. Presence of any of these forces the engine into its
# conservative general-movement plan (see workout.py deload/safety).
MEDICAL_RED_FLAGS = (
    "cardiac_history",
    "uncontrolled_bp",
    "pregnancy",
    "recent_surgery",
    "acute_pain",
    "dizziness_or_fainting",
)

MEDICAL_CLEARANCE_NOTICE = (
    "Based on what you flagged, get clearance from a physician before starting "
    "structured training. Until then this stays on general, low-intensity "
    "movement only — this is guidance, not a medical opinion."
)


def normalize_experience(value: str | None) -> str:
    """Coerce to a known experience level, defaulting to the safest one."""
    if not value:
        return DEFAULT_EXPERIENCE
    candidate = str(value).strip().lower()
    if candidate in EXPERIENCE_LEVELS:
        return candidate
    return DEFAULT_EXPERIENCE


def normalize_days_available(value: int | str | None) -> int:
    """Clamp training days into the range the split table covers."""
    if value is None or value == "":
        return DEFAULT_DAYS_AVAILABLE
    try:
        days = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_DAYS_AVAILABLE
    return int(clamp(days, MIN_DAYS_AVAILABLE, MAX_DAYS_AVAILABLE))


def normalize_session_time(value: int | str | None) -> int:
    """Clamp session length to a range that can hold a real session."""
    if value is None or value == "":
        return DEFAULT_SESSION_MIN
    try:
        minutes = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_MIN
    return int(clamp(minutes, MIN_SESSION_MIN, MAX_SESSION_MIN))


def _normalize_choice_list(
    value: list[str] | tuple[str, ...] | str | None,
    allowed: tuple[str, ...],
) -> list[str]:
    """Parse a JSON string / list into a deduped, allowed-only, ordered list."""
    if value is None or value == "":
        return []

    raw: list[str] = []
    if isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Could not parse choice list JSON: %r", text[:80])
                parsed = []
            if isinstance(parsed, list):
                raw = [str(v) for v in parsed]
        else:
            raw = text.split(",")

    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        candidate = item.strip().lower().replace(" ", "_").replace("-", "_")
        if candidate in allowed and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    # Stable order regardless of submission order, so an unchanged selection
    # doesn't look like a change to the engine's variation seed.
    return [option for option in allowed if option in seen]


def normalize_equipment(value: list[str] | str | None) -> list[str]:
    """Equipment list; empty selection degrades to bodyweight, never nothing."""
    equipment = _normalize_choice_list(value, EQUIPMENT_OPTIONS)
    if not equipment:
        return list(DEFAULT_EQUIPMENT)
    # full_gym implies everything; keep it alone so selection logic stays simple.
    if "full_gym" in equipment:
        return ["full_gym"]
    return equipment


def normalize_exclusions(value: list[str] | str | None) -> list[str]:
    """Movement patterns to exclude from programming."""
    return _normalize_choice_list(value, MOVEMENT_EXCLUSIONS)


def normalize_medical_flags(value: list[str] | str | None) -> list[str]:
    """Red-flag screen answers."""
    return _normalize_choice_list(value, MEDICAL_RED_FLAGS)


def normalize_injuries(value: str | None, max_len: int = 500) -> str:
    """Free-text injury notes, trimmed to a sane length."""
    if not value:
        return ""
    return str(value).strip()[:max_len]


def get_training_profile(user_id: int) -> dict:
    """Normalized training inputs for a user — the engine's input contract.

    Always returns a usable profile: a user who has answered nothing gets
    safe defaults (beginner, 3 days, bodyweight, 60 min) rather than None,
    so plan generation never depends on a completed questionnaire.
    """
    user = db.get_user_by_id(user_id) or {}

    medical_flags = normalize_medical_flags(user.get("medical_flags"))
    return {
        "experience_level": normalize_experience(user.get("experience_level")),
        "days_available": normalize_days_available(user.get("days_available")),
        "equipment": normalize_equipment(user.get("equipment")),
        "session_time_min": normalize_session_time(user.get("session_time_min")),
        "injuries": normalize_injuries(user.get("injuries")),
        "movement_exclusions": normalize_exclusions(user.get("movement_exclusions")),
        "medical_flags": medical_flags,
        "needs_medical_clearance": bool(medical_flags),
        "screened": bool(user.get("medical_screen_at")),
    }


def is_complete(profile: dict) -> bool:
    """True if the user actually answered the screen, vs. running on defaults.

    The engine works either way; this only drives whether the UI still nudges
    the user to fill it in.
    """
    return bool(profile.get("screened"))
