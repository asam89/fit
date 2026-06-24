"""Goals page — fit-ness goal engine with Claude-powered planning and debrief."""

import json
import logging
import traceback

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.web.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/goals")
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


@router.get("", response_class=HTMLResponse)
async def goals_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    active = db.get_active_goal(user["user_id"])
    archived = db.get_archived_goals(user["user_id"])
    stats = db.get_goal_stats(user["user_id"])

    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "user": user,
            "active_goal": active,
            "archived_goals": archived,
            "stats": stats,
        },
    )


@router.post("/api/build-plan")
async def api_build_plan(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    raw_text = body.get("text", "").strip()
    if not raw_text:
        return JSONResponse({"error": "No goal text provided"}, status_code=400)

    try:
        from fitnessbot.ai.goal_planner import build_plan
        plan = build_plan(raw_text, user_id=user["user_id"])

        uid_gen = __import__("secrets").token_hex
        steps = [{"id": uid_gen(4), "text": s, "done": False} for s in plan.get("steps", [])]

        goal_id = db.insert_goal_with_plan(
            user_id=user["user_id"],
            raw_input=raw_text,
            statement=plan["statement"],
            why=plan["why"],
            metric=plan["metric"],
            target_date=plan["targetDate"],
            steps=steps,
        )

        return JSONResponse({
            "goal_id": goal_id,
            "statement": plan["statement"],
            "why": plan["why"],
            "metric": plan["metric"],
            "targetDate": plan["targetDate"],
            "steps": steps,
        })
    except Exception as e:
        logger.error("Goal plan failed: %s\n%s", e, traceback.format_exc())
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/toggle-step")
async def api_toggle_step(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    goal_id = body.get("goal_id")
    step_id = body.get("step_id")

    goal = db.get_active_goal(user["user_id"])
    if not goal or goal["goal_id"] != goal_id:
        return JSONResponse({"error": "Goal not found"}, status_code=404)

    steps = goal["steps"]
    for s in steps:
        if s["id"] == step_id:
            s["done"] = not s["done"]
            break

    db.update_goal_steps(goal_id, steps)
    return JSONResponse({"ok": True, "steps": steps})


@router.post("/api/achieve")
async def api_achieve(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    goal_id = body.get("goal_id")

    goal = db.get_active_goal(user["user_id"])
    if not goal or goal["goal_id"] != goal_id:
        return JSONResponse({"error": "Goal not found"}, status_code=404)

    db.update_goal_status(goal_id, "achieved")
    return JSONResponse({"ok": True})


@router.post("/api/miss")
async def api_miss(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    goal_id = body.get("goal_id")

    goal = db.get_active_goal(user["user_id"])
    if not goal or goal["goal_id"] != goal_id:
        return JSONResponse({"error": "Goal not found"}, status_code=404)

    db.start_goal_debrief(goal_id)
    return JSONResponse({"ok": True})


@router.post("/api/debrief")
async def api_debrief(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    goal_id = body.get("goal_id")
    notes = body.get("notes", "")

    goal = db.get_active_goal(user["user_id"])
    if not goal or goal["goal_id"] != goal_id:
        return JSONResponse({"error": "Goal not found"}, status_code=404)

    try:
        from fitnessbot.ai.goal_planner import run_debrief
        steps_done = sum(1 for s in goal["steps"] if s.get("done"))
        result = run_debrief(
            statement=goal.get("refined_statement", ""),
            target_date=goal.get("refined_target_date", ""),
            metric=goal.get("refined_metric", ""),
            steps_done=steps_done,
            steps_total=len(goal["steps"]),
            notes=notes,
            user_id=user["user_id"],
        )

        db.save_goal_debrief(goal_id, notes, result)
        return JSONResponse({"ok": True, "debrief": result})
    except Exception as e:
        logger.error("Debrief failed: %s\n%s", e, traceback.format_exc())
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/archive-miss")
async def api_archive_miss(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    goal_id = body.get("goal_id")

    goal = db.get_active_goal(user["user_id"])
    if not goal or goal["goal_id"] != goal_id:
        return JSONResponse({"error": "Goal not found"}, status_code=404)

    db.update_goal_status(goal_id, "missed")
    return JSONResponse({"ok": True})
