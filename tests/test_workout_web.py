"""Tests for the generated-workout endpoints.

The endpoints are a pass-through to the engine, so what's worth asserting is
the boundary: authentication, user isolation, that a client can't widen the
engine's inputs through `/adjust`, and that a swap stays inside the user's
equipment and exclusions.
"""
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _seed(db_mod, db_path):
    db_mod.init_db()
    db_mod.run_migrations()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone, experience_level,"
        " days_available, equipment, session_time_min, medical_screen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("t@t.com", "hash", "T", "America/Toronto", "intermediate", 4,
         '["barbell","dumbbells"]', 60, "2026-06-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
        ("other@t.com", "hash", "O", "America/Toronto"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path):
    """Authenticated client for user 1, on a fresh database."""
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db as db_mod
        from fitnessbot.web.app import create_app
        _seed(db_mod, db_path)
        with patch("fitnessbot.web.workout.get_current_user", return_value={"user_id": 1}):
            yield TestClient(create_app())


@pytest.fixture
def anon_client(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db as db_mod
        from fitnessbot.web.app import create_app
        _seed(db_mod, db_path)
        with patch("fitnessbot.web.workout.get_current_user", return_value=None):
            yield TestClient(create_app())


class TestAuth:
    def test_every_endpoint_rejects_anonymous_callers(self, anon_client):
        assert anon_client.get("/api/workout/current").status_code == 401
        assert anon_client.post("/api/workout/generate", json={}).status_code == 401
        assert anon_client.post("/api/workout/log", json={"reps": 5}).status_code == 401
        assert anon_client.post("/api/workout/adjust", json={"days_available": 3}).status_code == 401


class TestCurrent:
    def test_reports_no_plan_before_one_is_generated(self, client):
        body = client.get("/api/workout/current").json()
        assert body["plan"] is None
        assert body["profile"]["days_available"] == 4
        assert body["options"]["max_days"] == 6

    def test_returns_the_generated_plan_with_sessions(self, client):
        client.post("/api/workout/generate", json={})
        body = client.get("/api/workout/current").json()
        plan = body["plan"]
        assert plan["split"]
        assert len(plan["sessions"]) == 4
        assert all(s["exercises"] for s in plan["sessions"])

    def test_numbers_match_the_stored_prescriptions(self, client, tmp_path):
        client.post("/api/workout/generate", json={})
        plan = client.get("/api/workout/current").json()["plan"]
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sets, reps, load FROM workout_exercise_prescriptions ORDER BY wep_id"
        ).fetchall()
        conn.close()
        served = [(e["sets"], e["reps"], e["load"]) for s in plan["sessions"] for e in s["exercises"]]
        assert served == [(r["sets"], r["reps"], r["load"]) for r in rows]

    def test_logged_sets_show_as_progress(self, client):
        client.post("/api/workout/generate", json={})
        first = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]
        client.post("/api/workout/log", json={"wep_id": first["wep_id"], "reps": 5, "weight": 100})
        again = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]
        assert again["sets_done"] == 1
        assert again["logged_sets"][0]["weight"] == 100


class TestGenerate:
    def test_creates_the_plan_and_its_calendar_items(self, client):
        assert client.post("/api/workout/generate", json={}).status_code == 201
        from fitnessbot import training_plan, workout_store
        week = workout_store.current_week_start(1)
        items = training_plan.get_plan_items(1, week)
        assert len([i for i in items if i["activity_type"] == "strength"]) == 4

    def test_regenerating_does_not_duplicate_sessions(self, client):
        client.post("/api/workout/generate", json={})
        client.post("/api/workout/generate", json={})
        plan = client.get("/api/workout/current").json()["plan"]
        assert len(plan["sessions"]) == 4


class TestLog:
    def test_logging_a_set_returns_the_estimate(self, client):
        client.post("/api/workout/generate", json={})
        wep = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]["wep_id"]
        body = client.post("/api/workout/log", json={"wep_id": wep, "reps": 5, "weight": 100}).json()
        assert body["set"]["set_index"] == 1
        assert body["set"]["estimated_1rm"] > 100

    def test_reps_are_required(self, client):
        client.post("/api/workout/generate", json={})
        assert client.post("/api/workout/log", json={"weight": 100}).status_code == 400

    def test_another_users_prescription_is_not_loggable(self, client):
        client.post("/api/workout/generate", json={})
        wep = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]["wep_id"]
        with patch("fitnessbot.web.workout.get_current_user", return_value={"user_id": 2}):
            assert client.post("/api/workout/log", json={"wep_id": wep, "reps": 5}).status_code == 404

    def test_completing_a_session_completes_its_calendar_item(self, client):
        client.post("/api/workout/generate", json={})
        session = client.get("/api/workout/current").json()["plan"]["sessions"][0]
        body = client.post(
            "/api/workout/log",
            json={"ws_id": session["ws_id"], "complete_session": True},
        ).json()
        assert body["session"]["status"] == "completed"

        from fitnessbot import training_plan
        items = training_plan.get_items_for_date(1, session["date"])
        assert any(i["status"] == "completed" for i in items)

    def test_completing_an_unknown_session_is_a_404(self, client):
        assert client.post(
            "/api/workout/log", json={"ws_id": 999, "complete_session": True}
        ).status_code == 404


class TestAdjust:
    def test_changing_days_regenerates_the_week(self, client):
        client.post("/api/workout/generate", json={})
        body = client.post("/api/workout/adjust", json={"days_available": 3}).json()
        assert body["adjusted"]["days_available"] == 3
        assert len(body["plan"]["sessions"]) == 3

    def test_out_of_range_days_are_clamped_not_honoured(self, client):
        """The split table only covers 2-6 days; a client can't ask for 14."""
        body = client.post("/api/workout/adjust", json={"days_available": 14}).json()
        assert body["adjusted"]["days_available"] == 6
        assert len(body["plan"]["sessions"]) == 6

    def test_absurd_session_length_is_clamped(self, client):
        body = client.post("/api/workout/adjust", json={"session_time_min": 600}).json()
        assert body["adjusted"]["session_time_min"] == 120

    def test_unknown_equipment_is_dropped(self, client):
        body = client.post(
            "/api/workout/adjust", json={"equipment": ["barbell", "trebuchet"]}
        ).json()
        assert "trebuchet" not in body["adjusted"]["equipment"]
        assert body["profile"]["equipment"] == ["barbell"]

    def test_exclusions_keep_the_pattern_out_of_the_plan(self, client):
        body = client.post("/api/workout/adjust", json={"movement_exclusions": ["squat"]}).json()
        patterns = {e["pattern"] for s in body["plan"]["sessions"] for e in s["exercises"]}
        assert "squat" not in patterns

    def test_adjusting_nothing_is_rejected(self, client):
        assert client.post("/api/workout/adjust", json={"nickname": "x"}).status_code == 400

    def test_fields_outside_the_allowlist_are_ignored(self, client):
        client.post("/api/workout/adjust", json={"days_available": 3, "email": "hacked@x.com"})
        from fitnessbot import db
        assert db.get_user_by_id(1)["email"] == "t@t.com"


class TestSwap:
    def test_swap_replaces_the_movement_in_place(self, client):
        client.post("/api/workout/generate", json={})
        before = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]
        body = client.post("/api/workout/adjust", json={"swap_wep_id": before["wep_id"]}).json()
        after = body["plan"]["sessions"][0]["exercises"][0]

        assert after["wep_id"] == before["wep_id"]
        assert after["exercise_id"] != before["exercise_id"]
        assert after["pattern"] == before["pattern"]

    def test_swap_does_not_duplicate_a_movement_already_in_the_session(self, client):
        client.post("/api/workout/generate", json={})
        session = client.get("/api/workout/current").json()["plan"]["sessions"][0]
        for exercise in session["exercises"]:
            client.post("/api/workout/adjust", json={"swap_wep_id": exercise["wep_id"]})
        after = client.get("/api/workout/current").json()["plan"]["sessions"][0]
        ids = [e["exercise_id"] for e in after["exercises"]]
        assert len(ids) == len(set(ids))

    def test_swap_respects_exclusions(self, client):
        client.post("/api/workout/adjust", json={"movement_exclusions": ["squat"]})
        session = client.get("/api/workout/current").json()["plan"]["sessions"][0]
        for exercise in session["exercises"]:
            client.post("/api/workout/adjust", json={"swap_wep_id": exercise["wep_id"]})
        after = client.get("/api/workout/current").json()["plan"]["sessions"][0]
        assert all(e["pattern"] != "squat" for e in after["exercises"])

    def test_swap_keeps_already_logged_sets(self, client):
        """A swap must not rewrite history — the old movement's sets stand."""
        client.post("/api/workout/generate", json={})
        exercise = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]
        client.post("/api/workout/log", json={"wep_id": exercise["wep_id"], "reps": 5, "weight": 90})
        client.post("/api/workout/adjust", json={"swap_wep_id": exercise["wep_id"]})

        from fitnessbot import workout_store
        history = workout_store.history_by_exercise(1)
        assert exercise["exercise_id"] in history

    def test_another_users_prescription_cannot_be_swapped(self, client):
        client.post("/api/workout/generate", json={})
        wep = client.get("/api/workout/current").json()["plan"]["sessions"][0]["exercises"][0]["wep_id"]
        with patch("fitnessbot.web.workout.get_current_user", return_value={"user_id": 2}):
            assert client.post("/api/workout/adjust", json={"swap_wep_id": wep}).status_code == 404


class TestIsolation:
    def test_a_users_plan_is_not_visible_to_another(self, client):
        client.post("/api/workout/generate", json={})
        with patch("fitnessbot.web.workout.get_current_user", return_value={"user_id": 2}):
            assert client.get("/api/workout/current").json()["plan"] is None
