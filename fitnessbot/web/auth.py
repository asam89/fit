"""Authentication: registration, login, session management."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))

SESSION_COOKIE = "fit_session"
SESSION_EXPIRY_DAYS = 30


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


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    timezone_str: str = Form("America/Toronto"),
    units_pref: str = Form("imperial"),
):
    existing = db.get_user_by_email(email)
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email already registered."},
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Password must be at least 6 characters."},
        )
    password_hash = _hash_password(password)
    user_id = db.insert_user(email, password_hash, display_name, timezone_str, units_pref)
    if email == Config.SUPER_ADMIN_EMAIL:
        db.ensure_superadmin(email)
    db.touch_last_active(user_id)
    token = _create_session(user_id)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_EXPIRY_DAYS * 86400, httponly=True, samesite="lax"
    )
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


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
            {"request": request, "error": "Invalid email or password."},
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
