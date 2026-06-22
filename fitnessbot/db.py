"""Database schema, migrations, and DAL helpers."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fitnessbot.config import Config

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- users
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/Toronto',
    sex TEXT,
    height REAL,
    birthdate TEXT,
    units_pref TEXT NOT NULL DEFAULT 'imperial',
    activity_level TEXT,
    dietary_restrictions TEXT,  -- JSON
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- sessions (web login)
CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT NOT NULL
);

-- telegram_connections
CREATE TABLE IF NOT EXISTS telegram_connections (
    conn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    bot_token_encrypted TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    bot_username TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    validated_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- health_data (flexible JSON for blood work, body comp, medical notes)
CREATE TABLE IF NOT EXISTS health_data (
    hd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    data_type TEXT NOT NULL,  -- blood_work, body_comp, fitness_baseline, medical
    data_json TEXT NOT NULL,  -- JSON
    notes TEXT,
    recorded_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- goals
CREATE TABLE IF NOT EXISTS goals (
    goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    goal_type TEXT NOT NULL,  -- cut, bulk, maintain, event
    title TEXT,
    description TEXT,
    target_weight REAL,
    target_body_fat REAL,
    event_date TEXT,
    event_name TEXT,
    target_calories INTEGER,
    target_protein INTEGER,
    target_carbs INTEGER,
    target_fat INTEGER,
    start_date TEXT NOT NULL,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active, completed, paused
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- diet_plans (AI-generated, versioned)
CREATE TABLE IF NOT EXISTS diet_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    goal_id INTEGER REFERENCES goals(goal_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    active INTEGER NOT NULL DEFAULT 1,
    daily_calories INTEGER,
    daily_protein INTEGER,
    daily_carbs INTEGER,
    daily_fat INTEGER,
    rationale_text TEXT,
    meal_timing_json TEXT,
    foods_to_emphasize TEXT,
    foods_to_avoid TEXT,
    superseded_by INTEGER REFERENCES diet_plans(plan_id),
    expires_at TEXT
);

-- training_plans (AI-generated phased programs)
CREATE TABLE IF NOT EXISTS training_plans (
    tp_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    goal_id INTEGER REFERENCES goals(goal_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    active INTEGER NOT NULL DEFAULT 1,
    phase_name TEXT,
    phase_description TEXT,
    start_date TEXT,
    end_date TEXT,
    workouts_json TEXT,  -- JSON array of daily workouts
    superseded_by INTEGER REFERENCES training_plans(tp_id)
);

-- daily_workouts (individual workout prescriptions)
CREATE TABLE IF NOT EXISTS daily_workouts (
    dw_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tp_id INTEGER REFERENCES training_plans(tp_id),
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    scheduled_date TEXT NOT NULL,
    workout_type TEXT,  -- daily_small, gym, rest, sport_specific
    description TEXT,
    exercises_json TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    user_notes TEXT
);

-- foods (cache of AI-inferred or looked-up foods)
CREATE TABLE IF NOT EXISTS foods (
    food_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    brand TEXT,
    serving_qty REAL,
    serving_unit TEXT,
    calories REAL,
    protein REAL,
    carbs REAL,
    fat REAL,
    fiber REAL,
    sugar REAL,
    sodium REAL,
    source TEXT NOT NULL DEFAULT 'claude',  -- claude, nutritionix, manual
    claude_confidence REAL,
    cached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- meals
CREATE TABLE IF NOT EXISTS meals (
    meal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    logged_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    meal_type TEXT,  -- breakfast, lunch, dinner, snack
    raw_text TEXT,
    source TEXT NOT NULL DEFAULT 'text',  -- voice, text, photo
    total_calories REAL,
    total_protein REAL,
    total_carbs REAL,
    total_fat REAL
);

-- meal_items (junction: a meal has many foods)
CREATE TABLE IF NOT EXISTS meal_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL REFERENCES meals(meal_id) ON DELETE CASCADE,
    food_id INTEGER REFERENCES foods(food_id),
    qty REAL,
    unit TEXT,
    calories REAL,
    protein REAL,
    carbs REAL,
    fat REAL
);

-- body_composition
CREATE TABLE IF NOT EXISTS body_composition (
    bc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    measured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    weight REAL,
    weight_unit TEXT NOT NULL DEFAULT 'lbs',
    body_fat_pct REAL,
    lean_mass REAL,
    source TEXT NOT NULL DEFAULT 'manual'
);

-- weight_trend (materialized smoothing output)
CREATE TABLE IF NOT EXISTS weight_trend (
    wt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    raw_weight REAL NOT NULL,
    smoothed_weight REAL,
    trend_7d REAL,
    trend_30d REAL,
    UNIQUE(user_id, date)
);

-- sleep
CREATE TABLE IF NOT EXISTS sleep (
    sleep_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    duration_min REAL,
    deep_min REAL,
    rem_min REAL,
    light_min REAL,
    awake_min REAL,
    efficiency REAL,
    hrv_overnight REAL,
    source TEXT NOT NULL DEFAULT 'manual'
);

-- vitals
CREATE TABLE IF NOT EXISTS vitals (
    vital_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    measured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resting_hr REAL,
    hrv REAL,
    spo2 REAL,
    body_temp REAL,
    systolic REAL,
    diastolic REAL,
    source TEXT NOT NULL DEFAULT 'manual'
);

-- exercise
CREATE TABLE IF NOT EXISTS exercise (
    ex_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    activity_type TEXT,
    duration_min REAL,
    calories_burned REAL,
    avg_hr REAL,
    max_hr REAL,
    distance REAL,
    source TEXT NOT NULL DEFAULT 'manual',
    notes TEXT
);

-- daily_summary (precomputed rollup with target vs actual)
CREATE TABLE IF NOT EXISTS daily_summary (
    ds_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    total_calories REAL,
    target_calories REAL,
    protein REAL,
    target_protein REAL,
    carbs REAL,
    target_carbs REAL,
    fat REAL,
    target_fat REAL,
    fiber REAL,
    weight_smoothed REAL,
    sleep_min REAL,
    resting_hr REAL,
    steps REAL,
    est_tdee REAL,
    surplus_deficit REAL,
    UNIQUE(user_id, date)
);

-- data_requests (proactive questions)
CREATE TABLE IF NOT EXISTS data_requests (
    req_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    asked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    category TEXT,
    question_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, answered, snoozed, expired
    answered_at TEXT,
    answer_value TEXT
);

-- llm_analysis (Claude call audit log)
CREATE TABLE IF NOT EXISTS llm_analysis (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(user_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    kind TEXT NOT NULL,  -- router, food_parse, diet_plan, training_plan, weekly, anomaly, report
    model TEXT,
    input_digest TEXT,
    output_text TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms REAL
);

-- device_sync_log (raw payload audit)
CREATE TABLE IF NOT EXISTS device_sync_log (
    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(user_id),
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source TEXT,
    raw_payload TEXT,  -- JSON
    parsed_ok INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

-- plan_history (audit trail of plan revisions)
CREATE TABLE IF NOT EXISTS plan_history (
    ph_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    plan_type TEXT NOT NULL,  -- diet, training
    old_plan_id INTEGER,
    new_plan_id INTEGER,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    reason TEXT
);

-- schema_version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def get_db_path() -> str:
    return Config.DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        # Record schema version if not already set
        row = conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()
        if row["v"] is None:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
    finally:
        conn.close()


def run_migrations() -> None:
    """Run any pending migrations based on current schema version."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()
        current = row["v"] if row and row["v"] else 0
        # Future migrations go here:
        # if current < 2:
        #     conn.execute("ALTER TABLE ...")
        #     conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        #     conn.commit()
    except sqlite3.OperationalError:
        # schema_version table doesn't exist yet; init_db will create it
        init_db()
    finally:
        conn.close()


# --- DAL helpers ---

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_user(
    email: str,
    password_hash: str,
    display_name: str,
    timezone_str: str = "America/Toronto",
    units_pref: str = "imperial",
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO users (email, password_hash, display_name, timezone, units_pref)
               VALUES (?, ?, ?, ?, ?)""",
            (email, password_hash, display_name, timezone_str, units_pref),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_user(user_id: int, **kwargs) -> None:
    if not kwargs:
        return
    allowed = {
        "display_name", "timezone", "sex", "height", "birthdate",
        "units_pref", "activity_level", "dietary_restrictions",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = utcnow()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE user_id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def insert_telegram_connection(
    user_id: int,
    bot_token_encrypted: str,
    chat_id: str,
    bot_username: str | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO telegram_connections
               (user_id, bot_token_encrypted, chat_id, bot_username, validated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, bot_token_encrypted, chat_id, bot_username, utcnow()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_telegram_connection(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM telegram_connections WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_telegram_connection(user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM telegram_connections WHERE user_id = ?", (user_id,)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_active_connections() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM telegram_connections WHERE is_active = 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_meal(
    user_id: int,
    raw_text: str,
    meal_type: str,
    source: str,
    total_calories: float,
    total_protein: float,
    total_carbs: float,
    total_fat: float,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO meals
               (user_id, raw_text, meal_type, source, total_calories, total_protein, total_carbs, total_fat)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, raw_text, meal_type, source, total_calories, total_protein, total_carbs, total_fat),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_food(
    name: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    fiber: float = 0,
    sugar: float = 0,
    sodium: float = 0,
    serving_qty: float | None = None,
    serving_unit: str | None = None,
    source: str = "claude",
    claude_confidence: float | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO foods
               (name, calories, protein, carbs, fat, fiber, sugar, sodium,
                serving_qty, serving_unit, source, claude_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, calories, protein, carbs, fat, fiber, sugar, sodium,
             serving_qty, serving_unit, source, claude_confidence),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_meal_item(
    meal_id: int,
    food_id: int,
    qty: float,
    unit: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO meal_items
               (meal_id, food_id, qty, unit, calories, protein, carbs, fat)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (meal_id, food_id, qty, unit, calories, protein, carbs, fat),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_today_totals(user_id: int, date_str: str) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(total_calories), 0) as calories,
                 COALESCE(SUM(total_protein), 0) as protein,
                 COALESCE(SUM(total_carbs), 0) as carbs,
                 COALESCE(SUM(total_fat), 0) as fat
               FROM meals
               WHERE user_id = ? AND DATE(logged_at) = ?""",
            (user_id, date_str),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_recent_meals(user_id: int, limit: int = 10) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM meals
               WHERE user_id = ?
               ORDER BY logged_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_meal_items(meal_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT mi.*, f.name as food_name
               FROM meal_items mi
               LEFT JOIN foods f ON mi.food_id = f.food_id
               WHERE mi.meal_id = ?""",
            (meal_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_last_meal(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT meal_id, raw_text FROM meals
               WHERE user_id = ?
               ORDER BY logged_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if row:
            meal = dict(row)
            conn.execute(
                "DELETE FROM meal_items WHERE meal_id = ?", (meal["meal_id"],)
            )
            conn.execute(
                "DELETE FROM meals WHERE meal_id = ?", (meal["meal_id"],)
            )
            conn.commit()
            return meal
        return None
    finally:
        conn.close()


def insert_body_composition(
    user_id: int,
    weight: float,
    weight_unit: str = "lbs",
    body_fat_pct: float | None = None,
    lean_mass: float | None = None,
    source: str = "manual",
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO body_composition
               (user_id, weight, weight_unit, body_fat_pct, lean_mass, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, weight, weight_unit, body_fat_pct, lean_mass, source),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_weight_history(user_id: int, limit: int = 90) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM body_composition
               WHERE user_id = ?
               ORDER BY measured_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_weight_trend(user_id: int, limit: int = 90) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM weight_trend
               WHERE user_id = ?
               ORDER BY date DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_weight_trend(
    user_id: int,
    date_str: str,
    raw_weight: float,
    smoothed_weight: float,
    trend_7d: float | None = None,
    trend_30d: float | None = None,
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO weight_trend (user_id, date, raw_weight, smoothed_weight, trend_7d, trend_30d)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET
                 raw_weight = excluded.raw_weight,
                 smoothed_weight = excluded.smoothed_weight,
                 trend_7d = excluded.trend_7d,
                 trend_30d = excluded.trend_30d""",
            (user_id, date_str, raw_weight, smoothed_weight, trend_7d, trend_30d),
        )
        conn.commit()
    finally:
        conn.close()


def get_active_diet_plan(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM diet_plans WHERE user_id = ? AND active = 1 ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_active_goals(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM goals WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_health_data(
    user_id: int,
    data_type: str,
    data_json: str,
    notes: str | None = None,
    recorded_at: str | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO health_data (user_id, data_type, data_json, notes, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, data_type, data_json, notes, recorded_at or utcnow()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_health_data(user_id: int, data_type: str | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if data_type:
            rows = conn.execute(
                "SELECT * FROM health_data WHERE user_id = ? AND data_type = ? ORDER BY recorded_at DESC",
                (user_id, data_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM health_data WHERE user_id = ? ORDER BY recorded_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_llm_analysis(
    kind: str,
    model: str,
    input_digest: str,
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    user_id: int | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO llm_analysis
               (user_id, kind, model, input_digest, output_text, input_tokens, output_tokens, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, kind, model, input_digest, output_text, input_tokens, output_tokens, latency_ms),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()
