"""Super admin dashboard — user overview, connections, activity."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


def _require_admin(request: Request) -> dict | None:
    user = get_current_user(request)
    if not user:
        return None
    if not user.get("is_superadmin"):
        return None
    return user


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/dashboard", status_code=303)

    users = db.get_all_users()
    platform_stats = db.get_platform_stats()

    # Enrich each user with activity stats
    for u in users:
        u["activity"] = db.get_user_activity_stats(u["user_id"])

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": user,
            "all_users": users,
            "stats": platform_stats,
        },
    )
