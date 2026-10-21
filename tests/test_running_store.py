"""Workstream A: runs data model, idempotent imports, duplicate flagging, migrations."""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _replay_migrations(db, db_path):
    db.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.commit()
    conn.close()
    db.run_migrations()


@pytest.fixture
def run_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        _replay_migrations(db, db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO users (user_id, email, password_hash, display_name, timezone) VALUES (1, 'a@t', 'h', 'A', 'America/Toronto')"
        )
        conn.execute(
            "INSERT INTO users (user_id, email, password_hash, display_name, timezone) VALUES (2, 'b@t', 'h', 'B', 'Asia/Tokyo')"
        )
        conn.commit()
        conn.close()
        from fitnessbot.running import store
        yield db, db_path, store


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _cols(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _version(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()


# --- schema / migrations ---

def test_fresh_init_matches_migrated_schema(tmp_path):
    fresh, migrated = str(tmp_path / "fresh.db"), str(tmp_path / "migrated.db")
    from fitnessbot import db
    with patch("fitnessbot.db.get_db_path", return_value=fresh):
        db.init_db()
        db.run_migrations()
    with patch("fitnessbot.db.get_db_path", return_value=migrated):
        _replay_migrations(db, migrated)
    for t in ("runs", "run_splits", "run_drafts"):
        assert _cols(fresh, t) == _cols(migrated, t)
    assert "run_units" in _cols(fresh, "users") and "run_units" in _cols(migrated, "users")
    assert _version(fresh) == _version(migrated) == db.SCHEMA_VERSION == 25


def test_migration_from_24_preserves_existing_data(tmp_path):
    """Simulate prod at v24: existing rows survive, new tables appear, users default to km."""
    db_path = str(tmp_path / "v24.db")
    from fitnessbot import db
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        _replay_migrations(db, db_path)
        db.migrate_down_25()
        assert _version(db_path) == 24
        assert not {"runs", "run_splits", "run_drafts"} & _tables(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO users (user_id, email, password_hash, display_name) VALUES (9, 'x@t', 'h', 'X')")
        conn.execute("INSERT INTO health_data (user_id, data_type, data_json, recorded_at) VALUES (9, 'workout', '{\"type\":\"cardio\"}', '2026-10-01T12:00:00Z')")
        conn.commit()
        conn.close()

        db.run_migrations()
        assert _version(db_path) == 25
        assert {"runs", "run_splits", "run_drafts"} <= _tables(db_path)
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT run_units, units_pref FROM users WHERE user_id = 9").fetchone() == ("km", "imperial")
        assert conn.execute("SELECT data_json FROM health_data WHERE user_id = 9").fetchone()[0] == '{"type":"cardio"}'
        conn.close()


def test_migrate_down_then_up_is_clean(run_db):
    db, db_path, store = run_db
    store.save_run(1, started_at="2026-10-20T12:00:00Z", distance_m=5000, duration_s=1650, source="telegram")
    db.migrate_down_25()
    assert not {"runs", "run_splits", "run_drafts"} & _tables(db_path)
    assert "run_units" not in _cols(db_path, "users")
    db.run_migrations()
    assert {"runs", "run_splits", "run_drafts"} <= _tables(db_path)
    assert store.list_runs(1) == []


def test_run_units_is_updatable(run_db):
    db, _, _ = run_db
    db.update_user(1, run_units="mi")
    assert db.get_user_by_id(1)["run_units"] == "mi"


# --- save / read ---

def test_save_and_get_run_with_splits(run_db):
    _, _, store = run_db
    res = store.save_run(
        1, started_at="2026-10-20T12:00:00Z", distance_m=2000, duration_s=600, source="gpx",
        external_id="abc", run_type="easy", surface="road", avg_hr=150,
        splits=[{"distance_m": 1000, "duration_s": 310}, {"distance_m": 1000, "duration_s": 290}],
    )
    assert res == {"run_id": res["run_id"], "created": True, "possible_duplicate_of": None}
    run = store.get_run(1, res["run_id"])
    assert run["distance_m"] == 2000 and run["duration_s"] == 600 and run["avg_hr"] == 150
    assert [s["duration_s"] for s in run["splits"]] == [310, 290]
    assert store.get_run(2, res["run_id"]) is None


def test_stores_raw_units_only(run_db):
    _, db_path, _ = run_db
    cols = set(_cols(db_path, "runs"))
    assert not {c for c in cols if "pace" in c or "speed" in c or "_km" in c or "_mi" in c}


@pytest.mark.parametrize("kwargs", [
    {"distance_m": 0}, {"duration_s": 0}, {"run_type": "jog"}, {"surface": "sand"},
    {"source": "garmin"}, {"perceived_effort": 11}, {"pace": 330},
])
def test_rejects_invalid_runs(run_db, kwargs):
    _, _, store = run_db
    base = {"started_at": "2026-10-20T12:00:00Z", "distance_m": 5000, "duration_s": 1500, "source": "telegram"}
    with pytest.raises(store.RunValidationError):
        store.save_run(1, **{**base, **kwargs})


def test_naive_start_time_is_rejected(run_db):
    _, _, store = run_db
    with pytest.raises(store.RunValidationError):
        store.save_run(1, started_at="2026-10-20T12:00:00", distance_m=5000, duration_s=1500, source="web")


# --- local date ---

def test_local_date_uses_user_timezone_across_midnight(run_db):
    _, _, store = run_db
    # 03:30 UTC on the 21st is 23:30 on the 20th in Toronto (EDT) and 12:30 on the 21st in Tokyo.
    a = store.save_run(1, started_at="2026-10-21T03:30:00Z", distance_m=5000, duration_s=1500, source="web")
    b = store.save_run(2, started_at="2026-10-21T03:30:00Z", distance_m=5000, duration_s=1500, source="web")
    assert store.get_run(1, a["run_id"])["local_date"] == "2026-10-20"
    assert store.get_run(2, b["run_id"])["local_date"] == "2026-10-21"


def test_local_date_is_fixed_at_save_time(run_db):
    db, _, store = run_db
    r = store.save_run(1, started_at="2026-10-21T03:30:00Z", distance_m=5000, duration_s=1500, source="web")
    db.update_user(1, timezone="Asia/Tokyo")
    assert store.get_run(1, r["run_id"])["local_date"] == "2026-10-20"


def test_update_started_at_recomputes_local_date(run_db):
    _, _, store = run_db
    r = store.save_run(1, started_at="2026-10-20T15:00:00Z", distance_m=5000, duration_s=1500, source="web")
    assert store.update_run(1, r["run_id"], started_at="2026-10-21T03:30:00Z", run_type="tempo")
    run = store.get_run(1, r["run_id"])
    assert run["local_date"] == "2026-10-20" and run["run_type"] == "tempo"
    assert store.update_run(1, r["run_id"], started_at="2026-10-21T15:00:00Z")
    assert store.get_run(1, r["run_id"])["local_date"] == "2026-10-21"


def test_list_runs_filters_and_orders(run_db):
    _, _, store = run_db
    for day, rt in ((18, "easy"), (19, "tempo"), (20, "easy")):
        store.save_run(1, started_at=f"2026-10-{day}T12:00:00Z", distance_m=5000, duration_s=1500,
                       source="web", run_type=rt)
    assert [r["local_date"] for r in store.list_runs(1)] == ["2026-10-20", "2026-10-19", "2026-10-18"]
    assert len(store.list_runs(1, start_date="2026-10-19")) == 2
    assert len(store.list_runs(1, run_type="easy", end_date="2026-10-19")) == 1
    assert store.latest_run(1)["local_date"] == "2026-10-20"
    assert store.list_runs(2) == []


# --- idempotency and duplicates ---

def test_importing_same_activity_twice_creates_one_run(run_db):
    _, _, store = run_db
    kw = dict(started_at="2026-10-20T12:00:00Z", distance_m=10000, duration_s=3000, source="strava",
              external_id="12345", splits=[{"distance_m": 10000, "duration_s": 3000}])
    first = store.save_run(1, **kw)
    second = store.save_run(1, **kw)
    assert first["created"] and not second["created"]
    assert first["run_id"] == second["run_id"]
    assert len(store.list_runs(1)) == 1
    assert len(store.get_run(1, first["run_id"])["splits"]) == 1


def test_same_external_id_other_user_or_source_is_separate(run_db):
    _, _, store = run_db
    store.save_run(1, started_at="2026-10-20T12:00:00Z", distance_m=10000, duration_s=3000, source="strava", external_id="1")
    other_user = store.save_run(2, started_at="2026-10-20T12:00:00Z", distance_m=10000, duration_s=3000, source="strava", external_id="1")
    assert other_user["created"]


def test_db_enforces_uniqueness_on_source_external_id(run_db):
    _, db_path, _ = run_db
    conn = sqlite3.connect(db_path)
    sql = ("INSERT INTO runs (user_id, started_at, local_date, distance_m, duration_s, source, external_id) "
           "VALUES (1, '2026-10-20T12:00:00Z', '2026-10-20', 5000, 1500, 'fit', 'x')")
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
    conn.close()


def test_cross_source_duplicate_is_flagged_not_dropped(run_db):
    _, _, store = run_db
    a = store.save_run(1, started_at="2026-10-20T12:00:00Z", distance_m=10000, duration_s=3000, source="gpx", external_id="f1")
    b = store.save_run(1, started_at="2026-10-20T12:01:30Z", distance_m=10250, duration_s=3010, source="strava", external_id="s1")
    assert b["created"] and b["possible_duplicate_of"] == a["run_id"]
    assert len(store.list_runs(1)) == 2


@pytest.mark.parametrize("offset_s,distance", [(121, 10000), (0, 10400)])
def test_not_a_duplicate_outside_thresholds(run_db, offset_s, distance):
    _, _, store = run_db
    store.save_run(1, started_at="2026-10-20T12:00:00Z", distance_m=10000, duration_s=3000, source="gpx")
    start = (datetime(2026, 10, 20, 12, tzinfo=timezone.utc) + timedelta(seconds=offset_s)).isoformat()
    b = store.save_run(1, started_at=start, distance_m=distance, duration_s=3000, source="strava")
    assert b["possible_duplicate_of"] is None


def test_delete_run_cascades_splits(run_db):
    _, db_path, store = run_db
    r = store.save_run(1, started_at="2026-10-20T12:00:00Z", distance_m=1000, duration_s=300, source="web",
                       splits=[{"distance_m": 1000, "duration_s": 300}])
    assert not store.delete_run(2, r["run_id"])
    assert store.delete_run(1, r["run_id"])
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM run_splits").fetchone()[0] == 0
    conn.close()


# --- drafts ---

def test_drafts_expire_and_are_user_scoped(run_db):
    _, _, store = run_db
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    d = store.create_draft(1, {"distance_m": 5000, "duration_s": 1650}, now=now)
    assert store.get_draft(1, d, now=now + timedelta(hours=1)) == {"distance_m": 5000, "duration_s": 1650}
    assert store.get_draft(2, d, now=now) is None
    assert store.get_draft(1, d, now=now + timedelta(hours=25)) is None
    assert store.purge_expired_drafts(now=now + timedelta(hours=25)) == 1
    assert store.list_runs(1) == []
