"""Settings routes: Profile + Connections (Telegram + AI providers)."""

import httpx
from cryptography.fernet import Fernet

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user
from fitnessbot.inference.factory import _encrypt_key, _mask_key, PROVIDERS, DEFAULT_MODELS

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


def _get_fernet() -> Fernet:
    key = Config.ENCRYPTION_KEY
    if not key:
        raise ValueError("ENCRYPTION_KEY not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()


def _decrypt_token(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


async def _validate_bot_token(token: str) -> dict | None:
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


async def _auto_detect_chat_id(token: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"limit": 10, "timeout": 0},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("result"):
                    for update in reversed(data["result"]):
                        msg = update.get("message") or update.get("edited_message")
                        if msg and msg.get("chat", {}).get("id"):
                            return str(msg["chat"]["id"])
    except httpx.HTTPError:
        pass
    return None


def _build_provider_display(user_id: int, user: dict) -> list[dict]:
    creds = db.get_all_llm_credentials(user_id)
    cred_map = {c["provider"]: c for c in creds}
    active_provider = user.get("active_provider") or "anthropic"
    active_model = user.get("active_model") or ""

    result = []
    for name, provider in PROVIDERS.items():
        cred = cred_map.get(name)
        entry = {
            "name": name,
            "display_name": {"anthropic": "Anthropic (Claude)", "openai": "OpenAI (ChatGPT)", "google": "Google (Gemini)"}[name],
            "models": provider.list_models(),
            "is_active": name == active_provider,
            "has_key": cred is not None,
            "key_hint": cred["key_hint"] if cred else "",
            "model": cred["model"] if cred and cred.get("model") else DEFAULT_MODELS.get(name, ""),
            "validated_at": cred["validated_at"] if cred else None,
        }
        result.append(entry)
    return result


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    conn = db.get_telegram_connection(uid)
    conn_display = None
    if conn:
        try:
            token = _decrypt_token(conn["bot_token_encrypted"])
            masked = token[:6] + "..." + token[-4:] if len(token) >= 10 else "****"
        except Exception:
            masked = "****"
        conn_display = {
            "bot_username": conn.get("bot_username", "Unknown"),
            "chat_id": conn["chat_id"],
            "is_active": conn["is_active"],
            "validated_at": conn.get("validated_at"),
            "token_masked": masked,
        }

    providers = _build_provider_display(uid, user)
    has_system_key = bool(Config.ANTHROPIC_API_KEY)
    notif_prefs = db.get_notification_preferences(uid)
    share = db.get_share_settings(uid)
    blocked_users = db.get_blocked_users(uid)

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "connection": conn_display,
            "providers": providers,
            "has_system_key": has_system_key,
            "notif": notif_prefs,
            "share": share,
            "blocked_users": blocked_users,
        },
    )


@router.post("/settings/profile")
async def update_profile(
    request: Request,
    display_name: str = Form(""),
    timezone_str: str = Form(""),
    sex: str = Form(""),
    height: str = Form(""),
    birthdate: str = Form(""),
    units_pref: str = Form(""),
    activity_level: str = Form(""),
    dietary_restrictions: str = Form(""),
    handle: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    updates = {}
    if display_name.strip():
        updates["display_name"] = display_name.strip()
    if timezone_str.strip():
        updates["timezone"] = timezone_str.strip()
    if sex.strip():
        updates["sex"] = sex.strip()
    if height.strip():
        try:
            updates["height"] = float(height.strip())
        except ValueError:
            pass
    if birthdate.strip():
        updates["birthdate"] = birthdate.strip()
    if units_pref.strip():
        updates["units_pref"] = units_pref.strip()
    if activity_level.strip():
        updates["activity_level"] = activity_level.strip()
    if dietary_restrictions.strip():
        updates["dietary_restrictions"] = dietary_restrictions.strip()

    if updates:
        db.update_user(user["user_id"], **updates)

    # Handle update (separate because of uniqueness constraint)
    handle_val = handle.strip().lower()
    if handle_val:
        db.update_user_handle(user["user_id"], handle_val)

    return RedirectResponse("/settings?saved=profile", status_code=303)


@router.post("/settings/telegram")
async def connect_telegram(
    request: Request,
    bot_token: str = Form(...),
    chat_id: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    bot_info = await _validate_bot_token(bot_token)
    if not bot_info:
        return RedirectResponse("/settings?error=telegram_invalid", status_code=303)

    if not chat_id.strip():
        detected = await _auto_detect_chat_id(bot_token)
        if detected:
            chat_id = detected
        else:
            return RedirectResponse("/settings?error=telegram_no_chat", status_code=303)

    db.delete_telegram_connection(user["user_id"])
    encrypted = _encrypt_token(bot_token)
    db.insert_telegram_connection(
        user_id=user["user_id"],
        bot_token_encrypted=encrypted,
        chat_id=chat_id,
        bot_username=bot_info.get("username"),
    )

    from fitnessbot.bot.manager import connection_manager
    await connection_manager.start_user_bot(user["user_id"])

    return RedirectResponse("/settings?saved=telegram", status_code=303)


@router.post("/settings/telegram/disconnect")
async def disconnect_telegram(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    from fitnessbot.bot.manager import connection_manager
    await connection_manager.stop_user_bot(user["user_id"])
    db.delete_telegram_connection(user["user_id"])

    return RedirectResponse("/settings?saved=telegram_disconnected", status_code=303)


@router.post("/settings/notifications")
async def save_notifications(
    request: Request,
    morning_brief_enabled: str = Form("0"),
    morning_brief_time: str = Form("07:30"),
    midday_check_enabled: str = Form("0"),
    midday_check_time: str = Form("13:00"),
    evening_wrap_enabled: str = Form("0"),
    evening_wrap_time: str = Form("20:30"),
    weekly_rollup_enabled: str = Form("0"),
    weekly_rollup_day: str = Form("6"),
    activity_prompts_enabled: str = Form("0"),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    db.upsert_notification_preferences(
        user["user_id"],
        morning_brief_enabled=int(morning_brief_enabled),
        morning_brief_time=morning_brief_time.strip() or "07:30",
        midday_check_enabled=int(midday_check_enabled),
        midday_check_time=midday_check_time.strip() or "13:00",
        evening_wrap_enabled=int(evening_wrap_enabled),
        evening_wrap_time=evening_wrap_time.strip() or "20:30",
        weekly_rollup_enabled=int(weekly_rollup_enabled),
        weekly_rollup_day=int(weekly_rollup_day) if weekly_rollup_day.strip() else 6,
        activity_prompts_enabled=int(activity_prompts_enabled),
    )
    return RedirectResponse("/settings?saved=notifications", status_code=303)


@router.post("/settings/provider")
async def save_provider_key(
    request: Request,
    provider: str = Form(...),
    api_key: str = Form(...),
    model: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    api_key = api_key.strip()
    if not api_key:
        return RedirectResponse("/settings?error=empty_key", status_code=303)

    if provider not in PROVIDERS:
        return RedirectResponse("/settings?error=unknown_provider", status_code=303)

    p = PROVIDERS[provider]
    valid = p.validate_key(api_key)
    if not valid:
        return RedirectResponse(f"/settings?error=invalid_key_{provider}", status_code=303)

    from fitnessbot.inference.factory import _encrypt_key, _mask_key
    encrypted = _encrypt_key(api_key)
    hint = _mask_key(api_key)
    validated_at = db.utcnow()

    if not model:
        model = DEFAULT_MODELS.get(provider, "")

    db.upsert_llm_credential(uid, provider, encrypted, hint, model, validated_at)
    db.update_user(uid, active_provider=provider, active_model=model)

    return RedirectResponse("/settings?saved=provider", status_code=303)


@router.post("/settings/provider/activate")
async def activate_provider(request: Request, provider: str = Form(...), model: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    cred = db.get_llm_credential(uid, provider)
    if not cred:
        return RedirectResponse("/settings?error=no_key", status_code=303)

    if model:
        db.update_llm_credential_model(uid, provider, model)
    db.update_user(uid, active_provider=provider, active_model=model or cred.get("model", ""))

    return RedirectResponse("/settings?saved=activated", status_code=303)


@router.post("/settings/provider/remove")
async def remove_provider_key(request: Request, provider: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["user_id"]
    db.delete_llm_credential(uid, provider)

    if user.get("active_provider") == provider:
        creds = db.get_all_llm_credentials(uid)
        if creds:
            db.update_user(uid, active_provider=creds[0]["provider"], active_model=creds[0].get("model", ""))
        else:
            db.update_user(uid, active_provider="anthropic", active_model="")

    return RedirectResponse("/settings?saved=removed", status_code=303)


@router.get("/api/provider/models")
async def get_provider_models(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    provider = request.query_params.get("provider", "")
    if provider not in PROVIDERS:
        return JSONResponse({"error": "Unknown provider"}, status_code=400)

    return JSONResponse({"models": PROVIDERS[provider].list_models()})
