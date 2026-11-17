"""Workstream D: stats engine, /runstats, the /running page and PR celebration."""
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from fitnessbot.running import stats

TODAY = date(2026, 11, 18)   # a Wednesday; this week starts Mon Nov 16
_ids = iter(range(1, 10_000))


def run(local_date, km, dur_s, run_type=None, splits=None, max_hr=None):
    rid = next(_ids)
    return {"run_id": rid, "local_date": local_date, "started_at": f"{local_date}T12:00:00Z",
            "distance_m": km * 1000, "duration_s": dur_s, "run_type": run_type,
            "splits": splits or [], "max_hr": max_hr}


def km_splits(paces_s):
    return [{"split_index": i, "distance_m": 1000.0, "duration_s": float(p)} for i, p in enumerate(paces_s)]


class TestWeeks:
    def test_week_starts_monday(self):
        assert stats.week_start("2026-11-22") == date(2026, 11, 16)  # Sunday
        assert stats.week_start("2026-11-16") == date(2026, 11, 16)

    def test_weekly_totals_bucket_by_local_week(self):
        runs = [run("2026-11-16", 5, 1500), run("2026-11-18", 8, 2600), run("2026-11-15", 10, 3300)]
        weeks = stats.weekly_totals(runs, TODAY, weeks=2)
        assert [w["week_start"] for w in weeks] == ["2026-11-09", "2026-11-16"]
        assert weeks[1]["distance_m"] == 13000 and weeks[1]["runs"] == 2 and weeks[1]["in_progress"]
        assert weeks[0]["distance_m"] == 10000 and not weeks[0]["in_progress"]

    def test_four_week_average_uses_completed_weeks_only(self):
        runs = [run("2026-11-17", 50, 15000),          # this week: ignored (Accuracy 1)
                run("2026-11-10", 20, 6000), run("2026-11-03", 10, 3000),
                run("2026-10-27", 10, 3000), run("2026-10-20", 20, 6000),
                run("2026-10-13", 99, 30000)]          # 5 weeks back: outside the window
        assert stats.four_week_average_m(runs, TODAY) == pytest.approx((20 + 10 + 10 + 20) * 1000 / 4)

    def test_four_week_average_counts_empty_weeks_after_first_run(self):
        # First run in the week of Nov 2: earlier weeks are excluded, later empty weeks count as 0.
        runs = [run("2026-11-02", 9, 2700)]
        assert stats.four_week_average_m(runs, TODAY) == pytest.approx(4500)   # (9 + 0) / 2

    def test_no_average_without_a_completed_week(self):
        assert stats.four_week_average_m([run("2026-11-17", 5, 1500)], TODAY) is None
        assert stats.four_week_average_m([], TODAY) is None


class TestStreak:
    def test_current_week_counts_once_it_has_a_run(self):
        runs = [run("2026-11-17", 5, 1500), run("2026-11-11", 5, 1500), run("2026-11-04", 5, 1500)]
        assert stats.streak_weeks(runs, TODAY) == 3

    def test_empty_current_week_does_not_break_streak(self):
        runs = [run("2026-11-11", 5, 1500), run("2026-11-04", 5, 1500)]
        assert stats.streak_weeks(runs, TODAY) == 2

    def test_gap_ends_streak(self):
        runs = [run("2026-11-17", 5, 1500), run("2026-11-04", 5, 1500)]
        assert stats.streak_weeks(runs, TODAY) == 1
        assert stats.streak_weeks([], TODAY) == 0


class TestLoadNote:
    def test_warns_when_well_above_average(self):
        assert "50% above" in stats.load_note(30000, 20000)

    def test_quiet_when_close_or_no_base(self):
        assert stats.load_note(25000, 20000) is None
        assert stats.load_note(10000, None) is None
        assert stats.load_note(10000, 3000) is None   # tiny base


class TestPRs:
    def test_whole_run_within_two_percent_counts(self):
        prs = stats.personal_records([run("2026-11-01", 5.05, 1515)])
        assert prs == [{"distance": "5K", "time_s": 1500, "run_id": prs[0]["run_id"], "local_date": "2026-11-01"}]

    def test_run_outside_tolerance_without_splits_is_not_a_pr(self):
        assert stats.personal_records([run("2026-11-01", 10.0, 3000)])[0]["distance"] == "10K"
        assert stats.personal_records([run("2026-11-01", 5.3, 1600)]) == []

    def test_best_effort_from_splits(self):
        # 10 km with a fast middle 5 km
        r = run("2026-11-01", 10, 3000, splits=km_splits([310, 310, 280, 280, 280, 280, 280, 310, 310, 360]))
        prs = {p["distance"]: p["time_s"] for p in stats.personal_records([r])}
        assert prs == {"1K": 280, "5K": 1400, "10K": 3000}

    def test_interval_reps_never_make_a_pr(self):
        r = run("2026-11-01", 4.8, 1128, run_type="interval",
                splits=[{"distance_m": 800.0, "duration_s": 188.0}] * 6)
        assert stats.personal_records([r]) == []

    def test_faster_run_takes_the_record_and_ties_keep_the_first(self):
        a, b, c = run("2026-10-01", 5, 1600), run("2026-10-08", 5, 1550), run("2026-10-15", 5, 1550)
        pr = stats.personal_records([c, a, b])[0]
        assert pr["run_id"] == b["run_id"] and pr["time_s"] == 1550

    def test_new_records_reports_previous_time(self):
        before = [{"distance": "5K", "time_s": 1690, "run_id": 1, "local_date": "x"}]
        after = [{"distance": "5K", "time_s": 1650, "run_id": 2, "local_date": "y"},
                 {"distance": "1K", "time_s": 300, "run_id": 2, "local_date": "y"}]
        out = stats.new_records(before, after)
        assert [(p["distance"], p["previous_s"]) for p in out] == [("5K", 1690), ("1K", None)]
        assert stats.format_new_records(out)[0] == "\U0001F3C6 New 5K PR: 27:30 (was 28:10)"


class TestPaceAndZones:
    def test_easy_pace_trend_is_distance_weighted_and_easy_only(self):
        runs = [run("2026-11-16", 5, 1800, "easy"), run("2026-11-17", 10, 3300, "easy"),
                run("2026-11-17", 5, 1200, "tempo")]
        trend = stats.easy_pace_trend(runs, TODAY, weeks=2)
        assert trend[0]["pace_s_per_km"] is None
        assert trend[1]["pace_s_per_km"] == pytest.approx(5100 / 15)

    def test_zones(self):
        assert stats.hr_zone(150, 190) == "Z3"
        assert stats.hr_zone(80, 190) is None
        assert stats.hr_zone(150, None) is None
        splits = [{"duration_s": 300, "avg_hr": 140}, {"duration_s": 280, "avg_hr": 175}, {"duration_s": 60}]
        z = {r["zone"]: r["seconds"] for r in stats.time_in_zones(splits, 190)}
        assert z["Z3"] == 300 and z["Z5"] == 280 and z["Z1"] == 0
        assert stats.time_in_zones([{"duration_s": 60}], 190) == []

    def test_max_hr_prefers_observed_then_age(self):
        assert stats.estimated_max_hr([run("2026-11-01", 5, 1500, max_hr=188)], "1980-01-01", TODAY) == 188
        assert stats.estimated_max_hr([], "1986-12-01", TODAY) == 181   # still 39 on Nov 18
        assert stats.estimated_max_hr([], None, TODAY) is None
        assert stats.estimated_max_hr([], "garbage", TODAY) is None


# --- Integration: the same numbers in Telegram and on the page ---

@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        db.init_db()
        db.run_migrations()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO users (user_id, email, password_hash, display_name, timezone) "
                     "VALUES (1, 'a@t', 'h', 'A', 'America/Toronto')")
        conn.execute("INSERT INTO users (user_id, email, password_hash, display_name, timezone) "
                     "VALUES (2, 'b@t', 'h', 'B', 'America/Toronto')")
        conn.commit()
        conn.close()
        from fitnessbot.running import store
        with patch("fitnessbot.tz.user_today", return_value=TODAY.isoformat()):
            yield store


def _save(store, user_id, local_date, km, dur_s, **kw):
    started = datetime.fromisoformat(f"{local_date}T17:00:00+00:00")
    return store.save_run(user_id, started_at=started, distance_m=km * 1000, duration_s=dur_s,
                          source="web", **kw)["run_id"]


def _client(user_id):
    from fitnessbot.web.app import create_app
    user = {"user_id": user_id, "display_name": "A"} if user_id else None
    return patch("fitnessbot.web.running.get_current_user", return_value=user), create_app


def _get(path, user_id=1):
    p, create_app = _client(user_id)
    with p:
        return TestClient(create_app()).get(path, follow_redirects=False)


def _seed_history(store):
    _save(store, 1, "2026-11-17", 12, 3720, run_type="easy")
    _save(store, 1, "2026-11-16", 10, 2950, run_type="race")
    _save(store, 1, "2026-11-10", 5, 1500, run_type="easy")
    _save(store, 1, "2026-11-03", 8, 2640, run_type="easy")
    _save(store, 2, "2026-11-17", 21.1, 6000)   # another user's run never leaks


def test_runstats_and_page_show_the_same_numbers(env):
    from fitnessbot.running import telegram as run_tg
    _seed_history(env)
    tg = run_tg.run_stats(1).text
    html = _get("/running").text
    view = stats.present(stats.user_summary(1), "km")

    assert view["week_distance"] == "22.00 km" and view["week_runs"] == 2
    assert view["four_week_avg"] == "6.50 km" and view["streak"] == 3
    assert "above your 4-week average" in view["load_note"]
    assert [(p["distance"], p["time"]) for p in view["prs"]] == [("5K", "25:00"), ("10K", "49:10")]

    for key in ("week_distance", "four_week_avg", "streak", "load_note"):
        page_val = re.search(rf'data-stat="{key}">([^<]+)<', html).group(1).strip()
        assert page_val == str(view[key])
        assert str(view[key]) in tg
    for p in view["prs"]:
        assert re.search(rf'data-pr="{p["distance"]}">([^<]+)<', html).group(1) == p["time"]
        assert f"{p['distance']}: {p['time']}" in tg
    assert "4-week average: 6.50 km/week" in tg
    assert "21.10 km" not in html


def test_runstats_without_runs(env):
    from fitnessbot.running import telegram as run_tg
    assert "No runs logged yet" in run_tg.run_stats(1).text
    assert "No runs yet" in _get("/running").text


def test_page_requires_login_and_hides_other_users_runs(env):
    _seed_history(env)
    assert _get("/running", user_id=None).status_code == 303
    other = env.list_runs(2)[0]["run_id"]
    assert _get(f"/running/{other}").status_code == 404


def test_history_filters(env):
    _seed_history(env)
    html = _get("/running?type=race").text
    history = html.split('data-testid="history"')[1]
    assert "10.00 km" in history and "12.00 km" not in history
    html = _get("/running?start=2026-11-10&end=2026-11-16&type=bogus").text
    history = html.split('data-testid="history"')[1]
    assert "5.00 km" in history and "10.00 km" in history and "8.00 km" not in history


def test_run_detail_shows_splits_and_zones(env):
    splits = [{"distance_m": 1000, "duration_s": 300, "avg_hr": 140},
              {"distance_m": 1000, "duration_s": 290, "avg_hr": 176}]
    rid = _save(env, 1, "2026-11-17", 2, 590, splits=splits, max_hr=190)
    html = _get(f"/running/{rid}").text
    assert 'data-testid="splits"' in html and "4:50/km" in html
    assert 'data-testid="zones"' in html and "max HR of 190" in html


def test_miles_users_see_miles(env):
    from fitnessbot import db
    db.update_user(1, run_units="mi")
    _save(env, 1, "2026-11-17", 1.609344 * 3, 1620)
    assert "3.00 mi" in _get("/running").text


def test_save_announces_new_pr(env):
    from fitnessbot.running import telegram as run_tg
    _save(env, 1, "2026-11-10", 5, 1690)
    with patch("fitnessbot.running.telegram.user_today", return_value="2026-11-17"), \
         patch("fitnessbot.running.telegram._calendar_entry", return_value=None):
        draft_id = env.create_draft(1, {"date": "2026-11-17", "distance_m": 5000, "duration_s": 1650})
        text = run_tg.handle_callback(1, f"run_save:{draft_id}", {}).text
        assert "\U0001F3C6 New 5K PR: 27:30 (was 28:10)" in text
        draft_id = env.create_draft(1, {"date": "2026-11-16", "distance_m": 5000, "duration_s": 1700})
        assert "PR" not in run_tg.handle_callback(1, f"run_save:{draft_id}", {}).text
