"""Health data intake routes (blood work, body comp, fitness baseline, medical notes)."""

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


@router.get("/health-data", response_class=HTMLResponse)
async def health_data_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    records = db.get_health_data(user["user_id"])
    return templates.TemplateResponse(
        "health_data.html",
        {"request": request, "user": user, "records": records},
    )


@router.post("/health-data")
async def health_data_submit(
    request: Request,
    data_type: str = Form(...),
    data_content: str = Form(...),
    notes: str = Form(""),
    recorded_at: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Store as JSON — if user typed free-form text, wrap it
    try:
        json.loads(data_content)
        data_json = data_content
    except (json.JSONDecodeError, TypeError):
        data_json = json.dumps({"raw_text": data_content})

    db.insert_health_data(
        user_id=user["user_id"],
        data_type=data_type,
        data_json=data_json,
        notes=notes if notes else None,
        recorded_at=recorded_at if recorded_at else None,
    )
    records = db.get_health_data(user["user_id"])
    return templates.TemplateResponse(
        "health_data.html",
        {"request": request, "user": user, "records": records, "success": "Health data saved."},
    )
