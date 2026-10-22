"""Workstream B: run text parser, guardrails and the draft -> Save Telegram flow."""
import sqlite3
from datetime import date
from unittest.mock import patch

import pytest

from fitnessbot.running.parser import guardrail_questions, looks_like_run, parse_run

TODAY = date(2026, 10, 21)  # a Wednesday
MI = 1609.344


def _p(text, units="km"):
    return parse_run(text, today=TODAY, default_units=units)


@pytest.mark.parametrize("text,dist_m,dur_s", [
    ("ran 5k in 27:30", 5000, 1650),
    ("10.2 km easy run 58 min avg hr 148", 10200, 3480),
    ("treadmill 30 min 5.1 km", 5100, 1800),
    ("intervals 6x800 at 3:55 pace", 4800, 1128),
    ("Ran 5K 27:30", 5000, 1650),
    ("ran 5 km in 27 min", 5000, 1620),
    ("rann 5km 27min", 5000, 1620),
    ("runn 8k 45 mins", 8000, 2700),
    ("went for a run, 6.5 kms, 38:20", 6500, 2300),
    ("jogged 3 miles in 30 minutes", 3 * MI, 1800),
    ("ran 3.1 mi 28:45", 3.1 * MI, 1725),
    ("ran 2 mile 18 min", 2 * MI, 1080),
    ("long run 18k 1:52:10", 18000, 6730),
    ("ran 10k in 1h05", 10000, 3900),
    ("ran 10k 1 hr 2 min", 10000, 3720),
    ("ran 21.1km in 2:15:30", 21100, 8130),
    ("half marathon 2:15:30", 21097.5, 8130),
    ("ran a marathon in 4:30:00", 42195, 16200),
    ("ran 5,5 km in 31:00", 5500, 1860),
    ("tempo run 8 km at 5:10/km", 8000, 2480),
    ("ran 5k at 5:30 min/km", 5000, 1650),
    ("ran 3 miles at 9:00/mi", 3 * MI, 1620),
    ("ran 40 min at 6:00 pace", 6666.7, 2400),
    ("parkrun 5k 29:12", 5000, 1752),
    ("trail run 12 km 1:20:00", 12000, 4800),
    ("ran 5k in 27:30 yesterday", 5000, 1650),
    ("easy jog 4km 26 min", 4000, 1560),
    ("ran 1.5 hours 14 km", 14000, 5400),
    ("ran 400m 1:30", 400, 90),
    ("10k race 52:03", 10000, 3123),
    ("ran 5k in 27:30 hr 150 max hr 172", 5000, 1650),
])
def test_parser_variants(text, dist_m, dur_s):
    assert looks_like_run(text)
    d = _p(text)
    assert d["distance_m"] == pytest.approx(dist_m, rel=0.002)
    assert d["duration_s"] == pytest.approx(dur_s, abs=1)
    assert d["missing"] == []


@pytest.mark.parametrize("text", [
    "ran out of eggs", "ran errands for 2 hours", "running late today", "ran into an old friend",
    "2 eggs and toast for breakfast", "weight 230", "did a 30 min brisk walk", "run the dishwasher",
])
def test_not_a_run(text):
    assert not looks_like_run(text)


def test_bare_number_uses_run_units():
    assert _p("ran 5 in 45:00", units="mi")["distance_m"] is None  # no unit in free text -> ask
    from fitnessbot.running.parser import parse_distance
    assert parse_distance("5", "mi") == pytest.approx(5 * MI)
    assert parse_distance("5", "km") == 5000


def test_missing_fields_reported():
    assert _p("went for a run 30 min")["missing"] == ["distance_m"]
    assert _p("ran 5k")["missing"] == ["duration_s"]
    assert _p("intervals 6x800")["missing"] == ["duration_s"]


def test_types_surfaces_and_extras():
    d = _p("intervals 6x800 at 3:55 pace")
    assert d["run_type"] == "interval" and len(d["splits"]) == 6
    assert d["splits"][0] == {"distance_m": 800.0, "duration_s": 188.0}
    t = _p("treadmill 30 min 5.1 km")
    assert t["run_type"] == "treadmill" and t["surface"] == "treadmill"
    h = _p("10.2 km easy run 58 min avg hr 148 max hr 171 rpe 4")
    assert (h["run_type"], h["avg_hr"], h["max_hr"], h["perceived_effort"]) == ("easy", 148, 171, 4)
    assert _p("trail run 12 km 1:20:00")["surface"] == "trail"
    assert _p("long run 18k 1:52:10")["run_type"] == "long"


@pytest.mark.parametrize("text,expected", [
    ("ran 5k in 27:30", "2026-10-21"),
    ("ran 5k in 27:30 yesterday", "2026-10-20"),
    ("ran 5k in 27:30 on 2026-10-12", "2026-10-12"),
    ("ran 5k in 27:30 on oct 12", "2026-10-12"),
    ("ran 5k in 27:30 on saturday", "2026-10-17"),
    ("ran 5k in 27:30 on wednesday", "2026-10-14"),
    ("half marathon 2:15:30", "2026-10-21"),
    ("ran 5k dec 30 27:30", "2025-12-30"),
])
def test_dates(text, expected):
    assert _p(text)["date"] == expected


def test_guardrails():
    assert guardrail_questions(_p("ran 5k in 27:30")) == []
    assert "world-record" in guardrail_questions(_p("ran 10k in 12:00"))[0]
    assert "slower than a walk" in guardrail_questions(_p("ran 2k in 45 min"))[0]
    assert any("100 km" in q for q in guardrail_questions(_p("ran 120 km in 11:00:00")))
    assert any("12 hours" in q for q in guardrail_questions(_p("ran 101 km in 13:00:00")))


# --- Telegram flow (real SQLite) -------------------------------------------------

@pytest.fixture
def tg(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        db.init_db()
        db.run_migrations()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO users (user_id, email, password_hash, display_name, timezone) "
                     "VALUES (1, 'a@t', 'h', 'A', 'America/Toronto')")
        conn.commit()
        conn.close()
        from fitnessbot.running import store, telegram
        with patch("fitnessbot.running.telegram.user_today", return_value="2026-10-21"):
            yield telegram, store, db_path


def _draft_id(reply, action="run_save"):
    return next(int(d.split(":")[1]) for row in reply.buttons for _, d in row if d.startswith(action))


def _count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_card_then_save(tg):
    telegram, store, db_path = tg
    state = {}
    reply = telegram.handle_text(1, "ran 5k in 27:30", state)
    assert "5.00 km" in reply.text and "27:30" in reply.text and "5:30/km" in reply.text
    assert "Nothing is saved" in reply.text
    assert [b[0] for b in reply.buttons[0]] == ["Save", "Edit", "Cancel"]
    assert _count(db_path, "runs") == 0
    saved = telegram.handle_callback(1, f"run_save:{_draft_id(reply)}", state)
    assert saved.text.startswith("\u2705 Saved: 5.00 km in 27:30")
    run = store.latest_run(1)
    assert run["source"] == "telegram" and run["local_date"] == "2026-10-21"
    assert run["plan_item_id"]  # shows on the training calendar
    assert _count(db_path, "run_drafts") == 0


def test_double_tap_save_saves_once(tg):
    telegram, store, db_path = tg
    reply = telegram.handle_text(1, "ran 5k in 27:30", {})
    did = _draft_id(reply)
    telegram.handle_callback(1, f"run_save:{did}", {})
    again = telegram.handle_callback(1, f"run_save:{did}", {})
    assert "already handled" in again.text
    assert _count(db_path, "runs") == 1


def test_cancel_saves_nothing(tg):
    telegram, store, db_path = tg
    reply = telegram.handle_text(1, "ran 5k in 27:30", {})
    out = telegram.handle_callback(1, f"run_cancel:{_draft_id(reply)}", {})
    assert "Nothing was saved" in out.text
    assert _count(db_path, "runs") == 0 and _count(db_path, "run_drafts") == 0


def test_edit_then_save(tg):
    telegram, store, db_path = tg
    state = {}
    reply = telegram.handle_text(1, "ran 5k in 27:30", state)
    telegram.handle_callback(1, f"run_edit:{_draft_id(reply)}", state)
    edited = telegram.handle_text(1, "28:10 yesterday tempo", state)
    assert "28:10" in edited.text and "Tue, Oct 20" in edited.text and "tempo" in edited.text
    telegram.handle_callback(1, f"run_save:{_draft_id(edited)}", state)
    run = store.latest_run(1)
    assert (run["duration_s"], run["local_date"], run["run_type"]) == (1690, "2026-10-20", "tempo")


def test_guardrail_card_asks_before_saving(tg):
    telegram, store, db_path = tg
    reply = telegram.handle_text(1, "ran 10k in 12:00", {})
    assert "\u26A0" in reply.text and reply.buttons[0][0][0] == "Yes, save it"
    assert _count(db_path, "runs") == 0


def test_missing_distance_asks_then_cards(tg):
    telegram, store, db_path = tg
    state = {}
    ask = telegram.handle_text(1, "went for a run 30 min", state)
    assert "How far" in ask.text
    card = telegram.handle_text(1, "5k", state)
    assert "5.00 km" in card.text and "30:00" in card.text


def test_guided_run_command(tg):
    telegram, store, db_path = tg
    state = {}
    assert "How far" in telegram.start_guided(1, state).text
    assert "How long" in telegram.handle_text(1, "5", state).text
    typ = telegram.handle_text(1, "27:30", state)
    assert "What kind" in typ.text
    card = telegram.handle_callback(1, "run_type:easy", state)
    assert "Type: easy" in card.text
    telegram.handle_callback(1, f"run_save:{_draft_id(card)}", state)
    assert store.latest_run(1)["run_type"] == "easy"


def test_cancel_word_ends_flow(tg):
    telegram, store, db_path = tg
    state = {}
    telegram.start_guided(1, state)
    assert "cancelled" in telegram.handle_text(1, "cancel", state).text
    assert telegram.handle_text(1, "2 eggs for breakfast", state) is None


def test_non_run_falls_through(tg):
    telegram, _, _ = tg
    assert telegram.handle_text(1, "2 eggs and toast for breakfast", {}) is None
    assert telegram.handle_text(1, "new PB 5K 24:30", {}) is None


def test_handler_markup():
    from fitnessbot.bot.handlers import _run_markup
    from fitnessbot.running.telegram import Reply
    assert _run_markup(Reply("x")) is None
    m = _run_markup(Reply("x", [[("Save", "run_save:1"), ("Cancel", "run_cancel:1")]]))
    assert [b.callback_data for b in m.inline_keyboard[0]] == ["run_save:1", "run_cancel:1"]


def test_miles_display(tg):
    telegram, store, db_path = tg
    from fitnessbot import db
    db.update_user(1, run_units="mi")
    reply = telegram.handle_text(1, "ran 5k in 27:30", {})
    assert "3.11 mi" in reply.text and "/mi" in reply.text


def test_runs_editrun_deleterun(tg):
    telegram, store, db_path = tg
    assert "No runs" in telegram.list_recent(1).text
    for t in ("ran 5k in 27:30", "ran 8k in 45:00 yesterday"):
        telegram.handle_callback(1, f"run_save:{_draft_id(telegram.handle_text(1, t, {}))}", {})
    listing = telegram.list_recent(1).text
    assert "Last 2 runs" in listing and listing.index("5.00 km") < listing.index("8.00 km")

    state = {}
    assert "Your latest run" in telegram.edit_latest(1, state).text
    card = telegram.handle_text(1, "5.2 km", state)
    assert "Update this run?" in card.text
    telegram.handle_callback(1, f"run_save:{_draft_id(card)}", state)
    assert store.latest_run(1)["distance_m"] == 5200
    assert _count(db_path, "runs") == 2

    confirm = telegram.delete_latest(1)
    rid = _draft_id(confirm, "run_del")
    assert "Deleted" in telegram.handle_callback(1, f"run_del:{rid}", {}).text
    assert _count(db_path, "runs") == 1
    assert _count(db_path, "training_plan_items") == 1
