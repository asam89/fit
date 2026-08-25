"""Tests for generated-plan persistence and its sync onto the plan calendar.

Two things have to hold for the rest of the feature to work: a generated plan
round-trips through the database unchanged (so the web/AI layers read the same
numbers the engine computed), and every session appears on the existing weekly
calendar as an ordinary item that completes and reconciles like any other.
"""
import sqlite3
from datetime import date, timedelta
from unittest.mock import patch

import pytest


def _fresh_db(db_mod, db_path):
    """A brand-new database, built the way startup builds one."""
    db_mod.init_db()
    db_mod.run_migrations()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone, experience_level,"
        " days_available, equipment, session_time_min) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t@t.com", "hash", "T", "America/Toronto", "intermediate", 4, '["barbell","dumbbells"]', 60),
    )
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
        ("other@t.com", "hash", "O", "America/Toronto"),
    )
    conn.commit()
    conn.close()
    return 1


def _migrated_db(db_mod, db_path):
    """A database that reached the current schema through migrations only."""
    db_mod.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM schema_version")
    conn.commit()
    conn.close()
    db_mod.run_migrations()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
        ("t@t.com", "hash", "T", "America/Toronto"),
    )
    conn.commit()
    conn.close()
    return 1


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


WORKOUT_TABLES = {
    "workout_plans",
    "workout_sessions",
    "workout_exercise_prescriptions",
    "workout_set_log",
}


class TestMigration:
    def test_fresh_database_has_the_workout_tables(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            _fresh_db(db_mod, db_path)
            assert WORKOUT_TABLES.issubset(_tables(db_path))

    def test_fresh_database_has_the_plan_calendar(self, tmp_path):
        """The plan surface must exist on a first install, not only after migrations."""
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            _fresh_db(db_mod, db_path)
            conn = sqlite3.connect(db_path)
            cols = [c[1] for c in conn.execute("PRAGMA table_info(training_plans)").fetchall()]
            conn.close()
            assert "week_start" in cols
            assert "training_plan_items" in _tables(db_path)

    def test_migration_path_creates_the_workout_tables(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            _migrated_db(db_mod, db_path)
            assert WORKOUT_TABLES.issubset(_tables(db_path))

    def test_migrations_are_idempotent(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            _fresh_db(db_mod, db_path)
            db_mod.run_migrations()
            db_mod.run_migrations()
            assert WORKOUT_TABLES.issubset(_tables(db_path))

    def test_existing_tables_survive(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            _fresh_db(db_mod, db_path)
            tables = _tables(db_path)
            for name in ("users", "health_data", "meals", "goals", "training_plan_items"):
                assert name in tables


@pytest.fixture
def store(tmp_path):
    """A store bound to a fresh DB, with a seeded intermediate user."""
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db as db_mod
        from fitnessbot import training_plan, workout_store
        uid = _fresh_db(db_mod, db_path)
        yield workout_store, training_plan, uid, db_path


class TestSessionDates:
    def test_spreads_sessions_across_the_week(self):
        from fitnessbot import workout_store
        dates = workout_store.session_dates("2026-06-22", 4)
        assert dates == ["2026-06-22", "2026-06-23", "2026-06-25", "2026-06-26"]

    def test_every_date_falls_inside_the_week(self):
        from fitnessbot import workout_store
        for days in (2, 3, 4, 5, 6):
            dates = workout_store.session_dates("2026-06-22", days)
            assert len(dates) == days
            assert all("2026-06-22" <= d <= "2026-06-28" for d in dates)


class TestSavePlan:
    def test_persists_plan_sessions_and_prescriptions(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")

        plan = workout_store.get_plan(uid, saved["wp_id"])
        assert plan["week_start"] == "2026-06-22"
        assert plan["goal"] == saved["plan"]["goal"]
        assert plan["split_name"] == saved["plan"]["split"]
        assert len(plan["sessions"]) == len(saved["plan"]["days"])
        assert all(s["exercises"] for s in plan["sessions"])

    def test_prescriptions_keep_the_engines_numbers(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        plan = workout_store.get_plan(uid, saved["wp_id"])

        for session, day in zip(plan["sessions"], saved["plan"]["days"]):
            assert session["focus"] == day["focus"]
            assert session["planned_duration_min"] == day["est_duration_min"]
            for stored, generated in zip(session["exercises"], day["exercises"]):
                assert stored["exercise_id"] == generated["exercise_id"]
                assert stored["sets"] == generated["sets"]
                assert stored["reps"] == generated["reps"]
                assert stored["load"] == generated["load"]
                assert stored["rest_s"] == generated["rest_s"]

    def test_prescriptions_are_ordered_as_prescribed(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        plan = workout_store.get_plan(uid, saved["wp_id"])
        for session in plan["sessions"]:
            assert [e["position"] for e in session["exercises"]] == list(
                range(len(session["exercises"]))
            )

    def test_sessions_belong_to_their_plan(self, store):
        workout_store, _, uid, db_path = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        conn = sqlite3.connect(db_path)
        wp_ids = {r[0] for r in conn.execute("SELECT DISTINCT wp_id FROM workout_sessions")}
        conn.close()
        assert wp_ids == {saved["wp_id"]}


class TestCalendarSync:
    def test_each_session_appears_on_the_calendar(self, store):
        workout_store, training_plan, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        items = training_plan.get_plan_items(uid, "2026-06-22")

        assert len(items) == len(saved["plan"]["days"])
        assert {i["activity_type"] for i in items} == {workout_store.SESSION_ACTIVITY_TYPE}

    def test_calendar_item_matches_the_session(self, store):
        workout_store, training_plan, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        plan = workout_store.get_plan(uid, saved["wp_id"])

        for session in plan["sessions"]:
            items = training_plan.get_items_for_date(uid, session["date"])
            item = next(i for i in items if i["item_id"] == session["item_id"])
            assert item["title"] == session["title"]
            assert item["planned_duration_min"] == session["planned_duration_min"]
            assert item["date"] == session["date"]
            assert item["day_of_week"] == date.fromisoformat(session["date"]).weekday()

    def test_session_links_to_its_calendar_item(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        plan = workout_store.get_plan(uid, saved["wp_id"])
        item_ids = [s["item_id"] for s in plan["sessions"]]
        assert all(item_ids)
        assert len(set(item_ids)) == len(item_ids)

    def test_regenerating_a_week_does_not_duplicate_calendar_items(self, store):
        workout_store, training_plan, uid, _ = store
        first = workout_store.generate_and_save(uid, "2026-06-22")
        second = workout_store.generate_and_save(uid, "2026-06-22")

        items = training_plan.get_plan_items(uid, "2026-06-22")
        assert len(items) == len(second["plan"]["days"])
        assert first["wp_id"] != second["wp_id"]

    def test_regenerating_retires_the_previous_plan(self, store):
        workout_store, _, uid, db_path = store
        first = workout_store.generate_and_save(uid, "2026-06-22")
        workout_store.generate_and_save(uid, "2026-06-22")

        conn = sqlite3.connect(db_path)
        active = conn.execute(
            "SELECT active FROM workout_plans WHERE wp_id = ?", (first["wp_id"],)
        ).fetchone()[0]
        conn.close()
        assert active == 0

    def test_regenerating_keeps_completed_training(self, store):
        """A regenerated week must not erase a session the user actually did."""
        workout_store, training_plan, uid, _ = store
        first = workout_store.generate_and_save(uid, "2026-06-22")
        done = workout_store.get_plan(uid, first["wp_id"])["sessions"][0]
        workout_store.complete_session(uid, done["ws_id"])

        workout_store.generate_and_save(uid, "2026-06-22")
        items = training_plan.get_items_for_date(uid, done["date"])
        completed = [i for i in items if i["status"] == "completed"]
        assert len(completed) == 1
        assert completed[0]["item_id"] == done["item_id"]

    def test_existing_manual_items_are_untouched(self, store):
        workout_store, training_plan, uid, _ = store
        manual = training_plan.add_item(uid, "2026-06-22", "2026-06-24", "sport", "Basketball", 60)
        workout_store.generate_and_save(uid, "2026-06-22")
        workout_store.generate_and_save(uid, "2026-06-22")

        items = training_plan.get_plan_items(uid, "2026-06-22")
        assert manual["item_id"] in [i["item_id"] for i in items]

    def test_adherence_counts_generated_sessions(self, store):
        workout_store, training_plan, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        sessions = workout_store.get_plan(uid, saved["wp_id"])["sessions"]
        workout_store.complete_session(uid, sessions[0]["ws_id"])

        adherence = training_plan.compute_adherence(training_plan.get_plan_items(uid, "2026-06-22"))
        assert adherence["total"] == len(sessions)
        assert adherence["completed"] == 1


class TestCompleteSession:
    def test_completes_the_session_and_its_calendar_item(self, store):
        workout_store, training_plan, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        session = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]

        completed = workout_store.complete_session(uid, session["ws_id"])
        assert completed["status"] == "completed"

        stored = workout_store.get_session(uid, session["ws_id"])
        assert stored["status"] == "completed"
        assert stored["completed_at"]

        items = training_plan.get_items_for_date(uid, session["date"])
        item = next(i for i in items if i["item_id"] == session["item_id"])
        assert item["status"] == "completed"

    def test_completion_reconciles_a_workout_entry(self, store):
        workout_store, _, uid, db_path = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        session = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]
        workout_store.complete_session(uid, session["ws_id"])

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT COUNT(*) FROM health_data WHERE user_id = ? AND data_type = 'workout'", (uid,)
        ).fetchone()[0]
        conn.close()
        assert rows == 1

    def test_unknown_session_is_ignored(self, store):
        workout_store, _, uid, _ = store
        assert workout_store.complete_session(uid, 999) is None

    def test_another_users_session_cannot_be_completed(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        session = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]
        assert workout_store.complete_session(2, session["ws_id"]) is None


class TestLogSet:
    def _first_prescription(self, workout_store, uid):
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        session = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]
        return session, session["exercises"][0]

    def test_logs_a_set_against_the_prescription(self, store):
        workout_store, _, uid, _ = store
        session, rx = self._first_prescription(workout_store, uid)

        logged = workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0, rpe=8.0)
        assert logged["ws_id"] == session["ws_id"]
        assert logged["exercise_id"] == rx["exercise_id"]
        assert logged["set_index"] == 1
        assert logged["estimated_1rm"] > 60.0

    def test_set_indices_increment_per_session(self, store):
        workout_store, _, uid, _ = store
        _, rx = self._first_prescription(workout_store, uid)
        indices = [
            workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0)["set_index"]
            for _ in range(3)
        ]
        assert indices == [1, 2, 3]

    def test_sets_are_readable_for_the_session(self, store):
        workout_store, _, uid, _ = store
        session, rx = self._first_prescription(workout_store, uid)
        workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0)
        workout_store.log_set(uid, rx["wep_id"], reps=7, weight=60.0, completed=False)

        sets = workout_store.sets_for_session(uid, session["ws_id"])
        assert len(sets) == 2
        assert [s["completed"] for s in sets] == [1, 0]

    def test_another_users_prescription_cannot_be_logged(self, store):
        workout_store, _, uid, _ = store
        _, rx = self._first_prescription(workout_store, uid)
        assert workout_store.log_set(2, rx["wep_id"], reps=8, weight=60.0) is None

    def test_ad_hoc_set_needs_an_exercise(self, store):
        workout_store, _, uid, _ = store
        assert workout_store.log_set(uid, None, reps=8, weight=60.0) is None
        logged = workout_store.log_set(uid, None, reps=8, weight=60.0, exercise_id="back_squat")
        assert logged["exercise_id"] == "back_squat"


class TestHistoryFeedback:
    def test_history_collapses_a_session_into_one_entry(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        rx = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]["exercises"][0]
        for _ in range(3):
            workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0)

        history = workout_store.history_by_exercise(uid)
        entries = history[rx["exercise_id"]]
        assert len(entries) == 1
        assert entries[0] == {
            "weight": 60.0,
            "reps": 8,
            "sets": 3,
            "completed": True,
            "logged_at": entries[0]["logged_at"],
        }

    def test_a_missed_set_marks_the_session_incomplete(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        rx = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]["exercises"][0]
        workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0)
        workout_store.log_set(uid, rx["wep_id"], reps=5, weight=60.0, completed=False)

        entry = workout_store.history_by_exercise(uid)[rx["exercise_id"]][0]
        assert entry["completed"] is False
        assert entry["reps"] == 5

    def test_one_rm_is_the_best_logged_estimate(self, store):
        workout_store, _, uid, _ = store
        from fitnessbot import workout
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        rx = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]["exercises"][0]
        workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0)
        workout_store.log_set(uid, rx["wep_id"], reps=5, weight=80.0)

        best = workout_store.one_rm_by_exercise(uid)[rx["exercise_id"]]
        assert best == pytest.approx(workout.estimate_1rm(80.0, 5))

    def test_logged_work_drives_the_next_weeks_load(self, store):
        workout_store, _, uid, _ = store
        first = workout_store.generate_and_save(uid, "2026-06-22")
        rx = workout_store.get_plan(uid, first["wp_id"])["sessions"][0]["exercises"][0]
        for _ in range(rx["sets"]):
            workout_store.log_set(uid, rx["wep_id"], reps=rx["reps"], weight=60.0)

        second = workout_store.generate_and_save(uid, "2026-06-29")
        prescribed = [
            e
            for s in workout_store.get_plan(uid, second["wp_id"])["sessions"]
            for e in s["exercises"]
            if e["exercise_id"] == rx["exercise_id"]
        ]
        assert prescribed
        assert all(e["load"] is not None for e in prescribed)

    def test_history_is_per_user(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        rx = workout_store.get_plan(uid, saved["wp_id"])["sessions"][0]["exercises"][0]
        workout_store.log_set(uid, rx["wep_id"], reps=8, weight=60.0)

        assert workout_store.history_by_exercise(2) == {}
        assert workout_store.one_rm_by_exercise(2) == {}

    def test_no_logged_work_means_no_history(self, store):
        workout_store, _, uid, _ = store
        assert workout_store.history_by_exercise(uid) == {}
        assert workout_store.one_rm_by_exercise(uid) == {}


class TestProgrammePosition:
    def test_starts_at_the_first_week(self, store):
        workout_store, _, uid, _ = store
        assert workout_store.next_plan_position(uid) == (0, 1)

    def test_advances_a_week_at_a_time(self, store):
        workout_store, _, uid, _ = store
        workout_store.generate_and_save(uid, "2026-06-22")
        assert workout_store.next_plan_position(uid) == (0, 2)

    def test_a_finished_deload_starts_a_new_mesocycle(self, store):
        workout_store, _, uid, db_path = store
        from fitnessbot import workout
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE workout_plans SET week_index = ? WHERE wp_id = ?",
            (workout.DELOAD_CYCLE_WEEKS, saved["wp_id"]),
        )
        conn.commit()
        conn.close()
        assert workout_store.next_plan_position(uid) == (1, 1)

    def test_missed_sessions_are_counted_before_today(self, store):
        workout_store, _, uid, db_path = store
        today = date.today()
        week_start = (today - timedelta(days=today.weekday() + 7)).isoformat()
        workout_store.generate_and_save(uid, week_start)
        assert workout_store.count_missed_sessions(uid) > 0

    def test_future_sessions_are_not_missed(self, store):
        workout_store, _, uid, _ = store
        today = date.today()
        next_week = (today - timedelta(days=today.weekday()) + timedelta(days=7)).isoformat()
        workout_store.generate_and_save(uid, next_week)
        assert workout_store.count_missed_sessions(uid) == 0


class TestCurrentPlanLookup:
    def test_current_week_plan_is_found(self, store):
        workout_store, _, uid, _ = store
        week_start = workout_store.current_week_start(uid)
        saved = workout_store.generate_and_save(uid, week_start)
        current = workout_store.get_current_plan(uid)
        assert current["wp_id"] == saved["wp_id"]
        assert current["sessions"]

    def test_no_plan_for_this_week_returns_none(self, store):
        workout_store, _, uid, _ = store
        workout_store.generate_and_save(uid, "2020-01-06")
        assert workout_store.get_current_plan(uid) is None

    def test_todays_session_is_found(self, store):
        workout_store, _, uid, _ = store
        week_start = workout_store.current_week_start(uid)
        workout_store.generate_and_save(uid, week_start)
        session = workout_store.get_session_for_date(uid, week_start)
        assert session["date"] == week_start
        assert session["exercises"]

    def test_a_rest_day_has_no_session(self, store):
        workout_store, _, uid, _ = store
        workout_store.generate_and_save(uid, "2026-06-22")
        assert workout_store.get_session_for_date(uid, "2026-06-28") is None

    def test_another_users_plan_is_not_visible(self, store):
        workout_store, _, uid, _ = store
        saved = workout_store.generate_and_save(uid, "2026-06-22")
        assert workout_store.get_plan(2, saved["wp_id"]) is None
        assert workout_store.get_current_plan(2) is None
