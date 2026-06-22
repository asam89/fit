"""User profile management routes."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("profile.html", {"request": request, "user": user})


@router.post("/profile")
async def profile_update(
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
    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "user": updated_user, "success": "Profile updated."},
    )
