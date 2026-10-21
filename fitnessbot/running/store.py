"""Run persistence. Raw metres and seconds only; display units are derived elsewhere."""

import json
from datetime import datetime, timedelta, timezone

from fitnessbot import db
from fitnessbot.tz import _tz, _user_tz_str

RUN_TYPES = ("easy", "tempo", "interval", "long", "race", "treadmill")
SURFACES = ("road", "trail", "track", "treadmill")
SOURCES = ("telegram", "web", "gpx", "fit", "strava", "apple_health")
CALORIE_SOURCES = ("device", "strava", "estimated")

DUPLICATE_START_WINDOW_S = 120
DUPLICATE_DISTANCE_TOLERANCE = 0.03
DRAFT_TTL = timedelta(hours=24)

_OPTIONAL_FIELDS = (
    "moving_time_s", "elevation_gain_m", "avg_hr", "max_hr", "avg_cadence",
    "run_type", "surface", "calories", "calories_source", "perceived_effort",
    "notes", "plan_item_id",
)
_UPDATABLE_FIELDS = ("started_at", "distance_m", "duration_s") + _OPTIONAL_FIELDS


class RunValidationError(ValueError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise RunValidationError("started_at must be timezone-aware")
    return dt.astimezone(timezone.utc)


def local_date_for(started_at: str | datetime, user_id: int) -> str:
    """User-local calendar date of a UTC start time, using the user's stored timezone."""
    return _parse_utc(started_at).astimezone(_tz(_user_tz_str(user_id))).strftime("%Y-%m-%d")


def _validate(fields: dict) -> None:
    if "distance_m" in fields and not (fields["distance_m"] and float(fields["distance_m"]) > 0):
        raise RunValidationError("distance_m must be positive")
    if "duration_s" in fields and not (fields["duration_s"] and int(fields["duration_s"]) > 0):
        raise RunValidationError("duration_s must be positive")
    for key, allowed in (("run_type", RUN_TYPES), ("surface", SURFACES),
                         ("calories_source", CALORIE_SOURCES)):
        if fields.get(key) is not None and fields[key] not in allowed:
            raise RunValidationError(f"{key} must be one of {allowed}")
    pe = fields.get("perceived_effort")
    if pe is not None and not 1 <= int(pe) <= 10:
        raise RunValidationError("perceived_effort must be 1-10")


def _row(r) -> dict:
    return dict(r) if r is not None else None


def _find_possible_duplicate(conn, user_id: int, started: datetime, distance_m: float) -> int | None:
    lo = _iso(started - timedelta(seconds=DUPLICATE_START_WINDOW_S))
    hi = _iso(started + timedelta(seconds=DUPLICATE_START_WINDOW_S))
    rows = conn.execute(
        "SELECT run_id, distance_m FROM runs WHERE user_id = ? AND started_at BETWEEN ? AND ? "
        "ORDER BY run_id",
        (user_id, lo, hi),
    ).fetchall()
    for r in rows:
        if abs(r["distance_m"] - distance_m) <= DUPLICATE_DISTANCE_TOLERANCE * max(r["distance_m"], distance_m):
            return r["run_id"]
    return None


def save_run(
    user_id: int,
    *,
    started_at: str | datetime,
    distance_m: float,
    duration_s: int,
    source: str,
    external_id: str | None = None,
    splits: list[dict] | None = None,
    **optional,
) -> dict:
    """Insert a run (and splits). Idempotent on (source, external_id).

    Returns {"run_id", "created", "possible_duplicate_of"}. A run that looks like
    one from another source is still saved but flagged, never silently dropped.
    """
    if source not in SOURCES:
        raise RunValidationError(f"source must be one of {SOURCES}")
    unknown = set(optional) - set(_OPTIONAL_FIELDS)
    if unknown:
        raise RunValidationError(f"unknown run fields: {sorted(unknown)}")
    fields = {"distance_m": distance_m, "duration_s": duration_s, **optional}
    _validate(fields)
    started = _parse_utc(started_at)
    ext = str(external_id) if external_id not in (None, "") else None

    conn = db.get_connection()
    try:
        if ext is not None:
            existing = conn.execute(
                "SELECT run_id, possible_duplicate_of FROM runs "
                "WHERE user_id = ? AND source = ? AND external_id = ?",
                (user_id, source, ext),
            ).fetchone()
            if existing:
                return {"run_id": existing["run_id"], "created": False,
                        "possible_duplicate_of": existing["possible_duplicate_of"]}

        dup_of = _find_possible_duplicate(conn, user_id, started, float(distance_m))
        cols = ["user_id", "started_at", "local_date", "distance_m", "duration_s",
                "source", "external_id", "possible_duplicate_of"]
        vals = [user_id, _iso(started), local_date_for(started, user_id), float(distance_m),
                int(duration_s), source, ext, dup_of]
        for k in _OPTIONAL_FIELDS:
            if optional.get(k) is not None:
                cols.append(k)
                vals.append(optional[k])
        cur = conn.execute(
            f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
        )
        run_id = cur.lastrowid
        _insert_splits(conn, run_id, splits or [])
        conn.commit()
        return {"run_id": run_id, "created": True, "possible_duplicate_of": dup_of}
    finally:
        conn.close()


def _insert_splits(conn, run_id: int, splits: list[dict]) -> None:
    for i, sp in enumerate(splits):
        if not (sp.get("distance_m") and sp["distance_m"] > 0 and sp.get("duration_s") and sp["duration_s"] > 0):
            raise RunValidationError(f"split {i} needs positive distance_m and duration_s")
        conn.execute(
            "INSERT INTO run_splits (run_id, split_index, distance_m, duration_s, avg_hr, elevation_change_m) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, sp.get("split_index", i), float(sp["distance_m"]), float(sp["duration_s"]),
             sp.get("avg_hr"), sp.get("elevation_change_m")),
        )


def get_run(user_id: int, run_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        run = _row(conn.execute(
            "SELECT * FROM runs WHERE run_id = ? AND user_id = ?", (run_id, user_id)
        ).fetchone())
        if run:
            run["splits"] = [dict(r) for r in conn.execute(
                "SELECT * FROM run_splits WHERE run_id = ? ORDER BY split_index", (run_id,)
            ).fetchall()]
        return run
    finally:
        conn.close()


def list_runs(user_id: int, *, start_date: str | None = None, end_date: str | None = None,
              run_type: str | None = None, limit: int | None = None) -> list[dict]:
    """Runs newest first, filtered by user-local date range (inclusive)."""
    sql = "SELECT * FROM runs WHERE user_id = ?"
    args: list = [user_id]
    if start_date:
        sql += " AND local_date >= ?"
        args.append(start_date)
    if end_date:
        sql += " AND local_date <= ?"
        args.append(end_date)
    if run_type:
        sql += " AND run_type = ?"
        args.append(run_type)
    sql += " ORDER BY started_at DESC, run_id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    conn = db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def latest_run(user_id: int) -> dict | None:
    runs = list_runs(user_id, limit=1)
    return get_run(user_id, runs[0]["run_id"]) if runs else None


def update_run(user_id: int, run_id: int, **fields) -> bool:
    unknown = set(fields) - set(_UPDATABLE_FIELDS)
    if unknown:
        raise RunValidationError(f"cannot update fields: {sorted(unknown)}")
    if not fields:
        return False
    _validate(fields)
    if "started_at" in fields:
        started = _parse_utc(fields["started_at"])
        fields["started_at"] = _iso(started)
        fields["local_date"] = local_date_for(started, user_id)
    fields["updated_at"] = db.utcnow()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            f"UPDATE runs SET {set_clause} WHERE run_id = ? AND user_id = ?",
            list(fields.values()) + [run_id, user_id],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_run(user_id: int, run_id: int) -> bool:
    conn = db.get_connection()
    try:
        cur = conn.execute("DELETE FROM runs WHERE run_id = ? AND user_id = ?", (run_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Drafts: a parsed run awaiting the user's explicit Save ---

def create_draft(user_id: int, payload: dict, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO run_drafts (user_id, payload_json, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, json.dumps(payload), _iso(now), _iso(now + DRAFT_TTL)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_draft(user_id: int, draft_id: int, *, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT payload_json FROM run_drafts WHERE draft_id = ? AND user_id = ? AND expires_at > ?",
            (draft_id, user_id, _iso(now)),
        ).fetchone()
        return json.loads(r["payload_json"]) if r else None
    finally:
        conn.close()


def delete_draft(user_id: int, draft_id: int) -> bool:
    conn = db.get_connection()
    try:
        cur = conn.execute("DELETE FROM run_drafts WHERE draft_id = ? AND user_id = ?", (draft_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def purge_expired_drafts(*, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    conn = db.get_connection()
    try:
        cur = conn.execute("DELETE FROM run_drafts WHERE expires_at <= ?", (_iso(now),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
