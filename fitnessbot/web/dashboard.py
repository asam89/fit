"""Dashboard routes: home, charts, reports."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.metrics import get_weight_summary
from fitnessbot.web.auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = db.get_today_totals(user["user_id"], today)
    plan = db.get_active_diet_plan(user["user_id"])
    recent_meals = db.get_recent_meals(user["user_id"], limit=5)
    weight = get_weight_summary(user["user_id"])
    goals = db.get_active_goals(user["user_id"])
    connection = db.get_telegram_connection(user["user_id"])

    # Targets from plan or defaults
    targets = {
        "calories": plan["daily_calories"] if plan and plan.get("daily_calories") else 2000,
        "protein": plan["daily_protein"] if plan and plan.get("daily_protein") else 140,
        "carbs": plan["daily_carbs"] if plan and plan.get("daily_carbs") else 200,
        "fat": plan["daily_fat"] if plan and plan.get("daily_fat") else 60,
    }

    # Compute percentages for progress bars
    pct = {
        "calories": min(100, int(totals["calories"] / targets["calories"] * 100)) if targets["calories"] else 0,
        "protein": min(100, int(totals["protein"] / targets["protein"] * 100)) if targets["protein"] else 0,
        "carbs": min(100, int(totals["carbs"] / targets["carbs"] * 100)) if targets["carbs"] else 0,
        "fat": min(100, int(totals["fat"] / targets["fat"] * 100)) if targets["fat"] else 0,
    }

    remaining_cal = targets["calories"] - totals["calories"]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "totals": totals,
            "targets": targets,
            "pct": pct,
            "remaining_cal": remaining_cal,
            "recent_meals": recent_meals,
            "weight": weight,
            "goals": goals,
            "connection": connection,
            "has_plan": plan is not None,
        },
    )
