"""Database schema, migrations, and DAL helpers."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fitnessbot.config import Config

SCHEMA_VERSION = 18

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
    muscle_mass REAL,
    muscle_mass_unit TEXT NOT NULL DEFAULT 'lbs',
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

-- notification_preferences
CREATE TABLE IF NOT EXISTS notification_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(user_id) ON DELETE CASCADE,
    morning_brief_enabled INTEGER NOT NULL DEFAULT 1,
    morning_brief_time TEXT NOT NULL DEFAULT '07:30',
    midday_check_enabled INTEGER NOT NULL DEFAULT 1,
    midday_check_time TEXT NOT NULL DEFAULT '13:00',
    evening_wrap_enabled INTEGER NOT NULL DEFAULT 1,
    evening_wrap_time TEXT NOT NULL DEFAULT '20:30',
    weekly_rollup_enabled INTEGER NOT NULL DEFAULT 1,
    weekly_rollup_day INTEGER NOT NULL DEFAULT 6,
    activity_prompts_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- invite_links
CREATE TABLE IF NOT EXISTS invite_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    code TEXT UNIQUE NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- event_goals (upcoming events with prep plans and motivation)
CREATE TABLE IF NOT EXISTS event_goals (
    eg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    sport_type TEXT,
    description TEXT,
    days_out INTEGER,
    prep_plan_json TEXT,
    science_notes TEXT,
    readiness_markers TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    motivation_frequency TEXT NOT NULL DEFAULT 'daily',
    last_checkin_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
            current = 7

        if current < 8:
            for sql in [
                """CREATE TABLE IF NOT EXISTS training_plans (
                    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    week_start TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    UNIQUE(user_id, week_start))""",
                """CREATE TABLE IF NOT EXISTS training_plan_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES training_plans(plan_id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    date TEXT NOT NULL,
                    day_of_week INTEGER NOT NULL,
                    activity_type TEXT NOT NULL DEFAULT 'other',
                    title TEXT NOT NULL,
                    planned_duration_min INTEGER,
                    notes TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'planned',
                    completed_at TEXT,
                    linked_exercise_id INTEGER)""",
                "CREATE INDEX IF NOT EXISTS idx_tpi_user_date ON training_plan_items(user_id, date)",
                "CREATE INDEX IF NOT EXISTS idx_tpi_plan ON training_plan_items(plan_id)",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (8)")
            conn.commit()
            current = 8

        if current < 9:
            # Fix: the old training_plans table (from goals system) had different columns.
            # Rename it and create the weekly plan version.
            try:
                cols = [c[1] for c in conn.execute("PRAGMA table_info(training_plans)").fetchall()]
                if "week_start" not in cols:
                    conn.execute("ALTER TABLE training_plans RENAME TO training_plans_legacy")
                    conn.execute("""CREATE TABLE training_plans (
                        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                        week_start TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                        UNIQUE(user_id, week_start))""")
            except sqlite3.OperationalError:
                pass
            conn.execute("INSERT INTO schema_version (version) VALUES (9)")
            conn.commit()
            current = 9

        if current < 10:
            conn.execute("""CREATE TABLE IF NOT EXISTS notification_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(user_id) ON DELETE CASCADE,
                morning_brief_enabled INTEGER NOT NULL DEFAULT 1,
                morning_brief_time TEXT NOT NULL DEFAULT '07:30',
                midday_check_enabled INTEGER NOT NULL DEFAULT 1,
                midday_check_time TEXT NOT NULL DEFAULT '13:00',
                evening_wrap_enabled INTEGER NOT NULL DEFAULT 1,
                evening_wrap_time TEXT NOT NULL DEFAULT '20:30',
                weekly_rollup_enabled INTEGER NOT NULL DEFAULT 1,
                weekly_rollup_day INTEGER NOT NULL DEFAULT 6,
                activity_prompts_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
            """)
            conn.execute("INSERT INTO schema_version (version) VALUES (10)")
            conn.commit()
            current = 10

        if current < 11:
            # Fix: training_plan_items FK referenced training_plans_legacy after rename.
            # Recreate the items table with correct FK.
            try:
                fk_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='training_plan_items'"
                ).fetchone()
                if fk_sql and 'training_plans_legacy' in (fk_sql[0] or ''):
                    conn.execute("DROP TABLE IF EXISTS training_plan_items")
                    conn.execute("""CREATE TABLE training_plan_items (
                        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plan_id INTEGER NOT NULL REFERENCES training_plans(plan_id) ON DELETE CASCADE,
                        user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                        date TEXT NOT NULL,
                        day_of_week INTEGER NOT NULL,
                        activity_type TEXT NOT NULL DEFAULT 'other',
                        title TEXT NOT NULL,
                        planned_duration_min INTEGER,
                        notes TEXT,
                        position INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'planned',
                        completed_at TEXT,
                        linked_exercise_id INTEGER)""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_tpi_user_date ON training_plan_items(user_id, date)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_tpi_plan ON training_plan_items(plan_id)")
            except sqlite3.OperationalError:
                pass
            conn.execute("INSERT INTO schema_version (version) VALUES (11)")
            conn.commit()
            current = 11

        if current < 12:
            conn.execute("""CREATE TABLE IF NOT EXISTS event_goals (
                eg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                event_date TEXT NOT NULL,
                sport_type TEXT,
                description TEXT,
                days_out INTEGER,
                prep_plan_json TEXT,
                science_notes TEXT,
                readiness_markers TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                motivation_frequency TEXT NOT NULL DEFAULT 'daily',
                last_checkin_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
            """)
            conn.execute("INSERT INTO schema_version (version) VALUES (12)")
            conn.commit()
            current = 12

        if current < 13:
            conn.execute("""CREATE TABLE IF NOT EXISTS invite_links (
                link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                code TEXT UNIQUE NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0,
                max_uses INTEGER,
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
            """)
            for col_name, col_type, col_default in [
                ("email_verified", "INTEGER", "0"),
                ("email_verify_code", "TEXT", None),
                ("google_id", "TEXT", None),
                ("invited_by", "INTEGER", None),
            ]:
                try:
                    default_clause = f" DEFAULT {col_default}" if col_default else ""
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}{default_clause}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (13)")
            conn.commit()

        if current < 14:
            conn.execute("""CREATE TABLE IF NOT EXISTS personal_bests (
                pb_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                exercise_name TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pb_user_exercise ON personal_bests(user_id, exercise_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pb_user_date ON personal_bests(user_id, recorded_at)")
            conn.execute("INSERT INTO schema_version (version) VALUES (14)")
            conn.commit()

        if current < 15:
            # Social features: handle, avatar, friendships, share settings, nudges
            for col_name, col_type, col_default in [
                ("handle", "TEXT", None),
                ("avatar_url", "TEXT", None),
            ]:
                try:
                    default_clause = f" DEFAULT '{col_default}'" if col_default else ""
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}{default_clause}")
                except sqlite3.OperationalError:
                    pass

            conn.execute("""CREATE TABLE IF NOT EXISTS friendships (
                friendship_id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                addressee_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                accepted_at TEXT,
                UNIQUE(requester_id, addressee_id))
            """)

            conn.execute("""CREATE TABLE IF NOT EXISTS share_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                share_goals INTEGER NOT NULL DEFAULT 1,
                share_progress INTEGER NOT NULL DEFAULT 1,
                share_diet INTEGER NOT NULL DEFAULT 1,
                share_workouts INTEGER NOT NULL DEFAULT 1,
                share_weight INTEGER NOT NULL DEFAULT 0,
                is_private INTEGER NOT NULL DEFAULT 0)
            """)

            conn.execute("""CREATE TABLE IF NOT EXISTS nudge_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                text TEXT NOT NULL,
                emoji TEXT,
                category TEXT NOT NULL DEFAULT 'cheer')
            """)

            conn.execute("""CREATE TABLE IF NOT EXISTS nudges (
                nudge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'preset',
                template_key TEXT,
                body TEXT,
                emoji TEXT,
                related_event_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                read_at TEXT)
            """)

            conn.execute("""CREATE TABLE IF NOT EXISTS blocks (
                block_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                blocked_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                UNIQUE(user_id, blocked_user_id))
            """)

            conn.execute("""CREATE TABLE IF NOT EXISTS reports (
                report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                reported_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                reason TEXT,
                nudge_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
            """)

            # Seed nudge templates
            templates = [
                # Hype / Cheer
                ("good_work", "Good work today!", "💪", "cheer"),
                ("beast_mode", "Beast mode activated", "🔥", "cheer"),
                ("proud", "Proud of you fr", "🙌", "cheer"),
                ("unstoppable", "Unstoppable", "🚀", "cheer"),
                ("keep_going", "Keep going!", "💯", "motivation"),
                ("streak_fire", "Streak on fire", "🔥", "progress"),
                # Trash talk / Cheeky
                ("couch_potato", "Get off the couch", "🛋\ufe0f", "trash_talk"),
                ("skipped_legs", "You skipped leg day didn't you", "🦵", "trash_talk"),
                ("gym_miss", "Gym misses you more than your ex", "💔", "trash_talk"),
                ("eating_good", "I see you eating good 👀", "🍕", "trash_talk"),
                ("snack_attack", "Put the snacks down", "🍪", "trash_talk"),
                ("cardio_who", "Cardio? Never heard of her", "🏃", "trash_talk"),
                ("sleeping_in", "Still sleeping? It's gym o'clock", "\u23f0", "trash_talk"),
                ("weak_sauce", "That's weak sauce", "😤", "trash_talk"),
                ("outpacing", "I'm outpacing you this week", "😏", "trash_talk"),
                ("catching_up", "Better catch up", "🏎\ufe0f", "trash_talk"),
                # Motivation
                ("where_gym", "Where's the gym today?", "🏋\ufe0f", "motivation"),
                ("protein_check", "Did you hit your protein?", "🥩", "motivation"),
                ("water_check", "Drink water!", "💧", "motivation"),
                ("go_outside", "Touch grass", "🌿", "motivation"),
                # Recovery
                ("rest_day", "Rest day earned", "😴", "recovery"),
                ("stretch", "Stretch! Your muscles are begging", "🧘", "recovery"),
            ]
            for key, text, emoji, category in templates:
                try:
                    conn.execute(
                        "INSERT INTO nudge_templates (key, text, emoji, category) VALUES (?, ?, ?, ?)",
                        (key, text, emoji, category),
                    )
                except sqlite3.IntegrityError:
                    pass

            conn.execute("INSERT INTO schema_version (version) VALUES (15)")
            conn.commit()

        if current < 16:
            # Add photo_path to meals, add fiber/sugar/sodium totals to meals
            for col_name, col_type in [
                ("photo_path", "TEXT"),
                ("total_fiber", "REAL DEFAULT 0"),
                ("total_sugar", "REAL DEFAULT 0"),
                ("total_sodium", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE meals ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # Add fiber/sugar/sodium to meal_items
            for col_name, col_type in [
                ("fiber", "REAL DEFAULT 0"),
                ("sugar", "REAL DEFAULT 0"),
                ("sodium", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE meal_items ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (16)")
            conn.commit()

        if current < 17:
            # Add missing columns to nutrition_targets
            for col_name, col_type in [
                ("sugar_target", "INTEGER DEFAULT 55"),
                ("sodium_target", "INTEGER DEFAULT 2300"),
                ("water_target", "INTEGER DEFAULT 2800"),
                ("bmr_estimate", "INTEGER"),
                ("goal_delta", "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE nutrition_targets ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # Add sat_fat and water_ml to meals
            for col_name, col_type in [
                ("total_sat_fat", "REAL DEFAULT 0"),
                ("total_water_ml", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE meals ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # Add sat_fat and water_ml to meal_items
            for col_name, col_type in [
                ("sat_fat", "REAL DEFAULT 0"),
                ("water_ml", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE meal_items ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # Add sat_fat and water_ml to foods
            for col_name, col_type in [
                ("sat_fat", "REAL DEFAULT 0"),
                ("water_ml", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE foods ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # Add macro_preset to users
            try:
                conn.execute("ALTER TABLE users ADD COLUMN macro_preset TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("INSERT INTO schema_version (version) VALUES (17)")
            conn.commit()

        # --- Migration 18: add muscle_mass columns to body_composition ---
        if current < 18:
            for col_name, col_type in [
                ("muscle_mass", "REAL"),
                ("muscle_mass_unit", "TEXT NOT NULL DEFAULT 'lbs'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE body_composition ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT INTO schema_version (version) VALUES (18)")
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
    total_fiber: float = 0,
    total_sugar: float = 0,
    total_sodium: float = 0,
    photo_path: str | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO meals
               (user_id, raw_text, meal_type, source, total_calories, total_protein,
                total_carbs, total_fat, total_fiber, total_sugar, total_sodium, photo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, raw_text, meal_type, source, total_calories, total_protein,
             total_carbs, total_fat, total_fiber, total_sugar, total_sodium, photo_path),
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
    fiber: float = 0,
    sugar: float = 0,
    sodium: float = 0,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO meal_items
               (meal_id, food_id, qty, unit, calories, protein, carbs, fat, fiber, sugar, sodium)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meal_id, food_id, qty, unit, calories, protein, carbs, fat, fiber, sugar, sodium),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_today_totals(user_id: int, date_str: str, *, utc_range: tuple[str, str] | None = None) -> dict:
    conn = get_connection()
    try:
        if utc_range:
            row = conn.execute(
                """SELECT
                     COALESCE(SUM(total_calories), 0) as calories,
                     COALESCE(SUM(total_protein), 0) as protein,
                     COALESCE(SUM(total_carbs), 0) as carbs,
                     COALESCE(SUM(total_fat), 0) as fat,
                     COALESCE(SUM(total_fiber), 0) as fiber,
                     COALESCE(SUM(total_sugar), 0) as sugar,
                     COALESCE(SUM(total_sodium), 0) as sodium,
                     COALESCE(SUM(total_sat_fat), 0) as sat_fat,
                     COALESCE(SUM(total_water_ml), 0) as water_ml
                   FROM meals
                   WHERE user_id = ? AND logged_at >= ? AND logged_at < ?""",
                (user_id, utc_range[0], utc_range[1]),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT
                     COALESCE(SUM(total_calories), 0) as calories,
                     COALESCE(SUM(total_protein), 0) as protein,
                     COALESCE(SUM(total_carbs), 0) as carbs,
                     COALESCE(SUM(total_fat), 0) as fat,
                     COALESCE(SUM(total_fiber), 0) as fiber,
                     COALESCE(SUM(total_sugar), 0) as sugar,
                     COALESCE(SUM(total_sodium), 0) as sodium,
                     COALESCE(SUM(total_sat_fat), 0) as sat_fat,
                     COALESCE(SUM(total_water_ml), 0) as water_ml
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


def get_meals_by_date(user_id: int, date_str: str, *, utc_range: tuple[str, str] | None = None) -> list[dict]:
    """Get all meals for a specific date (YYYY-MM-DD)."""
    conn = get_connection()
    try:
        if utc_range:
            rows = conn.execute(
                """SELECT * FROM meals
                   WHERE user_id = ? AND logged_at >= ? AND logged_at < ?
                   ORDER BY logged_at DESC""",
                (user_id, utc_range[0], utc_range[1]),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM meals
                   WHERE user_id = ? AND DATE(logged_at) = ?
                   ORDER BY logged_at DESC""",
                (user_id, date_str),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_meals_date_range(user_id: int, start_date: str, end_date: str, *, utc_range: tuple[str, str] | None = None) -> list[dict]:
    """Get all meals between start_date and end_date (inclusive, YYYY-MM-DD)."""
    conn = get_connection()
    try:
        if utc_range:
            rows = conn.execute(
                """SELECT * FROM meals
                   WHERE user_id = ? AND logged_at >= ? AND logged_at < ?
                   ORDER BY logged_at DESC""",
                (user_id, utc_range[0], utc_range[1]),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM meals
                   WHERE user_id = ? AND DATE(logged_at) BETWEEN ? AND ?
                   ORDER BY logged_at DESC""",
                (user_id, start_date, end_date),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_meal_dates_with_counts(user_id: int, limit: int = 30, *, utc_offset_hours: float | None = None) -> list[dict]:
    """Get distinct dates that have meals, with counts, most recent first."""
    conn = get_connection()
    try:
        if utc_offset_hours is not None:
            sign = "+" if utc_offset_hours >= 0 else "-"
            abs_h = abs(utc_offset_hours)
            h = int(abs_h)
            m = int((abs_h - h) * 60)
            offset_str = f"{sign}{h:02d}:{m:02d}"
            rows = conn.execute(
                f"""SELECT DATE(logged_at, '{offset_str}') as date, COUNT(*) as count, SUM(total_calories) as total_cal
                   FROM meals WHERE user_id = ?
                   GROUP BY DATE(logged_at, '{offset_str}')
                   ORDER BY date DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DATE(logged_at) as date, COUNT(*) as count, SUM(total_calories) as total_cal
                   FROM meals WHERE user_id = ?
                   GROUP BY DATE(logged_at)
                   ORDER BY date DESC LIMIT ?""",
                (user_id, limit),
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


def delete_meal_by_id(meal_id: int, user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT meal_id, raw_text, meal_type, total_calories FROM meals WHERE meal_id = ? AND user_id = ?",
            (meal_id, user_id),
        ).fetchone()
        if row:
            meal = dict(row)
            conn.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
            conn.execute("DELETE FROM meals WHERE meal_id = ?", (meal_id,))
            conn.commit()
            return meal
        return None
    finally:
        conn.close()


def update_meal_type(meal_id: int, user_id: int, meal_type: str) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE meals SET meal_type = ? WHERE meal_id = ? AND user_id = ?",
            (meal_type, meal_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def insert_body_composition(
    user_id: int,
    weight: float | None = None,
    weight_unit: str = "lbs",
    body_fat_pct: float | None = None,
    lean_mass: float | None = None,
    muscle_mass: float | None = None,
    muscle_mass_unit: str = "lbs",
    source: str = "manual",
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO body_composition
               (user_id, weight, weight_unit, body_fat_pct, lean_mass, muscle_mass, muscle_mass_unit, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, weight, weight_unit, body_fat_pct, lean_mass, muscle_mass, muscle_mass_unit, source),
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


def get_latest_body_composition(user_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT body_fat_pct, muscle_mass, muscle_mass_unit, measured_at
               FROM body_composition
               WHERE user_id = ? AND (body_fat_pct IS NOT NULL OR muscle_mass IS NOT NULL)
               ORDER BY measured_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_body_composition_history(user_id: int, limit: int = 10) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT body_fat_pct, muscle_mass, muscle_mass_unit, measured_at
               FROM body_composition
               WHERE user_id = ? AND (body_fat_pct IS NOT NULL OR muscle_mass IS NOT NULL)
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
        from fitnessbot.tz import user_today, day_utc_range
        today = user_today(user_id)
        urange = day_utc_range(today, user_id)
        meals_today = conn.execute(
            "SELECT COUNT(*) as c FROM meals WHERE user_id = ? AND logged_at >= ? AND logged_at < ?",
            (user_id, urange[0], urange[1]),
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


def get_calorie_history(user_id: int, days: int = 30, *, utc_offset_hours: float | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if utc_offset_hours is not None:
            sign = "+" if utc_offset_hours >= 0 else "-"
            abs_h = abs(utc_offset_hours)
            h = int(abs_h)
            m = int((abs_h - h) * 60)
            offset_str = f"{sign}{h:02d}:{m:02d}"
            rows = conn.execute(
                f"""SELECT DATE(logged_at, '{offset_str}') as date,
                          SUM(total_calories) as calories,
                          SUM(total_protein) as protein
                   FROM meals WHERE user_id = ?
                   AND DATE(logged_at, '{offset_str}') >= date('now', '{offset_str}', ?)
                   GROUP BY DATE(logged_at, '{offset_str}')
                   ORDER BY date ASC""",
                (user_id, f"-{days} days"),
            ).fetchall()
        else:
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


def get_logging_heatmap(user_id: int, days: int = 30, *, utc_offset_hours: float | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if utc_offset_hours is not None:
            sign = "+" if utc_offset_hours >= 0 else "-"
            abs_h = abs(utc_offset_hours)
            h = int(abs_h)
            m = int((abs_h - h) * 60)
            offset_str = f"{sign}{h:02d}:{m:02d}"
            rows = conn.execute(
                f"""SELECT DATE(logged_at, '{offset_str}') as date, COUNT(*) as count
                   FROM meals WHERE user_id = ?
                   AND DATE(logged_at, '{offset_str}') >= date('now', '{offset_str}', ? || ' days')
                   GROUP BY DATE(logged_at, '{offset_str}')
                   ORDER BY date ASC""",
                (user_id, str(-days)),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DATE(logged_at) as date, COUNT(*) as count
                   FROM meals WHERE user_id = ?
                   AND DATE(logged_at) >= date('now', ? || ' days')
                   GROUP BY DATE(logged_at)
                   ORDER BY date ASC""",
                (user_id, str(-days)),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_meal_count_today(user_id: int, date_str: str, *, utc_range: tuple[str, str] | None = None) -> int:
    conn = get_connection()
    try:
        if utc_range:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM meals WHERE user_id = ? AND logged_at >= ? AND logged_at < ?",
                (user_id, utc_range[0], utc_range[1]),
            ).fetchone()
        else:
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


def get_notification_preferences(user_id: int) -> dict:
    defaults = {
        "morning_brief_enabled": 1, "morning_brief_time": "07:30",
        "midday_check_enabled": 1, "midday_check_time": "13:00",
        "evening_wrap_enabled": 1, "evening_wrap_time": "20:30",
        "weekly_rollup_enabled": 1, "weekly_rollup_day": 6,
        "activity_prompts_enabled": 1,
    }
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM notification_preferences WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
        return {"user_id": user_id, **defaults}
    except Exception:
        return {"user_id": user_id, **defaults}
    finally:
        conn.close()


def upsert_notification_preferences(user_id: int, **kwargs) -> None:
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM notification_preferences WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [utcnow(), user_id]
            conn.execute(f"UPDATE notification_preferences SET {sets}, updated_at = ? WHERE user_id = ?", vals)
        else:
            cols = ["user_id"] + list(kwargs.keys())
            placeholders = ", ".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO notification_preferences ({', '.join(cols)}) VALUES ({placeholders})",
                [user_id] + list(kwargs.values()),
            )
        conn.commit()
    finally:
        conn.close()


def get_activity_patterns(user_id: int, lookback_days: int = 60) -> list[dict]:
    """Analyze workout + plan history to find common activities by day of week."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT activity_type, title, day_of_week, COUNT(*) as freq,
                   AVG(planned_duration_min) as avg_duration,
                   MAX(date) as last_date
            FROM training_plan_items
            WHERE user_id = ? AND date >= date('now', ?)
            GROUP BY activity_type, title, day_of_week
            ORDER BY freq DESC
        """, (user_id, f"-{lookback_days} days")).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stale_activities(user_id: int, min_freq: int = 2, stale_days: int = 10) -> list[dict]:
    """Find activities the user used to do regularly but hasn't done recently."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT activity_type, title, COUNT(*) as freq,
                   MAX(date) as last_date,
                   julianday('now') - julianday(MAX(date)) as days_since
            FROM training_plan_items
            WHERE user_id = ? AND status IN ('completed', 'planned')
            GROUP BY activity_type, title
            HAVING freq >= ? AND days_since >= ?
            ORDER BY days_since DESC
        """, (user_id, min_freq, stale_days)).fetchall()
        return [dict(r) for r in rows]
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
        from fitnessbot.tz import user_today, day_utc_range
        today = user_today()
        urange = day_utc_range(today)
        meals_today = conn.execute(
            "SELECT COUNT(*) as c FROM meals WHERE logged_at >= ? AND logged_at < ?",
            (urange[0], urange[1]),
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
            "bmr": r.get("bmr_estimate"),
            "goal_type": r.get("goal_type", "maintain"),
            "goal_delta": r.get("goal_delta", 0),
            "calories": r.get("calorie_target"),
            "protein": r.get("protein_target"),
            "carbs": r.get("carbs_target"),
            "fat": r.get("fat_target"),
            "fiber": r.get("fiber_target"),
            "sugar": r.get("sugar_target"),
            "sodium": r.get("sodium_target", 2300),
            "water_ml": r.get("water_target"),
            "method": r.get("method", "default"),
            "eating_focus": r.get("eating_focus"),
            "computed_at": r.get("computed_at"),
            "weight_used": None,
            "floor_applied": False,
        }
    finally:
        conn.close()


def upsert_nutrition_targets(user_id: int, targets: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO nutrition_targets
               (user_id, tdee_estimate, bmr_estimate, method, goal_type, goal_delta,
                calorie_target, protein_target, carbs_target, fat_target, fiber_target,
                sugar_target, sodium_target, water_target,
                eating_focus, computation_inputs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 tdee_estimate = excluded.tdee_estimate,
                 bmr_estimate = excluded.bmr_estimate,
                 method = excluded.method,
                 goal_type = excluded.goal_type,
                 goal_delta = excluded.goal_delta,
                 calorie_target = excluded.calorie_target,
                 protein_target = excluded.protein_target,
                 carbs_target = excluded.carbs_target,
                 fat_target = excluded.fat_target,
                 fiber_target = excluded.fiber_target,
                 sugar_target = excluded.sugar_target,
                 sodium_target = excluded.sodium_target,
                 water_target = excluded.water_target,
                 eating_focus = excluded.eating_focus,
                 computation_inputs = excluded.computation_inputs,
                 computed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (
                user_id,
                targets.get("tdee"),
                targets.get("bmr"),
                targets.get("method", "default"),
                targets.get("goal_type", "maintain"),
                targets.get("goal_delta", 0),
                targets.get("calories", 2200),
                targets.get("protein", 140),
                targets.get("carbs", 220),
                targets.get("fat", 60),
                targets.get("fiber", 30),
                targets.get("sugar", 55),
                targets.get("sodium", 2300),
                targets.get("water_ml", 2800),
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


def get_macro_history(user_id: int, days: int = 7, *, utc_offset_hours: float | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if utc_offset_hours is not None:
            sign = "+" if utc_offset_hours >= 0 else "-"
            abs_h = abs(utc_offset_hours)
            h = int(abs_h)
            m = int((abs_h - h) * 60)
            offset_str = f"{sign}{h:02d}:{m:02d}"
            rows = conn.execute(
                f"""SELECT DATE(logged_at, '{offset_str}') as date,
                          SUM(total_calories) as calories,
                          SUM(total_protein) as protein,
                          SUM(total_carbs) as carbs,
                          SUM(total_fat) as fat,
                          COUNT(*) as meal_count
                   FROM meals WHERE user_id = ?
                   AND DATE(logged_at, '{offset_str}') >= date('now', '{offset_str}', ?)
                   GROUP BY DATE(logged_at, '{offset_str}')
                   ORDER BY date ASC""",
                (user_id, f"-{days} days"),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DATE(logged_at) as date,
                          SUM(total_calories) as calories,
                          SUM(total_protein) as protein,
                          SUM(total_carbs) as carbs,
                          SUM(total_fat) as fat,
                          COUNT(*) as meal_count
                   FROM meals WHERE user_id = ?
                   AND DATE(logged_at) >= date('now', ?)
                   GROUP BY DATE(logged_at)
                   ORDER BY date ASC""",
                (user_id, f"-{days} days"),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_sleep_history(user_id: int, days: int = 7) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT DATE(recorded_at) as date, data_json
               FROM health_data WHERE user_id = ? AND data_type = 'sleep'
               AND DATE(recorded_at) >= date('now', ?)
               ORDER BY date ASC""",
            (user_id, f"-{days} days"),
        ).fetchall()
        result = []
        for r in rows:
            import json as _json
            d = _json.loads(r["data_json"]) if r["data_json"] else {}
            d["date"] = r["date"]
            result.append(d)
        return result
    finally:
        conn.close()


def get_workout_history(user_id: int, days: int = 7) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT DATE(recorded_at) as date, data_json, notes
               FROM health_data WHERE user_id = ? AND data_type = 'workout'
               AND DATE(recorded_at) >= date('now', ?)
               ORDER BY date ASC""",
            (user_id, f"-{days} days"),
        ).fetchall()
        result = []
        for r in rows:
            import json as _json
            d = _json.loads(r["data_json"]) if r["data_json"] else {}
            d["date"] = r["date"]
            d["notes"] = r["notes"]
            result.append(d)
        return result
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


# --- event_goals helpers ---

def insert_event_goal(
    user_id: int, title: str, event_date: str, sport_type: str | None = None,
    description: str | None = None, days_out: int | None = None,
    prep_plan_json: str | None = None, science_notes: str | None = None,
    readiness_markers: str | None = None, motivation_frequency: str = "daily",
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO event_goals
               (user_id, title, event_date, sport_type, description, days_out,
                prep_plan_json, science_notes, readiness_markers, motivation_frequency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, title, event_date, sport_type, description, days_out,
             prep_plan_json, science_notes, readiness_markers, motivation_frequency),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_active_event_goals(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM event_goals WHERE user_id = ? AND status = 'active' ORDER BY event_date ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_event_goal(eg_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM event_goals WHERE eg_id = ?", (eg_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_event_goal(eg_id: int, **kwargs) -> None:
    conn = get_connection()
    try:
        kwargs["updated_at"] = utcnow()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(f"UPDATE event_goals SET {sets} WHERE eg_id = ?", list(kwargs.values()) + [eg_id])
        conn.commit()
    finally:
        conn.close()


def get_due_event_checkins() -> list[dict]:
    """Get all active event goals needing a motivation check-in today."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT eg.*, u.timezone, tc.chat_id, tc.bot_token_encrypted
            FROM event_goals eg
            JOIN users u ON eg.user_id = u.user_id
            JOIN telegram_connections tc ON eg.user_id = tc.user_id AND tc.is_active = 1
            WHERE eg.status = 'active'
            AND eg.event_date >= date('now')
            AND (eg.last_checkin_at IS NULL OR DATE(eg.last_checkin_at) < date('now'))
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- invite_links helpers ---

def create_invite_link(user_id: int, code: str, max_uses: int | None = None, expires_at: str | None = None) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO invite_links (user_id, code, max_uses, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, code, max_uses, expires_at),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_invite_link(code: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM invite_links WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def increment_invite_uses(code: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE invite_links SET uses = uses + 1 WHERE code = ?", (code,))
        conn.commit()
    finally:
        conn.close()


def get_user_invite_links(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM invite_links WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_by_google_id(google_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_email_verify_code(user_id: int, code: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET email_verify_code = ? WHERE user_id = ?", (code, user_id))
        conn.commit()
    finally:
        conn.close()


def verify_email(user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET email_verified = 1, email_verify_code = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def set_weight_goal(user_id: int, weight_goal: float) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO goals (user_id, goal_type, title, target_weight, start_date, status)
               VALUES (?, 'maintain', 'Weight goal', ?, date('now'), 'active')
               ON CONFLICT(user_id, goal_type) DO UPDATE SET
                 target_weight = excluded.target_weight""",
            (user_id, weight_goal),
        )
        conn.commit()
    except Exception:
        # goals table may not have unique constraint — update existing active goal
        conn.execute(
            """UPDATE goals SET target_weight = ?
               WHERE user_id = ? AND status = 'active' AND goal_type IN ('cut','bulk','maintain')""",
            (weight_goal, user_id),
        )
        if conn.total_changes == 0:
            conn.execute(
                """INSERT INTO goals (user_id, goal_type, title, target_weight, start_date, status)
                   VALUES (?, 'maintain', 'Weight goal', ?, date('now'), 'active')""",
                (user_id, weight_goal),
            )
        conn.commit()
    finally:
        conn.close()


def get_weight_goal(user_id: int) -> float | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT target_weight FROM goals
               WHERE user_id = ? AND status = 'active' AND target_weight IS NOT NULL
               ORDER BY goal_id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        return row["target_weight"] if row else None
    finally:
        conn.close()



# --- Personal Bests ---

def insert_personal_best(user_id: int, exercise_name: str, value: float, unit: str = "", notes: str = "", recorded_at: str | None = None) -> int:
    conn = get_connection()
    try:
        if recorded_at is None:
            recorded_at = utcnow()
        cursor = conn.execute(
            """INSERT INTO personal_bests (user_id, exercise_name, value, unit, recorded_at, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, exercise_name.strip().lower(), value, unit.strip(), recorded_at, notes),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_personal_bests(user_id: int, limit: int = 50) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT pb_id, exercise_name, value, unit, recorded_at, notes
               FROM personal_bests WHERE user_id = ?
               ORDER BY recorded_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_personal_bests_by_exercise(user_id: int, exercise_name: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT pb_id, exercise_name, value, unit, recorded_at, notes
               FROM personal_bests WHERE user_id = ? AND exercise_name = ?
               ORDER BY recorded_at DESC""",
            (user_id, exercise_name.strip().lower()),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_top_personal_bests(user_id: int) -> list[dict]:
    """Get the best (max value) for each exercise — used for display."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT exercise_name, MAX(value) as value, unit, MAX(recorded_at) as recorded_at
               FROM personal_bests WHERE user_id = ?
               GROUP BY exercise_name
               ORDER BY recorded_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_personal_best(pb_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM personal_bests WHERE pb_id = ? AND user_id = ?",
            (pb_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# --- Social / Friends DAL ---

def update_user_handle(user_id: int, handle: str) -> bool:
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET handle = ?, updated_at = ? WHERE user_id = ?", (handle.lower(), utcnow(), user_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_user_avatar(user_id: int, avatar_url: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET avatar_url = ?, updated_at = ? WHERE user_id = ?", (avatar_url, utcnow(), user_id))
        conn.commit()
    finally:
        conn.close()


def search_users(query: str, exclude_user_id: int, limit: int = 10) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT user_id, display_name, handle, avatar_url FROM users
               WHERE user_id != ? AND (handle LIKE ? OR email LIKE ? OR display_name LIKE ?)
               LIMIT ?""",
            (exclude_user_id, f"%{query}%", f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_by_handle(handle: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE handle = ?", (handle.lower(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def send_friend_request(requester_id: int, addressee_id: int) -> str:
    conn = get_connection()
    try:
        # Check if blocked
        block = conn.execute(
            "SELECT 1 FROM blocks WHERE (user_id=? AND blocked_user_id=?) OR (user_id=? AND blocked_user_id=?)",
            (requester_id, addressee_id, addressee_id, requester_id),
        ).fetchone()
        if block:
            return "blocked"
        # Check existing
        existing = conn.execute(
            """SELECT status FROM friendships
               WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)""",
            (requester_id, addressee_id, addressee_id, requester_id),
        ).fetchone()
        if existing:
            if existing["status"] == "accepted":
                return "already_friends"
            return "already_pending"
        conn.execute(
            "INSERT INTO friendships (requester_id, addressee_id, status) VALUES (?, ?, 'pending')",
            (requester_id, addressee_id),
        )
        conn.commit()
        return "sent"
    finally:
        conn.close()


def accept_friend_request(friendship_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        result = conn.execute(
            "UPDATE friendships SET status='accepted', accepted_at=? WHERE friendship_id=? AND addressee_id=? AND status='pending'",
            (utcnow(), friendship_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    finally:
        conn.close()


def decline_friend_request(friendship_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        result = conn.execute(
            "DELETE FROM friendships WHERE friendship_id=? AND addressee_id=? AND status='pending'",
            (friendship_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    finally:
        conn.close()


def remove_friend(friendship_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        result = conn.execute(
            "DELETE FROM friendships WHERE friendship_id=? AND (requester_id=? OR addressee_id=?) AND status='accepted'",
            (friendship_id, user_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    finally:
        conn.close()


def get_friends(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT f.friendship_id, u.user_id, u.display_name, u.handle, u.avatar_url
               FROM friendships f
               JOIN users u ON u.user_id = CASE WHEN f.requester_id = ? THEN f.addressee_id ELSE f.requester_id END
               WHERE (f.requester_id = ? OR f.addressee_id = ?) AND f.status = 'accepted'""",
            (user_id, user_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_requests(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT f.friendship_id, u.user_id, u.display_name, u.handle, u.avatar_url, f.created_at
               FROM friendships f JOIN users u ON u.user_id = f.requester_id
               WHERE f.addressee_id = ? AND f.status = 'pending'
               ORDER BY f.created_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_sent_requests(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT f.friendship_id, u.user_id, u.display_name, u.handle, u.avatar_url, f.created_at
               FROM friendships f JOIN users u ON u.user_id = f.addressee_id
               WHERE f.requester_id = ? AND f.status = 'pending'
               ORDER BY f.created_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def block_user(user_id: int, blocked_user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
            (user_id, blocked_user_id, blocked_user_id, user_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO blocks (user_id, blocked_user_id) VALUES (?, ?)",
            (user_id, blocked_user_id),
        )
        conn.commit()
    finally:
        conn.close()


def unblock_user(user_id: int, blocked_user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM blocks WHERE user_id=? AND blocked_user_id=?", (user_id, blocked_user_id))
        conn.commit()
    finally:
        conn.close()


def get_blocked_users(user_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT b.block_id, u.user_id, u.display_name, u.handle
               FROM blocks b JOIN users u ON u.user_id = b.blocked_user_id
               WHERE b.user_id = ?""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def is_blocked(user_id: int, other_user_id: int) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM blocks WHERE (user_id=? AND blocked_user_id=?) OR (user_id=? AND blocked_user_id=?)",
            (user_id, other_user_id, other_user_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def are_friends(user_id: int, other_user_id: int) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT 1 FROM friendships
               WHERE ((requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?))
               AND status='accepted'""",
            (user_id, other_user_id, other_user_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# --- Share Settings ---

def get_share_settings(user_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM share_settings WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
        return {
            "share_goals": 1, "share_progress": 1, "share_diet": 1,
            "share_workouts": 1, "share_weight": 0, "is_private": 0,
        }
    finally:
        conn.close()


def upsert_share_settings(user_id: int, settings: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO share_settings (user_id, share_goals, share_progress, share_diet, share_workouts, share_weight, is_private)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 share_goals=excluded.share_goals, share_progress=excluded.share_progress,
                 share_diet=excluded.share_diet, share_workouts=excluded.share_workouts,
                 share_weight=excluded.share_weight, is_private=excluded.is_private""",
            (user_id, settings.get("share_goals", 1), settings.get("share_progress", 1),
             settings.get("share_diet", 1), settings.get("share_workouts", 1),
             settings.get("share_weight", 0), settings.get("is_private", 0)),
        )
        conn.commit()
    finally:
        conn.close()


# --- Nudges ---

def get_nudge_templates() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM nudge_templates ORDER BY category, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def send_nudge(sender_id: int, recipient_id: int, kind: str = "preset",
               template_key: str | None = None, body: str | None = None,
               emoji: str | None = None, related_event_id: int | None = None) -> int | None:
    conn = get_connection()
    try:
        if is_blocked(sender_id, recipient_id):
            return None
        if not are_friends(sender_id, recipient_id):
            return None
        cursor = conn.execute(
            """INSERT INTO nudges (sender_id, recipient_id, kind, template_key, body, emoji, related_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sender_id, recipient_id, kind, template_key, body, emoji, related_event_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_nudges_for_user(user_id: int, limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT n.*, u.display_name as sender_name, u.handle as sender_handle, u.avatar_url as sender_avatar,
                      t.text as template_text, t.emoji as template_emoji
               FROM nudges n
               JOIN users u ON u.user_id = n.sender_id
               LEFT JOIN nudge_templates t ON t.key = n.template_key
               WHERE n.recipient_id = ?
               ORDER BY n.created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_nudges_read(user_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE nudges SET read_at = ? WHERE recipient_id = ? AND read_at IS NULL", (utcnow(), user_id))
        conn.commit()
    finally:
        conn.close()


def get_unread_nudge_count(user_id: int) -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) as c FROM nudges WHERE recipient_id = ? AND read_at IS NULL", (user_id,)).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


def count_nudges_sent_today(sender_id: int, recipient_id: int) -> int:
    conn = get_connection()
    try:
        from fitnessbot.tz import user_today
        today = user_today(sender_id)
        row = conn.execute(
            "SELECT COUNT(*) as c FROM nudges WHERE sender_id=? AND recipient_id=? AND created_at >= ?",
            (sender_id, recipient_id, today),
        ).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


# --- Reports ---

def report_user(reporter_id: int, reported_user_id: int, reason: str | None = None, nudge_id: int | None = None) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO reports (reporter_id, reported_user_id, reason, nudge_id) VALUES (?, ?, ?, ?)",
            (reporter_id, reported_user_id, reason, nudge_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_friend_summary(user_id: int, viewer_id: int) -> dict:
    """Get a friend's progress summary, filtered by their share settings."""
    settings = get_share_settings(user_id)
    if settings.get("is_private"):
        return {"private": True}

    summary = {"user_id": user_id, "private": False}
    conn = get_connection()
    try:
        from fitnessbot.tz import user_today
        today = user_today(user_id)

        if settings.get("share_goals"):
            goal = conn.execute(
                "SELECT goal_type, title, target_weight FROM goals WHERE user_id=? AND status='active' ORDER BY goal_id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            summary["goal"] = dict(goal) if goal else None

        if settings.get("share_progress"):
            # Training plan adherence this week
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            plan_items = conn.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as done FROM training_plan_items WHERE user_id=? AND date >= ?",
                (user_id, monday),
            ).fetchone()
            summary["plan_adherence"] = {"done": plan_items["done"] or 0, "total": plan_items["total"] or 0} if plan_items else None

        if settings.get("share_diet"):
            totals = conn.execute(
                "SELECT total_calories as calories, protein, carbs, fat FROM daily_summary WHERE user_id=? AND date=?",
                (user_id, today),
            ).fetchone()
            if not totals or not totals["calories"]:
                # Fallback: sum from meals table directly
                meal_totals = conn.execute(
                    "SELECT SUM(total_calories) as calories, SUM(total_protein) as protein, SUM(total_carbs) as carbs, SUM(total_fat) as fat FROM meals WHERE user_id=? AND logged_at >= ?",
                    (user_id, today),
                ).fetchone()
                summary["today_macros"] = dict(meal_totals) if meal_totals and meal_totals["calories"] else None
            else:
                summary["today_macros"] = dict(totals)

        if settings.get("share_workouts"):
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            workouts = conn.execute(
                "SELECT COUNT(*) as c FROM exercise WHERE user_id=? AND started_at >= ?",
                (user_id, monday),
            ).fetchone()
            count = workouts["c"] if workouts else 0
            if count == 0:
                # Fallback: check daily_workouts (completed ones)
                dw = conn.execute(
                    "SELECT COUNT(*) as c FROM daily_workouts WHERE user_id=? AND scheduled_date >= ? AND completed=1",
                    (user_id, monday),
                ).fetchone()
                count = dw["c"] if dw else 0
            summary["workouts_this_week"] = count

        if settings.get("share_weight"):
            weight_row = conn.execute(
                "SELECT raw_weight as weight, date FROM weight_trend WHERE user_id=? ORDER BY date DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            summary["latest_weight"] = dict(weight_row) if weight_row else None

        return summary
    finally:
        conn.close()
