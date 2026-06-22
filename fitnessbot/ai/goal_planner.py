"""Claude-powered goal planning and debrief."""

import json
import logging
import time

import anthropic

from fitnessbot.config import Config
from fitnessbot import db

logger = logging.getLogger(__name__)

PLAN_SYSTEM = """You are a sharp, no-nonsense fitness coach. Turn the user's rough goal into a structured plan.
Return ONLY a JSON object, no prose, no markdown fences, with exactly these keys:
"statement": a crisp, specific restatement of the goal (one sentence, measurable),
"why": one short motivating sentence on why this goal matters / what it unlocks,
"metric": the single number or signal that proves success (short phrase),
"targetDate": a realistic target as a short relative phrase (e.g. "8 weeks", "by Sept 1"),
"steps": an array of 4 to 7 concrete, checkable action steps in order (each a short imperative phrase).
Be specific and realistic. No medical claims."""

DEBRIEF_SYSTEM = """You are an honest, constructive fitness coach running a post-mortem on a MISSED goal.
Be direct but supportive. Ground everything in what the user actually says. No platitudes, no medical claims.
Return ONLY a JSON object, no markdown fences, with these keys:
"reasons": array of 2-4 short, specific likely reasons the goal was missed,
"improvements": array of 3-4 concrete, doable changes for next time,
"nextMove": one single sentence — the very next action to take this week."""


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)


def _parse_json(text: str) -> dict:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end >= 0:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def build_plan(raw_text: str, user_id: int | None = None) -> dict:
    client = _get_client()
    start = time.time()
    response = client.messages.create(
        model=Config.ANALYSIS_MODEL,
        max_tokens=1000,
        system=PLAN_SYSTEM,
        messages=[{"role": "user", "content": raw_text}],
    )
    latency_ms = (time.time() - start) * 1000
    raw_output = response.content[0].text.strip()

    try:
        db.insert_llm_analysis(
            kind="goal_plan",
            model=Config.ANALYSIS_MODEL,
            input_digest=raw_text[:200],
            output_text=raw_output[:500],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Failed to log LLM analysis: %s", e)

    return _parse_json(raw_output)


def run_debrief(
    statement: str,
    target_date: str,
    metric: str,
    steps_done: int,
    steps_total: int,
    notes: str,
    user_id: int | None = None,
) -> dict:
    client = _get_client()
    prompt = (
        f"GOAL: {statement}\n"
        f"TARGET: {target_date} · SUCCESS METRIC: {metric}\n"
        f"STEPS COMPLETED: {steps_done} of {steps_total}\n"
        f"WHAT GOT IN THE WAY (user's words): {notes or '(none provided)'}"
    )

    start = time.time()
    response = client.messages.create(
        model=Config.ANALYSIS_MODEL,
        max_tokens=1000,
        system=DEBRIEF_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = (time.time() - start) * 1000
    raw_output = response.content[0].text.strip()

    try:
        db.insert_llm_analysis(
            kind="goal_debrief",
            model=Config.ANALYSIS_MODEL,
            input_digest=prompt[:200],
            output_text=raw_output[:500],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Failed to log LLM analysis: %s", e)

    return _parse_json(raw_output)
