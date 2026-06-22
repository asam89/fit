"""Weekly Training Plan: CRUD, adherence, exercise reconciliation, Telegram formatting."""

import json
import logging
from datetime import datetime, timezone, timedelta, date

from fitnessbot import db

logger = logging.getLogger(__name__)

ACTIVITY_TYPES = ("strength", "run", "cardio", "mobility", "sport", "rest", "other")
ACTIVITY_ICONS = {
    "strength": "\U0001f4aa",
    "run": "\U0001f3c3",
    "cardio": "\u2764\ufe0f",
    "mobility": "\U0001f9d8",
    "sport": "\u26bd",
    "rest": "\U0001f4a4",
    "other": "\u2b50",
}


def _monday_of_week(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def _today() -> date:
    return datetime.now(timezone.utc).date()


def get_or_create_plan(user_id: int, week_start: str) -> dict:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM training_plans WHERE user_id = ? AND week_start = ?",
            (user_id, week_start),
        ).fetchone()
        if row:
            return dict(row)
        cursor = conn.execute(
            "INSERT INTO training_plans (user_id, week_start) VALUES (?, ?)",
            (user_id, week_start),
        )
        conn.commit()
        return {"plan_id": cursor.lastrowid, "user_id": user_id, "week_start": week_start}
    finally:
        conn.close()


def get_plan_items(user_id: int, week_start: str) -> list[dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """SELECT tpi.* FROM training_plan_items tpi
               JOIN training_plans tp ON tpi.plan_id = tp.plan_id
               WHERE tp.user_id = ? AND tp.week_start = ?
               ORDER BY tpi.date, tpi.position""",
            (user_id, week_start),
        ).fetchall()
        items = [dict(r) for r in rows]
        today = _today().isoformat()
        for item in items:
            if item["status"] == "planned" and item["date"] < today:
                item["display_status"] = "missed"
            else:
                item["display_status"] = item["status"]
        return items
    finally:
        conn.close()


def get_items_for_date(user_id: int, date_str: str) -> list[dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM training_plan_items WHERE user_id = ? AND date = ? ORDER BY position",
            (user_id, date_str),
        ).fetchall()
        items = [dict(r) for r in rows]
        today = _today().isoformat()
        for item in items:
            if item["status"] == "planned" and item["date"] < today:
                item["display_status"] = "missed"
            else:
                item["display_status"] = item["status"]
        return items
    finally:
        conn.close()


def add_item(user_id: int, week_start: str, date_str: str, activity_type: str,
             title: str, duration_min: int | None = None, notes: str | None = None) -> dict:
    plan = get_or_create_plan(user_id, week_start)
    d = date.fromisoformat(date_str)
    day_of_week = d.weekday()

    conn = db.get_connection()
    try:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) as m FROM training_plan_items WHERE plan_id = ? AND date = ?",
            (plan["plan_id"], date_str),
        ).fetchone()["m"]

        cursor = conn.execute(
            """INSERT INTO training_plan_items
               (plan_id, user_id, date, day_of_week, activity_type, title, planned_duration_min, notes, position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan["plan_id"], user_id, date_str, day_of_week, activity_type, title, duration_min, notes, max_pos + 1),
        )
        conn.commit()
        return {"item_id": cursor.lastrowid, "title": title, "activity_type": activity_type, "date": date_str}
    finally:
        conn.close()


def update_item(item_id: int, user_id: int, **kwargs) -> bool:
    allowed = {"activity_type", "title", "planned_duration_min", "notes", "position"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [item_id, user_id]
    conn = db.get_connection()
    try:
        conn.execute(f"UPDATE training_plan_items SET {set_clause} WHERE item_id = ? AND user_id = ?", values)
        conn.commit()
        return True
    finally:
        conn.close()


def remove_item(item_id: int, user_id: int) -> bool:
    conn = db.get_connection()
    try:
        r = conn.execute("DELETE FROM training_plan_items WHERE item_id = ? AND user_id = ?", (item_id, user_id))
        conn.commit()
        return r.rowcount > 0
    finally:
        conn.close()


def complete_item(item_id: int, user_id: int, actual_duration_min: int | None = None) -> dict | None:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM training_plan_items WHERE item_id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)

        now = db.utcnow()
        new_status = "planned" if item["status"] == "completed" else "completed"
        completed_at = now if new_status == "completed" else None

        duration = actual_duration_min or item.get("planned_duration_min")
        if actual_duration_min and actual_duration_min != item.get("planned_duration_min"):
            conn.execute(
                "UPDATE training_plan_items SET planned_duration_min = ? WHERE item_id = ?",
                (actual_duration_min, item_id),
            )

        conn.execute(
            "UPDATE training_plan_items SET status = ?, completed_at = ? WHERE item_id = ?",
            (new_status, completed_at, item_id),
        )
        conn.commit()

        # Auto-create exercise entry if completing and no linked exercise
        exercise_id = None
        if new_status == "completed" and item["activity_type"] != "rest":
            exercise_id = _reconcile_exercise(user_id, item, duration, conn)
            if exercise_id:
                conn.execute(
                    "UPDATE training_plan_items SET linked_exercise_id = ? WHERE item_id = ?",
                    (exercise_id, item_id),
                )
                conn.commit()

        item["status"] = new_status
        item["completed_at"] = completed_at
        item["linked_exercise_id"] = exercise_id
        item["display_status"] = new_status
        return item
    finally:
        conn.close()


def _reconcile_exercise(user_id: int, item: dict, duration: int | None, conn) -> int | None:
    """Link to existing exercise or create one. Avoids duplicates."""
    item_date = item["date"]
    activity = item["activity_type"]

    existing = conn.execute(
        """SELECT exercise_id FROM health_data
           WHERE user_id = ? AND data_type = 'workout' AND DATE(recorded_at) = ?
           AND data_json LIKE ?""",
        (user_id, item_date, f'%"{activity}"%'),
    ).fetchone()

    if existing:
        return existing["exercise_id"] if "exercise_id" in existing.keys() else None

    # Create lightweight exercise entry
    data_json = json.dumps({
        "activity": item["title"],
        "type": activity,
        "duration_min": duration,
        "source": "training_plan",
    })
    cursor = conn.execute(
        "INSERT INTO health_data (user_id, data_type, data_json, recorded_at) VALUES (?, 'workout', ?, ?)",
        (user_id, data_json, item_date + "T12:00:00Z"),
    )
    conn.commit()
    return cursor.lastrowid


def compute_adherence(items: list[dict]) -> dict:
    """Compute adherence stats from a week's items."""
    planned_non_rest = [i for i in items if i["activity_type"] != "rest"]
    completed = [i for i in planned_non_rest if i["status"] == "completed"]
    missed = [i for i in planned_non_rest if i.get("display_status") == "missed"]
    rest_days = [i for i in items if i["activity_type"] == "rest"]

    total = len(planned_non_rest)
    done = len(completed)
    pct = int(done / total * 100) if total else 0

    return {
        "total": total,
        "completed": done,
        "missed": len(missed),
        "rest_days": len(rest_days),
        "pct": pct,
        "label": f"{done}/{total} done",
    }


def copy_last_week(user_id: int, target_week_start: str) -> int:
    """Copy items from previous week into target week. Returns count of items copied."""
    target_date = date.fromisoformat(target_week_start)
    prev_week_start = (target_date - timedelta(days=7)).isoformat()

    prev_items = get_plan_items(user_id, prev_week_start)
    if not prev_items:
        return 0

    plan = get_or_create_plan(user_id, target_week_start)
    conn = db.get_connection()
    try:
        count = 0
        for item in prev_items:
            old_date = date.fromisoformat(item["date"])
            new_date = old_date + timedelta(days=7)
            conn.execute(
                """INSERT INTO training_plan_items
                   (plan_id, user_id, date, day_of_week, activity_type, title, planned_duration_min, notes, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (plan["plan_id"], user_id, new_date.isoformat(), new_date.weekday(),
                 item["activity_type"], item["title"], item.get("planned_duration_min"),
                 item.get("notes"), item.get("position", 0)),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def get_current_week_start(user_id: int) -> str:
    return _monday_of_week(_today())


def format_plan_telegram(user_id: int, week_start: str | None = None) -> str:
    """Format the week's plan for Telegram display."""
    if not week_start:
        week_start = get_current_week_start(user_id)

    items = get_plan_items(user_id, week_start)
    adherence = compute_adherence(items)

    ws = date.fromisoformat(week_start)
    we = ws + timedelta(days=6)
    header = f"\U0001f4cb Week of {ws.strftime('%b %d')}–{we.strftime('%d')} ({adherence['label']})\n"

    if not items:
        return header + "\nNo activities planned. Tell me your week — e.g. \"legs Monday, run Tuesday, rest Wednesday\""

    days = {}
    for item in items:
        d = item["date"]
        if d not in days:
            days[d] = []
        days[d].append(item)

    lines = [header]
    for i in range(7):
        day_date = (ws + timedelta(days=i)).isoformat()
        day_name = (ws + timedelta(days=i)).strftime("%a")
        day_items = days.get(day_date, [])

        if not day_items:
            continue

        day_parts = []
        for item in day_items:
            icon = ACTIVITY_ICONS.get(item["activity_type"], "\u2b50")
            status_mark = ""
            if item.get("display_status") == "completed":
                status_mark = " \u2713"
            elif item.get("display_status") == "missed":
                status_mark = " \u2717"
            dur = f" {item['planned_duration_min']}min" if item.get("planned_duration_min") else ""
            day_parts.append(f"{icon} {item['title']}{dur}{status_mark}")

        lines.append(f"**{day_name}:** {' · '.join(day_parts)}")

    return "\n".join(lines)


def format_today_plan(user_id: int) -> str | None:
    """Format today's planned activities for briefings."""
    today_str = _today().isoformat()
    items = get_items_for_date(user_id, today_str)
    if not items:
        return None

    parts = []
    for item in items:
        icon = ACTIVITY_ICONS.get(item["activity_type"], "\u2b50")
        dur = f" ({item['planned_duration_min']}min)" if item.get("planned_duration_min") else ""
        status = ""
        if item["status"] == "completed":
            status = " \u2014 done"
        parts.append(f"{icon} {item['title']}{dur}{status}")

    return "Today's plan: " + ", ".join(parts)


def format_day_adherence(user_id: int, date_str: str) -> str | None:
    """Format a day's adherence for evening wrap."""
    items = get_items_for_date(user_id, date_str)
    if not items:
        return None

    parts = []
    for item in items:
        if item["activity_type"] == "rest":
            parts.append(f"Rest day \U0001f4a4")
        elif item["status"] == "completed":
            parts.append(f"{item['title']} \u2014 \u2713 done")
        else:
            parts.append(f"{item['title']} \u2014 not logged")

    return "Planned: " + " | ".join(parts)


def set_plan_from_text(user_id: int, activities: list[dict]) -> dict:
    """Set plan from parsed NLU output.

    activities: [{"day": "monday", "title": "Legs", "type": "strength", "duration": 45}, ...]
    """
    week_start = get_current_week_start(user_id)
    ws = date.fromisoformat(week_start)
    day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
               "friday": 4, "saturday": 5, "sunday": 6}

    added = 0
    for act in activities:
        day_name = act.get("day", "").lower()
        day_offset = day_map.get(day_name)
        if day_offset is None:
            continue
        target_date = (ws + timedelta(days=day_offset)).isoformat()
        activity_type = act.get("type", "other")
        if activity_type not in ACTIVITY_TYPES:
            activity_type = "other"
        title = act.get("title", activity_type.capitalize())
        duration = act.get("duration")

        add_item(user_id, week_start, target_date, activity_type, title, duration)
        added += 1

    return {"added": added, "week_start": week_start}


def complete_by_title(user_id: int, title_hint: str, actual_duration: int | None = None) -> dict | None:
    """Complete a planned item matching the title hint for today."""
    today_str = _today().isoformat()
    items = get_items_for_date(user_id, today_str)

    hint_lower = title_hint.lower()
    match = None
    for item in items:
        if item["status"] != "completed" and (
            hint_lower in item["title"].lower() or
            hint_lower in item["activity_type"].lower()
        ):
            match = item
            break

    if not match:
        # Try this week
        week_start = get_current_week_start(user_id)
        all_items = get_plan_items(user_id, week_start)
        for item in all_items:
            if item["status"] != "completed" and (
                hint_lower in item["title"].lower() or
                hint_lower in item["activity_type"].lower()
            ):
                match = item
                break

    if match:
        return complete_item(match["item_id"], user_id, actual_duration)
    return None
