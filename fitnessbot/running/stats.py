"""Running calculations, unit formatting and the stats engine. The only place running stats are derived."""

from datetime import date, timedelta

M_PER_KM = 1000.0
M_PER_MI = 1609.344


def pace_s_per_km(distance_m: float, duration_s: float) -> float | None:
    if not distance_m or not duration_s or distance_m <= 0 or duration_s <= 0:
        return None
    return duration_s / (distance_m / M_PER_KM)


def speed_kmh(distance_m: float, duration_s: float) -> float | None:
    if not distance_m or not duration_s or distance_m <= 0 or duration_s <= 0:
        return None
    return (distance_m / M_PER_KM) / (duration_s / 3600.0)


def format_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_distance(distance_m: float, units: str = "km") -> str:
    if units == "mi":
        return f"{distance_m / M_PER_MI:.2f} mi"
    return f"{distance_m / M_PER_KM:.2f} km"


def format_pace(sec_per_km: float | None, units: str = "km") -> str:
    if not sec_per_km:
        return "–"
    per_unit = sec_per_km * (M_PER_MI / M_PER_KM) if units == "mi" else sec_per_km
    s = int(round(per_unit))
    return f"{s // 60}:{s % 60:02d}/{'mi' if units == 'mi' else 'km'}"


# ---------------------------------------------------------------------------
# Stats engine (Workstream D). Pure functions over run dicts as returned by
# running.store. Both /runstats and the /running page call build_summary(), so
# their numbers are identical by construction (invariant 11).
#
# Weeks are user-local Monday-Sunday, using each run's stored local_date.
# Accuracy 1: averages use completed weeks only. The streak counts weeks with
# at least one run, so the current week counts once it has a run.
# ---------------------------------------------------------------------------

PR_DISTANCES = (
    ("1K", 1000.0),
    ("5K", 5000.0),
    ("10K", 10000.0),
    ("Half", 21097.5),
    ("Marathon", 42195.0),
)
PR_TOLERANCE = 0.02           # a whole run counts for a PR distance within +/-2%
LOAD_WARN_RATIO = 1.3         # this week >30% above the 4-week average
LOAD_MIN_AVG_M = 5000.0       # don't nag about load on a very small base
HR_ZONES = (                  # % of max HR, lower bound inclusive
    ("Z1", 0.50), ("Z2", 0.60), ("Z3", 0.70), ("Z4", 0.80), ("Z5", 0.90),
)


def _d(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def week_start(day) -> date:
    """Monday of the user-local week containing `day`."""
    day = _d(day)
    return day - timedelta(days=day.weekday())


def weekly_totals(runs: list[dict], today, weeks: int = 12) -> list[dict]:
    """One row per week, oldest first, ending with the current (in-progress) week."""
    current = week_start(today)
    starts = [current - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    rows = {s: {"week_start": s.isoformat(), "distance_m": 0.0, "duration_s": 0, "runs": 0,
                "in_progress": s == current} for s in starts}
    for r in runs:
        s = week_start(r["local_date"])
        if s in rows:
            row = rows[s]
            row["distance_m"] += float(r["distance_m"])
            row["duration_s"] += int(r["duration_s"])
            row["runs"] += 1
    return [rows[s] for s in starts]


def this_week(runs: list[dict], today) -> dict:
    return weekly_totals(runs, today, weeks=1)[0]


def four_week_average_m(runs: list[dict], today) -> float | None:
    """Mean weekly distance over the 4 completed weeks before this one.

    None when the user has no run before this week (no base to average yet).
    Weeks with no runs count as 0 once the user has started running.
    """
    current = week_start(today)
    first = min((_d(r["local_date"]) for r in runs), default=None)
    if first is None or first >= current:
        return None
    window = [w for w in weekly_totals(runs, today, weeks=5) if not w["in_progress"]]
    window = [w for w in window if _d(w["week_start"]) >= week_start(first)]
    if not window:
        return None
    return sum(w["distance_m"] for w in window) / len(window)


def streak_weeks(runs: list[dict], today) -> int:
    """Consecutive weeks with >=1 run, ending this week (if it has a run) or last week."""
    weeks_with_runs = {week_start(r["local_date"]) for r in runs}
    cursor = week_start(today)
    if cursor not in weeks_with_runs:
        cursor -= timedelta(weeks=1)
    n = 0
    while cursor in weeks_with_runs:
        n += 1
        cursor -= timedelta(weeks=1)
    return n


def load_note(week_m: float, avg_m: float | None) -> str | None:
    """A short note when this week's distance is well above the 4-week average."""
    if not avg_m or avg_m < LOAD_MIN_AVG_M:
        return None
    ratio = week_m / avg_m
    if ratio > LOAD_WARN_RATIO:
        return (f"This week is {round((ratio - 1) * 100)}% above your 4-week average. "
                "Consider keeping the rest of the week easy.")
    return None


def _best_effort_from_splits(splits: list[dict], target_m: float) -> float | None:
    """Fastest run of consecutive splits covering target_m (+/- tolerance).

    Durations are scaled to exactly target_m so a 5.04 km window becomes a 5K time.
    """
    best = None
    n = len(splits)
    for i in range(n):
        dist = dur = 0.0
        for j in range(i, n):
            dist += float(splits[j]["distance_m"])
            dur += float(splits[j]["duration_s"])
            if dist >= target_m * (1 - PR_TOLERANCE):
                if dist <= target_m * (1 + PR_TOLERANCE):
                    t = dur * target_m / dist
                    best = t if best is None or t < best else best
                break
    return best


def _efforts(run: dict) -> dict[str, float]:
    """PR-distance -> time (s) this run counts for."""
    out: dict[str, float] = {}
    dist = float(run["distance_m"])
    splits = run.get("splits") or []
    # Interval splits are work reps only, not continuous, so they never make a PR.
    use_splits = splits and run.get("run_type") != "interval"
    for label, target in PR_DISTANCES:
        times = []
        if abs(dist - target) <= PR_TOLERANCE * target:
            times.append(float(run["duration_s"]) * target / dist)
        if use_splits and dist > target * (1 + PR_TOLERANCE):
            t = _best_effort_from_splits(splits, target)
            if t is not None:
                times.append(t)
        if times:
            out[label] = min(times)
    return out


def personal_records(runs: list[dict]) -> list[dict]:
    """Best time per PR distance. Ties go to the earlier run."""
    best: dict[str, dict] = {}
    ordered = sorted(runs, key=lambda r: (r["started_at"], r["run_id"]))
    for r in ordered:
        for label, t in _efforts(r).items():
            if label not in best or t < best[label]["time_s"] - 0.5:
                best[label] = {"distance": label, "time_s": round(t), "run_id": r["run_id"],
                               "local_date": r["local_date"]}
    return [best[label] for label, _ in PR_DISTANCES if label in best]


def new_records(before: list[dict], after: list[dict]) -> list[dict]:
    """PRs present in `after` that are new or faster than in `before`, with the old time."""
    old = {p["distance"]: p for p in before}
    out = []
    for p in after:
        prev = old.get(p["distance"])
        if prev is None or p["time_s"] < prev["time_s"]:
            out.append({**p, "previous_s": prev["time_s"] if prev else None})
    return out


def easy_pace_trend(runs: list[dict], today, weeks: int = 8) -> list[dict]:
    """Weekly average pace (s/km) of easy runs, oldest first; None for weeks without one."""
    easy = [r for r in runs if r.get("run_type") == "easy"]
    return [{"week_start": w["week_start"],
             "pace_s_per_km": pace_s_per_km(w["distance_m"], w["duration_s"]),
             "in_progress": w["in_progress"]}
            for w in weekly_totals(easy, today, weeks=weeks)]


def hr_zone(hr: float | None, max_hr: float | None) -> str | None:
    if not hr or not max_hr:
        return None
    pct = hr / max_hr
    zone = None
    for label, lo in HR_ZONES:
        if pct >= lo:
            zone = label
    return zone


def time_in_zones(splits: list[dict], max_hr: float | None) -> list[dict]:
    """Seconds per HR zone from per-split average HR. Empty when HR or max HR is missing."""
    if not max_hr:
        return []
    totals = {label: 0.0 for label, _ in HR_ZONES}
    any_hr = False
    for sp in splits:
        z = hr_zone(sp.get("avg_hr"), max_hr)
        if z:
            totals[z] += float(sp["duration_s"])
            any_hr = True
    return [{"zone": z, "seconds": round(s)} for z, s in totals.items()] if any_hr else []


def estimated_max_hr(runs: list[dict], birthdate: str | None, today) -> float | None:
    """Highest max HR seen on a run, else 220 - age, else None."""
    seen = [float(r["max_hr"]) for r in runs if r.get("max_hr")]
    if seen:
        return max(seen)
    if birthdate:
        try:
            b, t = _d(birthdate[:10]), _d(today)
        except ValueError:
            return None
        age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
        if 10 <= age <= 100:
            return float(220 - age)
    return None


def build_summary(runs: list[dict], today) -> dict:
    """Everything /runstats and the /running page show. Runs need their `splits`."""
    week = this_week(runs, today)
    avg = four_week_average_m(runs, today)
    return {
        "today": _d(today).isoformat(),
        "this_week": week,
        "four_week_avg_m": avg,
        "streak_weeks": streak_weeks(runs, today),
        "load_note": load_note(week["distance_m"], avg),
        "prs": personal_records(runs),
        "weekly": weekly_totals(runs, today, weeks=12),
        "easy_pace": easy_pace_trend(runs, today, weeks=8),
        "total_runs": len(runs),
    }


def user_summary(user_id: int) -> dict:
    """Load a user's runs and build the summary. The single entry point for callers."""
    from fitnessbot.running import store
    from fitnessbot.tz import user_today
    return build_summary(store.list_runs_with_splits(user_id), user_today(user_id))


def _short_date(iso: str) -> str:
    d = _d(iso)
    return f"{d.strftime('%b')} {d.day}"


def week_label(week_start_iso: str) -> str:
    s = _d(week_start_iso)
    e = s + timedelta(days=6)
    return f"{_short_date(s.isoformat())}–{e.day}" if s.month == e.month else \
        f"{_short_date(s.isoformat())}–{_short_date(e.isoformat())}"


def present(summary: dict, units: str = "km") -> dict:
    """Display strings for a summary. Telegram and the web page both render these."""
    w = summary["this_week"]
    avg = summary["four_week_avg_m"]
    return {
        "week_label": week_label(w["week_start"]),
        "week_distance": format_distance(w["distance_m"], units),
        "week_runs": w["runs"],
        "four_week_avg": format_distance(avg, units) if avg is not None else "–",
        "streak": summary["streak_weeks"],
        "load_note": summary["load_note"],
        "prs": [{"distance": p["distance"], "time": format_duration(p["time_s"]),
                 "date": _short_date(p["local_date"]), "local_date": p["local_date"],
                 "run_id": p["run_id"]} for p in summary["prs"]],
    }


def format_new_records(records: list[dict]) -> list[str]:
    out = []
    for p in records:
        line = f"\U0001F3C6 New {p['distance']} PR: {format_duration(p['time_s'])}"
        if p.get("previous_s"):
            line += f" (was {format_duration(p['previous_s'])})"
        out.append(line)
    return out
