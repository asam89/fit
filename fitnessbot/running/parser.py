"""Deterministic parser: free-text run message -> structured draft. No LLM, nothing saved here."""

import re
from datetime import date, timedelta

from fitnessbot.running.stats import M_PER_KM, M_PER_MI, pace_s_per_km

MIN_PACE_S_PER_KM = 150      # 2:30 /km
MAX_PACE_S_PER_KM = 900      # 15:00 /km
MAX_DISTANCE_M = 100_000
MAX_DURATION_S = 12 * 3600

_RUN_WORD = re.compile(
    r"\b(ran|rann|run|runs|runn|rnu|running|runing|jog|jogs|jogged|jogging|treadmill|intervals?|"
    r"tempo|long\s+run|parkrun|half\s+marathon|marathon|\d+(?:\.\d+)?\s*k\b)",
    re.I,
)
_NOT_A_RUN = re.compile(
    r"\b(ran|run|running|runs)\s+(out|into|errands?|late|over|away|around\s+(?:the\s+)?(?:house|store|errands?)|"
    r"the\s+(?:numbers|dishwasher|washer|meeting|show|code|tests?))\b|\brun(?:ning)?\s+(?:a|my)\s+(?:bath|business|errand)",
    re.I,
)

# Messages that mention a run but aren't a run to log now: other sports, steps, questions,
# future plans, PBs and meals. These fall through to the normal NLU path unchanged.
_OTHER_ACTIVITY = re.compile(
    r"\b(walk(?:ed|ing|s)?|hik(?:e|ed|es|ing)|bik(?:e|ed|es|ing)|bicycl\w*|cycl\w*|ride|rode|riding|"
    r"row(?:ed|ing)?|swim(?:ming)?|swam|elliptical|spin(?:ning)?|steps|skat\w*|ski(?:ed|ing)?)\b",
    re.I,
)
_QUESTION = re.compile(
    r"\?\s*$|^\s*(what|how|when|where|why|which|should|shall|can|could|would|will|do|does|did\s+i|is|are|"
    r"any|plan|schedule|move|skip|remind|suggest|give\s+me|make\s+me)\b",
    re.I,
)
_FUTURE = re.compile(
    r"\b(want\s+to|wanna|going\s+to|gonna|planning|plan\s+to|will\s+run|tomorrow|next\s+(?:week|month|year|time)|"
    r"signed\s+up|registered|(?:i'?m|i\s+am)\s+running)\b",
    re.I,
)
_PB_WORDS = re.compile(r"\b(pb|pr|personal\s+(?:best|record))\b", re.I)
_FOOD = re.compile(r"\b(ate|eaten|drank|had\s+\d|for\s+(?:breakfast|lunch|dinner|a\s+snack|snack))\b", re.I)

_UNIT_KM = r"(?:k|km|kms|kilometers?|kilometres?|klm)"
_UNIT_MI = r"(?:mi|mile|miles|mls?)"
_DIST = re.compile(rf"(\d+(?:[.,]\d+)?)\s*({_UNIT_KM}|{_UNIT_MI}|m|meters?|metres?)\b", re.I)
_NAMED_DIST = (
    (re.compile(r"\bhalf[\s-]*marathon\b", re.I), 21097.5),
    (re.compile(r"\bmarathon\b", re.I), 42195.0),
)
_INTERVALS = re.compile(r"\b(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(m|k|km|mi)?\b", re.I)
_PACE = re.compile(
    r"(?:@|at)?\s*(\d{1,2}):(\d{2})\s*(?:min\s*)?(?:/\s*(km|k|mi|mile)|per\s+(km|k|mile|mi)|\s*pace\b)",
    re.I,
)
_HMS = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2}):(\d{2})(?![\d:])")
_MS = re.compile(r"(?<![\d:])(\d{1,3}):(\d{2})(?![\d:])")
_H_M = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)(?:\s*(\d{1,2})\s*(?:min|mins|minutes?|m)\b|(\d{2})\b|\b)", re.I)
_MIN = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:min|mins|minutes?|mns?)\b", re.I)
_AVG_HR = re.compile(r"\b(?:avg|average|av)\.?\s*(?:hr|heart\s*rate|bpm)\s*(?:of\s*)?(\d{2,3})\b|\b(?:hr|heart\s*rate)\s*(?:avg\s*)?(\d{2,3})\b|\b(\d{2,3})\s*bpm\b", re.I)
_MAX_HR = re.compile(r"\bmax\s*(?:hr|heart\s*rate)\s*(\d{2,3})\b", re.I)
_RPE = re.compile(r"\b(?:rpe|effort)\s*(\d{1,2})\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!:)",
    re.I,
)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY = re.compile(r"\b(?:on|last)\s+(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b", re.I)

_TYPE_WORDS = (
    ("interval", re.compile(r"\b(intervals?|repeats|reps|fartlek)\b|\d+\s*[x×]\s*\d+", re.I)),
    ("treadmill", re.compile(r"\btreadmill\b", re.I)),
    ("race", re.compile(r"\b(race|parkrun|raced)\b", re.I)),
    ("tempo", re.compile(r"\b(tempo|threshold)\b", re.I)),
    ("long", re.compile(r"\blong\s*run\b|\blong\b", re.I)),
    ("easy", re.compile(r"\b(easy|recovery|shakeout|slow)\b", re.I)),
)
_SURFACE_WORDS = (
    ("treadmill", re.compile(r"\btreadmill\b", re.I)),
    ("trail", re.compile(r"\btrails?\b", re.I)),
    ("track", re.compile(r"\btrack\b", re.I)),
    ("road", re.compile(r"\broad\b", re.I)),
)


def looks_like_run(text: str) -> bool:
    t = text or ""
    if _NOT_A_RUN.search(t) or not _RUN_WORD.search(t):
        return False
    if any(p.search(t) for p in (_OTHER_ACTIVITY, _QUESTION, _FUTURE, _PB_WORDS, _FOOD)):
        return False
    return bool(_DIST.search(t) or _INTERVALS.search(t) or _HMS.search(t) or _MS.search(t)
                or _MIN.search(t) or _H_M.search(t) or any(p.search(t) for p, _ in _NAMED_DIST))


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _to_m(value: float, unit: str | None, default_units: str) -> float:
    u = (unit or "").lower()
    if re.fullmatch(_UNIT_MI, u):
        return value * M_PER_MI
    if re.fullmatch(_UNIT_KM, u):
        return value * M_PER_KM
    if u in ("m", "meter", "meters", "metre", "metres"):
        return value
    return value * (M_PER_MI if default_units == "mi" else M_PER_KM)


def parse_distance(text: str, default_units: str = "km") -> float | None:
    """Distance in metres from text like '5k', '3.1 miles', '800m', or a bare number in default units."""
    t = text.strip()
    for pat, metres in _NAMED_DIST:
        if pat.search(t):
            return metres
    m = _DIST.search(t)
    if m:
        unit = m.group(2)
        val = _num(m.group(1))
        if unit.lower() == "m" and val < 50:  # "5m" almost always means minutes, not metres
            return None
        return _to_m(val, unit, default_units)
    bare = re.fullmatch(r"(\d+(?:[.,]\d+)?)", t)
    return _to_m(_num(bare.group(1)), None, default_units) if bare else None


def parse_duration(text: str) -> int | None:
    """Duration in seconds from '27:30', '1:05:30', '58 min', '1h05', '1.5 hours'. Ignores paces."""
    t = _AVG_HR.sub(" ", _MAX_HR.sub(" ", _PACE.sub(" ", text)))
    m = _HMS.search(t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = _H_M.search(t)
    if m:
        return int(round(_num(m.group(1)) * 3600 + int(m.group(2) or m.group(3) or 0) * 60))
    m = _MS.search(t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _MIN.search(t)
    if m:
        return int(round(_num(m.group(1)) * 60))
    return None


def _parse_pace(text: str, default_units: str) -> float | None:
    m = _PACE.search(text)
    if not m:
        return None
    secs = int(m.group(1)) * 60 + int(m.group(2))
    unit = (m.group(3) or m.group(4) or default_units).lower()
    return secs / (M_PER_MI / M_PER_KM) if unit.startswith("mi") else secs


def _parse_date(text: str, today: date) -> date:
    t = text.lower()
    if re.search(r"\byesterday\b|\blast\s+night\b", t):
        return today - timedelta(days=1)
    m = _ISO_DATE.search(t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _MONTH_DATE.search(t)
    if m:
        try:
            d = date(today.year, _MONTHS[m.group(1)[:3]], int(m.group(2)))
            return d if d <= today else d.replace(year=today.year - 1)
        except ValueError:
            pass
    m = _WEEKDAY.search(t)
    if m:
        target = next(i for i, w in enumerate(_WEEKDAYS) if w.startswith(m.group(1)[:3]))
        back = (today.weekday() - target) % 7 or 7
        return today - timedelta(days=back)
    return today


def parse_run(text: str, *, today: date, default_units: str = "km") -> dict:
    """Parse a run message into a draft dict.

    Keys: distance_m, duration_s, date (ISO), run_type, surface, avg_hr, max_hr,
    perceived_effort, splits, notes, missing (list of required fields not found).
    """
    draft: dict = {"date": _parse_date(text, today).isoformat(), "notes": text.strip()[:500]}
    pace = _parse_pace(text, default_units)

    iv = _INTERVALS.search(text)
    distance = duration = None
    if iv:
        reps, rep_val = int(iv.group(1)), _num(iv.group(2))
        unit = iv.group(3) or ("m" if rep_val >= 50 else default_units)
        rep_m = _to_m(rep_val, unit, default_units)
        distance = reps * rep_m
        if pace:
            rep_s = pace * rep_m / M_PER_KM
            draft["splits"] = [{"distance_m": rep_m, "duration_s": round(rep_s, 1)} for _ in range(reps)]
            duration = int(round(rep_s * reps))
    else:
        cleaned = _INTERVALS.sub(" ", text)
        distance = parse_distance(cleaned, default_units) if _DIST.search(cleaned) or any(
            p.search(cleaned) for p, _ in _NAMED_DIST) else None
    if duration is None:
        duration = parse_duration(text)
    if distance is None and duration and pace:
        distance = duration / pace * M_PER_KM
    if duration is None and distance and pace:
        duration = int(round(distance / M_PER_KM * pace))

    draft["distance_m"] = round(distance, 1) if distance else None
    draft["duration_s"] = int(duration) if duration else None

    for name, pat in _TYPE_WORDS:
        if pat.search(text):
            draft["run_type"] = name
            break
    for name, pat in _SURFACE_WORDS:
        if pat.search(text):
            draft["surface"] = name
            break
    m = _MAX_HR.search(text)
    if m:
        draft["max_hr"] = int(m.group(1))
    m = _AVG_HR.search(_MAX_HR.sub(" ", text))
    if m:
        draft["avg_hr"] = int(next(g for g in m.groups() if g))
    m = _RPE.search(text)
    if m and 1 <= int(m.group(1)) <= 10:
        draft["perceived_effort"] = int(m.group(1))

    draft["missing"] = [k for k in ("distance_m", "duration_s") if not draft.get(k)]
    return draft


def guardrail_questions(draft: dict, default_units: str = "km") -> list[str]:
    """Clarifying questions for implausible values. Empty list means the draft looks sane."""
    from fitnessbot.running.stats import format_distance, format_duration, format_pace

    qs = []
    d, t = draft.get("distance_m"), draft.get("duration_s")
    if d and d > MAX_DISTANCE_M:
        qs.append(f"{format_distance(d, default_units)} is longer than 100 km. Is that right?")
    if t and t > MAX_DURATION_S:
        qs.append(f"{format_duration(t)} is longer than 12 hours. Is that right?")
    p = pace_s_per_km(d, t) if d and t else None
    if p and p < MIN_PACE_S_PER_KM:
        qs.append(f"That's a {format_pace(p, default_units)} pace, faster than world-record speed. "
                  f"Did you mean a different distance or time?")
    elif p and p > MAX_PACE_S_PER_KM:
        qs.append(f"That's a {format_pace(p, default_units)} pace, slower than a walk. "
                  f"Did you mean a different distance or time?")
    return qs
