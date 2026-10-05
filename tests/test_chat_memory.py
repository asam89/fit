"""Telegram conversation memory: turns are stored and fed back to the model."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mem_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    with patch("fitnessbot.db.get_db_path", return_value=db_path):
        from fitnessbot import db
        # Fresh-DB SCHEMA_SQL lags the migrations (e.g. meals.total_fiber), so
        # replay them from scratch like a long-lived install would have.
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


class FakeLLM:
    """Stand-in for get_inference(): NLU calls classify as general chat."""

    def __init__(self, intents=None):
        self.calls = []
        self.intents = intents or [{"type": "general", "text": "", "confidence": 0.9}]

    def __call__(self, *, system, messages, max_tokens=1024, json_mode=False):
        self.calls.append({"system": system, "content": messages[-1]["content"], "json_mode": json_mode})
        if json_mode:
            return {"text": json.dumps({"intents": self.intents})}
        return {"text": f"Coach reply {len(self.calls)}"}

    def nlu_calls(self):
        return [c for c in self.calls if c["json_mode"]]

    def reply_calls(self):
        return [c for c in self.calls if not c["json_mode"]]


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_turns_round_trip_oldest_first_with_limit_and_window(mem_db):
    db, db_path = mem_db
    for i in range(5):
        db.insert_chat_turn(1, "user" if i % 2 == 0 else "assistant", f"m{i}")
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE chat_turns SET created_at = ? WHERE text = 'm0'", (old,))
    conn.commit()
    conn.close()

    turns = db.get_recent_chat_turns(1, limit=3, since_hours=12)
    assert [t["text"] for t in turns] == ["m2", "m3", "m4"]
    assert [t["text"] for t in db.get_recent_chat_turns(1, limit=10, since_hours=12)] == ["m1", "m2", "m3", "m4"]


def test_turns_past_retention_are_pruned_and_reset_clears(mem_db):
    db, db_path = mem_db
    db.insert_chat_turn(1, "user", "ancient")
    stale = (datetime.now(timezone.utc) - timedelta(days=db.CHAT_RETENTION_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE chat_turns SET created_at = ?", (stale,))
    conn.commit()
    conn.close()

    db.insert_chat_turn(1, "user", "fresh")
    conn = sqlite3.connect(db_path)
    assert [r[0] for r in conn.execute("SELECT text FROM chat_turns")] == ["fresh"]
    conn.close()

    assert db.clear_chat_turns(1) == 1
    assert db.get_recent_chat_turns(1, since_hours=24 * 365) == []


def test_follow_up_sees_previous_user_message_and_bot_reply(mem_db):
    db, _ = mem_db
    from fitnessbot.bot.conversation import process_message
    llm = FakeLLM()
    with patch("fitnessbot.inference.factory.get_inference", return_value=llm):
        first = _run(process_message(1, "my knee feels a bit sore after yesterday"))
        _run(process_message(1, "should I still go tonight?"))

    assert "[View dashboard]" in first
    nlu2, reply2 = llm.nlu_calls()[1], llm.reply_calls()[1]
    for content in (nlu2["content"], reply2["content"]):
        assert "RECENT CONVERSATION" in content
        assert "User: my knee feels a bit sore after yesterday" in content
        assert "Coach: Coach reply 2" in content
        assert "View dashboard" not in content
    assert "RECENT CONVERSATION" not in llm.nlu_calls()[0]["content"]
    assert "RECENT CONVERSATION" in reply2["system"]

    turns = db.get_recent_chat_turns(1)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "my knee feels a bit sore after yesterday"),
        ("assistant", "Coach reply 2"),
        ("user", "should I still go tonight?"),
        ("assistant", "Coach reply 4"),
    ]


def test_chat_message_gets_a_model_reply_not_a_canned_ack(mem_db):
    from fitnessbot.bot.conversation import process_message
    llm = FakeLLM()
    with patch("fitnessbot.inference.factory.get_inference", return_value=llm):
        reply = _run(process_message(1, "thanks, that helps"))
    assert reply.startswith("Coach reply")
    assert len(llm.reply_calls()) == 1


def test_scheduled_message_joins_the_conversation(mem_db):
    db, _ = mem_db
    from fitnessbot import briefings
    from fitnessbot.bot.conversation import process_message

    resp = MagicMock(status_code=200)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    with patch.object(briefings.db, "get_telegram_connection", return_value={"bot_token_encrypted": "x", "chat_id": 5}), \
         patch.object(briefings, "decrypt_token", return_value="tok"), \
         patch("httpx.AsyncClient", return_value=client):
        assert _run(briefings._send_telegram(1, "How did you sleep last night?\n\n[View dashboard](https://x/dashboard)", kind="morning"))

    turns = db.get_recent_chat_turns(1)
    assert [(t["role"], t["kind"], t["text"]) for t in turns] == [
        ("assistant", "morning", "How did you sleep last night?"),
    ]

    llm = FakeLLM()
    with patch("fitnessbot.inference.factory.get_inference", return_value=llm):
        _run(process_message(1, "pretty rough honestly"))
    assert "Coach (morning briefing): How did you sleep last night?" in llm.nlu_calls()[0]["content"]


def test_failed_send_is_not_remembered(mem_db):
    db, _ = mem_db
    from fitnessbot import briefings
    with patch.object(briefings.db, "get_telegram_connection", return_value=None):
        assert not _run(briefings._send_telegram(1, "hello"))
    assert db.get_recent_chat_turns(1) == []


def test_remember_false_skips_memory(mem_db):
    db, _ = mem_db
    from fitnessbot.bot.conversation import process_message
    with patch("fitnessbot.inference.factory.get_inference", return_value=FakeLLM()):
        _run(process_message(1, "[HEALTH_SYNC] steps 8000", remember=False))
    assert db.get_recent_chat_turns(1) == []
