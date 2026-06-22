"""API routes for the Weekly Training Plan."""

import json
import logging
from datetime import date, timedelta, datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse

from fitnessbot.web.auth import get_current_user
from fitnessbot import training_plan

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/plan")
async def get_plan(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    uid = user["user_id"]
    week_start = request.query_params.get("week")
    if not week_start:
        week_start = training_plan.get_current_week_start(uid)

    items = training_plan.get_plan_items(uid, week_start)
    adherence = training_plan.compute_adherence(items)

    ws = date.fromisoformat(week_start)
    we = ws + timedelta(days=6)

    return JSONResponse({
        "week_start": week_start,
        "week_end": we.isoformat(),
        "week_label": f"{ws.strftime('%b %d')}–{we.strftime('%d')}",
        "items": items,
        "adherence": adherence,
    })


@router.post("/api/plan/item")
async def add_plan_item(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    uid = user["user_id"]
    date_str = body.get("date")
    activity_type = body.get("activity_type", "other")
    title = body.get("title", "")
    duration = body.get("duration_min")
    notes = body.get("notes")

    if not date_str or not title:
        return JSONResponse({"error": "date and title required"}, status_code=400)

    d = date.fromisoformat(date_str)
    week_start = training_plan._monday_of_week(d)

    result = training_plan.add_item(uid, week_start, date_str, activity_type, title, duration, notes)
    return JSONResponse(result, status_code=201)


@router.put("/api/plan/item/{item_id}")
async def update_plan_item(request: Request, item_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    ok = training_plan.update_item(item_id, user["user_id"], **body)
    return JSONResponse({"ok": ok})


@router.delete("/api/plan/item/{item_id}")
async def delete_plan_item(request: Request, item_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    ok = training_plan.remove_item(item_id, user["user_id"])
    return JSONResponse({"ok": ok})


@router.post("/api/plan/item/{item_id}/complete")
async def toggle_complete(request: Request, item_id: int):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    actual_duration = body.get("actual_duration_min")

    result = training_plan.complete_item(item_id, user["user_id"], actual_duration)
    if not result:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse(result)


@router.post("/api/plan/copy-last-week")
async def copy_last_week(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    week_start = body.get("week_start") or training_plan.get_current_week_start(user["user_id"])

    count = training_plan.copy_last_week(user["user_id"], week_start)
    return JSONResponse({"copied": count, "week_start": week_start})
