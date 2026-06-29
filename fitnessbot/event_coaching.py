"""Event-goal coaching: prep plans, science breakdowns, motivation, readiness checks."""

import json
import logging
import re
from datetime import datetime, timezone, timedelta

from fitnessbot import db
from fitnessbot.inference.base import InferenceError

logger = logging.getLogger(__name__)

# --- Date parsing ---

_RELATIVE_DATE_PAT = re.compile(r"(\d+)\s*(?:days?|weeks?|months?)\s*(?:away|out|from\s*now|left)?", re.I)
_ABSOLUTE_DATE_PAT = re.compile(
    r"(?:on\s+)?(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
    re.I,
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_EVENT_KEYWORDS = re.compile(
    r"\b(tournament|marathon|half.?marathon|race|competition|game|match|fight|bout|meet|event|triathlon|5k|10k|spartan|obstacle|challenge|tryout|combine|showcase|season|vacation|trip|wedding|reunion|photoshoot)\b",
    re.I,
)

_SPORT_KEYWORDS = {
    "basketball": "basketball", "soccer": "soccer", "football": "football",
    "tennis": "tennis", "swimming": "swimming", "cycling": "cycling",
    "running": "running", "marathon": "running", "half marathon": "running",
    "5k": "running", "10k": "running", "triathlon": "triathlon",
    "boxing": "boxing", "mma": "mma", "wrestling": "wrestling",
    "volleyball": "volleyball", "baseball": "baseball", "hockey": "hockey",
    "golf": "golf", "crossfit": "crossfit", "spartan": "obstacle_race",
    "obstacle": "obstacle_race", "powerlifting": "powerlifting",
    "weightlifting": "weightlifting", "bodybuilding": "bodybuilding",
}


def parse_event_date(text: str) -> str | None:
    """Extract event date from text, return ISO format or None."""
    now = datetime.now(timezone.utc)

    # Try relative ("in 25 days", "30 days away")
    m = _RELATIVE_DATE_PAT.search(text)
    if m:
        num = int(m.group(1))
        lower = m.group(0).lower()
        if "week" in lower:
            delta = timedelta(weeks=num)
        elif "month" in lower:
            delta = timedelta(days=num * 30)
        else:
            delta = timedelta(days=num)
        return (now + delta).strftime("%Y-%m-%d")

    # Try absolute ("July 17th", "Jul 17", "7/17")
    m = _ABSOLUTE_DATE_PAT.search(text)
    if m:
        raw = m.group(0).strip()
        # Remove ordinal suffixes
        raw = re.sub(r"(st|nd|rd|th)", "", raw)
        raw = re.sub(r"^on\s+", "", raw)

        # Try month name format
        for prefix, month_num in _MONTH_MAP.items():
            if raw.lower().startswith(prefix):
                nums = re.findall(r"\d+", raw)
                if nums:
                    day = int(nums[0])
                    year = int(nums[1]) if len(nums) > 1 and int(nums[1]) > 31 else now.year
                    if year < 100:
                        year += 2000
                    target = datetime(year, month_num, day)
                    if target.date() < now.date():
                        target = target.replace(year=year + 1)
                    return target.strftime("%Y-%m-%d")
                break

        # Try numeric format (M/D or M/D/Y)
        nums = re.findall(r"\d+", raw)
        if len(nums) >= 2:
            month, day = int(nums[0]), int(nums[1])
            year = int(nums[2]) if len(nums) > 2 else now.year
            if year < 100:
                year += 2000
            try:
                target = datetime(year, month, day)
                if target.date() < now.date():
                    target = target.replace(year=year + 1)
                return target.strftime("%Y-%m-%d")
            except ValueError:
                pass

    return None


def detect_sport_type(text: str) -> str | None:
    lower = text.lower()
    for keyword, sport in _SPORT_KEYWORDS.items():
        if keyword in lower:
            return sport
    return None


def is_event_goal_message(text: str) -> bool:
    """Detect if this message is about an upcoming event/goal."""
    lower = text.lower()
    has_event = bool(_EVENT_KEYWORDS.search(text))
    has_date = bool(_RELATIVE_DATE_PAT.search(text)) or bool(_ABSOLUTE_DATE_PAT.search(text))
    has_prep_intent = any(w in lower for w in (
        "prepare", "prep", "ready", "train for", "get ready",
        "help me", "can you help", "what can i do", "how can i",
        "breakdown", "break down", "science", "motivated", "motivation",
        "plan for", "get in shape", "be ready", "haven't been training",
        "not been training", "want to drop", "want to lose", "want to gain",
        "how should i", "what should i", "signed up",
    ))
    # Need either (event + date) or (event + prep intent) or (date + prep intent)
    return (has_event and has_date) or (has_event and has_prep_intent) or (has_date and has_prep_intent)


def is_readiness_check(text: str) -> bool:
    """Detect if this is a readiness assessment question."""
    lower = text.lower()
    return any(phrase in lower for phrase in (
        "am i ready", "am i prepared", "how ready", "ready for",
        "on track for", "prepared for", "how am i doing for",
        "will i be ready", "can i make it",
    ))


# --- Prompts ---

EVENT_PREP_SYSTEM = """You are a sport science coach. The user has an upcoming event and wants a preparation plan.

Given the event details, create a comprehensive but practical prep plan. Return a JSON object with:
{
  "prep_plan": {
    "phases": [
      {"name": "Phase name", "weeks": "1-2", "focus": "description", "key_workouts": ["workout1", "workout2"], "nutrition_notes": "relevant nutrition adjustments"}
    ],
    "weekly_schedule_suggestion": "brief template",
    "taper_strategy": "if applicable, how to taper before event",
    "race_day_tips": ["tip1", "tip2"]
  },
  "science_notes": "2-3 paragraphs explaining the physiological adaptations happening during prep: what systems are being trained, how the body adapts, energy system development, relevant biochemistry in plain language",
  "readiness_markers": ["marker1: what to look for", "marker2: what to look for"],
  "motivation_hooks": ["short motivational insight 1", "short motivational insight 2", "short motivational insight 3"],
  "nutrition_adjustments": "brief notes on how diet should shift during prep vs event week"
}

Be specific to the sport/event type. Use evidence-based training principles. Keep it practical and actionable.
If the prep window is short (<2 weeks), focus on what's realistically achievable and event-day readiness rather than building new fitness."""

MOTIVATION_SYSTEM = """You are a fitness coach with a complex, human personality — not a motivational poster. Generate a brief, personalized check-in message.

EMOTIONAL RANGE — pick the right tone for the situation:
- FIRED UP: when they're close to the event and training hard. Match their energy. Be the hype man. "6 workouts in 7 days. You're not hoping to be ready — you're making sure of it."
- TOUGH: when they've been slacking — missed workouts, inconsistent logging, overeating. Don't be mean, but don't let them off the hook. "3 missed sessions this week. The tournament doesn't care about your excuses. Get in the gym today."
- THOUGHTFUL: when they're grinding but burning out, or when sleep/recovery data looks bad. Pull back. "Your body's telling you something. 6 hours of sleep isn't enough to recover from what you're putting it through. Tonight: bed by 10, no negotiation."
- REAL TALK: when the data shows a pattern they might not see. Connect the dots honestly. "You're training hard but eating 500 over target 4 of the last 7 days. The gym work won't outrun the kitchen. Pick one: tighten the diet or accept the weight stays."

Rules:
- Reference their specific event and days remaining
- Include ONE actionable tip for today
- Mention their recent progress if data is provided
- Keep it to 3-5 lines max
- Never be generic. Never sound like a bot. Sound like someone who actually knows them.
- End with a question or prompt that invites engagement"""

READINESS_SYSTEM = """You are a sport science coach assessing readiness for an upcoming event.

Based on the user's actual logged data (training, nutrition, sleep, weight) and their prep plan, give an honest readiness assessment.

Rules:
- Score readiness 1-10 based on available data
- Identify strengths (what they've done well)
- Identify gaps (what's missing or concerning)
- Give 2-3 specific actionable items for the remaining time
- Be honest — if they're behind, say so constructively
- Reference actual numbers from their data
- Keep it to 6-10 lines"""


def build_prep_plan(user_id: int, title: str, event_date: str, sport_type: str | None, description: str) -> dict:
    """Generate a prep plan via LLM. Returns structured plan dict."""
    from fitnessbot.inference.factory import get_inference

    now = datetime.now(timezone.utc)
    target = datetime.strptime(event_date, "%Y-%m-%d")
    days_out = (target.date() - now.date()).days

    user = db.get_user_by_id(user_id)
    profile_info = ""
    if user:
        parts = []
        if user.get("activity_level"):
            parts.append(f"Activity level: {user['activity_level']}")
        if user.get("sex"):
            parts.append(f"Sex: {user['sex']}")
        profile_info = ". ".join(parts)

    workout_hist = db.get_workout_history(user_id, 30)
    recent_workouts = ""
    if workout_hist:
        recent_workouts = f"Recent workouts ({len(workout_hist)} in last 30 days): " + ", ".join(
            f"{w.get('type', 'workout')} {w.get('duration_min', '?')}min" for w in workout_hist[-5:]
        )

    prompt = f"""Event: {title}
Date: {event_date} ({days_out} days from now)
Sport/Type: {sport_type or 'general fitness'}
User description: {description}
{f'Profile: {profile_info}' if profile_info else ''}
{f'Recent activity: {recent_workouts}' if recent_workouts else 'No recent workout data'}

Create a preparation plan."""

    try:
        infer = get_inference(user_id)
        result = infer(
            system=EVENT_PREP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            json_mode=True,
        )
        raw = result["text"]
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        parsed = json.loads(raw)
        return {
            "prep_plan": parsed.get("prep_plan", {}),
            "science_notes": parsed.get("science_notes", ""),
            "readiness_markers": parsed.get("readiness_markers", []),
            "motivation_hooks": parsed.get("motivation_hooks", []),
            "nutrition_adjustments": parsed.get("nutrition_adjustments", ""),
            "days_out": days_out,
        }
    except (InferenceError, json.JSONDecodeError, Exception) as e:
        logger.warning("Prep plan generation failed: %s", e)
        return _deterministic_prep_plan(title, event_date, sport_type, days_out)


def _deterministic_prep_plan(title: str, event_date: str, sport_type: str | None, days_out: int) -> dict:
    """Fallback prep plan without LLM."""
    if days_out <= 7:
        phase = "Taper & prepare"
        tips = ["Focus on rest and recovery", "Practice event-specific movements at low intensity", "Hydrate well", "Get 8+ hours sleep"]
    elif days_out <= 14:
        phase = "Sharpen"
        tips = ["Reduce volume 20-30%", "Maintain intensity", "Practice event pace/rhythm", "Dial in nutrition"]
    elif days_out <= 30:
        phase = "Build"
        tips = ["Progressive overload on key movements", "Event-specific conditioning 3-4x/week", "Maintain protein >1g/lb", "Sleep 7-9 hours consistently"]
    else:
        phase = "Base building"
        tips = ["Build aerobic base and work capacity", "Gradual volume increases", "Address weak points", "Establish consistent schedule"]

    return {
        "prep_plan": {"phases": [{"name": phase, "weeks": f"1-{max(1, days_out // 7)}", "focus": phase, "key_workouts": tips[:2], "nutrition_notes": "Maintain caloric surplus for training, high protein"}], "taper_strategy": "Reduce volume 40-50% in final week" if days_out > 14 else "Already in taper window"},
        "science_notes": f"With {days_out} days until your {sport_type or 'event'}, your body will primarily be adapting through neuromuscular coordination, energy system efficiency, and sport-specific movement patterns.",
        "readiness_markers": ["Consistent training 4+ days/week", "Sleep averaging 7+ hours", "Energy levels stable", "No nagging injuries"],
        "motivation_hooks": [f"Every training session between now and {event_date} counts", "Your body is adapting even on rest days", "Trust the process — consistency beats intensity"],
        "days_out": days_out,
    }


def build_motivation_checkin(event_goal: dict, user_id: int) -> str:
    """Generate a motivation check-in message for an active event goal."""
    from fitnessbot.inference.factory import get_inference
    from fitnessbot.tz import utc_offset_hours as _utc_off

    now = datetime.now(timezone.utc)
    event_date = datetime.strptime(event_goal["event_date"], "%Y-%m-%d")
    days_remaining = (event_date.date() - now.date()).days

    workout_hist = db.get_workout_history(user_id, 7)
    recent_activity = f"{len(workout_hist)} workouts in the last 7 days" if workout_hist else "No workouts logged this week"

    # Richer context for emotional tone selection
    sleep_hist = db.get_sleep_history(user_id, 7)
    avg_sleep = None
    if sleep_hist:
        sleep_hours = [s.get("hours", 0) for s in sleep_hist if s.get("hours")]
        if sleep_hours:
            avg_sleep = sum(sleep_hours) / len(sleep_hours)

    macro_hist = db.get_macro_history(user_id, 7, utc_offset_hours=_utc_off(user_id))
    nutrition_context = ""
    if macro_hist:
        from fitnessbot.nutrition import get_nutrition_targets
        targets = get_nutrition_targets(user_id)
        over_days = sum(1 for d in macro_hist if (d.get("calories") or 0) > targets["calories"] * 1.05)
        avg_cal = sum(d.get("calories", 0) or 0 for d in macro_hist) / len(macro_hist)
        nutrition_context = f"Avg calories (7d): {avg_cal:.0f} (target: {targets['calories']}). Days over target: {over_days}/{len(macro_hist)}."

    # Training plan adherence
    from fitnessbot import training_plan
    from fitnessbot.training_plan import _monday_of_week
    from fitnessbot.tz import user_now
    ws = _monday_of_week(user_now(user_id).date())
    plan_items = training_plan.get_plan_items(user_id, ws)
    plan_context = ""
    if plan_items:
        adherence = training_plan.compute_adherence(plan_items)
        plan_context = f"Training plan: {adherence['label']}."

    prep_plan = json.loads(event_goal["prep_plan_json"]) if event_goal.get("prep_plan_json") else {}
    hooks = prep_plan.get("motivation_hooks", [])
    hook = hooks[days_remaining % len(hooks)] if hooks else ""

    prompt = f"""Event: {event_goal['title']}
Days remaining: {days_remaining}
Sport: {event_goal.get('sport_type', 'general')}
Recent activity: {recent_activity}
{f'Sleep avg (7d): {avg_sleep:.1f}h' if avg_sleep else 'Sleep: not tracked'}
{nutrition_context if nutrition_context else 'Nutrition: not enough data'}
{plan_context if plan_context else ''}
Motivation angle: {hook}

Pick the right emotional tone based on the data above and generate a check-in."""

    try:
        infer = get_inference(user_id)
        result = infer(
            system=MOTIVATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        return result["text"].strip()
    except (InferenceError, Exception):
        return _deterministic_motivation(event_goal["title"], days_remaining, recent_activity)


def _deterministic_motivation(title: str, days_remaining: int, recent_activity: str) -> str:
    """Fallback motivation without LLM."""
    lines = [f"📅 *{days_remaining} days* until {title}."]
    if "No workouts" in recent_activity:
        lines.append("No training logged this week — even a light session keeps momentum.")
    else:
        lines.append(f"{recent_activity} — keep building.")

    if days_remaining <= 7:
        lines.append("Final week: prioritize sleep, hydration, and confidence. You've done the work.")
    elif days_remaining <= 14:
        lines.append("Two weeks out — time to sharpen. Quality over quantity now.")
    else:
        lines.append("Consistency today = confidence on event day. What's today's session?")

    return "\n".join(lines)


def build_readiness_assessment(user_id: int, event_goal: dict) -> str:
    """Assess readiness for an event based on actual logged data."""
    from fitnessbot.inference.factory import get_inference

    now = datetime.now(timezone.utc)
    event_date = datetime.strptime(event_goal["event_date"], "%Y-%m-%d")
    days_remaining = (event_date.date() - now.date()).days

    from fitnessbot.tz import utc_offset_hours as _utc_off
    workout_hist = db.get_workout_history(user_id, 30)
    macro_hist = db.get_macro_history(user_id, 14, utc_offset_hours=_utc_off(user_id))
    sleep_hist = db.get_sleep_history(user_id, 14)

    prep_plan = json.loads(event_goal["prep_plan_json"]) if event_goal.get("prep_plan_json") else {}
    markers = prep_plan.get("readiness_markers", event_goal.get("readiness_markers", ""))

    data_lines = [
        f"Event: {event_goal['title']} on {event_goal['event_date']} ({days_remaining} days out)",
        f"Sport: {event_goal.get('sport_type', 'general')}",
        f"Workouts (30d): {len(workout_hist)} sessions",
    ]

    if macro_hist:
        avg_cal = sum(d.get("calories", 0) or 0 for d in macro_hist) / len(macro_hist)
        avg_pro = sum(d.get("protein", 0) or 0 for d in macro_hist) / len(macro_hist)
        data_lines.append(f"Nutrition (14d avg): {avg_cal:.0f} cal/day, {avg_pro:.0f}g protein/day")
    else:
        data_lines.append("Nutrition: No data logged")

    if sleep_hist:
        sleep_hours = [s.get("hours", 0) for s in sleep_hist if s.get("hours")]
        if sleep_hours:
            data_lines.append(f"Sleep (14d avg): {sum(sleep_hours)/len(sleep_hours):.1f}h")
    else:
        data_lines.append("Sleep: Not tracked")

    if markers:
        data_lines.append(f"Readiness markers to check: {markers if isinstance(markers, str) else json.dumps(markers)}")

    prompt = "\n".join(data_lines)

    try:
        infer = get_inference(user_id)
        result = infer(
            system=READINESS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return result["text"].strip()
    except (InferenceError, Exception):
        return _deterministic_readiness(event_goal, days_remaining, workout_hist, macro_hist, sleep_hist)


def _deterministic_readiness(event_goal: dict, days_remaining: int, workouts: list, macros: list, sleep: list) -> str:
    """Fallback readiness assessment without LLM."""
    lines = [f"📊 Readiness check — {event_goal['title']} ({days_remaining} days out)"]
    score = 5

    if workouts:
        per_week = len(workouts) / 4.3
        lines.append(f"Training: {len(workouts)} sessions in 30 days ({per_week:.1f}/week)")
        if per_week >= 4:
            score += 2
            lines.append("  ✓ Consistent training volume")
        elif per_week >= 2:
            score += 1
            lines.append("  ~ Moderate training — could increase frequency")
        else:
            lines.append("  ⚠ Low training volume for event prep")
            score -= 1
    else:
        lines.append("Training: No workouts logged — can't assess")
        score -= 2

    if macros:
        avg_pro = sum(d.get("protein", 0) or 0 for d in macros) / len(macros)
        if avg_pro >= 120:
            score += 1
            lines.append(f"Nutrition: {avg_pro:.0f}g protein/day avg ✓")
        else:
            lines.append(f"Nutrition: {avg_pro:.0f}g protein/day — consider increasing for recovery")
    else:
        lines.append("Nutrition: Not tracked")

    if sleep:
        sleep_hours = [s.get("hours", 0) for s in sleep if s.get("hours")]
        if sleep_hours:
            avg = sum(sleep_hours) / len(sleep_hours)
            if avg >= 7:
                score += 1
                lines.append(f"Sleep: {avg:.1f}h avg ✓")
            else:
                lines.append(f"Sleep: {avg:.1f}h avg — aim for 7-9h for optimal recovery")

    score = max(1, min(10, score))
    lines.insert(1, f"Readiness score: {score}/10")
    return "\n".join(lines)


def format_prep_plan_summary(plan_data: dict) -> str:
    """Format the prep plan for a Telegram message."""
    lines = []
    prep = plan_data.get("prep_plan", {})
    phases = prep.get("phases", [])
    for phase in phases[:3]:
        lines.append(f"*{phase.get('name', 'Phase')}* (weeks {phase.get('weeks', '?')})")
        lines.append(f"  Focus: {phase.get('focus', '')}")
        workouts = phase.get("key_workouts", [])
        if workouts:
            lines.append(f"  Key: {', '.join(workouts[:3])}")

    taper = prep.get("taper_strategy")
    if taper:
        lines.append(f"\n🔻 Taper: {taper}")

    tips = prep.get("race_day_tips", [])
    if tips:
        lines.append("\n🎯 Event day:")
        for tip in tips[:3]:
            lines.append(f"  • {tip}")

    return "\n".join(lines)
