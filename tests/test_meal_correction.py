"""Corrections rewrite the last meal in place with the full corrected item list."""
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def meal_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        db.init_db()
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
        conn.close()
        db.run_migrations()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO users (user_id, email, password_hash, display_name, timezone) VALUES (1, ?, ?, ?, ?)",
            ("t@t.com", "hash", "T", "America/Toronto"),
        )
        conn.commit()
        conn.close()
        yield db, db_path


EGG = {"name": "egg", "qty": 1, "unit": "large", "calories": 72, "protein": 6.3, "carbs": 0.4,
       "fat": 4.8, "fiber": 0, "sugar": 0.2, "sodium": 71}
TOAST = {"name": "toast", "qty": 1, "unit": "slice", "calories": 80, "protein": 3, "carbs": 14,
         "fat": 1, "fiber": 2, "sugar": 1.5, "sodium": 150}


def test_correction_replaces_items_and_totals(meal_db):
    db, db_path = meal_db
    from fitnessbot.ai.food_parser import log_meal_from_parsed
    from fitnessbot.bot.conversation import _act_correction

    meal = log_meal_from_parsed(1, "2 eggs and toast", [{**EGG, "qty": 2, "calories": 144}, TOAST])
    three_eggs = {**EGG, "qty": 3, "calories": 216, "protein": 18.9}

    with patch("fitnessbot.bot.conversation.parse_meal", return_value=[three_eggs, TOAST]) as parse:
        result = _act_correction({"type": "correction", "what": "make it 3 eggs"}, 1, "imperial")

    sent = parse.call_args.args[0]
    assert "2 eggs and toast" in sent and "make it 3 eggs" in sent
    assert result["action"] == "correction_applied"
    assert result["meal_id"] == meal["meal_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    meals = conn.execute("SELECT * FROM meals").fetchall()
    assert len(meals) == 1
    assert meals[0]["total_calories"] == pytest.approx(296)
    assert meals[0]["total_protein"] == pytest.approx(21.9)
    assert meals[0]["total_fiber"] == pytest.approx(2)
    assert meals[0]["total_sodium"] == pytest.approx(221)
    items = conn.execute(
        "SELECT mi.qty, f.name FROM meal_items mi JOIN foods f ON f.food_id = mi.food_id ORDER BY mi.item_id"
    ).fetchall()
    assert [(i["name"], i["qty"]) for i in items] == [("egg", 3), ("toast", 1)]
    conn.close()


def test_act_failure_does_not_leak_exception_text(meal_db):
    from fitnessbot.bot.conversation import _act_on_intents
    with patch("fitnessbot.bot.conversation._act_correction", side_effect=RuntimeError("no such column: name")):
        results = _act_on_intents([{"type": "correction", "what": "x"}], 1, "x")
    assert results[0]["action"] == "error"
    assert "column" not in results[0]["error"]
