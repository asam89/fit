"""Settings routes: Profile + Connections combined into one page."""

import json

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user
from fitnessbot.web.connections import encrypt_token, auto_detect_chat_id, validate_bot_token

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    connection = db.get_telegram_connection(user["user_id"])
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": user, "connection": connection},
    )


@router.post("/settings/profile")
async def settings_profile_update(
    request: Request,
    display_name: str = Form(...),
    timezone_str: str = Form("America/Toronto"),
    sex: str = Form(""),
    height: str = Form(""),
    birthdate: str = Form(""),
    units_pref: str = Form("imperial"),
    activity_level: str = Form(""),
    dietary_restrictions: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    updates = {
        "display_name": display_name,
        "timezone": timezone_str,
        "units_pref": units_pref,
    }
    if sex:
        updates["sex"] = sex
    if height:
        try:
            updates["height"] = float(height)
        except ValueError:
            pass
    if birthdate:
        updates["birthdate"] = birthdate
    if activity_level:
        updates["activity_level"] = activity_level
    if dietary_restrictions:
        updates["dietary_restrictions"] = dietary_restrictions

    db.update_user(user["user_id"], **updates)
    updated_user = db.get_user_by_id(user["user_id"])
    connection = db.get_telegram_connection(user["user_id"])
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": updated_user, "connection": connection, "profile_success": "Profile updated."},
    )


@router.post("/settings/telegram")
async def settings_telegram_connect(
    request: Request,
    bot_token: str = Form(...),
    chat_id: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    bot_token = bot_token.strip()
    bot_info = await validate_bot_token(bot_token)
    if not bot_info:
        connection = db.get_telegram_connection(user["user_id"])
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "user": user, "connection": connection,
             "telegram_error": "Invalid bot token. Check it and try again."},
        )

    if not chat_id.strip():
        detected = await auto_detect_chat_id(bot_token)
        if detected:
            chat_id = detected
        else:
            connection = db.get_telegram_connection(user["user_id"])
            return templates.TemplateResponse(
                "settings.html",
                {"request": request, "user": user, "connection": connection,
                 "telegram_error": "Could not auto-detect Chat ID. Send a message to your bot first, then try again."},
            )

    encrypted = encrypt_token(bot_token)
    db.upsert_telegram_connection(
        user_id=user["user_id"],
        bot_token_encrypted=encrypted,
        chat_id=chat_id.strip(),
        bot_username=bot_info.get("username", ""),
    )

    from fitnessbot.bot.manager import ConnectionManager
    try:
        manager = ConnectionManager.get_instance()
        await manager.start_bot(user["user_id"])
    except Exception:
        pass

    updated_user = db.get_user_by_id(user["user_id"])
    connection = db.get_telegram_connection(user["user_id"])
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": updated_user, "connection": connection,
         "telegram_success": f"Connected to @{bot_info.get('username', 'bot')}!"},
    )


@router.post("/settings/telegram/disconnect")
async def settings_telegram_disconnect(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    from fitnessbot.bot.manager import ConnectionManager
    try:
        manager = ConnectionManager.get_instance()
        await manager.stop_bot(user["user_id"])
    except Exception:
        pass

    db.delete_telegram_connection(user["user_id"])
    connection = db.get_telegram_connection(user["user_id"])
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": user, "connection": connection,
         "telegram_success": "Telegram disconnected."},
    )
