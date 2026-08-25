"""Persistence for generated workout plans, and their sync onto the plan surface.

The engine in `workout.py` is pure; this module is the only place generated
plans touch the database. Two directions of flow:

- **Write:** a generated plan is stored as `workout_plans` → `workout_sessions`
  → `workout_exercise_prescriptions`, and each session is also mirrored onto the
  existing weekly calendar as a `training_plan_items` row so it renders exactly
  like a manually planned activity. Completion is delegated to
  `training_plan.complete_item`, which owns the `health_data` reconciliation.
- **Read:** logged sets in `workout_set_log` feed back into the engine as
  `history_by_exercise` / `one_rm_by_exercise`, so the next week's prescription
  is computed from what the user actually lifted.
"""

import json
import logging
from datetime import date, timedelta

from fitnessbot import db, training_plan, workout
from fitnessbot.tz import user_now

logger = logging.getLogger(__name__)

# Generated sessions are strength work, which is an `activity_type` the plan
# surface already renders and scores. The spec suggested a new "workout" type,
# but that would land without an icon and outside `compute_adherence`.
SESSION_ACTIVITY_TYPE = "strength"

# Weekday offsets from Monday for a given number of training days, spaced so
# consecutive hard sessions are separated where the day count allows.
WEEKDAY_SPREAD: dict[int, tuple[int, ...]] = {
    2: (0, 3),
    3: (0, 2, 4),
    4: (0, 1, 3, 4),
    5: (0, 1, 2, 4, 5),
    6: (0, 1, 2, 3, 4, 5),
}

# How many logged sessions per exercise the engine needs to progress a load.
HISTORY_DEPTH = 8


def _monday_of(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def current_week_start(user_id: int) -> str:
    return _monday_of(user_now(user_id).date())


def session_dates(week_start: str, day_count: int) -> list[str]:
    """Calendar dates for each session in a week."""
    monday = date.fromisoformat(week_start)
    offsets = WEEKDAY_SPREAD.get(day_count, tuple(range(min(day_count, 7))))
    return [(monday + timedelta(days=off)).isoformat() for off in offsets]


def next_plan_position(user_id: int) -> tuple[int, int]:
    """Where the user is in their programme: `(mesocycle_index, week_index)`.

    A finished deload week closes the mesocycle, which is what rotates the
    variation seed onto a fresh set of movements.
    """
    latest = _latest_plan_row(user_id)
    if not latest:
        return 0, 1
    mesocycle = int(latest["mesocycle_index"])
    week = int(latest["week_index"])
    if week >= workout.DELOAD_CYCLE_WEEKS:
        return mesocycle + 1, 1
    return mesocycle, week + 1


def _latest_plan_row(user_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM workout_plans WHERE user_id = ? ORDER BY created_at DESC, wp_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Reading logged work back into the engine ---------------------------


def history_by_exercise(user_id: int, depth: int = HISTORY_DEPTH) -> dict[str, list[dict]]:
    """Per-exercise session history, oldest first, in `next_prescription` shape.

    Sets logged on the same session are collapsed into one entry — progression
    keys off "did I finish the session I was prescribed", not off single sets.
    """
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """SELECT exercise_id, ws_id, wep_id, reps, weight, rpe, completed, logged_at
               FROM workout_set_log WHERE user_id = ? ORDER BY logged_at, wsl_id""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, dict[str, dict]] = {}
    for row in rows:
        exercise_id = row["exercise_id"]
        # Ad-hoc sets have no session; keep them separated by timestamp.
        session_key = str(row["ws_id"]) if row["ws_id"] else f"adhoc:{row['logged_at']}"
        session = grouped.setdefault(exercise_id, {}).setdefault(
            session_key,
            {"weight": None, "reps": None, "sets": 0, "completed": True, "logged_at": row["logged_at"]},
        )
        session["sets"] += 1
        if row["weight"] is not None:
            session["weight"] = max(session["weight"] or 0.0, float(row["weight"]))
        if row["reps"] is not None:
            reps = int(row["reps"])
            session["reps"] = reps if session["reps"] is None else min(session["reps"], reps)
        if not row["completed"]:
            session["completed"] = False

    history: dict[str, list[dict]] = {}
    for exercise_id, sessions in grouped.items():
        ordered = sorted(sessions.values(), key=lambda s: s["logged_at"])
        history[exercise_id] = ordered[-depth:]
    return history


def one_rm_by_exercise(user_id: int) -> dict[str, float]:
    """Best estimated 1RM per exercise from every logged set."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """SELECT exercise_id, reps, weight FROM workout_set_log
               WHERE user_id = ? AND weight IS NOT NULL AND completed = 1""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    best: dict[str, float] = {}
    for row in rows:
        estimate = workout.estimate_1rm(row["weight"], row["reps"])
        if estimate and estimate > best.get(row["exercise_id"], 0.0):
            best[row["exercise_id"]] = estimate
    return best


def count_missed_sessions(user_id: int, since_days: int = 14) -> int:
    """Sessions whose date has passed without being completed."""
    cutoff = (user_now(user_id).date() - timedelta(days=since_days)).isoformat()
    today = user_now(user_id).date().isoformat()
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM workout_sessions
               WHERE user_id = ? AND status = 'planned' AND date >= ? AND date < ?""",
            (user_id, cutoff, today),
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


# --- Writing a generated plan ------------------------------------------


def generate_and_save(user_id: int, week_start: str | None = None) -> dict:
    """Generate this user's next week from their logged work and persist it."""
    week_start = week_start or current_week_start(user_id)
    mesocycle_index, week_index = next_plan_position(user_id)
    plan = workout.build_plan(
        user_id,
        mesocycle_index=mesocycle_index,
        week_index=week_index,
        missed_sessions=count_missed_sessions(user_id),
        one_rm_by_exercise=one_rm_by_exercise(user_id),
        history_by_exercise=history_by_exercise(user_id),
    )
    return save_plan(user_id, plan, week_start)


def save_plan(user_id: int, plan: dict, week_start: str | None = None) -> dict:
    """Persist a generated plan and mirror its sessions onto the calendar.

    Regenerating a week supersedes the previous plan for it. Calendar items the
    user already completed are left alone — a regenerated plan must not erase
    training that actually happened.
    """
    week_start = week_start or current_week_start(user_id)
    dates = session_dates(week_start, len(plan["days"]))

    _supersede_week(user_id, week_start)

    conn = db.get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO workout_plans
               (user_id, week_start, goal, experience, split_key, split_name, mesocycle_index,
                week_index, deload, next_deload_week, variation_seed, progression_rule, plan_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                week_start,
                plan["goal"],
                plan["experience"],
                plan["split_key"],
                plan["split"],
                plan["mesocycle_index"],
                plan["week_index"],
                1 if plan["deload"] else 0,
                plan["next_deload_week"],
                plan["variation_seed"],
                plan["progression_rule"],
                json.dumps(plan),
            ),
        )
        wp_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    session_ids = []
    for day_index, (day, date_str) in enumerate(zip(plan["days"], dates)):
        session_ids.append(_save_session(user_id, wp_id, day_index, date_str, day, plan))

    return {"wp_id": wp_id, "week_start": week_start, "session_ids": session_ids, "plan": plan}


def _save_session(user_id: int, wp_id: int, day_index: int, date_str: str, day: dict, plan: dict) -> int:
    duration = day.get("est_duration_min") or plan.get("session_time_min")
    title = f"{day['title']} — {plan['split']}"
    notes = _session_notes(day, plan)

    item = training_plan.add_item(
        user_id,
        _monday_of(date.fromisoformat(date_str)),
        date_str,
        SESSION_ACTIVITY_TYPE,
        title,
        duration,
        notes,
    )

    conn = db.get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO workout_sessions
               (wp_id, user_id, date, day_index, focus, title, planned_duration_min, cardio_json, item_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wp_id,
                user_id,
                date_str,
                day_index,
                day["focus"],
                title,
                duration,
                json.dumps(day["cardio"]) if day.get("cardio") else None,
                item["item_id"],
            ),
        )
        ws_id = cursor.lastrowid
        for position, ex in enumerate(day["exercises"]):
            conn.execute(
                """INSERT INTO workout_exercise_prescriptions
                   (ws_id, user_id, exercise_id, name, pattern, position, sets, reps,
                    pct_1rm, rpe, load, rest_s, progression, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ws_id,
                    user_id,
                    ex["exercise_id"],
                    ex["name"],
                    ex["pattern"],
                    position,
                    ex["sets"],
                    ex["reps"],
                    ex.get("pct_1rm"),
                    ex.get("rpe"),
                    ex.get("load"),
                    ex.get("rest_s"),
                    ex.get("progression"),
                    ex.get("cue"),
                ),
            )
        conn.commit()
        return ws_id
    finally:
        conn.close()


def _session_notes(day: dict, plan: dict) -> str:
    """One-line summary for the calendar item, so the card reads on its own."""
    parts = [f"{len(day['exercises'])} lifts"]
    if plan["deload"]:
        parts.append("deload week")
    if day.get("cardio"):
        parts.append(f"+{day['cardio']['minutes_per_session']} min {day['cardio']['intensity']}")
    return ", ".join(parts)


def _supersede_week(user_id: int, week_start: str) -> None:
    """Retire an earlier plan for the same week and drop its untouched items."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT wp_id FROM workout_plans WHERE user_id = ? AND week_start = ? AND active = 1",
            (user_id, week_start),
        ).fetchall()
        if not rows:
            return
        wp_ids = [row["wp_id"] for row in rows]
        placeholders = ",".join("?" for _ in wp_ids)
        items = conn.execute(
            f"""SELECT item_id FROM workout_sessions
                WHERE user_id = ? AND wp_id IN ({placeholders}) AND status = 'planned' AND item_id IS NOT NULL""",
            [user_id, *wp_ids],
        ).fetchall()
        conn.execute(
            f"UPDATE workout_plans SET active = 0 WHERE user_id = ? AND wp_id IN ({placeholders})",
            [user_id, *wp_ids],
        )
        conn.commit()
    finally:
        conn.close()

    for row in items:
        training_plan.remove_item(row["item_id"], user_id)


# --- Reading plans back out --------------------------------------------


def get_current_plan(user_id: int) -> dict | None:
    """The active plan for this week, with sessions and prescriptions nested."""
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM workout_plans
               WHERE user_id = ? AND week_start = ? AND active = 1
               ORDER BY wp_id DESC LIMIT 1""",
            (user_id, current_week_start(user_id)),
        ).fetchone()
        if not row:
            return None
        return _hydrate_plan(conn, dict(row))
    finally:
        conn.close()


def get_plan(user_id: int, wp_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM workout_plans WHERE wp_id = ? AND user_id = ?",
            (wp_id, user_id),
        ).fetchone()
        if not row:
            return None
        return _hydrate_plan(conn, dict(row))
    finally:
        conn.close()


def _hydrate_plan(conn, plan_row: dict) -> dict:
    plan_row["deload"] = bool(plan_row["deload"])
    plan_row["plan"] = json.loads(plan_row["plan_json"])
    sessions = []
    for session in conn.execute(
        "SELECT * FROM workout_sessions WHERE wp_id = ? ORDER BY day_index",
        (plan_row["wp_id"],),
    ).fetchall():
        session = dict(session)
        session["cardio"] = json.loads(session["cardio_json"]) if session["cardio_json"] else None
        session["exercises"] = [
            dict(ex)
            for ex in conn.execute(
                """SELECT * FROM workout_exercise_prescriptions
                   WHERE ws_id = ? ORDER BY position""",
                (session["ws_id"],),
            ).fetchall()
        ]
        sessions.append(session)
    plan_row["sessions"] = sessions
    return plan_row


def get_session(user_id: int, ws_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM workout_sessions WHERE ws_id = ? AND user_id = ?",
            (ws_id, user_id),
        ).fetchone()
        if not row:
            return None
        session = dict(row)
        session["cardio"] = json.loads(session["cardio_json"]) if session["cardio_json"] else None
        session["exercises"] = [
            dict(ex)
            for ex in conn.execute(
                "SELECT * FROM workout_exercise_prescriptions WHERE ws_id = ? ORDER BY position",
                (ws_id,),
            ).fetchall()
        ]
        return session
    finally:
        conn.close()


def get_session_for_date(user_id: int, date_str: str | None = None) -> dict | None:
    date_str = date_str or user_now(user_id).date().isoformat()
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT ws.ws_id FROM workout_sessions ws
               JOIN workout_plans wp ON ws.wp_id = wp.wp_id
               WHERE ws.user_id = ? AND ws.date = ? AND wp.active = 1
               ORDER BY ws.ws_id DESC LIMIT 1""",
            (user_id, date_str),
        ).fetchone()
    finally:
        conn.close()
    return get_session(user_id, row["ws_id"]) if row else None


# --- Logging work -------------------------------------------------------


def log_set(
    user_id: int,
    wep_id: int | None,
    reps: int,
    weight: float | None = None,
    rpe: float | None = None,
    completed: bool = True,
    exercise_id: str | None = None,
    ws_id: int | None = None,
) -> dict | None:
    """Record one set. Resolves the exercise and session from the prescription."""
    conn = db.get_connection()
    try:
        if wep_id:
            row = conn.execute(
                "SELECT ws_id, exercise_id FROM workout_exercise_prescriptions WHERE wep_id = ? AND user_id = ?",
                (wep_id, user_id),
            ).fetchone()
            if not row:
                return None
            ws_id = row["ws_id"]
            exercise_id = row["exercise_id"]
        if not exercise_id:
            return None

        next_index = conn.execute(
            """SELECT COALESCE(MAX(set_index), 0) + 1 AS n FROM workout_set_log
               WHERE user_id = ? AND exercise_id = ? AND ws_id IS ?""",
            (user_id, exercise_id, ws_id),
        ).fetchone()["n"]

        cursor = conn.execute(
            """INSERT INTO workout_set_log
               (user_id, wep_id, ws_id, exercise_id, set_index, reps, weight, rpe, completed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, wep_id, ws_id, exercise_id, next_index, reps, weight, rpe, 1 if completed else 0),
        )
        conn.commit()
        return {
            "wsl_id": cursor.lastrowid,
            "ws_id": ws_id,
            "wep_id": wep_id,
            "exercise_id": exercise_id,
            "set_index": next_index,
            "reps": reps,
            "weight": weight,
            "rpe": rpe,
            "completed": completed,
            "estimated_1rm": workout.estimate_1rm(weight, reps) if weight else None,
        }
    finally:
        conn.close()


def swap_exercise(user_id: int, wep_id: int) -> dict | None:
    """Replace one prescribed movement with the next legal alternative.

    Rotates through the same candidate order the generator drew from, skipping
    movements already in the session, and re-prescribes through the engine — so
    a swap can't smuggle in an excluded pattern or an unearned load. Sets
    already logged keep their own `exercise_id`, so history stays truthful about
    what was actually lifted.
    """
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT wep.*, ws.wp_id FROM workout_exercise_prescriptions wep
               JOIN workout_sessions ws ON wep.ws_id = ws.ws_id
               WHERE wep.wep_id = ? AND wep.user_id = ?""",
            (wep_id, user_id),
        ).fetchone()
        if not row:
            return None
        current = dict(row)
        plan_row = conn.execute(
            "SELECT plan_json, deload FROM workout_plans WHERE wp_id = ? AND user_id = ?",
            (current["wp_id"], user_id),
        ).fetchone()
        siblings = conn.execute(
            "SELECT exercise_id FROM workout_exercise_prescriptions WHERE ws_id = ?",
            (current["ws_id"],),
        ).fetchall()
    finally:
        conn.close()

    if not plan_row:
        return None
    plan = json.loads(plan_row["plan_json"])
    accessory = current["pattern"] in workout.ACCESSORY_PATTERNS
    candidates = workout.pattern_candidates(
        current["pattern"],
        plan["equipment"],
        plan["exclusions"],
        prefer_compound=not accessory,
    )
    in_session = {s["exercise_id"] for s in siblings if s["exercise_id"] != current["exercise_id"]}
    start = next((i for i, ex in enumerate(candidates) if ex["id"] == current["exercise_id"]), -1)
    rotated = candidates[start + 1:] + candidates[: start + 1]
    replacement = next((ex for ex in rotated if ex["id"] not in in_session and ex["id"] != current["exercise_id"]), None)
    if not replacement:
        return None

    prescription = workout.prescribe_exercise(
        replacement,
        plan["experience"],
        plan["goal"],
        plan["intensity"],
        deload=bool(plan_row["deload"]),
        history=history_by_exercise(user_id).get(replacement["id"], []),
        one_rm=one_rm_by_exercise(user_id).get(replacement["id"]),
    )

    conn = db.get_connection()
    try:
        conn.execute(
            """UPDATE workout_exercise_prescriptions
               SET exercise_id = ?, name = ?, sets = ?, reps = ?, pct_1rm = ?, rpe = ?,
                   load = ?, rest_s = ?, progression = ?, note = ?
               WHERE wep_id = ? AND user_id = ?""",
            (
                prescription["exercise_id"],
                prescription["name"],
                prescription["sets"],
                prescription["reps"],
                prescription["pct_1rm"],
                prescription["rpe"],
                prescription["load"],
                prescription["rest_s"],
                prescription["progression"],
                prescription["cue"],
                wep_id,
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {"wep_id": wep_id, "ws_id": current["ws_id"], "replaced": current["exercise_id"], **prescription}


def complete_session(user_id: int, ws_id: int, actual_duration_min: int | None = None) -> dict | None:
    """Mark a session done and complete its calendar item.

    The `health_data` workout entry is created by `training_plan.complete_item`,
    so a generated session reconciles exactly like any other planned activity.
    """
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM workout_sessions WHERE ws_id = ? AND user_id = ?",
            (ws_id, user_id),
        ).fetchone()
        if not row:
            return None
        session = dict(row)
        conn.execute(
            "UPDATE workout_sessions SET status = 'completed', completed_at = ? WHERE ws_id = ?",
            (db.utcnow(), ws_id),
        )
        conn.commit()
    finally:
        conn.close()

    item = None
    if session["item_id"]:
        item = training_plan.get_items_for_date(user_id, session["date"])
        item = next((i for i in item if i["item_id"] == session["item_id"]), None)
        if item and item["status"] != "completed":
            item = training_plan.complete_item(session["item_id"], user_id, actual_duration_min)

    session["status"] = "completed"
    session["item"] = item
    return session


def sets_for_session(user_id: int, ws_id: int) -> list[dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM workout_set_log WHERE user_id = ? AND ws_id = ? ORDER BY wsl_id",
            (user_id, ws_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
