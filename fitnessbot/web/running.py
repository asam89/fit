"""Running page: weekly distance, easy-pace trend, PRs, history and run detail.

Every number comes from fitnessbot.running.stats (invariant 11); this module only
loads, filters and renders.
"""

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from fitnessbot import db
from fitnessbot.config import Config
from fitnessbot.running import stats, store
from fitnessbot.running.store import RUN_TYPES
from fitnessbot.tz import user_today
from fitnessbot.web.auth import get_current_user

router = APIRouter(prefix="/running")
templates = Jinja2Templates(directory=str(Config.TEMPLATE_DIR))


def _units(user: dict) -> str:
    return "mi" if user.get("run_units") == "mi" else "km"


def _valid_date(value: str | None) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None


def _row(r: dict, units: str) -> dict:
    return {
        "run_id": r["run_id"],
        "date": stats._short_date(r["local_date"]),
        "local_date": r["local_date"],
        "distance": stats.format_distance(r["distance_m"], units),
        "time": stats.format_duration(r["duration_s"]),
        "pace": stats.format_pace(stats.pace_s_per_km(r["distance_m"], r["duration_s"]), units),
        "run_type": r.get("run_type") or "",
        "avg_hr": int(r["avg_hr"]) if r.get("avg_hr") else None,
        "source": r["source"],
    }


def _chart_data(summary: dict, units: str) -> dict:
    per_unit = stats.M_PER_MI if units == "mi" else stats.M_PER_KM
    pace_factor = stats.M_PER_MI / stats.M_PER_KM if units == "mi" else 1.0
    return {
        "weekly_labels": [stats.week_label(w["week_start"]) for w in summary["weekly"]],
        "weekly_values": [round(w["distance_m"] / per_unit, 2) for w in summary["weekly"]],
        "pace_labels": [stats.week_label(w["week_start"]) for w in summary["easy_pace"]],
        # seconds per display unit; the page formats them as m:ss
        "pace_values": [round(w["pace_s_per_km"] * pace_factor) if w["pace_s_per_km"] else None
                        for w in summary["easy_pace"]],
        "units": units,
    }


@router.get("", response_class=HTMLResponse)
async def running_page(request: Request, type: str | None = None, start: str | None = None,
                       end: str | None = None):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["user_id"]
    full = db.get_user_by_id(uid) or user
    units = _units(full)
    summary = stats.user_summary(uid)
    run_type = type if type in RUN_TYPES else None
    start, end = _valid_date(start), _valid_date(end)
    history = store.list_runs(uid, start_date=start, end_date=end, run_type=run_type, limit=200)
    return templates.TemplateResponse("running.html", {
        "request": request,
        "user": user,
        "units": units,
        "view": stats.present(summary, units),
        "has_runs": summary["total_runs"] > 0,
        "chart": _chart_data(summary, units),
        "history": [_row(r, units) for r in history],
        "run_types": RUN_TYPES,
        "filters": {"type": run_type or "", "start": start or "", "end": end or ""},
    })


@router.get("/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["user_id"]
    run = store.get_run(uid, run_id)
    if run is None:
        return HTMLResponse("Run not found", status_code=404)
    full = db.get_user_by_id(uid) or user
    units = _units(full)
    max_hr = stats.estimated_max_hr(store.list_runs(uid), full.get("birthdate"), user_today(uid))
    cumulative = 0.0
    splits = []
    for sp in run["splits"]:
        cumulative += sp["distance_m"]
        splits.append({
            "index": sp["split_index"] + 1,
            "distance": stats.format_distance(sp["distance_m"], units),
            "cumulative": stats.format_distance(cumulative, units),
            "time": stats.format_duration(sp["duration_s"]),
            "pace": stats.format_pace(stats.pace_s_per_km(sp["distance_m"], sp["duration_s"]), units),
            "avg_hr": int(sp["avg_hr"]) if sp.get("avg_hr") else None,
            "zone": stats.hr_zone(sp.get("avg_hr"), max_hr),
        })
    zones = [{**z, "time": stats.format_duration(z["seconds"])}
             for z in stats.time_in_zones(run["splits"], max_hr)]
    return templates.TemplateResponse("run_detail.html", {
        "request": request,
        "user": user,
        "run": {**_row(run, units), "notes": run.get("notes"), "max_hr": run.get("max_hr"),
                "surface": run.get("surface"), "elevation_gain_m": run.get("elevation_gain_m"),
                "interval": run.get("run_type") == "interval"},
        "splits": splits,
        "zones": zones,
        "max_hr": int(max_hr) if max_hr else None,
    })
