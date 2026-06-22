"""Telegram bot handlers: text, voice, commands, callbacks."""

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
from fitnessbot.ai.food_parser import parse_meal, log_meal_from_parsed
from fitnessbot.router import classify_message
from fitnessbot.metrics import log_weight, get_weight_summary
from fitnessbot.voice import download_voice_file, transcribe_audio

logger = logging.getLogger(__name__)


def register_handlers(app: Application, user_id: int) -> None:
    """Register all handlers for a user's bot instance."""

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = db.get_user_by_id(user_id)
        name = user["display_name"] if user else "there"
        await update.message.reply_text(
            f"Hey {name}! I'm your fitness tracking assistant.\n\n"
            "Send me what you ate (text or voice) and I'll log it with full nutritional info.\n\n"
            "Commands:\n"
            "/today - Today's calories & macros\n"
            "/weight - Weight trend\n"
            "/log <food> - Log a meal\n"
            "/undo - Remove last meal\n"
            "/plan - Current diet plan\n"
            "/goal - Active goals\n"
            "/dashboard - Open web dashboard"
        )

    async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        totals = db.get_today_totals(user_id, today)
        plan = db.get_active_diet_plan(user_id)

        target_cal = plan["daily_calories"] if plan and plan.get("daily_calories") else 2000
        target_pro = plan["daily_protein"] if plan and plan.get("daily_protein") else 140
        target_carb = plan["daily_carbs"] if plan and plan.get("daily_carbs") else 200
        target_fat = plan["daily_fat"] if plan and plan.get("daily_fat") else 60

        remaining = target_cal - totals["calories"]
        msg = (
            f"Today's intake:\n"
            f"Calories: {totals['calories']:.0f} / {target_cal} ({remaining:.0f} remaining)\n"
            f"Protein: {totals['protein']:.0f}g / {target_pro}g\n"
            f"Carbs: {totals['carbs']:.0f}g / {target_carb}g\n"
            f"Fat: {totals['fat']:.0f}g / {target_fat}g"
        )
        await update.message.reply_text(msg)

    async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
        summary = get_weight_summary(user_id)
        if not summary["has_data"]:
            await update.message.reply_text("No weight data yet. Send me your weight like: weight 182")
            return
        lines = [f"Weight trend (smoothed): {summary['current_smoothed']} lbs"]
        if summary.get("trend_7d") is not None:
            direction = "down" if summary["trend_7d"] < 0 else "up"
            lines.append(f"7-day: {abs(summary['trend_7d'])} lbs {direction}")
        if summary.get("trend_30d") is not None:
            direction = "down" if summary["trend_30d"] < 0 else "up"
            lines.append(f"30-day: {abs(summary['trend_30d'])} lbs {direction}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = " ".join(context.args) if context.args else ""
        if not text:
            await update.message.reply_text("Usage: /log <food description>\nExample: /log two eggs and toast")
            return
        await _process_meal(update, text, source="text")

    async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        deleted = db.delete_last_meal(user_id)
        if deleted:
            await update.message.reply_text(f"Deleted last meal: {deleted['raw_text']}")
        else:
            await update.message.reply_text("No meals to undo.")

    async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        plan = db.get_active_diet_plan(user_id)
        if not plan:
            await update.message.reply_text("No active diet plan. Set one up on the dashboard!")
            return
        msg = (
            f"Current diet plan:\n"
            f"Calories: {plan.get('daily_calories', 'N/A')}\n"
            f"Protein: {plan.get('daily_protein', 'N/A')}g\n"
            f"Carbs: {plan.get('daily_carbs', 'N/A')}g\n"
            f"Fat: {plan.get('daily_fat', 'N/A')}g\n"
        )
        if plan.get("rationale_text"):
            msg += f"\n{plan['rationale_text'][:300]}"
        await update.message.reply_text(msg)

    async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
        goals = db.get_active_goals(user_id)
        if not goals:
            await update.message.reply_text("No active goals. Set one up on the dashboard!")
            return
        lines = ["Active goals:"]
        for g in goals:
            line = f"- {g.get('title', g['goal_type'])}"
            if g.get("event_date"):
                days = _days_until(g["event_date"])
                line += f" ({days} days away)" if days > 0 else " (today!)"
            lines.append(line)
        await update.message.reply_text("\n".join(lines))

    async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Open your dashboard at: (configure your domain)")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if not text:
            return
        result = classify_message(text)
        intent = result.get("intent", "other")

        if intent == "meal":
            raw = result.get("extracted", {}).get("raw_text", text)
            await _process_meal(update, raw, source="text")
        elif intent == "metric":
            extracted = result.get("extracted", {})
            metric_type = extracted.get("metric_type", "")
            value = extracted.get("value", "")
            if metric_type in ("weight", "weigh"):
                try:
                    w = float(value)
                    info = log_weight(user_id, w)
                    await update.message.reply_text(
                        f"Logged weight: {info['raw']} lbs\n"
                        f"Smoothed trend: {info['smoothed']} lbs"
                    )
                except ValueError:
                    await update.message.reply_text("Couldn't parse that weight. Try: weight 182")
            else:
                await update.message.reply_text(f"Logged {metric_type}: {value}")
        elif intent == "query":
            await update.message.reply_text("Let me check... (query support coming in Phase 3)")
        elif intent == "goal":
            await update.message.reply_text("Goal noted! Set up goals on the dashboard for full tracking.")
        else:
            # Default: try as a meal
            await _process_meal(update, text, source="text")

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        voice = update.message.voice
        if not voice:
            return
        await update.message.reply_text("Transcribing your voice message...")
        try:
            file = await context.bot.get_file(voice.file_id)
            audio_bytes = await download_voice_file(file.file_path)
            transcript = transcribe_audio(audio_bytes)
            if not transcript:
                await update.message.reply_text("Couldn't transcribe that. Try typing it instead.")
                return
            await update.message.reply_text(f'Heard: "{transcript}"')
            result = classify_message(transcript)
            intent = result.get("intent", "meal")
            if intent == "meal":
                await _process_meal(update, transcript, source="voice")
            else:
                await handle_text.__wrapped__(update, context) if hasattr(handle_text, '__wrapped__') else None
                # Re-process as text
                update.message.text = transcript
                await handle_text(update, context)
        except Exception as e:
            logger.error("Voice processing error: %s", e)
            await update.message.reply_text("Error processing voice message. Try typing it instead.")

    async def _process_meal(update: Update, text: str, source: str = "text"):
        user = db.get_user_by_id(user_id)
        units = user.get("units_pref", "imperial") if user else "imperial"

        await update.message.reply_text("Analyzing your meal...")
        items = parse_meal(text, units_pref=units)
        if not items:
            await update.message.reply_text(
                "Couldn't parse that meal. Try being more specific, e.g.:\n"
                '"two eggs, toast with butter, and a coffee"'
            )
            return

        result = log_meal_from_parsed(user_id, text, items, source=source)

        # Build confirmation card
        lines = []
        for item in result["items"]:
            name = item.get("name", "Unknown")
            cal = item.get("calories", 0)
            p = item.get("protein", 0)
            c = item.get("carbs", 0)
            f = item.get("fat", 0)
            lines.append(f"  {name} — {cal:.0f} cal | P: {p:.0f}g C: {c:.0f}g F: {f:.0f}g")

        lines.append("─" * 30)
        lines.append(
            f"Meal total: {result['total_calories']:.0f} cal | "
            f"P: {result['total_protein']:.0f}g "
            f"C: {result['total_carbs']:.0f}g "
            f"F: {result['total_fat']:.0f}g"
        )

        # Running daily budget
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        totals = db.get_today_totals(user_id, today)
        plan = db.get_active_diet_plan(user_id)
        target = plan["daily_calories"] if plan and plan.get("daily_calories") else 2000
        remaining = target - totals["calories"]

        lines.append("")
        lines.append(f"Today: {totals['calories']:.0f} / {target} cal ({remaining:.0f} remaining)")

        target_pro = plan["daily_protein"] if plan and plan.get("daily_protein") else 140
        target_carb = plan["daily_carbs"] if plan and plan.get("daily_carbs") else 200
        target_fat = plan["daily_fat"] if plan and plan.get("daily_fat") else 60
        lines.append(
            f"P: {totals['protein']:.0f}/{target_pro}g | "
            f"C: {totals['carbs']:.0f}/{target_carb}g | "
            f"F: {totals['fat']:.0f}/{target_fat}g"
        )

        await update.message.reply_text("\n".join(lines))

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("weight", cmd_weight))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("goal", cmd_goal))
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
