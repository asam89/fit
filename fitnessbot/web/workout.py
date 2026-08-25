"""API routes for the generated workout plan.

Thin layer on purpose: every number in the response comes from the engine via
`workout_store`, and every user-supplied adjustment goes through
`training_profile`'s normalizers before it can reach generation. The route
handlers themselves never decide sets, reps or loads.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fitnessbot import db, training_profile, workout, workout_store
from fitnessbot.tz import user_now
from fitnessbot.web.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# Fields /adjust may change. Anything else on the user record is out of reach
# of this endpoint even if a client sends it.
ADJUSTABLE_FIELDS = ("days_available", "session_time_min", "equipment", "movement_exclusions")


def _normalize_adjustment(body: dict) -> dict:
    """Clamp requested changes to the engine's supported input ranges."""
    updates: dict[str, int | str] = {}
    if "days_available" in body:
        updates["days_available"] = training_profile.normalize_days_available(body["days_available"])
    if "session_time_min" in body:
        updates["session_time_min"] = training_profile.normalize_session_time(body["session_time_min"])
    if "equipment" in body:
        updates["equipment"] = json.dumps(training_profile.normalize_equipment(body["equipment"]))
    if "movement_exclusions" in body:
        updates["movement_exclusions"] = json.dumps(
            training_profile.normalize_exclusions(body["movement_exclusions"])
        )
    return updates


def _progress(user_id: int, sessions: list[dict]) -> None:
    """Attach logged sets to each prescription, in place."""
    for session in sessions:
        logged = workout_store.sets_for_session(user_id, session["ws_id"])
        by_prescription: dict[int, list[dict]] = {}
        for entry in logged:
            by_prescription.setdefault(entry["wep_id"], []).append(entry)
        for exercise in session["exercises"]:
            sets = by_prescription.get(exercise["wep_id"], [])
            exercise["logged_sets"] = sets
            exercise["sets_done"] = len(sets)


def _payload(user_id: int) -> dict:
    profile = training_profile.get_training_profile(user_id)
    plan = workout_store.get_current_plan(user_id)
    body: dict = {
        "profile": profile,
        "profile_complete": training_profile.is_complete(profile),
        "needs_medical_clearance": profile["needs_medical_clearance"],
        "clearance_notice": (
            training_profile.MEDICAL_CLEARANCE_NOTICE if profile["needs_medical_clearance"] else None
        ),
        "options": {
            "equipment": list(training_profile.EQUIPMENT_OPTIONS),
            "exclusions": list(training_profile.MOVEMENT_EXCLUSIONS),
            "min_days": training_profile.MIN_DAYS_AVAILABLE,
            "max_days": training_profile.MAX_DAYS_AVAILABLE,
        },
        "goal": workout.resolve_training_goal(user_id),
    }
    if not plan:
        body["plan"] = None
        return body

    _progress(user_id, plan["sessions"])
    today = user_now(user_id).date().isoformat()
    generated = plan["plan"]
    body["plan"] = {
        "wp_id": plan["wp_id"],
        "week_start": plan["week_start"],
        "goal": plan["goal"],
        "experience": plan["experience"],
        "split": plan["split_name"],
        "mesocycle_index": plan["mesocycle_index"],
        "week_index": plan["week_index"],
        "deload": plan["deload"],
        "next_deload_week": plan["next_deload_week"],
        "progression_rule": plan["progression_rule"],
        "progression_note": generated.get("progression_note"),
        "notes": generated.get("notes", []),
        "volume_target": generated.get("volume_target"),
        "volume_by_muscle": generated.get("weekly_volume_by_muscle"),
        "volume_shortfall": generated.get("volume_shortfall"),
        "cardio": generated.get("cardio"),
        "sessions": plan["sessions"],
        "today": today,
        "today_ws_id": next((s["ws_id"] for s in plan["sessions"] if s["date"] == today), None),
    }
    return body


@router.get("/api/workout/current")
async def current_workout(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse(_payload(user["user_id"]))


@router.post("/api/workout/generate")
async def generate_workout(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await _json_body(request)
    uid = user["user_id"]
    workout_store.generate_and_save(uid, body.get("week_start"))
    return JSONResponse(_payload(uid), status_code=201)


@router.post("/api/workout/log")
async def log_workout(request: Request):
    """Log one set, or mark the whole session done."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await _json_body(request)
    uid = user["user_id"]

    if body.get("complete_session"):
        ws_id = body.get("ws_id")
        if not ws_id:
            return JSONResponse({"error": "ws_id required"}, status_code=400)
        session = workout_store.complete_session(uid, int(ws_id), body.get("actual_duration_min"))
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse({"session": session})

    reps = body.get("reps")
    if reps in (None, ""):
        return JSONResponse({"error": "reps required"}, status_code=400)

    logged = workout_store.log_set(
        uid,
        body.get("wep_id"),
        int(reps),
        weight=_as_float(body.get("weight")),
        rpe=_as_float(body.get("rpe")),
        completed=bool(body.get("completed", True)),
        exercise_id=body.get("exercise_id"),
        ws_id=body.get("ws_id"),
    )
    if not logged:
        return JSONResponse({"error": "Unknown exercise"}, status_code=404)
    return JSONResponse({"set": logged}, status_code=201)


@router.post("/api/workout/adjust")
async def adjust_workout(request: Request):
    """Change training inputs or swap a movement, then reflect it in the plan.

    An input change regenerates the week (the split itself depends on days and
    equipment); a swap only rewrites the one prescription, so the rest of the
    week the user has already started stays put.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await _json_body(request)
    uid = user["user_id"]

    if body.get("swap_wep_id"):
        swapped = workout_store.swap_exercise(uid, int(body["swap_wep_id"]))
        if not swapped:
            return JSONResponse({"error": "No alternative available"}, status_code=404)
        return JSONResponse({"swapped": swapped, **_payload(uid)})

    updates = _normalize_adjustment(body)
    if not updates:
        return JSONResponse({"error": f"Nothing to adjust; expected one of {list(ADJUSTABLE_FIELDS)}"}, status_code=400)

    db.update_user(uid, **updates)
    workout_store.generate_and_save(uid, body.get("week_start"))
    return JSONResponse({"adjusted": updates, **_payload(uid)})


async def _json_body(request: Request) -> dict:
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _as_float(value: float | int | str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
