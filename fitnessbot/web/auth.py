"""Authentication: registration, login, session management, Google OAuth, email verification."""

import hashlib
import logging
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))

SESSION_COOKIE = "fit_session"
SESSION_EXPIRY_DAYS = 30

GOOGLE_CLIENT_ID = Config.GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET = Config.GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI = Config.GOOGLE_REDIRECT_URI


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}:{h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    salt, h = stored.split(":")
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return computed.hex() == h


def _create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_EXPIRY_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (user_id, token_hash, expires),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT s.user_id, s.expires_at FROM sessions s
               WHERE s.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < db.utcnow():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
            return None
        return db.get_user_by_id(row["user_id"])
    finally:
        conn.close()


def require_login(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def _send_verification_email(email: str, code: str) -> bool:
    if not Config.SMTP_HOST:
        logger.warning("SMTP not configured, skipping verification email for %s", email)
        return False
    msg = EmailMessage()
    msg["Subject"] = "Verify your fit-ness.ca email"
    msg["From"] = Config.SMTP_FROM
    msg["To"] = email
    msg.set_content(
        f"Your verification code is: {code}\n\n"
        f"Or click: {Config.BASE_URL}/verify-email?code={code}\n\n"
        "This code expires in 24 hours."
    )
    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as s:
            s.starttls()
            if Config.SMTP_USER:
                s.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        logger.error("Failed to send verification email: %s", e)
        return False


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    invite = request.query_params.get("invite", "")
    google_enabled = bool(GOOGLE_CLIENT_ID)
    return templates.TemplateResponse("register.html", {
        "request": request, "invite_code": invite, "google_enabled": google_enabled,
    })


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    timezone_str: str = Form("America/Toronto"),
    units_pref: str = Form("imperial"),
    invite_code: str = Form(""),
):
    existing = db.get_user_by_email(email)
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email already registered.", "invite_code": invite_code, "google_enabled": bool(GOOGLE_CLIENT_ID)},
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Password must be at least 6 characters.", "invite_code": invite_code, "google_enabled": bool(GOOGLE_CLIENT_ID)},
        )

    invited_by = None
    if invite_code:
        invite = db.get_invite_link(invite_code)
        if invite:
            if invite.get("max_uses") and invite["uses"] >= invite["max_uses"]:
                pass
            elif invite.get("expires_at") and invite["expires_at"] < db.utcnow():
                pass
            else:
                invited_by = invite["user_id"]
                db.increment_invite_uses(invite_code)

    password_hash = _hash_password(password)
    user_id = db.insert_user(email, password_hash, display_name, timezone_str, units_pref)

    if invited_by:
        conn = db.get_connection()
        try:
            conn.execute("UPDATE users SET invited_by = ? WHERE user_id = ?", (invited_by, user_id))
            conn.commit()
        finally:
            conn.close()

    if email == Config.SUPER_ADMIN_EMAIL:
        db.ensure_superadmin(email)

    verify_code = secrets.token_urlsafe(32)
    db.set_email_verify_code(user_id, verify_code)
    _send_verification_email(email, verify_code)

    db.touch_last_active(user_id)
    token = _create_session(user_id)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_EXPIRY_DAYS * 86400, httponly=True, samesite="lax"
    )
    return response


@router.get("/verify-email")
async def verify_email_page(request: Request, code: str = Query("")):
    if not code:
        return RedirectResponse("/dashboard", status_code=303)
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT user_id FROM users WHERE email_verify_code = ?", (code,)).fetchone()
    finally:
        conn.close()
    if not row:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid or expired verification link."})
    db.verify_email(row["user_id"])
    user = get_current_user(request)
    if user and user["user_id"] == row["user_id"]:
        return RedirectResponse("/dashboard?verified=1", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "success": "Email verified! You can now sign in."})


@router.post("/resend-verification")
async def resend_verification(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    verify_code = secrets.token_urlsafe(32)
    db.set_email_verify_code(user["user_id"], verify_code)
    _send_verification_email(user["email"], verify_code)
    return RedirectResponse("/dashboard?resent=1", status_code=303)


@router.get("/auth/google")
async def google_login(request: Request):
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse("/login", status_code=303)
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "state": state,
    }
    from urllib.parse import urlencode
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = Query(""), error: str = Query("")):
    if error or not code:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Google sign-in was cancelled."})

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            return templates.TemplateResponse("login.html", {"request": request, "error": "Google authentication failed."})

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return templates.TemplateResponse("login.html", {"request": request, "error": "No access token from Google."})

        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            return templates.TemplateResponse("login.html", {"request": request, "error": "Could not fetch Google profile."})

    info = userinfo_resp.json()
    google_id = info.get("id")
    email = info.get("email", "")
    name = info.get("name", email.split("@")[0])

    user = db.get_user_by_google_id(google_id)
    if not user:
        user = db.get_user_by_email(email)
        if user:
            conn = db.get_connection()
            try:
                conn.execute("UPDATE users SET google_id = ?, email_verified = 1 WHERE user_id = ?", (google_id, user["user_id"]))
                conn.commit()
            finally:
                conn.close()
        else:
            placeholder_hash = _hash_password(secrets.token_urlsafe(32))
            user_id = db.insert_user(email, placeholder_hash, name, "America/Toronto", "imperial")
            conn = db.get_connection()
            try:
                conn.execute("UPDATE users SET google_id = ?, email_verified = 1 WHERE user_id = ?", (google_id, user_id))
                conn.commit()
            finally:
                conn.close()
            if email == Config.SUPER_ADMIN_EMAIL:
                db.ensure_superadmin(email)
            user = db.get_user_by_id(user_id)

    db.touch_last_active(user["user_id"])
    session_token = _create_session(user["user_id"])
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, session_token, max_age=SESSION_EXPIRY_DAYS * 86400, httponly=True, samesite="lax"
    )
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    google_enabled = bool(GOOGLE_CLIENT_ID)
    return templates.TemplateResponse("login.html", {"request": request, "google_enabled": google_enabled})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    user = db.get_user_by_email(email)
    if not user or not _verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password.", "google_enabled": bool(GOOGLE_CLIENT_ID)},
        )
    if email == Config.SUPER_ADMIN_EMAIL:
        db.ensure_superadmin(email)
    db.touch_last_active(user["user_id"])
    token = _create_session(user["user_id"])
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_EXPIRY_DAYS * 86400, httponly=True, samesite="lax"
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
        finally:
            conn.close()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
