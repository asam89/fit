"""Database schema, migrations, and DAL helpers."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fitnessbot.config import Config

SCHEMA_VERSION = 7

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
    is_superadmin INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT,
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
    status TEXT NOT NULL DEFAULT 'active',  -- active, completed, paused, achieved, missed
    raw_input TEXT,  -- original user input
    refined_statement TEXT,  -- Claude-refined goal statement
    refined_why TEXT,
    refined_metric TEXT,
    refined_target_date TEXT,
    steps_json TEXT,  -- JSON array: [{"id": "...", "text": "...", "done": false}]
    debrief_notes TEXT,
    debrief_json TEXT,  -- JSON: {"reasons": [], "improvements": [], "nextMove": ""}
    closed_at TEXT,
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

-- weekly_summary (materialized rollup)
CREATE TABLE IF NOT EXISTS weekly_summary (
    ws_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    week_start TEXT NOT NULL,
    avg_calories REAL,
    avg_protein REAL,
    avg_carbs REAL,
    avg_fat REAL,
    weight_start REAL,
    weight_end REAL,
    weight_change REAL,
    avg_sleep_min REAL,
    avg_resting_hr REAL,
    total_steps REAL,
    workouts INTEGER,
    logging_days INTEGER,
    days_on_target INTEGER,
    UNIQUE(user_id, week_start)
);

-- monthly_summary (materialized rollup)
CREATE TABLE IF NOT EXISTS monthly_summary (
    ms_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    avg_calories REAL,
    avg_protein REAL,
    avg_carbs REAL,
    avg_fat REAL,
    weight_start REAL,
    weight_end REAL,
    weight_change REAL,
    avg_sleep_min REAL,
    avg_resting_hr REAL,
    total_steps REAL,
    workouts INTEGER,
    logging_days INTEGER,
    days_on_target INTEGER,
    UNIQUE(user_id, month)
);

-- briefing_log (audit trail for scheduled briefings)
CREATE TABLE IF NOT EXISTS briefing_log (
    bl_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    briefing_type TEXT NOT NULL,
    content_summary TEXT,
    had_nudge INTEGER NOT NULL DEFAULT 0
);

-- llm_credentials (per-user provider keys)
CREATE TABLE IF NOT EXISTS llm_credentials (
    cred_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    encrypted_key TEXT NOT NULL,
    key_hint TEXT,
    model TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    validated_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, provider)
);

-- intake_sessions (AI-guided intake audit)
CREATE TABLE IF NOT EXISTS intake_sessions (
    is_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    completed_at TEXT,
    questions_asked INTEGER NOT NULL DEFAULT 0,
    answers_captured INTEGER NOT NULL DEFAULT 0
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
        if current < 2:
            # Add is_superadmin and last_active_at columns
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_superadmin INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT")
            except sqlite3.OperationalError:
                pass
            # Set superadmin for configured email
            from fitnessbot.config import Config
            if Config.SUPER_ADMIN_EMAIL:
                conn.execute(
                    "UPDATE users SET is_superadmin = 1 WHERE email = ?",
                    (Config.SUPER_ADMIN_EMAIL,),
                )
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            conn.commit()
        if current < 3:
            goal_cols = [
                ("raw_input", "TEXT"),
                ("refined_statement", "TEXT"),
                ("refined_why", "TEXT"),
                ("refined_metric", "TEXT"),
                ("refined_target_date", "TEXT"),
                ("steps_json", "TEXT"),
                ("debrief_notes", "TEXT"),
                ("debrief_json", "TEXT"),
                ("closed_at", "TEXT"),
            ]
            for col_name, col_type in goal_cols:
                try:
                    conn.execute(f"ALTER TABLE goals ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (3)")
            conn.commit()
        if current < 4:
            for sql in [
                """CREATE TABLE IF NOT EXISTS weekly_summary (
                    ws_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    week_start TEXT NOT NULL, avg_calories REAL, avg_protein REAL, avg_carbs REAL, avg_fat REAL,
                    weight_start REAL, weight_end REAL, weight_change REAL, avg_sleep_min REAL, avg_resting_hr REAL,
                    total_steps REAL, workouts INTEGER, logging_days INTEGER, days_on_target INTEGER,
                    UNIQUE(user_id, week_start))""",
                """CREATE TABLE IF NOT EXISTS monthly_summary (
                    ms_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    month TEXT NOT NULL, avg_calories REAL, avg_protein REAL, avg_carbs REAL, avg_fat REAL,
                    weight_start REAL, weight_end REAL, weight_change REAL, avg_sleep_min REAL, avg_resting_hr REAL,
                    total_steps REAL, workouts INTEGER, logging_days INTEGER, days_on_target INTEGER,
                    UNIQUE(user_id, month))""",
                """CREATE TABLE IF NOT EXISTS briefing_log (
                    bl_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    briefing_type TEXT NOT NULL, content_summary TEXT, had_nudge INTEGER NOT NULL DEFAULT 0)""",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (4)")
            conn.commit()
        if current < 5:
            for sql in [
                """CREATE TABLE IF NOT EXISTS llm_credentials (
                    cred_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, encrypted_key TEXT NOT NULL, key_hint TEXT,
                    model TEXT, is_active INTEGER NOT NULL DEFAULT 1, validated_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    UNIQUE(user_id, provider))""",
                """CREATE TABLE IF NOT EXISTS intake_sessions (
                    is_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    completed_at TEXT, questions_asked INTEGER NOT NULL DEFAULT 0,
                    answers_captured INTEGER NOT NULL DEFAULT 0)""",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            for col_name, col_type in [("active_provider", "TEXT"), ("active_model", "TEXT")]:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (5)")
            conn.commit()
            current = 5

        if current < 6:
            for sql in [
                """CREATE TABLE IF NOT EXISTS message_log (
                    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    channel TEXT NOT NULL DEFAULT 'text',
                    transcript TEXT,
                    detected_intents TEXT,
                    writes TEXT,
                    response_text TEXT,
                    model TEXT,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0)""",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (6)")
            conn.commit()
            current = 6

        if current < 7:
            for sql in [
                """CREATE TABLE IF NOT EXISTS nutrition_targets (
                    nt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    tdee_estimate INTEGER,
                    method TEXT NOT NULL DEFAULT 'default',
                    goal_type TEXT NOT NULL DEFAULT 'maintain',
                    calorie_target INTEGER NOT NULL,
                    protein_target INTEGER NOT NULL,
                    carbs_target INTEGER NOT NULL,
                    fat_target INTEGER NOT NULL,
                    fiber_target INTEGER DEFAULT 30,
                    eating_focus TEXT,
                    computation_inputs TEXT,
                    UNIQUE(user_id))""",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (7)")
            conn.commit()
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
        "active_provider", "active_model",
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


# --- Admin helpers ---

def touch_last_active(user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET last_active_at = ? WHERE user_id = ?",
            (utcnow(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_superadmin(email: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET is_superadmin = 1 WHERE email = ?", (email,)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_users() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT u.user_id, u.email, u.display_name, u.is_superadmin,
                      u.last_active_at, u.created_at,
                      tc.bot_username, tc.chat_id, tc.is_active as conn_active,
                      tc.validated_at
               FROM users u
               LEFT JOIN telegram_connections tc ON u.user_id = tc.user_id
               ORDER BY u.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_activity_stats(user_id: int) -> dict:
    conn = get_connection()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        meals_today = conn.execute(
            "SELECT COUNT(*) as c FROM meals WHERE user_id = ? AND DATE(logged_at) = ?",
            (user_id, today),
        ).fetchone()["c"]
        meals_total = conn.execute(
            "SELECT COUNT(*) as c FROM meals WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        weights_total = conn.execute(
            "SELECT COUNT(*) as c FROM body_composition WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        health_records = conn.execute(
            "SELECT COUNT(*) as c FROM health_data WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        llm_calls = conn.execute(
            "SELECT COUNT(*) as c FROM llm_analysis WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        last_meal = conn.execute(
            "SELECT logged_at FROM meals WHERE user_id = ? ORDER BY logged_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return {
            "meals_today": meals_today,
            "meals_total": meals_total,
            "weights_total": weights_total,
            "health_records": health_records,
            "llm_calls": llm_calls,
            "last_meal_at": last_meal["logged_at"] if last_meal else None,
        }
    finally:
        conn.close()


# --- Goals DAL helpers ---

def insert_goal_with_plan(
    user_id: int,
    raw_input: str,
    statement: str,
    why: str,
    metric: str,
    target_date: str,
    steps: list[dict],
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO goals
               (user_id, goal_type, title, start_date, status,
                raw_input, refined_statement, refined_why, refined_metric,
                refined_target_date, steps_json)
               VALUES (?, 'event', ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
            (
                user_id, statement, utcnow(),
                raw_input, statement, why, metric, target_date,
                json.dumps(steps),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_active_goal(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM goals WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        g = dict(row)
        g["steps"] = json.loads(g["steps_json"]) if g.get("steps_json") else []
        g["debrief"] = json.loads(g["debrief_json"]) if g.get("debrief_json") else None
        return g
    finally:
        conn.close()


def get_archived_goals(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM goals WHERE user_id = ? AND status IN ('achieved', 'missed') ORDER BY closed_at DESC",
            (user_id,),
        ).fetchall()
        results = []
        for row in rows:
            g = dict(row)
            g["steps"] = json.loads(g["steps_json"]) if g.get("steps_json") else []
            g["debrief"] = json.loads(g["debrief_json"]) if g.get("debrief_json") else None
            results.append(g)
        return results
    finally:
        conn.close()


def update_goal_steps(goal_id: int, steps: list[dict]) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE goals SET steps_json = ?, updated_at = ? WHERE goal_id = ?",
            (json.dumps(steps), utcnow(), goal_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_goal_status(goal_id: int, status: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE goals SET status = ?, closed_at = ?, updated_at = ? WHERE goal_id = ?",
            (status, utcnow(), utcnow(), goal_id),
        )
        conn.commit()
    finally:
        conn.close()


def start_goal_debrief(goal_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE goals SET debrief_notes = '', updated_at = ? WHERE goal_id = ?",
            (utcnow(), goal_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_goal_debrief(goal_id: int, notes: str, debrief_result: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE goals SET debrief_notes = ?, debrief_json = ?, updated_at = ? WHERE goal_id = ?",
            (notes, json.dumps(debrief_result), utcnow(), goal_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_goal_stats(user_id: int) -> dict:
    conn = get_connection()
    try:
        achieved = conn.execute(
            "SELECT COUNT(*) as c FROM goals WHERE user_id = ? AND status = 'achieved'",
            (user_id,),
        ).fetchone()["c"]
        missed = conn.execute(
            "SELECT COUNT(*) as c FROM goals WHERE user_id = ? AND status = 'missed'",
            (user_id,),
        ).fetchone()["c"]
        return {"achieved": achieved, "missed": missed, "total": achieved + missed}
    finally:
        conn.close()


def get_llm_credential(user_id: int, provider: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM llm_credentials WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_llm_credentials(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM llm_credentials WHERE user_id = ? ORDER BY provider",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_llm_credential(
    user_id: int,
    provider: str,
    encrypted_key: str,
    key_hint: str,
    model: str | None = None,
    validated_at: str | None = None,
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO llm_credentials (user_id, provider, encrypted_key, key_hint, model, validated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, provider) DO UPDATE SET
                 encrypted_key = excluded.encrypted_key,
                 key_hint = excluded.key_hint,
                 model = COALESCE(excluded.model, llm_credentials.model),
                 validated_at = excluded.validated_at""",
            (user_id, provider, encrypted_key, key_hint, model, validated_at),
        )
        conn.commit()
    finally:
        conn.close()


def delete_llm_credential(user_id: int, provider: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM llm_credentials WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        conn.commit()
    finally:
        conn.close()


def update_llm_credential_model(user_id: int, provider: str, model: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE llm_credentials SET model = ? WHERE user_id = ? AND provider = ?",
            (model, user_id, provider),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_telegram_connection(
    user_id: int,
    bot_token_encrypted: str,
    chat_id: str,
    bot_username: str | None = None,
) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM telegram_connections WHERE user_id = ?", (user_id,))
        conn.execute(
            """INSERT INTO telegram_connections
               (user_id, bot_token_encrypted, chat_id, bot_username, validated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, bot_token_encrypted, chat_id, bot_username, utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def get_weight_trend_range(user_id: int, days: int = 30) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT date, raw_weight as raw, smoothed_weight as smoothed
               FROM weight_trend WHERE user_id = ?
               AND date >= date('now', ?)
               ORDER BY date ASC""",
            (user_id, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_calorie_history(user_id: int, days: int = 30) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT DATE(logged_at) as date,
                      SUM(total_calories) as calories,
                      SUM(total_protein) as protein
               FROM meals WHERE user_id = ?
               AND DATE(logged_at) >= date('now', ?)
               GROUP BY DATE(logged_at)
               ORDER BY date ASC""",
            (user_id, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_logging_heatmap(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT DATE(logged_at) as date, COUNT(*) as count
               FROM meals WHERE user_id = ?
               AND DATE(logged_at) >= date('now', '-365 days')
               GROUP BY DATE(logged_at)
               ORDER BY date ASC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_meal_count_today(user_id: int, date_str: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM meals WHERE user_id = ? AND DATE(logged_at) = ?",
            (user_id, date_str),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


def insert_briefing_log(user_id: int, briefing_type: str, content_summary: str, had_nudge: bool = False) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO briefing_log (user_id, briefing_type, content_summary, had_nudge) VALUES (?, ?, ?, ?)",
            (user_id, briefing_type, content_summary, 1 if had_nudge else 0),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_briefings_sent_today(user_id: int, briefing_type: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM briefing_log WHERE user_id = ? AND briefing_type = ? AND DATE(sent_at) = date('now')",
            (user_id, briefing_type),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


def get_platform_stats() -> dict:
    conn = get_connection()
    try:
        users_total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        connections_active = conn.execute(
            "SELECT COUNT(*) as c FROM telegram_connections WHERE is_active = 1"
        ).fetchone()["c"]
        meals_total = conn.execute("SELECT COUNT(*) as c FROM meals").fetchone()["c"]
        llm_total = conn.execute("SELECT COUNT(*) as c FROM llm_analysis").fetchone()["c"]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        meals_today = conn.execute(
            "SELECT COUNT(*) as c FROM meals WHERE DATE(logged_at) = ?", (today,)
        ).fetchone()["c"]
        return {
            "users_total": users_total,
            "connections_active": connections_active,
            "meals_total": meals_total,
            "meals_today": meals_today,
            "llm_calls_total": llm_total,
        }
    finally:
        conn.close()


# --- message_log helpers ---

def insert_message_log(
    user_id: int,
    channel: str,
    transcript: str | None = None,
    detected_intents: str | None = None,
    writes: str | None = None,
    response_text: str | None = None,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO message_log
               (user_id, channel, transcript, detected_intents, writes, response_text, model, tokens_in, tokens_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, channel, transcript, detected_intents, writes, response_text, model, tokens_in, tokens_out),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# --- data_requests helpers ---

def get_pending_data_request(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM data_requests WHERE user_id = ? AND status = 'pending' ORDER BY asked_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_data_request(user_id: int, category: str, question_text: str) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO data_requests (user_id, category, question_text) VALUES (?, ?, ?)",
            (user_id, category, question_text),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def resolve_data_request(req_id: int, answer_value: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE data_requests SET status = 'answered', answered_at = ?, answer_value = ? WHERE req_id = ?",
            (utcnow(), answer_value, req_id),
        )
        conn.commit()
    finally:
        conn.close()


def expire_old_data_requests(user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE data_requests SET status = 'expired' WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_meal(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM meals WHERE user_id = ? ORDER BY logged_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_meal_items(meal_id: int, items: list[dict]) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
        for item in items:
            conn.execute(
                """INSERT INTO meal_items (meal_id, food_id, name, quantity, unit, calories, protein, carbs, fat, fiber)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    meal_id,
                    item.get("food_id"),
                    item.get("name", ""),
                    item.get("quantity", 1),
                    item.get("unit", "serving"),
                    item.get("calories", 0),
                    item.get("protein", 0),
                    item.get("carbs", 0),
                    item.get("fat", 0),
                    item.get("fiber", 0),
                ),
            )
        total_cal = sum(i.get("calories", 0) for i in items)
        total_pro = sum(i.get("protein", 0) for i in items)
        total_carb = sum(i.get("carbs", 0) for i in items)
        total_fat = sum(i.get("fat", 0) for i in items)
        conn.execute(
            "UPDATE meals SET total_calories = ?, total_protein = ?, total_carbs = ?, total_fat = ? WHERE meal_id = ?",
            (total_cal, total_pro, total_carb, total_fat, meal_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- nutrition_targets helpers ---

def get_nutrition_targets(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM nutrition_targets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        return {
            "tdee": r.get("tdee_estimate"),
            "goal_type": r.get("goal_type", "maintain"),
            "calories": r.get("calorie_target"),
            "protein": r.get("protein_target"),
            "carbs": r.get("carbs_target"),
            "fat": r.get("fat_target"),
            "fiber": r.get("fiber_target"),
            "method": r.get("method", "default"),
            "eating_focus": r.get("eating_focus"),
            "computed_at": r.get("computed_at"),
            "weight_used": None,
        }
    finally:
        conn.close()


def upsert_nutrition_targets(user_id: int, targets: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO nutrition_targets
               (user_id, tdee_estimate, method, goal_type, calorie_target,
                protein_target, carbs_target, fat_target, fiber_target, eating_focus, computation_inputs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 tdee_estimate = excluded.tdee_estimate,
                 method = excluded.method,
                 goal_type = excluded.goal_type,
                 calorie_target = excluded.calorie_target,
                 protein_target = excluded.protein_target,
                 carbs_target = excluded.carbs_target,
                 fat_target = excluded.fat_target,
                 fiber_target = excluded.fiber_target,
                 eating_focus = excluded.eating_focus,
                 computation_inputs = excluded.computation_inputs,
                 computed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (
                user_id,
                targets.get("tdee"),
                targets.get("method", "default"),
                targets.get("goal_type", "maintain"),
                targets.get("calories", 2200),
                targets.get("protein", 140),
                targets.get("carbs", 220),
                targets.get("fat", 60),
                targets.get("fiber", 30),
                targets.get("eating_focus"),
                json.dumps({"weight_used": targets.get("weight_used")}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_health_data_today(user_id: int, date_str: str, data_type: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM health_data WHERE user_id = ? AND data_type = ? AND DATE(recorded_at) = ? ORDER BY recorded_at DESC",
            (user_id, data_type, date_str),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_workout_count_range(user_id: int, days: int) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM health_data WHERE user_id = ? AND data_type = 'workout' AND DATE(recorded_at) >= date('now', ?)",
            (user_id, f"-{days} days"),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()
