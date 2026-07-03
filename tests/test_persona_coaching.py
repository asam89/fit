"""Tests for persona composition helper and adaptive coaching features."""
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from fitnessbot.ai.prompts import (
    COACH_PERSONA, compose_prompt,
    TASK_COACHING_REPLY, TASK_QUERY_RESPONSE,
    TASK_GOAL_FIT_CHECK, TASK_WORKOUT_EXPLAINER,
)


class TestComposePrompt:
    """Tests for the compose_prompt helper."""

    def test_default_neutral_tone(self):
        result = compose_prompt("Do the task.")
        assert COACH_PERSONA in result
        assert "Do the task." in result
        # Neutral has no tone modifier
        assert "Tone preference: BLUNT" not in result
        assert "Tone preference: SUPPORTIVE" not in result

    def test_blunt_tone(self):
        result = compose_prompt("Do the task.", tone_pref="blunt")
        assert "Tone preference: BLUNT" in result
        assert "no-BS feedback" in result

    def test_supportive_tone(self):
        result = compose_prompt("Do the task.", tone_pref="supportive")
        assert "Tone preference: SUPPORTIVE" in result
        assert "encouragement" in result

    def test_performance_signal_included(self):
        result = compose_prompt("Do the task.", performance_signal="calories exceeded today")
        assert "calories exceeded today" in result
        assert "Performance context:" in result

    def test_performance_signal_empty(self):
        result = compose_prompt("Do the task.", performance_signal="")
        assert "Performance context:" not in result

    def test_all_options(self):
        result = compose_prompt(
            "Reply to user.",
            tone_pref="blunt",
            performance_signal="weight trending up",
        )
        assert COACH_PERSONA in result
        assert "Tone preference: BLUNT" in result
        assert "weight trending up" in result
        assert "Reply to user." in result

    def test_task_instructions_in_task_section(self):
        result = compose_prompt("Check their macros.")
        assert "--- TASK ---" in result
        assert "Check their macros." in result

    def test_persona_safety_boundaries(self):
        assert "Never provide medical advice" in COACH_PERSONA
        assert "Never mock, shame" in COACH_PERSONA
        assert "tone label" in COACH_PERSONA


class TestTaskPrompts:
    """Verify task prompt constants exist and have key content."""

    def test_coaching_reply_prompt(self):
        assert "SHORT reply" in TASK_COACHING_REPLY
        assert "2-4 lines" in TASK_COACHING_REPLY

    def test_query_response_prompt(self):
        assert "ONLY on the data provided" in TASK_QUERY_RESPONSE

    def test_goal_fit_check_prompt(self):
        assert "align" in TASK_GOAL_FIT_CHECK
        assert "better-fitting alternatives" in TASK_GOAL_FIT_CHECK

    def test_workout_explainer_prompt(self):
        assert "Moving better" in TASK_WORKOUT_EXPLAINER
        assert "Hip mobility" in TASK_WORKOUT_EXPLAINER
        assert "Core strength" in TASK_WORKOUT_EXPLAINER
        assert "WHY" in TASK_WORKOUT_EXPLAINER


class TestTonePreferenceMigration:
    """Test that tone preference column exists and defaults properly."""

    def test_migration_adds_column(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            db_mod.init_db()
            db_mod.run_migrations()
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # Insert a user
            conn.execute(
                "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
                ("test@test.com", "hash", "Test"),
            )
            conn.commit()
            row = conn.execute("SELECT feedback_tone_preference FROM users WHERE email = ?", ("test@test.com",)).fetchone()
            # New users default to 'neutral'
            assert row["feedback_tone_preference"] == "neutral"
            conn.close()

    def test_update_user_accepts_tone(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with patch("fitnessbot.db.get_db_path", return_value=db_path):
            from fitnessbot import db as db_mod
            db_mod.init_db()
            db_mod.run_migrations()
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
                ("test2@test.com", "hash", "Test2"),
            )
            conn.commit()
            conn.close()
            # update_user should accept feedback_tone_preference
            db_mod.update_user(1, feedback_tone_preference="blunt")
            user = db_mod.get_user_by_id(1)
            assert user["feedback_tone_preference"] == "blunt"


class TestGoalFitDetection:
    """Test goal-fit query detection patterns."""

    def test_detects_goal_fit_questions(self):
        from fitnessbot.bot.conversation import _is_goal_fit_query
        assert _is_goal_fit_query("does basketball fit my goal")
        assert _is_goal_fit_query("Does running align with my goals?")
        assert _is_goal_fit_query("is this the right workout for my goal")

    def test_rejects_non_goal_fit(self):
        from fitnessbot.bot.conversation import _is_goal_fit_query
        assert not _is_goal_fit_query("I ate a sandwich")
        assert not _is_goal_fit_query("how am I doing today")


class TestWorkoutExplainerDetection:
    """Test workout explainer query detection patterns."""

    def test_detects_explainer_questions(self):
        from fitnessbot.bot.conversation import _is_workout_explainer_query
        assert _is_workout_explainer_query("what are some mobility workouts for hip pain")
        assert _is_workout_explainer_query("give me core strength exercises for stability")
        assert _is_workout_explainer_query("suggest workouts for strength")

    def test_detects_category_mentions(self):
        from fitnessbot.bot.conversation import _is_workout_explainer_query
        assert _is_workout_explainer_query("hip mobility")
        assert _is_workout_explainer_query("core strength")

    def test_rejects_non_explainer(self):
        from fitnessbot.bot.conversation import _is_workout_explainer_query
        assert not _is_workout_explainer_query("I did 30 pushups")
        assert not _is_workout_explainer_query("how am I doing")


class TestToneChangeDetection:
    """Test natural language tone change detection."""

    def test_detects_blunt_requests(self):
        from fitnessbot.bot.conversation import _TONE_CHANGE_PAT
        assert _TONE_CHANGE_PAT.search("be more blunt with me")
        assert _TONE_CHANGE_PAT.search("be tough on me")
        assert _TONE_CHANGE_PAT.search("I want direct feedback")
        assert _TONE_CHANGE_PAT.search("give me no-bs feedback")

    def test_detects_supportive_requests(self):
        from fitnessbot.bot.conversation import _TONE_CHANGE_PAT
        assert _TONE_CHANGE_PAT.search("be more gentle")
        assert _TONE_CHANGE_PAT.search("go easy on me")
        assert _TONE_CHANGE_PAT.search("be supportive")
        assert _TONE_CHANGE_PAT.search("I prefer encouraging feedback")

    def test_detects_neutral_requests(self):
        from fitnessbot.bot.conversation import _TONE_CHANGE_PAT
        assert _TONE_CHANGE_PAT.search("be balanced")
        assert _TONE_CHANGE_PAT.search("switch to neutral")

    def test_rejects_non_tone(self):
        from fitnessbot.bot.conversation import _TONE_CHANGE_PAT
        assert not _TONE_CHANGE_PAT.search("I ate a salad")
        assert not _TONE_CHANGE_PAT.search("how am I doing today")

    def test_fast_path_maps_to_correct_tone(self):
        from fitnessbot.bot.conversation import _fast_path_intents
        result = _fast_path_intents("be more blunt with me", None)
        assert result is not None
        assert result[0]["type"] == "tone_change"
        assert result[0]["tone"] == "blunt"

        result = _fast_path_intents("go easy on me", None)
        assert result is not None
        assert result[0]["tone"] == "supportive"

        result = _fast_path_intents("switch to neutral", None)
        assert result is not None
        assert result[0]["tone"] == "neutral"
