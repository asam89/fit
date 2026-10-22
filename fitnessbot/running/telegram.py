"""Telegram run logging: parse -> draft -> confirmation card -> Save. Transport-agnostic.

Every function returns a Reply; handlers.py turns it into a Telegram message.
`state` is the per-chat dict (context.user_data) holding an in-progress guided
flow or edit. Nothing touches the runs table until the user taps Save.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from fitnessbot import db
from fitnessbot.running import store
from fitnessbot.running.parser import guardrail_questions, looks_like_run, parse_distance, parse_duration, parse_run
from fitnessbot.running.stats import format_distance, format_duration, format_pace, pace_s_per_km
from fitnessbot.tz import user_now, user_today

logger = logging.getLogger(__name__)

TYPE_CHOICES = ("easy", "tempo", "interval", "long", "race", "treadmill")
_FLOW_KEY = "run_flow"
_EDIT_KEY = "run_edit"
_CANCEL_WORDS = {"cancel", "stop", "nevermind", "never mind", "/cancel"}
_DRAFT_FIELDS = ("date", "distance_m", "duration_s", "run_type", "surface", "avg_hr", "max_hr",
                 "perceived_effort", "splits", "notes")


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)


def _units(user_id: int) -> str:
    user = db.get_user_by_id(user_id) or {}
    return "mi" if user.get("run_units") == "mi" else "km"


def _today(user_id: int) -> date:
    return date.fromisoformat(user_today(user_id))


def _nice_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%a, %b %d")


def _summary_lines(run: dict, units: str) -> list[str]:
    d, t = run.get("distance_m"), run.get("duration_s")
    lines = [
        f"Date: {_nice_date(run.get('local_date') or run['date'])}",
        f"Distance: {format_distance(d, units)}",
        f"Time: {format_duration(t)}",
        f"Pace: {format_pace(pace_s_per_km(d, t), units)}",
        f"Type: {run.get('run_type') or 'not set'}",
    ]
    if run.get("surface") and run.get("surface") != run.get("run_type"):
        lines.append(f"Surface: {run['surface']}")
    if run.get("avg_hr"):
        lines.append(f"Avg HR: {int(run['avg_hr'])}")
    if run.get("max_hr"):
        lines.append(f"Max HR: {int(run['max_hr'])}")
    if run.get("perceived_effort"):
        lines.append(f"Effort: {run['perceived_effort']}/10")
    splits = run.get("splits") or []
    if splits:
        lines.append(f"Reps: {len(splits)} x {format_distance(splits[0]['distance_m'], units)}")
    return lines


def _card(user_id: int, draft_id: int, draft: dict) -> Reply:
    units = _units(user_id)
    title = "Update this run?" if draft.get("run_id") else "Log this run?"
    lines = [f"\U0001F3C3 {title}", *_summary_lines(draft, units)]
    questions = guardrail_questions(draft, units)
    if questions:
        lines += ["", *[f"\u26A0\uFE0F {q}" for q in questions]]
    lines += ["", "Nothing is saved until you tap Save."]
    save_label = "Yes, save it" if questions else "Save"
    return Reply("\n".join(lines), [[
        (save_label, f"run_save:{draft_id}"),
        ("Edit", f"run_edit:{draft_id}"),
        ("Cancel", f"run_cancel:{draft_id}"),
    ]])


def _new_draft_card(user_id: int, draft: dict) -> Reply:
    payload = {k: draft[k] for k in (*_DRAFT_FIELDS, "run_id") if draft.get(k) is not None}
    draft_id = store.create_draft(user_id, payload)
    return _card(user_id, draft_id, payload)


def _ask_missing(user_id: int, draft: dict, state: dict) -> Reply:
    missing = [k for k in ("distance_m", "duration_s") if not draft.get(k)]
    guided = bool(state.get(_FLOW_KEY, {}).get("guided"))
    if missing:
        step = missing[0]
        state[_FLOW_KEY] = {"step": step, "draft": draft, "guided": guided}
        units = _units(user_id)
        if step == "distance_m":
            return Reply(f"How far did you run? (e.g. 5k, 3.1 mi, or just a number in {units})")
        return Reply("How long did it take? (e.g. 27:30, 58 min or 1:05:00)")
    if not draft.get("run_type") and guided:
        state[_FLOW_KEY] = {"step": "run_type", "draft": draft, "guided": True}
        row1 = [(t.title(), f"run_type:{t}") for t in TYPE_CHOICES[:3]]
        row2 = [(t.title(), f"run_type:{t}") for t in TYPE_CHOICES[3:]]
        return Reply("What kind of run was it?", [row1, row2, [("Skip", "run_type:skip")]])
    state.pop(_FLOW_KEY, None)
    return _new_draft_card(user_id, draft)


def start_guided(user_id: int, state: dict, args_text: str = "") -> Reply:
    """/run [text]: with text, parse it; without, ask distance, time, then type."""
    state.pop(_EDIT_KEY, None)
    if args_text.strip():
        draft = parse_run(args_text, today=_today(user_id), default_units=_units(user_id))
        state[_FLOW_KEY] = {"guided": True}
        return _ask_missing(user_id, draft, state)
    state[_FLOW_KEY] = {"guided": True}
    return _ask_missing(user_id, {"date": _today(user_id).isoformat()}, state)


def _continue_flow(user_id: int, text: str, state: dict) -> Reply:
    flow = state[_FLOW_KEY]
    draft = dict(flow.get("draft") or {})
    step = flow.get("step")
    units = _units(user_id)
    if step == "distance_m":
        d = parse_distance(text, units)
        if not d:
            return Reply("I didn't catch the distance. Try something like 5k, 3.1 mi or 8 (or say cancel).")
        draft["distance_m"] = round(d, 1)
        t = parse_duration(text)
        if t and not draft.get("duration_s"):
            draft["duration_s"] = t
    elif step == "duration_s":
        t = parse_duration(text) or (int(float(text)) * 60 if text.strip().replace(".", "", 1).isdigit() else None)
        if not t:
            return Reply("I didn't catch the time. Try 27:30, 58 min or 1:05:00 (or say cancel).")
        draft["duration_s"] = t
    elif step == "run_type":
        word = text.strip().lower()
        draft["run_type"] = next((c for c in TYPE_CHOICES if word.startswith(c[:4])), None)
        state.pop(_FLOW_KEY, None)
        return _new_draft_card(user_id, draft)
    flow["draft"] = draft
    return _ask_missing(user_id, draft, state)


def _apply_edit(user_id: int, text: str, state: dict) -> Reply:
    draft_id = state.pop(_EDIT_KEY)
    base = store.get_draft(user_id, draft_id)
    if base is None:
        return Reply("That run card expired. Send the run again, e.g. 'ran 5k in 27:30'.")
    store.delete_draft(user_id, draft_id)
    payload = dict(base)
    parsed = parse_run(text, today=_today(user_id), default_units=_units(user_id))
    for k in ("distance_m", "duration_s", "run_type", "surface", "avg_hr", "max_hr", "perceived_effort", "splits"):
        if parsed.get(k) is not None:
            payload[k] = parsed[k]
    if parsed["date"] != _today(user_id).isoformat() or "today" in text.lower():
        payload["date"] = parsed["date"]
    return _new_draft_card(user_id, payload)


def handle_text(user_id: int, text: str, state: dict) -> Reply | None:
    """Route a plain message. Returns None when the message isn't about a run."""
    if (state.get(_FLOW_KEY) or state.get(_EDIT_KEY)) and text.strip().lower() in _CANCEL_WORDS:
        state.pop(_FLOW_KEY, None)
        state.pop(_EDIT_KEY, None)
        return Reply("OK, cancelled. Nothing was saved.")
    if state.get(_EDIT_KEY):
        return _apply_edit(user_id, text, state)
    if state.get(_FLOW_KEY, {}).get("step"):
        return _continue_flow(user_id, text, state)
    from fitnessbot.router import _PB_PATTERN
    if _PB_PATTERN.match(text.strip()) or not looks_like_run(text):
        return None  # personal bests keep their existing flow
    draft = parse_run(text, today=_today(user_id), default_units=_units(user_id))
    return _ask_missing(user_id, draft, state)


def _started_at(user_id: int, local_date: str, duration_s: int) -> datetime:
    now = user_now(user_id)
    d = date.fromisoformat(local_date)
    if d == now.date():
        start = now - timedelta(seconds=duration_s)
        if start.date() != d:
            start = datetime.combine(d, time(0, 0), tzinfo=now.tzinfo)
    else:
        start = datetime.combine(d, time(12, 0), tzinfo=now.tzinfo)
    return start.astimezone(timezone.utc)


def _calendar_entry(user_id: int, run: dict) -> int | None:
    from fitnessbot import training_plan
    try:
        item = training_plan.log_completed_activity(
            user_id, "Run", max(1, round(run["duration_s"] / 60)), None, run["local_date"])
        return item.get("item_id") if item else None
    except Exception as e:
        logger.warning("Calendar entry for run failed: %s", e)
        return None


def _remove_calendar_entry(user_id: int, item_id: int | None) -> None:
    if not item_id:
        return
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT linked_exercise_id FROM training_plan_items WHERE item_id = ? AND user_id = ?",
                           (item_id, user_id)).fetchone()
        if row and row[0]:
            conn.execute("DELETE FROM health_data WHERE hd_id = ? AND user_id = ?", (row[0], user_id))
        conn.execute("DELETE FROM training_plan_items WHERE item_id = ? AND user_id = ?", (item_id, user_id))
        conn.commit()
    finally:
        conn.close()


def _save(user_id: int, draft_id: int) -> Reply:
    draft = store.get_draft(user_id, draft_id)
    if draft is None or not store.delete_draft(user_id, draft_id):
        return Reply("That run card expired or was already handled. Nothing new was saved.")
    p = draft
    units = _units(user_id)
    optional = {k: p[k] for k in ("run_type", "surface", "avg_hr", "max_hr", "perceived_effort", "notes")
                if p.get(k) is not None}
    if p.get("run_id"):
        run = store.get_run(user_id, p["run_id"])
        if run is None:
            return Reply("That run no longer exists.")
        fields = {"distance_m": p["distance_m"], "duration_s": p["duration_s"], **optional}
        if p["date"] != run["local_date"]:
            fields["started_at"] = _started_at(user_id, p["date"], p["duration_s"])
        store.update_run(user_id, run["run_id"], **fields)
        run = store.get_run(user_id, run["run_id"])
        if run.get("plan_item_id"):
            _remove_calendar_entry(user_id, run["plan_item_id"])
            store.update_run(user_id, run["run_id"], plan_item_id=_calendar_entry(user_id, run))
        verb = "Updated"
    else:
        result = store.save_run(
            user_id,
            started_at=_started_at(user_id, p["date"], p["duration_s"]),
            distance_m=p["distance_m"], duration_s=p["duration_s"], source="telegram",
            splits=p.get("splits"), **optional,
        )
        run = store.get_run(user_id, result["run_id"])
        item_id = _calendar_entry(user_id, run)
        if item_id:
            store.update_run(user_id, run["run_id"], plan_item_id=item_id)
        verb = "Saved"
    text = (f"\u2705 {verb}: {format_distance(run['distance_m'], units)} in {format_duration(run['duration_s'])} "
            f"({format_pace(pace_s_per_km(run['distance_m'], run['duration_s']), units)}) "
            f"on {_nice_date(run['local_date'])}.")
    if run.get("possible_duplicate_of"):
        text += "\nThis looks like a run you already have from another source. Both are kept for now."
    return Reply(text)


def handle_callback(user_id: int, data: str, state: dict) -> Reply | None:
    action, _, arg = data.partition(":")
    if action == "run_save":
        return _save(user_id, int(arg))
    if action == "run_cancel":
        store.delete_draft(user_id, int(arg))
        state.pop(_EDIT_KEY, None)
        return Reply("Cancelled. Nothing was saved.")
    if action == "run_edit":
        if store.get_draft(user_id, int(arg)) is None:
            return Reply("That run card expired. Send the run again.")
        state.pop(_FLOW_KEY, None)
        state[_EDIT_KEY] = int(arg)
        return Reply("Send what to change, e.g. '28:10', '5.2 km', 'tempo', 'yesterday' or 'avg hr 150'. "
                     "Say cancel to stop.")
    if action == "run_type":
        flow = state.get(_FLOW_KEY) or {}
        if flow.get("step") != "run_type":
            return Reply("That question expired. Send the run again.")
        draft = dict(flow["draft"])
        if arg in TYPE_CHOICES:
            draft["run_type"] = arg
        state.pop(_FLOW_KEY, None)
        return _new_draft_card(user_id, draft)
    if action == "run_del":
        run = store.get_run(user_id, int(arg))
        if run is None or not store.delete_run(user_id, int(arg)):
            return Reply("That run was already deleted.")
        _remove_calendar_entry(user_id, run.get("plan_item_id"))
        return Reply(f"Deleted the {format_distance(run['distance_m'], _units(user_id))} run "
                     f"from {_nice_date(run['local_date'])}.")
    if action == "run_keep":
        return Reply("Kept it.")
    return None


def list_recent(user_id: int, limit: int = 5) -> Reply:
    runs = store.list_runs(user_id, limit=limit)
    if not runs:
        return Reply("No runs logged yet. Send one like 'ran 5k in 27:30' or use /run.")
    units = _units(user_id)
    lines = [f"\U0001F3C3 Last {len(runs)} run{'s' if len(runs) > 1 else ''}:"]
    for r in runs:
        lines.append(
            f"{_nice_date(r['local_date'])}: {format_distance(r['distance_m'], units)} in "
            f"{format_duration(r['duration_s'])} ({format_pace(pace_s_per_km(r['distance_m'], r['duration_s']), units)})"
            + (f", {r['run_type']}" if r.get("run_type") else ""))
    return Reply("\n".join(lines))


def edit_latest(user_id: int, state: dict) -> Reply:
    run = store.latest_run(user_id)
    if run is None:
        return Reply("No runs to edit yet.")
    payload = {k: run[k] for k in ("distance_m", "duration_s", "run_type", "surface", "avg_hr", "max_hr",
                                   "perceived_effort", "notes") if run.get(k) is not None}
    payload.update(date=run["local_date"], run_id=run["run_id"])
    draft_id = store.create_draft(user_id, payload)
    state.pop(_FLOW_KEY, None)
    state[_EDIT_KEY] = draft_id
    lines = ["Your latest run:", *_summary_lines(run, _units(user_id)), "",
             "Send what to change, e.g. '28:10', '5.2 km', 'tempo' or 'yesterday'. Say cancel to stop."]
    return Reply("\n".join(lines))


def delete_latest(user_id: int) -> Reply:
    run = store.latest_run(user_id)
    if run is None:
        return Reply("No runs to delete.")
    lines = ["Delete this run?", *_summary_lines(run, _units(user_id))]
    return Reply("\n".join(lines), [[("Delete", f"run_del:{run['run_id']}"), ("Keep", "run_keep")]])
