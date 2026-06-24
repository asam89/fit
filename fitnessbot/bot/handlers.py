"""Telegram bot handlers: text, voice, photo, commands — wired to the conversational engine."""

import json
import logging
import secrets
import traceback
from datetime import datetime, timezone

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fitnessbot import db, training_plan
from fitnessbot.bot.conversation import process_message
from fitnessbot.config import Config
from fitnessbot.metrics import get_weight_summary
from fitnessbot.voice import download_voice_file, transcribe_audio

logger = logging.getLogger(__name__)

MEAL_TYPES = ["Breakfast", "Lunch", "Snack", "Dinner", "Midnight Snack"]

PHOTO_MEAL_SYSTEM = """You are a nutrition analyst. Given a photo of food, identify each item and estimate macros.

Return ONLY a JSON object:
{
  "items": [
    {"name": "food name", "quantity": "portion estimate", "calories": int, "protein": float, "carbs": float, "fat": float, "fiber": float, "sugar": float, "sodium": float}
  ],
  "total_calories": int,
  "total_protein": float,
  "total_carbs": float,
  "total_fat": float,
  "total_fiber": float,
  "total_sugar": float,
  "total_sodium": float,
  "description": "brief 1-line description of the meal"
}

Sodium is in milligrams. All other macros are in grams.
Be practical with portions — estimate based on typical serving sizes visible in the photo.
If you can't identify a food clearly, make your best educated guess and note uncertainty in the name (e.g. "sauce (estimated)").
All numbers should be reasonable real-world values. Err on the side of accuracy over precision."""


def register_handlers(app: Application, user_id: int) -> None:
    """Register all handlers for a user's bot instance."""

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = db.get_user_by_id(user_id)
        name = user["display_name"] if user else "there"
        await update.message.reply_text(
            f"Hey {name}! I'm your fitness tracking assistant.\n\n"
            "Just talk to me naturally — tell me what you ate, your weight, "
            "how you slept, workouts, anything. I'll track it all.\n\n"
            "Voice notes work too. Or send a photo of your meal!\n\n"
            "Shortcuts:\n"
            "/today - Today's intake & macros\n"
            "/weight - Weight trend\n"
            "/undo - Remove last meal\n"
            "/plan - Training plan\n"
            "/invite - Generate invite link\n"
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
        recent = db.get_recent_meals(user_id, limit=5)
        if not recent:
            await update.message.reply_text("No meals to delete.")
            return
        lines = ["Which meal do you want to delete?\n"]
        buttons = []
        for m in recent:
            desc = m["raw_text"][:40] + ("..." if len(m["raw_text"]) > 40 else "")
            cal = int(m.get("total_calories", 0) or 0)
            mtype = (m.get("meal_type") or "meal").title()
            time_str = m["logged_at"][11:16] if m.get("logged_at") else ""
            lines.append(f"\u2022 {mtype} {time_str}: {desc} ({cal} cal)")
            buttons.append([InlineKeyboardButton(
                f"\U0001F5D1 {mtype} {time_str} \u2014 {desc}",
                callback_data=f"meal_del:{m['meal_id']}",
            )])
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"Open your dashboard at: {Config.BASE_URL}")

    async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = training_plan.format_plan_telegram(user_id)
        today_items = training_plan.get_items_for_date(
            user_id, datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        incomplete = [i for i in today_items if i["status"] == "planned" and i["activity_type"] != "rest"]
        if incomplete:
            buttons = []
            for item in incomplete[:4]:
                buttons.append(InlineKeyboardButton(
                    f"\u2713 {item['title']}",
                    callback_data=f"plan_done:{item['item_id']}",
                ))
            rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        if not data or not data.startswith("plan_done:"):
            return
        await query.answer()
        item_id = int(data.split(":")[1])
        result = training_plan.complete_item(item_id, user_id)
        if result:
            title = result.get("title", "activity")
            today_items = training_plan.get_items_for_date(
                user_id, datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )
            done_count = sum(1 for i in today_items if i["status"] == "completed")
            total_count = sum(1 for i in today_items if i["activity_type"] != "rest")
            reply = f"\u2713 {title} marked done! {done_count}/{total_count} today."
            dur = result.get("planned_duration_min")
            if dur:
                reply += f"\n\nDid you do the full {dur} minutes, or want to adjust?"
            await query.edit_message_text(reply)
        else:
            await query.edit_message_text("Could not find that activity.")

    async def handle_meal_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        if not data or not data.startswith("meal_type:"):
            return
        await query.answer()
        parts = data.split(":")
        meal_id = int(parts[1])
        new_type = parts[2]
        if db.update_meal_type(meal_id, user_id, new_type):
            await query.edit_message_text(
                query.message.text + f"\n\n\u2705 Changed to {new_type.title()}"
            )
        else:
            await query.edit_message_text(query.message.text + "\n\nCouldn't update meal type.")

    async def handle_meal_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        if not data or not data.startswith("meal_del:"):
            return
        await query.answer()
        meal_id = int(data.split(":")[1])
        deleted = db.delete_meal_by_id(meal_id, user_id)
        if deleted:
            desc = deleted["raw_text"][:50]
            cal = int(deleted.get("total_calories", 0) or 0)
            await query.edit_message_text(f"\U0001F5D1 Deleted: {desc} ({cal} cal)")
        else:
            await query.edit_message_text("Meal not found or already deleted.")

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

    async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = secrets.token_urlsafe(12)
        db.create_invite_link(user_id, code)
        link = f"{Config.BASE_URL}/register?invite={code}"
        user = db.get_user_by_id(user_id)
        name = user["display_name"] if user else "Someone"
        await update.message.reply_text(
            f"\U0001F3CB\uFE0F fit-ness.ca Invite Link\n\n{link}\n\n"
            f"Share this with friends to join your fitness network on fit-ness.ca.\n"
            f"They'll be connected to you when they sign up."
        )

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        photo = update.message.photo
        if not photo:
            return
        await update.message.reply_text("Analyzing your meal...")
        try:
            largest = photo[-1]
            file = await context.bot.get_file(largest.file_id)

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(file.file_path)
                image_data = resp.content

            ext = (file.file_path or "").rsplit(".", 1)[-1].lower()
            media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")

            from fitnessbot.inference.factory import get_vision_inference
            from fitnessbot.inference.base import InferenceError
            try:
                vision = get_vision_inference(user_id)
            except InferenceError:
                await update.message.reply_text(
                    "I need an AI key to analyze food photos. Add one in Settings \u2192 AI Providers on the dashboard."
                )
                return

            caption = update.message.caption or ""
            prompt = "Identify the food items in this photo and estimate their nutritional content (calories, protein, carbs, fat, fiber)."
            if caption:
                prompt += f"\n\nAdditional context from user: {caption}"

            result = vision(system=PHOTO_MEAL_SYSTEM, image_data=image_data, media_type=media_type,
                            prompt=prompt, max_tokens=1024, json_mode=True)

            raw_text = result["text"]
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                cleaned = raw_text.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
                data = json.loads(cleaned)

            description = data.get("description", "meal from photo")
            items = data.get("items", [])
            total_cal = data.get("total_calories", 0)
            total_protein = data.get("total_protein", 0)
            total_carbs = data.get("total_carbs", 0)
            total_fat = data.get("total_fat", 0)
            total_fiber = data.get("total_fiber", 0)
            total_sugar = data.get("total_sugar", 0)
            total_sodium = data.get("total_sodium", 0)

            # Save photo to disk
            photo_rel = None
            try:
                Config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                photo_filename = f"{user_id}_{ts}_{secrets.token_hex(4)}.{ext or 'jpg'}"
                photo_disk = Config.UPLOAD_DIR / photo_filename
                photo_disk.write_bytes(image_data)
                photo_rel = f"/uploads/meals/{photo_filename}"
            except Exception:
                logger.warning("Failed to save meal photo to disk", exc_info=True)

            inferred_type = _infer_meal_type()
            meal_id = db.insert_meal(
                user_id=user_id,
                raw_text=f"[photo] {description}",
                meal_type=inferred_type,
                source="photo_vision",
                total_calories=total_cal,
                total_protein=total_protein,
                total_carbs=total_carbs,
                total_fat=total_fat,
                total_fiber=total_fiber,
                total_sugar=total_sugar,
                total_sodium=total_sodium,
                photo_path=photo_rel,
            )
            for item in items:
                food_id = db.insert_food(
                    name=item.get("name", "Unknown"),
                    calories=item.get("calories", 0),
                    protein=item.get("protein", 0),
                    carbs=item.get("carbs", 0),
                    fat=item.get("fat", 0),
                    fiber=item.get("fiber", 0),
                    source="photo_vision",
                )
                db.insert_meal_item(
                    meal_id=meal_id,
                    food_id=food_id,
                    qty=1.0,
                    unit="serving",
                    calories=item.get("calories", 0),
                    protein=item.get("protein", 0),
                    carbs=item.get("carbs", 0),
                    fat=item.get("fat", 0),
                    fiber=item.get("fiber", 0),
                    sugar=item.get("sugar", 0),
                    sodium=item.get("sodium", 0),
                )

            lines = [f"\u2705 Logged as {inferred_type.title()}: {description}\n"]
            for item in items:
                lines.append(f"  \u2022 {item.get('name', 'Item')} ({item.get('quantity', '1 serving')}) \u2014 {item.get('calories', 0)} cal, {item.get('protein', 0)}g protein")
            lines.append(f"\n\U0001F4CA Total: {total_cal} cal | {total_protein}g P | {total_carbs}g C | {total_fat}g F")

            remaining = _get_remaining_macros(user_id)
            if remaining:
                lines.append(f"\n\U0001F3AF Remaining: {remaining}")

            buttons = []
            for mt in MEAL_TYPES:
                label = f"\u2713 {mt}" if mt.lower() == inferred_type else mt
                buttons.append(InlineKeyboardButton(label, callback_data=f"meal_type:{meal_id}:{mt.lower()}"))
            type_row = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
            type_row.append([InlineKeyboardButton("\U0001F5D1 Delete this meal", callback_data=f"meal_del:{meal_id}")])

            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(type_row),
            )

        except json.JSONDecodeError as e:
            logger.error("Photo JSON parse error: %s\n%s", e, traceback.format_exc())
            await update.message.reply_text("I could see the food but had trouble parsing the nutrition data. Try again or type it out instead.")
        except Exception as e:
            logger.error("Photo processing error: %s\n%s", e, traceback.format_exc())
            await update.message.reply_text("Error analyzing the photo. Try again or type what you ate instead.")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("weight", cmd_weight))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("delete", cmd_undo))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CallbackQueryHandler(handle_plan_callback, pattern=r"^plan_done:"))
    app.add_handler(CallbackQueryHandler(handle_meal_type_callback, pattern=r"^meal_type:"))
    app.add_handler(CallbackQueryHandler(handle_meal_delete_callback, pattern=r"^meal_del:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))


def _days_until(date_str: str) -> int:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return (target - now).days
    except ValueError:
        return -1


def _infer_meal_type() -> str:
    import pytz
    from fitnessbot.config import Config
    try:
        tz = pytz.timezone(Config.TIMEZONE)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now(timezone.utc)
    h = now.hour
    if h < 6:
        return "midnight snack"
    if h < 11:
        return "breakfast"
    if h < 15:
        return "lunch"
    if h < 18:
        return "snack"
    if h < 22:
        return "dinner"
    return "midnight snack"


def _get_remaining_macros(user_id: int) -> str:
    from fitnessbot.nutrition import get_nutrition_targets
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    targets = get_nutrition_targets(user_id)
    if not targets:
        return ""
    totals = db.get_today_totals(user_id, today)
    rem_cal = targets["calories"] - totals.get("calories", 0)
    rem_pro = targets["protein"] - totals.get("protein", 0)
    rem_carbs = targets["carbs"] - totals.get("carbs", 0)
    rem_fat = targets["fat"] - totals.get("fat", 0)
    return f"{rem_cal:.0f} cal | {rem_pro:.0f}g protein | {rem_carbs:.0f}g carbs | {rem_fat:.0f}g fat"
