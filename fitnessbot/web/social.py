"""Social / Friends routes — API endpoints for the friends dashboard section."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fitnessbot import db
from fitnessbot.config import Config
from fitnessbot.web.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/social")

NUDGE_DAILY_LIMIT = 10
CUSTOM_NUDGE_MAX_LEN = 200


def _require_auth(request: Request) -> dict | None:
    user = get_current_user(request)
    if not user:
        return None
    return user


# --- Profile / Handle ---

@router.post("/handle")
async def set_handle(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    handle = data.get("handle", "").strip().lower()
    if not handle or len(handle) < 3 or len(handle) > 30:
        return JSONResponse({"error": "Handle must be 3-30 characters"}, status_code=400)
    if not handle.replace("_", "").replace(".", "").isalnum():
        return JSONResponse({"error": "Handle can only contain letters, numbers, underscores, dots"}, status_code=400)
    ok = db.update_user_handle(user["user_id"], handle)
    if not ok:
        return JSONResponse({"error": "Handle already taken"}, status_code=409)
    return JSONResponse({"ok": True, "handle": handle})


# --- Search / Discovery ---

@router.get("/search")
async def search_users(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    results = db.search_users(q, user["user_id"])
    return JSONResponse({"results": results})


# --- Friend Requests ---

@router.post("/friends/request")
async def send_friend_request(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    target_id = data.get("user_id")
    if not target_id or target_id == user["user_id"]:
        return JSONResponse({"error": "Invalid user"}, status_code=400)
    result = db.send_friend_request(user["user_id"], target_id)
    if result == "blocked":
        return JSONResponse({"error": "Cannot send request"}, status_code=403)
    if result == "already_friends":
        return JSONResponse({"error": "Already friends"}, status_code=409)
    if result == "already_pending":
        return JSONResponse({"error": "Request already pending"}, status_code=409)
    # Notify via Telegram if possible
    _notify_friend_request(user, target_id)
    return JSONResponse({"ok": True, "status": result})


@router.post("/friends/accept")
async def accept_request(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    fid = data.get("friendship_id")
    if not fid:
        return JSONResponse({"error": "Missing friendship_id"}, status_code=400)
    ok = db.accept_friend_request(fid, user["user_id"])
    if not ok:
        return JSONResponse({"error": "Request not found or already handled"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/friends/decline")
async def decline_request(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    fid = data.get("friendship_id")
    if not fid:
        return JSONResponse({"error": "Missing friendship_id"}, status_code=400)
    ok = db.decline_friend_request(fid, user["user_id"])
    return JSONResponse({"ok": True})


@router.post("/friends/remove")
async def remove_friend(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    fid = data.get("friendship_id")
    if not fid:
        return JSONResponse({"error": "Missing friendship_id"}, status_code=400)
    db.remove_friend(fid, user["user_id"])
    return JSONResponse({"ok": True})


# --- Friends List ---

@router.get("/friends")
async def get_friends(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    friends = db.get_friends(user["user_id"])
    pending = db.get_pending_requests(user["user_id"])
    return JSONResponse({"friends": friends, "pending": pending})


@router.get("/friends/summaries")
async def get_friend_summaries(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    friends = db.get_friends(user["user_id"])
    summaries = []
    for f in friends:
        summary = db.get_friend_summary(f["user_id"], user["user_id"])
        summary["display_name"] = f["display_name"]
        summary["handle"] = f.get("handle")
        summary["avatar_url"] = f.get("avatar_url")
        summary["friendship_id"] = f["friendship_id"]
        summary["user_id"] = f["user_id"]
        summaries.append(summary)
    return JSONResponse({"friends": summaries})


# --- Block ---

@router.post("/block")
async def block_user(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    target_id = data.get("user_id")
    if not target_id or target_id == user["user_id"]:
        return JSONResponse({"error": "Invalid user"}, status_code=400)
    db.block_user(user["user_id"], target_id)
    return JSONResponse({"ok": True})


@router.post("/unblock")
async def unblock_user(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    target_id = data.get("user_id")
    db.unblock_user(user["user_id"], target_id)
    return JSONResponse({"ok": True})


@router.get("/blocked")
async def get_blocked(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    blocked = db.get_blocked_users(user["user_id"])
    return JSONResponse({"blocked": blocked})


# --- Share Settings ---

@router.get("/share-settings")
async def get_share_settings(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    settings = db.get_share_settings(user["user_id"])
    return JSONResponse(settings)


@router.post("/share-settings")
async def update_share_settings(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    db.upsert_share_settings(user["user_id"], data)
    return JSONResponse({"ok": True})


# --- Nudges ---

@router.get("/nudges/templates")
async def get_templates(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    templates = db.get_nudge_templates()
    return JSONResponse({"templates": templates})


@router.post("/nudges/send")
async def send_nudge(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    recipient_id = data.get("recipient_id")
    if not recipient_id:
        return JSONResponse({"error": "Missing recipient"}, status_code=400)

    # Rate limit
    count = db.count_nudges_sent_today(user["user_id"], recipient_id)
    if count >= NUDGE_DAILY_LIMIT:
        return JSONResponse({"error": f"Daily limit reached ({NUDGE_DAILY_LIMIT} per friend)"}, status_code=429)

    kind = data.get("kind", "preset")
    template_key = data.get("template_key")
    body = data.get("body", "")
    emoji = data.get("emoji")

    if kind == "custom" and len(body) > CUSTOM_NUDGE_MAX_LEN:
        return JSONResponse({"error": f"Message too long (max {CUSTOM_NUDGE_MAX_LEN} chars)"}, status_code=400)

    nudge_id = db.send_nudge(
        sender_id=user["user_id"],
        recipient_id=recipient_id,
        kind=kind,
        template_key=template_key,
        body=body,
        emoji=emoji,
        related_event_id=data.get("related_event_id"),
    )
    if nudge_id is None:
        return JSONResponse({"error": "Cannot send nudge (not friends or blocked)"}, status_code=403)

    # Deliver via Telegram
    _deliver_nudge_telegram(user, recipient_id, kind, template_key, body, emoji)
    return JSONResponse({"ok": True, "nudge_id": nudge_id})


@router.get("/nudges")
async def get_nudges(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    nudges = db.get_nudges_for_user(user["user_id"])
    db.mark_nudges_read(user["user_id"])
    return JSONResponse({"nudges": nudges})


@router.get("/nudges/unread")
async def get_unread_count(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    count = db.get_unread_nudge_count(user["user_id"])
    return JSONResponse({"count": count})


# --- Report ---

@router.post("/report")
async def report_user(request: Request):
    user = _require_auth(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    reported_id = data.get("user_id")
    reason = data.get("reason", "")
    nudge_id = data.get("nudge_id")
    db.report_user(user["user_id"], reported_id, reason, nudge_id)
    return JSONResponse({"ok": True})


# --- Telegram integration helpers ---

def _get_bot_app(user_id: int):
    """Get the Telegram Application for a user if running."""
    from fitnessbot.bot.manager import connection_manager
    return connection_manager._bots.get(user_id)


def _notify_friend_request(sender: dict, recipient_id: int):
    """Send friend request notification via Telegram."""
    try:
        conn_info = db.get_telegram_connection(recipient_id)
        if not conn_info:
            return
        app = _get_bot_app(recipient_id)
        if not app:
            return
        import asyncio
        msg = f"👋 {sender['display_name']} sent you a friend request on fit-ness.ca!\n\nAccept at {Config.BASE_URL}/dashboard"
        asyncio.ensure_future(app.bot.send_message(chat_id=conn_info["chat_id"], text=msg))
    except Exception as e:
        logger.warning(f"Failed to notify friend request via Telegram: {e}")


def _deliver_nudge_telegram(sender: dict, recipient_id: int, kind: str,
                            template_key: str | None, body: str | None, emoji: str | None):
    """Deliver nudge via Telegram to recipient."""
    try:
        conn_info = db.get_telegram_connection(recipient_id)
        if not conn_info:
            return
        app = _get_bot_app(recipient_id)
        if not app:
            return
        import asyncio

        if kind == "preset" and template_key:
            templates = db.get_nudge_templates()
            tmpl = next((t for t in templates if t["key"] == template_key), None)
            if tmpl:
                text = f"{tmpl['emoji']} {sender['display_name']}: \"{tmpl['text']}\""
            else:
                text = f"{sender['display_name']} sent you a nudge"
        else:
            text = f"💬 {sender['display_name']}: \"{body or emoji or 'Hey!'}\""

        asyncio.ensure_future(app.bot.send_message(chat_id=conn_info["chat_id"], text=text))
    except Exception as e:
        logger.warning(f"Failed to deliver nudge via Telegram: {e}")
