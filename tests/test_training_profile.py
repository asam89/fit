"""Tests for Phase-0 training input capture.

Covers:
- normalization/clamping of every new field
- migration 22 adds the columns additively and is idempotent
- update_user accepts the new fields and leaves existing ones working
- users with NULL new fields still get a usable profile (safe defaults)
- the red-flag screen drives needs_medical_clearance
"""
import json
import sqlite3
from unittest.mock import patch

from fitnessbot.training_profile import (
    DEFAULT_DAYS_AVAILABLE,
    DEFAULT_EXPERIENCE,
    DEFAULT_SESSION_MIN,
    MAX_DAYS_AVAILABLE,
    MAX_SESSION_MIN,
    MIN_DAYS_AVAILABLE,
    MIN_SESSION_MIN,
    get_training_profile,
    is_complete,
    normalize_days_available,
    normalize_equipment,
    normalize_exclusions,
    normalize_experience,
    normalize_injuries,
    normalize_medical_flags,
    normalize_session_time,
)


def _build_db(db_mod, db_path, **user_fields):
    """Fresh DB at the latest schema with one user."""
    db_mod.init_db()
    db_mod.run_migrations()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
        ("t@t.com", "hash", "T", "America/Toronto"),
    )
    conn.commit()
    conn.close()
    if user_fields:
        db_mod.update_user(1, **user_fields)
    return 1


class TestNormalizeExperience:
    def test_accepts_valid_levels(self):
        assert normalize_experience("beginner") == "beginner"
        assert normalize_experience("Intermediate") == "intermediate"
        assert normalize_experience(" ADVANCED ") == "advanced"

    def test_unknown_and_missing_fall_back_to_beginner(self):
        assert normalize_experience("elite") == DEFAULT_EXPERIENCE
        assert normalize_experience("") == DEFAULT_EXPERIENCE
        assert normalize_experience(None) == DEFAULT_EXPERIENCE


class TestNormalizeDaysAvailable:
    def test_in_range_preserved(self):
        for days in range(MIN_DAYS_AVAILABLE, MAX_DAYS_AVAILABLE + 1):
            assert normalize_days_available(days) == days

    def test_clamps_out_of_range(self):
        assert normalize_days_available(1) == MIN_DAYS_AVAILABLE
        assert normalize_days_available(0) == MIN_DAYS_AVAILABLE
        assert normalize_days_available(7) == MAX_DAYS_AVAILABLE
        assert normalize_days_available(99) == MAX_DAYS_AVAILABLE

    def test_parses_strings_and_defaults_on_garbage(self):
        assert normalize_days_available("4") == 4
        assert normalize_days_available("") == DEFAULT_DAYS_AVAILABLE
        assert normalize_days_available("lots") == DEFAULT_DAYS_AVAILABLE
        assert normalize_days_available(None) == DEFAULT_DAYS_AVAILABLE


class TestNormalizeSessionTime:
    def test_clamps_to_usable_range(self):
        assert normalize_session_time(45) == 45
        assert normalize_session_time(5) == MIN_SESSION_MIN
        assert normalize_session_time(600) == MAX_SESSION_MIN

    def test_defaults_on_missing_or_garbage(self):
        assert normalize_session_time(None) == DEFAULT_SESSION_MIN
        assert normalize_session_time("") == DEFAULT_SESSION_MIN
        assert normalize_session_time("an hour") == DEFAULT_SESSION_MIN


class TestNormalizeEquipment:
    def test_filters_unknown_and_dedupes(self):
        result = normalize_equipment(["dumbbells", "dumbbells", "kettlebell_of_doom"])
        assert result == ["dumbbells"]

    def test_parses_json_and_csv(self):
        assert normalize_equipment('["barbell", "machines"]') == ["barbell", "machines"]
        assert normalize_equipment("bands, dumbbells") == ["dumbbells", "bands"]

    def test_normalizes_spaces_and_dashes(self):
        assert normalize_equipment(["full gym"]) == ["full_gym"]
        assert normalize_equipment(["full-gym"]) == ["full_gym"]

    def test_empty_degrades_to_bodyweight(self):
        assert normalize_equipment([]) == ["bodyweight"]
        assert normalize_equipment(None) == ["bodyweight"]
        assert normalize_equipment("nothing_valid") == ["bodyweight"]

    def test_full_gym_subsumes_everything_else(self):
        assert normalize_equipment(["dumbbells", "full_gym", "bands"]) == ["full_gym"]

    def test_order_is_stable_regardless_of_submission_order(self):
        assert normalize_equipment(["bands", "dumbbells"]) == normalize_equipment(
            ["dumbbells", "bands"]
        )

    def test_malformed_json_does_not_raise(self):
        assert normalize_equipment('["barbell"') == ["bodyweight"]


class TestNormalizeExclusions:
    def test_valid_patterns_kept(self):
        assert normalize_exclusions(["overhead", "jumping"]) == ["overhead", "jumping"]

    def test_unknown_dropped_and_empty_allowed(self):
        assert normalize_exclusions(["moonwalking"]) == []
        assert normalize_exclusions(None) == []


class TestNormalizeMedicalFlags:
    def test_valid_flags_kept(self):
        assert normalize_medical_flags(["pregnancy", "acute_pain"]) == [
            "pregnancy",
            "acute_pain",
        ]

    def test_unknown_dropped(self):
        assert normalize_medical_flags(["vibes"]) == []
        assert normalize_medical_flags([]) == []


class TestNormalizeInjuries:
    def test_trims_and_truncates(self):
        assert normalize_injuries("  bad left knee  ") == "bad left knee"
        assert len(normalize_injuries("x" * 900)) == 500

    def test_empty(self):
        assert normalize_injuries(None) == ""
        assert normalize_injuries("") == ""


class TestMigration:
    def test_adds_columns_and_is_idempotent(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            db_mod.init_db()
            db_mod.run_migrations()
            # Re-running must not raise (duplicate-column errors swallowed)
            db_mod.run_migrations()

            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            conn.close()
            for col in (
                "experience_level", "days_available", "equipment",
                "session_time_min", "injuries", "movement_exclusions",
                "medical_flags", "medical_screen_at",
            ):
                assert col in cols

    def test_upgrades_a_pre_migration_database(self, tmp_path):
        """A user row created before v22 keeps working and gets the new columns."""
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            db_mod.init_db()
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO users (email, password_hash, display_name, timezone) VALUES (?, ?, ?, ?)",
                ("old@t.com", "hash", "Old", "America/Toronto"),
            )
            # Simulate a DB stamped at the previous version
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (21)")
            conn.commit()
            conn.close()

            db_mod.run_migrations()
            profile = get_training_profile(1)
            assert profile["experience_level"] == DEFAULT_EXPERIENCE
            assert profile["days_available"] == DEFAULT_DAYS_AVAILABLE
            assert profile["needs_medical_clearance"] is False


class TestUpdateUser:
    def test_persists_new_training_fields(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            uid = _build_db(
                db_mod, db_path,
                experience_level="intermediate",
                days_available=4,
                equipment=json.dumps(["dumbbells", "bands"]),
                session_time_min=45,
                injuries="left shoulder impingement",
                movement_exclusions=json.dumps(["overhead"]),
                medical_flags=json.dumps(["acute_pain"]),
                medical_screen_at="2026-01-01T00:00:00Z",
            )

            profile = get_training_profile(uid)
            assert profile["experience_level"] == "intermediate"
            assert profile["days_available"] == 4
            assert profile["equipment"] == ["dumbbells", "bands"]
            assert profile["session_time_min"] == 45
            assert profile["injuries"] == "left shoulder impingement"
            assert profile["movement_exclusions"] == ["overhead"]
            assert profile["medical_flags"] == ["acute_pain"]
            assert profile["needs_medical_clearance"] is True
            assert is_complete(profile) is True

    def test_existing_profile_updates_still_work(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            uid = _build_db(db_mod, db_path)
            db_mod.update_user(
                uid, display_name="Alex", sex="male", height=70.0,
                activity_level="very_active", feedback_tone_preference="blunt",
            )
            user = db_mod.get_user_by_id(uid)
            assert user["display_name"] == "Alex"
            assert user["sex"] == "male"
            assert user["height"] == 70.0
            assert user["activity_level"] == "very_active"
            assert user["feedback_tone_preference"] == "blunt"

    def test_unknown_fields_still_rejected(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            uid = _build_db(db_mod, db_path)
            db_mod.update_user(uid, is_superadmin=1)
            assert db_mod.get_user_by_id(uid)["is_superadmin"] == 0


class TestGetTrainingProfile:
    def test_null_fields_yield_safe_defaults(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            uid = _build_db(db_mod, db_path)
            profile = get_training_profile(uid)
            assert profile == {
                "experience_level": DEFAULT_EXPERIENCE,
                "days_available": DEFAULT_DAYS_AVAILABLE,
                "equipment": ["bodyweight"],
                "session_time_min": DEFAULT_SESSION_MIN,
                "injuries": "",
                "movement_exclusions": [],
                "medical_flags": [],
                "needs_medical_clearance": False,
                "screened": False,
            }
            assert is_complete(profile) is False

    def test_out_of_range_stored_values_are_clamped_on_read(self, tmp_path):
        """The engine is defended even if a value lands in the DB out of range."""
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            uid = _build_db(db_mod, db_path, days_available=14, session_time_min=1)
            profile = get_training_profile(uid)
            assert profile["days_available"] == MAX_DAYS_AVAILABLE
            assert profile["session_time_min"] == MIN_SESSION_MIN

    def test_missing_user_returns_defaults(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            db_mod.init_db()
            db_mod.run_migrations()
            profile = get_training_profile(999)
            assert profile["experience_level"] == DEFAULT_EXPERIENCE
            assert profile["needs_medical_clearance"] is False
