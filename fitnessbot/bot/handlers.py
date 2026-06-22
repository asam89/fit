"""Telegram bot handlers: text, voice, commands — wired to the conversational engine."""

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fitnessbot import db
from fitnessbot.bot.conversation import process_message
from fitnessbot.metrics import get_weight_summary
from fitnessbot.voice import download_voice_file, transcribe_audio

logger = logging.getLogger(__name__)


def register_handlers(app: Application, user_id: int) -> None:
    """Register all handlers for a user's bot instance."""

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = db.get_user_by_id(user_id)
        name = user["display_name"] if user else "there"
        await update.message.reply_text(
            f"Hey {name}! I'm your fitness tracking assistant.\n\n"
            "Just talk to me naturally — tell me what you ate, your weight, "
            "how you slept, workouts, anything. I'll track it all.\n\n"
            "Voice notes work too. Or use these shortcuts:\n"
            "/today - Today's intake & macros\n"
            "/weight - Weight trend\n"
            "/undo - Remove last meal\n"
            "/dashboard - Open web dashboard"
        )

    async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
        reply = await process_message(user_id, "how am I doing today?", channel="text")
        await update.message.reply_text(reply)

    async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
        summary = get_weight_summary(user_id)
        if not summary["has_data"]:
            await update.message.reply_text("No weight data yet. Send me your weight like: weight 182")
            return
        lines = [f"Weight (smoothed): {summary['current_smoothed']} lbs"]
        if summary.get("trend_7d") is not None:
            direction = "down" if summary["trend_7d"] < 0 else "up"
            lines.append(f"7-day: {abs(summary['trend_7d']):.1f} lbs {direction}")
        if summary.get("trend_30d") is not None:
            direction = "down" if summary["trend_30d"] < 0 else "up"
            lines.append(f"30-day: {abs(summary['trend_30d']):.1f} lbs {direction}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        deleted = db.delete_last_meal(user_id)
        if deleted:
            await update.message.reply_text(f"Removed last meal: {deleted['raw_text']}")
        else:
            await update.message.reply_text("No meals to undo.")

    async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Open your dashboard at: http://fit.140.238.131.77.nip.io")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if not text:
            return
        reply = await process_message(user_id, text, channel="text")
        await update.message.reply_text(reply)

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        voice = update.message.voice
        if not voice:
            return
        await update.message.reply_text("Transcribing...")
        try:
            file = await context.bot.get_file(voice.file_id)
            audio_bytes = await download_voice_file(file.file_path)
            transcript = transcribe_audio(audio_bytes)
            if not transcript:
                await update.message.reply_text("Couldn't transcribe that. Try typing it instead or resend.")
                return
            echo = f'Heard: "{transcript}"'
            reply = await process_message(user_id, transcript, channel="voice")
            await update.message.reply_text(f"{echo}\n\n{reply}")
        except Exception as e:
            logger.error("Voice processing error: %s", e)
            await update.message.reply_text("Error processing voice message. Try typing it instead.")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("weight", cmd_weight))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))


def _days_until(date_str: str) -> int:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return (target - now).days
    except ValueError:
        return -1
