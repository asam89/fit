"""Telegram bot connection management routes."""

import httpx
from cryptography.fernet import Fernet

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


def _get_fernet() -> Fernet:
    key = Config.ENCRYPTION_KEY
    if not key:
        raise ValueError("ENCRYPTION_KEY not set in environment")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


async def validate_bot_token(token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data["result"]
    except httpx.HTTPError:
        pass
    return None


def _mask_token(token: str) -> str:
    if len(token) < 10:
        return "****"
    return token[:6] + "..." + token[-4:]


@router.get("/connections", response_class=HTMLResponse)
async def connections_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db.get_telegram_connection(user["user_id"])
    conn_display = None
    if conn:
        conn_display = {
            "bot_username": conn.get("bot_username", "Unknown"),
            "chat_id": conn["chat_id"],
            "is_active": conn["is_active"],
            "validated_at": conn.get("validated_at"),
            "token_masked": _mask_token(decrypt_token(conn["bot_token_encrypted"])),
        }

    return templates.TemplateResponse(
        "connections.html",
        {"request": request, "user": user, "connection": conn_display},
    )


@router.post("/connections/add")
async def add_connection(
    request: Request,
    bot_token: str = Form(...),
    chat_id: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Validate token
    bot_info = await validate_bot_token(bot_token)
    if not bot_info:
        conn = db.get_telegram_connection(user["user_id"])
        return templates.TemplateResponse(
            "connections.html",
            {
                "request": request,
                "user": user,
                "connection": None,
                "error": "Invalid bot token. Please check and try again.",
            },
        )

    # Remove existing connection if any
    db.delete_telegram_connection(user["user_id"])

    encrypted = encrypt_token(bot_token)
    db.insert_telegram_connection(
        user_id=user["user_id"],
        bot_token_encrypted=encrypted,
        chat_id=chat_id,
        bot_username=bot_info.get("username"),
    )

    return RedirectResponse("/connections", status_code=303)


@router.post("/connections/disconnect")
async def disconnect(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    db.delete_telegram_connection(user["user_id"])
    return RedirectResponse("/connections", status_code=303)


@router.post("/connections/test")
async def test_connection(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = db.get_telegram_connection(user["user_id"])
    if not conn:
        return RedirectResponse("/connections", status_code=303)

    token = decrypt_token(conn["bot_token_encrypted"])
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": conn["chat_id"],
                    "text": "Connected to Fitness & Health Intelligence Platform!",
                },
            )
            if resp.status_code == 200:
                return RedirectResponse("/connections?test=success", status_code=303)
    except httpx.HTTPError:
        pass

    return RedirectResponse("/connections?test=fail", status_code=303)
