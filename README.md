# fit

A personal fitness and health coach built around a **conversational Telegram bot** and a **web dashboard** that read and write the same data.

Live at [fit-ness.ca](https://fit-ness.ca).

You talk to the bot in plain English — "chicken burrito and a coke for lunch", "did basketball for an hour", "weight 218 this morning", "how am I doing today?" — and it parses the message, logs it, and replies as a coach with context on your targets, training plan, and weight trend. Everything you log shows up on the dashboard, and everything on the dashboard is visible to the bot.

```
Telegram message ──► NLU (intent + structured data) ──► act (write to DB) ──► coaching reply
                                                            │
Web dashboard ◄─────────────── same SQLite tables ──────────┘
```

## What it does

**Natural-language logging.** Meals (text, voice note, or photo), workouts, weight, sleep, resting HR, HRV, blood pressure, SpO2, body fat, mood, energy, and hydration. One message can carry several things at once and is split into multiple intents. Voice notes are transcribed locally with `faster-whisper`; meal photos are analyzed by a vision model.

**Nutrition targets.** Calorie and macro targets are derived from your profile (Mifflin-St Jeor BMR → activity multiplier → goal adjustment), with a safety floor. Once you have ~4 weeks of weight and intake history, targets switch to an **adaptive TDEE** computed from your actual weight change rather than the formula. Fiber, sugar, and sodium are tracked alongside protein/carbs/fat.

**Weekly training plan.** A Mon–Sun plan of activities lives in `training_plans` / `training_plan_items`, editable from the dashboard or by telling the bot. Say "mark my workout as done" and it matches and completes today's planned item. Any workout you log that *isn't* on the plan still lands on the calendar as a completed item, and the bot offers inline day buttons to reassign it — so the dashboard calendar and the workout log never disagree.

**Health benefits per activity.** MET values from the Compendium of Physical Activities estimate calories burned per session and classify each activity by benefit type (cardiovascular, muscle building, flexibility) and muscle groups worked, summarized daily and weekly.

**Weight trends.** Each weigh-in is stored raw and as an exponentially smoothed value, with 7-day and 30-day deltas matched to the nearest entry within a tolerance (±3 and ±5 days) so a missed weigh-in doesn't blank the trend. The bot turns that into a verdict — whether your diet and training are actually working — plus suggestions.

**A coach with one consistent voice.** A single persona block in `ai/prompts.py` is composed with task-specific instructions and your context for *every* generated message, so live replies, briefings, and event coaching all sound like the same person. Feedback tone is per-user (`supportive` / `neutral` / `blunt`) and changeable in natural language ("be more blunt with me"). Tone also adapts to your data: specific praise when you're performing, corrective when you're slipping, re-motivating when you say you're struggling — never shaming.

**Meal quality nudges.** A logged meal is scored from its food names (fast food, deep-fried, liquid sugar, dessert, alcohol) and macros (sugar, sodium, big calories with no protein). Indulgent meals get an honest call-out plus a push to move — but only if you haven't trained that day, and as a real 15–30 minute bout, never "burn off what you ate" math. Food suggestions rotate across ~58 items tagged by the meal slot they suit, skipping anything in your last 12 meals, so it doesn't just say "chicken breast" every time.

**Training intelligence.** Ask "how should I train this week?" or "how do I build explosiveness?" and it pulls your actual week of planned and logged sessions plus your goals, then coaches on proximity to failure, progressive overload, the breakdown → supercompensation cycle, rep ranges for your goal, and how a set and the next morning should feel — including which sensations mean stop.

**Research-grounded answers.** Ask "what does the research say about training to failure?" and it queries PubMed (NCBI E-utilities), summarizes real abstracts, and appends citations built deterministically from the API response, so links always resolve to the actual paper.

**Event goals.** Register an upcoming event ("basketball tournament July 17th") and get a prep plan, days-out coaching check-ins, and readiness assessments on request.

**Scheduled briefings.** Morning brief, midday check, evening wrap, and a weekly rollup, sent on each user's own schedule in their own timezone, with quiet hours respected. Dedup is keyed on the user's local date, and event check-ins are capped at one per day.

**Wearable ingestion.** `/sync` accepts Apple Health, Samsung Health, Garmin, Fitbit, Oura, and WHOOP exports as an attachment or pasted text.

**Dashboard.** Today's ring and macro bars, weight tracking card with trend chart and goal analysis, health benefits (today / this week), training plan grid, meals with photos and expandable nutrition detail, a food diary page, personal bests, a 30-day consistency heatmap, trends, and friends. Mobile layout is separately optimized; desktop is unchanged.

**Multi-user, bring-your-own bot.** Each user connects their *own* Telegram bot token (encrypted at rest with Fernet) and the `ConnectionManager` runs one polling instance per user. LLM keys are also per-user, over a provider-agnostic layer supporting Anthropic, OpenAI, and Gemini.

## Stack

FastAPI + Jinja2 (server-rendered, no frontend build step) · `python-telegram-bot` · SQLite with numbered in-code migrations · APScheduler · Anthropic / OpenAI / Gemini behind a provider abstraction · `faster-whisper` for local voice transcription.

Timezone correctness matters throughout: every user-facing date goes through the helpers in `fitnessbot/tz.py` (`user_today`, `user_now`, `day_utc_range`) rather than UTC, because UTC dates caused duplicate messages and wrong daily totals for evening-timezone users.

## Layout

```
fitnessbot/
  main.py            entrypoint — FastAPI app + bot manager + scheduler in one process
  config.py          env-based config
  db.py              schema, migrations, and all DAL helpers
  tz.py              user-local date/time — single source of truth
  bot/
    conversation.py  the understand → act → respond loop (NLU, intent dispatch, replies)
    handlers.py      Telegram handlers: text, voice, photo, commands, callbacks
    manager.py       per-user bot polling instances
  ai/
    prompts.py       COACH_PERSONA + compose_prompt() + task prompts
    food_parser.py   meal text/photo → structured macros
    goal_planner.py  goal planning and debrief
  inference/         provider abstraction (Anthropic, OpenAI, Gemini) + per-user key resolution
  web/               FastAPI routers + Jinja templates (dashboard, settings, goals, plan, admin, social)
  nutrition.py       BMR/TDEE, adaptive TDEE, macro targets
  metrics.py         weight, body composition, vitals, trend math
  training_plan.py   weekly plan CRUD, adherence, reconciliation
  health_benefits.py MET-based calorie burn and benefit classification
  meal_quality.py    meal scoring and movement nudges
  pubmed.py          PubMed evidence lookup
  briefings.py       morning / midday / evening / weekly messages
  event_coaching.py  event prep, check-ins, readiness
  scheduler.py       per-minute dispatcher honoring per-user schedules
tests/               266 tests, no external calls
docs/ADMIN_GUIDE.md  operations: deployment, DB, monitoring, troubleshooting
```

## Running it

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set SECRET_KEY and ENCRYPTION_KEY at minimum
python -m fitnessbot.main # dashboard on :8000; migrations run automatically at startup
```

Then register at `/register`, and in Settings connect a Telegram bot token (from [@BotFather](https://t.me/BotFather)) and an LLM API key. The bot starts polling as soon as the token is saved.

`docker compose up` works too. For production deployment (systemd unit in `infrastructure/`, Nginx, backups, monitoring), see the [Administration Guide](docs/ADMIN_GUIDE.md).

```bash
python -m pytest       # 266 tests
```

## Notes and boundaries

The coach is deliberately **not** a medical tool. It gives educational guidance only, never diagnoses, tells you to see a professional for pain or possible injury rather than pushing through it, and won't recommend extreme restriction or overtraining. It also won't shame you over a meal — one indulgent meal is a data point, not a failure — and it never frames exercise as punishment for eating.
