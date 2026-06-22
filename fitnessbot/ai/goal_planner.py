"""Goal planning and debrief via provider abstraction."""

import json
import logging
import time

from fitnessbot import db
from fitnessbot.inference.base import InferenceError

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


def _parse_json(text: str) -> dict:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end >= 0:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def build_plan(raw_text: str, user_id: int | None = None) -> dict:
    from fitnessbot.inference.factory import get_inference, get_inference_for_system

    try:
        infer = get_inference(user_id) if user_id else get_inference_for_system()
    except InferenceError as e:
        raise InferenceError(f"Cannot build goal plan: {e}")

    start = time.time()
    result = infer(
        system=PLAN_SYSTEM,
        messages=[{"role": "user", "content": raw_text}],
        max_tokens=1000,
        json_mode=True,
    )
    latency_ms = (time.time() - start) * 1000
    raw_output = result["text"]

    try:
        db.insert_llm_analysis(
            kind="goal_plan",
            model="provider",
            input_digest=raw_text[:200],
            output_text=raw_output[:500],
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
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
    from fitnessbot.inference.factory import get_inference, get_inference_for_system

    try:
        infer = get_inference(user_id) if user_id else get_inference_for_system()
    except InferenceError as e:
        raise InferenceError(f"Cannot run debrief: {e}")

    prompt = (
        f"GOAL: {statement}\n"
        f"TARGET: {target_date} · SUCCESS METRIC: {metric}\n"
        f"STEPS COMPLETED: {steps_done} of {steps_total}\n"
        f"WHAT GOT IN THE WAY (user's words): {notes or '(none provided)'}"
    )

    start = time.time()
    result = infer(
        system=DEBRIEF_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
        json_mode=True,
    )
    latency_ms = (time.time() - start) * 1000
    raw_output = result["text"]

    try:
        db.insert_llm_analysis(
            kind="goal_debrief",
            model="provider",
            input_digest=prompt[:200],
            output_text=raw_output[:500],
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
            latency_ms=latency_ms,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Failed to log LLM analysis: %s", e)

    return _parse_json(raw_output)
