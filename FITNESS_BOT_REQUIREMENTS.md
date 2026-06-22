# Fitness & Health Intelligence Platform — Build Requirements

**Target executor:** Devin AI (autonomous engineering)
**Owner:** Alex
**Last updated:** 2026-06-22

---

## 0. TL;DR for the agent

Build a **multi-user fitness & health intelligence platform** with a **web front-end dashboard** and per-user **Telegram bot connections**. Users sign up via the web dashboard, create a profile (including health data like blood work, body fat %, weight), and connect their own Telegram bot (by providing a bot token + chat ID). Once connected, the Telegram bot lets them track meals, body metrics, and fitness data through **natural-language and voice messages**.

**Claude AI is the core intelligence layer** — it parses meal descriptions into full nutritional breakdowns (no third-party food API dependency), generates personalized diet plans based on all available health data, creates event-driven training programs (e.g. "get game-shape ready for a basketball tournament in 25 days"), and continuously adapts calorie/macro targets based on actual progress. The web dashboard provides visualization, goal management, profile editing, and AI-generated plans.

The platform runs on an **OCI Ampere (ARM64, Ubuntu 24.04)** instance. The bot transcribes voice, logs meals with AI-inferred macro/calorie data, stores everything in a local database, ingests data from smart devices (watch + ring) via a webhook, **proactively asks for missing data points**, and uses the **Claude API to infer health trends** and produce periodic reports.

Treat this as a phased build. Ship Phase 1 end-to-end and working before moving on. Every phase must be independently runnable and testable.

---

## 1. Goals & non-goals

### Goals
- **Web dashboard** — a responsive front-end where users sign up, create a profile, configure their Telegram bot connection, view logged data, charts, AI-generated plans, and reports.
- **Multi-user** — users register, manage their own profile, and each user connects their own Telegram bot. The system manages multiple independent bot connections.
- **Telegram bot connection** — users provide their own BotFather-created bot token + chat ID via the dashboard. The platform spins up a polling/webhook listener for each connected bot.
- **Claude-powered nutritional inference** — users type or speak natural meal descriptions ("I ate one multi grain toast with jam and butter and 2 cucumbers") and Claude AI infers calories, macros, and full nutritional breakdown. No dependency on Nutritionix or other food APIs — Claude is the primary food intelligence engine.
- **AI-personalized diet plans** — Claude generates a unique diet plan for each user based on all available data points: blood work results, body fat %, weight, fitness metrics, activity level, health history, and stated goals.
- **Event-driven goal coaching** — users set goals tied to real events ("basketball tournament on July 17th") and Claude builds a progressive plan: daily calorie targets, small daily workouts, gym sessions, tapering schedule, and game-day nutrition — all adapted as the event approaches.
- **Running calorie budget** — every meal logged is instantly scored against the user's AI-recommended daily calorie/macro target, with a running total and remaining budget shown after each entry.
- Log meals by typing or **speaking** into Telegram.
- Track weight progression with **trend smoothing** (filter daily water-weight noise).
- Track body composition, sleep, vitals, and workouts.
- Ingest smartwatch / smart-ring data automatically.
- Use **Claude** to surface trends, correlations, and anomalies, and to write weekly/monthly summaries.
- Have the bot **ask me for data points** when it's missing something, instead of me remembering to log.
- Run entirely self-hosted on my own OCI instance. No third-party SaaS dependency beyond the APIs listed.

### Non-goals (for now)
- No hosting or provisioning Telegram bots on behalf of users — each user creates and manages their own bot via BotFather and supplies the token.
- No mobile-native app. The web dashboard is responsive; Telegram is the real-time interface.
- No medical-grade claims. This is personal informational tracking, not diagnosis.

---

## 2. Infrastructure & constraints

| Item | Detail |
|---|---|
| Host | OCI Ampere A1, **ARM64 / aarch64** |
| OS | Ubuntu 24.04 LTS |
| Networking | Tailscale (OCI node reachable on tailnet); bot also needs a public HTTPS path for the device webhook (see §6) |
| Process mgmt | **systemd** service, auto-restart on failure, start on boot |
| Language | Python 3.12 |
| DB | SQLite to start (single file, WAL mode). Schema written so a Postgres migration is low-friction later. |
| Web framework | **FastAPI** serves both the API and the dashboard (Jinja2 templates + Tailwind CSS, or a static SPA build) |
| Secrets | `.env` file loaded via `python-dotenv`, never committed. Provide `.env.example`. |
| Logging | Structured logging to stdout (captured by `journald`) + rotating file handler. |

**ARM64 gotcha:** every Python dependency must have an aarch64 wheel or build cleanly on ARM. Pin versions in `requirements.txt` and verify the install on the target arch (or in an ARM container) before declaring done. Avoid libraries with x86-only binaries.

---

## 3. Tech stack

- **Web framework / API:** `FastAPI` + `uvicorn` (serves the dashboard and the REST API).
- **Dashboard front-end:** Jinja2 templates + **Tailwind CSS** + HTMX for interactivity (keeps the stack Python-only; no separate Node build step). Or optionally a lightweight SPA (React/Preact) if the dashboard grows complex enough.
- **Authentication:** session-based auth with secure cookies (via `itsdangerous` or `python-jose` for JWT). Registration + login on the web dashboard.
- **Bot framework:** `python-telegram-bot` (v21+, async). One bot instance per user connection, managed by a connection manager.
- **Voice transcription:** OpenAI Whisper API (`whisper-1`) for the audio → text step. (Keep the transcription call behind an interface so a local `whisper.cpp` ARM build can be swapped in later to cut cost/latency.)
- **Food / nutrition intelligence:** **Claude AI** — no third-party food API required. Claude parses natural-language meal descriptions and infers per-food calories, protein, carbs, fat, fiber, sugar, sodium. A local `foods` cache table stores previous inferences for consistency and speed. Nutritionix can optionally be wired in as a verification/fallback layer later.
- **Diet & training plan generation:** **Claude AI** — ingests the user's full health profile (blood work, body composition, fitness metrics, goals) and generates personalized diet plans, calorie targets, and training programs.
- **Reasoning / trends:** Anthropic Claude API. Use a current model (e.g. `claude-sonnet-4-6` for routing/quick calls, escalate to a larger model for the heavy weekly analysis and plan generation).
- **Scheduling:** `APScheduler` (async) for summaries, proactive prompts, plan recalculations, and report generation.
- **DB access:** `aiosqlite` (async) or `sqlite3` with a thin DAL; use parameterized queries everywhere.
- **HTTP webhook (device ingest):** integrated into the FastAPI app. Must coexist with the Telegram polling loops.

---

## 4. Module layout

```
fitnessbot/
├── config.py              # env loading, constants, model names, API clients
├── db.py                  # schema init, migrations, DAL helpers
├── main.py                # wires everything, starts event loop
│
├── web/                   # --- Web Dashboard ---
│   ├── app.py             # FastAPI app factory, mount routes
│   ├── auth.py            # registration, login, session/token management
│   ├── dashboard.py       # dashboard routes: home, charts, reports
│   ├── profile.py         # user profile CRUD routes
│   ├── health_data.py     # health data intake routes (blood work, metrics upload)
│   ├── connections.py     # Telegram bot connection management routes
│   ├── plans.py           # diet plan + training plan views and management
│   ├── goals.py           # event-driven goal setup and tracking
│   └── templates/         # Jinja2 HTML templates
│       ├── base.html
│       ├── login.html
│       ├── register.html
│       ├── dashboard.html
│       ├── profile.html
│       ├── health_data.html
│       ├── connections.html
│       ├── plans.html
│       ├── goals.html
│       └── reports.html
│
├── bot/                   # --- Telegram Bot Layer ---
│   ├── manager.py         # ConnectionManager: start/stop per-user bot instances
│   ├── handlers.py        # handlers: text, voice, commands, callbacks
│   └── commands.py        # command definitions and help text
│
├── ai/                    # --- Claude AI Intelligence Layer ---
│   ├── food_parser.py     # meal text → structured nutritional breakdown via Claude
│   ├── diet_planner.py    # generate/update personalized diet plans from health profile
│   ├── training_planner.py # generate event-driven workout programs
│   ├── goal_coach.py      # event-goal progress tracking, plan adaptation, check-ins
│   └── prompts.py         # all Claude prompt templates (versioned, testable)
│
├── voice.py               # download ogg → Whisper → text
├── router.py              # classify freetext: meal | metric | query | goal | data-answer
├── meals.py               # Claude-inferred meal logging, daily totals, budget tracking
├── metrics.py             # weight/body-comp/vitals logging + trend math
├── devices.py             # webhook server, payload parsing, normalization
├── proactive.py           # detect missing data, queue + send questions
├── analysis.py            # Claude trend inference, anomaly detection, reports
└── scheduler.py           # APScheduler jobs (summaries, plan recalcs, prompts)
```

---

## 5. Core features & behavior

### 5.0 User registration, profiles & Telegram connection

#### Web registration & login
1. User visits the dashboard, clicks **Register**.
2. Fills in: email, password, display name, timezone, sex, height, birthdate, unit preference (metric/imperial).
3. Account is created; user is logged in and lands on the dashboard home.
4. **Login** is email + password with session cookie.

#### Profile management
- Users can edit all profile fields from a **Profile** page on the dashboard.
- Profile data drives calorie targets, display preferences, and bot personalization.

#### Health data intake
- From the **Health Data** page, users can input or upload:
  - **Blood work results** — key markers (cholesterol, triglycerides, fasting glucose, HbA1c, iron, vitamin D, testosterone, thyroid, etc.). Free-form text entry or structured form; Claude parses either.
  - **Body composition** — current weight, body fat %, lean mass, waist measurement.
  - **Fitness baseline** — current activity level, max lifts, resting HR, VO2max estimate, any injuries/limitations.
  - **Medical notes** — allergies, dietary restrictions, medications (used by Claude to avoid dangerous recommendations).
- This data feeds directly into the AI diet/training plan generation. Users can update it anytime; Claude re-evaluates plans when significant data changes.

#### Telegram bot connection
1. From the dashboard **Connections** page, the user clicks **"Connect Telegram Bot"**.
2. They enter their **Bot Token** (from @BotFather) and **Chat ID** (their personal chat or a group ID).
3. The platform validates the token by calling the Telegram `getMe` API. If valid, the connection is saved.
4. The platform's **ConnectionManager** starts a polling loop for that bot.
5. The user can **test** the connection (sends a hello message), **disconnect**, or **replace** the token.
6. Each user can have **one active Telegram bot connection** (expandable to multiple later).
7. Bot tokens are stored encrypted at rest; never displayed in full after initial entry.

#### Dashboard home
- After login, the dashboard shows: today's macro summary with remaining calorie budget, weight trend chart, recent meals, active diet/training plan summary, upcoming event goals, active alerts, and connection status.
- Charts rendered server-side (matplotlib → PNG) or client-side (Chart.js via CDN).

### 5.1 Claude-powered meal logging (the headline feature)

This replaces a traditional food API with Claude AI as the nutritional inference engine.

**Flow:**
1. User sends a Telegram message (typed or voice): *"I ate one multi grain toast with jam and butter and 2 cucumbers"*
2. If voice: bot downloads `.ogg` → Whisper → transcript text.
3. Transcript goes into `router.py` → classified as a **meal**.
4. `ai/food_parser.py` sends the text to Claude with a structured prompt:
   - Claude returns JSON: each food item with estimated calories, protein, carbs, fat, fiber, sugar, sodium, and serving size.
   - Claude uses its training knowledge of food databases, USDA data, and common serving sizes.
   - The prompt includes the user's unit preference (metric/imperial) for serving sizes.
5. Results are cached in the `foods` table so identical descriptions resolve instantly next time.
6. Bot replies with a **confirmation card**:
   ```
   🍞 Multigrain toast w/ jam & butter — 280 cal | P: 5g C: 38g F: 12g
   🥒 Cucumber (2) — 30 cal | P: 1g C: 6g F: 0g
   ─────────────────────────────
   Meal total: 310 cal | P: 6g C: 44g F: 12g

   Today so far: 1,240 / 1,800 cal (560 remaining)
   Protein: 82 / 140g | Carbs: 160 / 200g | Fat: 48 / 60g
   ```
7. If Claude flags ambiguity (portion size unclear, multiple interpretations), the bot asks a clarifying question with inline buttons.
8. User can reply "no" or "fix" to correct a misparse — the bot re-prompts or lets them edit.

**Why Claude over Nutritionix:**
- Handles typos, slang, regional food names, and complex descriptions naturally.
- No per-query API cost to a third-party — just Claude tokens (which are already used for routing).
- Can infer cooking method impact (fried vs. baked), estimate restaurant portions, and handle compound dishes ("my mom's chicken biryani").
- Nutritionix can be added as an optional verification/fallback layer later if desired.

### 5.2 Smart freetext router
A single inbound message (typed or transcribed) gets classified into one of:
- **meal log** → §5.1
- **metric entry** → "weight 182", "slept 6 hours", "resting heart rate 54"
- **query** → "how's my protein this week?", "am I losing weight?"
- **goal update** → "my tournament is now July 20th", "I want to target 175 lbs"
- **data-answer** → a reply to a question the bot previously asked (match against the pending-question queue first)

Implement as a fast Claude classification call returning structured JSON (intent + extracted fields). Keep a deterministic keyword fast-path for obvious cases to save API calls. Always fall back gracefully ("I logged that as a meal — say 'no' if that's wrong").

### 5.3 AI-personalized diet plans

Claude generates a **unique diet plan** for each user based on their complete health profile.

**Inputs (all optional — Claude works with whatever is available, asks for more over time):**
- Body metrics: weight, height, body fat %, lean mass, waist measurement
- Blood work: cholesterol, triglycerides, glucose, HbA1c, iron, vitamin D, hormones, thyroid
- Fitness data: activity level, workout frequency, resting HR, VO2max, step count averages
- Goals: target weight, target body fat %, event deadlines, performance goals
- Restrictions: allergies, dietary preferences (vegetarian, halal, keto, etc.), medications
- History: logged meals, weight trend, adherence patterns from past weeks

**Output:**
- **Daily calorie target** — not a static TDEE formula; Claude reasons over all data points and adjusts. Example: "Based on your blood work showing elevated triglycerides and your goal to cut 10 lbs before July 17th, I recommend 1,800 cal/day with a focus on reducing refined carbs."
- **Macro split** — protein/carbs/fat targets tailored to the goal and health markers.
- **Meal timing suggestions** — based on workout schedule and activity patterns.
- **Foods to emphasize / avoid** — based on blood work and goals (e.g., "increase omega-3 sources" if triglycerides are high).
- **Weekly recalculation** — Claude reviews the past week's actual intake, weight trend, and any new data, then adjusts the plan. The adjustment is surfaced via Telegram and on the dashboard.

**Dashboard view:** full diet plan displayed on the **Plans** page with rationale, current targets, and history of plan revisions.

### 5.4 Event-driven goal coaching

Users can set goals tied to specific events, and Claude builds a complete progressive plan.

**Example:** *"I have a basketball tournament on July 17th, 2026. It's currently June 22, 2026. What's the best way to lose weight and be game-shape ready?"*

**How it works:**
1. User creates a goal via the dashboard **Goals** page or by telling the bot.
2. Claude receives: the event details, days remaining, the user's full health profile, current fitness level, and any constraints.
3. Claude generates a **phased training + nutrition plan**:
   - **Weeks 1-2 (build phase):** higher calorie deficit, strength-focused gym sessions 3x/week, daily 20-min mobility/cardio.
   - **Week 3 (conditioning phase):** moderate deficit, sport-specific drills, HIIT sessions, basketball conditioning.
   - **Final days (taper):** calorie normalization, light movement, hydration focus, game-day nutrition plan.
4. Each day the user gets:
   - A **small daily workout** pushed via Telegram (e.g., "Morning: 15-min walk + dynamic stretching. Evening: 10-min core circuit").
   - A **gym session** on training days (e.g., "Legs & Conditioning: 5x5 squats @ 185, 3x12 lunges, 4x20s battle ropes, 3x400m runs").
   - **Calorie/macro targets** adjusted for training vs. rest days.
5. Claude tracks progress against the plan and adapts:
   - If weight loss is ahead of schedule → increase calories slightly to preserve energy.
   - If behind → adjust training intensity or suggest dietary tweaks.
   - If the user logs feeling fatigued → pull back volume, suggest rest.
6. **Countdown** visible on the dashboard and in daily bot summaries.

**Multiple concurrent goals** are supported (e.g., a weight target + an event).

### 5.5 Running calorie budget & real-time tracking

Every meal logged is instantly scored against the AI-set daily target:
- After each meal, the bot shows: meal calories, running daily total, remaining budget, and per-macro progress bars.
- If a meal puts the user over target, Claude adds a gentle note: "You're 120 cal over today's target. Consider a lighter dinner — here's a suggestion: grilled chicken salad (~350 cal)."
- The **dashboard home** shows a real-time calorie ring/progress bar: consumed vs. target, with macro breakdown.
- End-of-day summary compares actual vs. target and notes the surplus/deficit.

### 5.6 Meal & macro tracking
- Claude-inferred foods are **cached** in a local `foods` table for consistency and speed.
- Track per-meal and per-day totals: calories, protein, carbs, fat, fiber, sugar, sodium.
- Support meal types (breakfast/lunch/dinner/snack) inferred from time of day, overridable.
- `/today` command → today's totals and remaining macros vs. AI-set target.
- Scheduled daily summary (e.g. 9pm) pushed to Telegram.
- **Dashboard view:** daily/weekly macro breakdown with charts, meal history with edit/delete.

### 5.7 Weight & body-composition tracking
- Log weight from a message, the device webhook, or a proactive prompt.
- **Trend smoothing:** store raw weigh-ins but compute a **weighted moving average** (e.g. exponentially-weighted, ~7–10 day half-life) so the reported trend ignores daily water fluctuation. This is the number used for progress and for adaptive-TDEE math (§5.10).
- Track body-fat %, lean mass when available (from a smart scale or ring).
- `/weight` command → current trend, change over 7/30/90 days, small inline chart (render with `matplotlib` to a PNG and send as a photo, or an ASCII sparkline as a fallback).
- **Dashboard view:** interactive weight trend chart with raw points + smoothed line, body composition history.

### 5.8 Proactive data collection (the bot asks me)
This is what separates it from a passive logger.
- A `proactive.py` engine checks for **gaps**: no weight logged in N days, no breakfast by mid-morning, missing sleep data, a workout detected by the watch but not described, etc.
- When a gap is found, the bot **sends a question** and writes a row to a `data_requests` table (status `pending`).
- My next message is checked against pending requests **first** by the router, so a bare reply ("181.4") is correctly attributed to the question instead of being misread.
- Use Telegram inline buttons where the answer is constrained (e.g. "How'd you sleep? Great / OK / Poor").
- Don't nag: rate-limit prompts, respect quiet hours, and let me snooze/disable categories.

### 5.9 Smart device ingestion
- Expose a **secured webhook** (`POST /ingest`, bearer-token auth) that accepts JSON health payloads.
- Primary path: the iOS **Health Auto Export** app posts HealthKit data (steps, HR, HRV, sleep, weight from a connected scale, workouts, resting energy) on a schedule to this endpoint. Document the exact JSON shape it sends and map each metric to the schema.
- Provide pluggable connectors so other sources can be added later: **Oura** (clean REST API + token), **Garmin Connect**, **Whoop**, **Fitbit**. Build the Oura connector as the reference example since its API is the most straightforward (daily sleep, readiness, activity, HRV).
- Normalize all incoming units (lb/kg, °C/°F, ms vs s) at the boundary; store canonical units in the DB and convert on display.
- Log every raw payload to `device_sync_log` so nothing is lost if parsing changes.

### 5.10 Claude-powered health intelligence
- **Adaptive TDEE / maintenance estimate:** combine logged intake with the smoothed weight trend over a rolling window to back-calculate actual maintenance calories (the MacroFactor-style trick: weight change vs. calories in over time reveals true expenditure). Adjust calorie/macro targets toward the goal (cut/bulk/maintain). The arithmetic should be a deterministic algorithm; Claude narrates and sanity-checks it, not does the math.
- **Trend & correlation inference:** weekly, feed Claude a structured digest (not raw rows) — weight trend, average macros, sleep, resting HR/HRV, workout load — and ask for plain-language observations and correlations (e.g. "protein dipped on low-sleep days," "weight stalled despite the deficit — likely under-logging on weekends").
- **Anomaly detection:** flag resting-HR spikes, HRV drops, poor-sleep streaks, sudden weight jumps. Surface gently, never alarmingly, and never as medical advice.
- **Reports:** generate a Markdown weekly report and a longer monthly review, pushed to Telegram and **viewable on the dashboard**. Optionally written to the Obsidian vault (§9).
- **Cost control:** cache, batch, and digest before calling Claude. Log every call (prompt summary, tokens, model, latency) to `llm_analysis`.

---

## 6. Webhook security

- TLS termination via Caddy or nginx in front of the app (Caddy gets you automatic Let's Encrypt with one line). Or expose via a Tailscale Funnel / Cloudflare Tunnel if you'd rather not open a port.
- Bearer token in the `Authorization` header, compared in constant time. Reject anything else with 401.
- Validate and size-limit payloads. Rate-limit the endpoint.

---

## 7. Proposed database schema (21 tables)

SQLite, WAL mode, foreign keys on. All timestamps stored UTC ISO-8601; convert to user's local tz on display. Every table carries `user_id`.

1. **users** — user_id (PK), email (unique), password_hash, display_name, timezone, sex, height, birthdate, units_pref (metric/imperial), activity_level, dietary_restrictions (JSON), created_at, updated_at
2. **sessions** — session_id (PK), user_id (FK), token_hash, created_at, expires_at *(web login sessions)*
3. **telegram_connections** — conn_id (PK), user_id (FK, unique), bot_token_encrypted, chat_id, bot_username, is_active, validated_at, created_at, updated_at *(each user's Telegram bot link)*
4. **health_data** — hd_id (PK), user_id (FK), data_type (blood_work/body_comp/fitness_baseline/medical), data_json (JSON), notes, recorded_at, created_at *(stores blood work results, baseline fitness, medical notes — flexible JSON for varying lab panels)*
5. **goals** — goal_id, user_id, goal_type (cut/bulk/maintain/event), title, description, target_weight, target_body_fat, event_date, event_name, target_calories, target_protein/carbs/fat, start_date, end_date, status (active/completed/paused), created_at, updated_at
6. **diet_plans** — plan_id (PK), user_id (FK), goal_id (FK, nullable), created_at, active, daily_calories, daily_protein, daily_carbs, daily_fat, rationale_text, meal_timing_json, foods_to_emphasize, foods_to_avoid, superseded_by (FK, self-ref), expires_at *(AI-generated, versioned — new plan supersedes old)*
7. **training_plans** — tp_id (PK), user_id (FK), goal_id (FK), created_at, active, phase_name, phase_description, start_date, end_date, workouts_json (JSON array of daily workouts), superseded_by (FK, self-ref) *(AI-generated phased training programs)*
8. **daily_workouts** — dw_id (PK), tp_id (FK), user_id (FK), scheduled_date, workout_type (daily_small/gym/rest/sport_specific), description, exercises_json, completed, completed_at, user_notes *(individual workout prescriptions from a training plan)*
9. **foods** — food_id, name, brand, serving_qty, serving_unit, calories, protein, carbs, fat, fiber, sugar, sodium, source (claude/nutritionix/manual), claude_confidence, cached_at *(cache of AI-inferred or looked-up foods)*
10. **meals** — meal_id, user_id, logged_at, meal_type, raw_text, source (voice/text/photo), total_calories, total_protein/carbs/fat
11. **meal_items** — item_id, meal_id, food_id, qty, unit, calories, protein, carbs, fat *(junction: a meal has many foods)*
12. **body_composition** — bc_id, user_id, measured_at, weight, weight_unit, body_fat_pct, lean_mass, source
13. **weight_trend** — wt_id, user_id, date, raw_weight, smoothed_weight, trend_7d, trend_30d *(materialized smoothing output)*
14. **sleep** — sleep_id, user_id, date, duration_min, deep_min, rem_min, light_min, awake_min, efficiency, hrv_overnight, source
15. **vitals** — vital_id, user_id, measured_at, resting_hr, hrv, spo2, body_temp, systolic, diastolic, source
16. **exercise** — ex_id, user_id, started_at, activity_type, duration_min, calories_burned, avg_hr, max_hr, distance, source, notes
17. **daily_summary** — date, user_id, total_calories, target_calories, protein, target_protein, carbs, target_carbs, fat, target_fat, fiber, weight_smoothed, sleep_min, resting_hr, steps, est_tdee, surplus_deficit *(precomputed rollup — now includes target vs. actual for budget tracking)*
18. **data_requests** — req_id, user_id, asked_at, category, question_text, status (pending/answered/snoozed/expired), answered_at, answer_value
19. **llm_analysis** — analysis_id, user_id, created_at, kind (router/food_parse/diet_plan/training_plan/weekly/anomaly/report), model, input_digest, output_text, input_tokens, output_tokens, latency_ms
20. **device_sync_log** — sync_id, user_id, received_at, source, raw_payload (JSON), parsed_ok, error *(audit/raw store)*
21. **plan_history** — ph_id (PK), user_id, plan_type (diet/training), old_plan_id, new_plan_id, changed_at, reason *(audit trail of plan revisions for the user to review why targets changed)*

> Key additions vs. original: `health_data` (flexible health intake), `diet_plans` (AI-generated, versioned), `training_plans` + `daily_workouts` (event-driven programs), `plan_history` (audit trail). `goals` expanded with event fields. `daily_summary` now tracks target vs. actual. `foods.source` includes `claude`. `llm_analysis.kind` expanded for new AI call types.

Provide a `db.py` that creates the schema idempotently and a lightweight migration mechanism (a `schema_version` table + ordered migration functions).

---

## 8. Telegram commands

| Command | Action |
|---|---|
| `/start` | greet + link to dashboard for profile setup if not yet connected |
| `/today` | today's calories & macros vs. AI target + remaining budget |
| `/weight` | weight trend + deltas + chart |
| `/log <text>` | force-log as a meal |
| `/metric <text>` | force-log as a metric |
| `/plan` | view current diet plan summary + today's workout |
| `/goal` | view active goals, countdowns, and progress |
| `/report` | generate on-demand weekly report |
| `/snooze <category>` | mute proactive prompts for a category |
| `/undo` | delete the last logged entry |
| `/export` | dump data as CSV |
| `/dashboard` | link back to the web dashboard |

Plus: any non-command text or voice goes through the router.

---

## 9. Obsidian vault integration (existing system — wire in, don't rebuild)

There's already an Obsidian vault synced to Google Drive via **rclone** on this instance, with Claude-powered querying. Have the fitness bot **write its weekly/monthly reports and daily summaries into the vault** as Markdown notes (e.g. `Health/Daily/2026-06-22.md`, `Health/Weekly/2026-W25.md`) so they're searchable alongside everything else. Reuse the existing rclone sync rather than standing up a new Drive integration. Keep the write path behind a feature flag in case sync isn't ready.

---

## 10. Additional features worth including (my asks + suggestions)

Things I want considered, roughly in priority order. Build the starred ones into the core phases; treat the rest as backlog.

- ⭐ **Photo meal logging** — send a photo of a plate, Claude vision estimates the foods and macros, I confirm/correct. Huge for convenience.
- ⭐ **Adaptive targets** — recompute calorie/macro targets weekly from the actual TDEE estimate, not a static formula. *(Now built into §5.3 as core.)*
- ⭐ **Weekend under-logging detection** — Claude flags when intake data looks implausibly low vs. weight trend.
- ⭐ **Blood work integration** — after uploading blood work, Claude highlights actionable dietary changes (e.g., "your LDL is borderline — reduce saturated fat, increase fiber").
- **Barcode scanning** — send a photo of a barcode for packaged foods (Open Food Facts API or Claude vision).
- **Hydration tracking** — quick "+500ml" logging and a daily target.
- **Workout logging by voice** — "did 5x5 squats at 225, felt strong" → parsed and stored, matched against the training plan.
- **Streaks & gentle gamification** — logging streaks, protein-target hit rate, without being annoying.
- **Quiet hours & smart timing** — learn when I usually eat and time prompts accordingly.
- **Correlations on request** — "does my sleep affect my weight?" runs an ad-hoc analysis.
- **Supplement / medication reminders** — simple scheduled prompts with logging.
- **CSV/JSON export & backup** — nightly DB backup to the Drive vault folder.
- **"Coach mode"** — a weekly Claude check-in conversation that asks how I'm feeling and adjusts the plan.
- **Dashboard admin view** — usage stats, active connections, error logs (if expanding to more users later).

---

## 11. Phased delivery plan

**Phase 1 — Web dashboard + AI meal logging + core loop (ship first, fully working):**
Web dashboard with registration, login, profile management, health data intake page, and Telegram bot connection setup (validate token, save encrypted, start/stop polling). ConnectionManager to run per-user bot instances. Telegram bot scaffolding + systemd service; voice → Whisper → text; freetext router; **Claude-powered meal logging** with calorie/macro inference and confirmation cards; running calorie budget after each meal; SQLite schema (all 21 tables); `/today`, `/weight`, `/log`, `/undo`; weight logging with trend smoothing; daily summary job. Dashboard home showing today's macros + calorie budget ring + weight trend + connection status.

**Phase 2 — AI diet plans + event goals + proactive:**
Claude diet plan generation from health profile; event-driven goal creation with training plan generation; daily workout prescriptions via Telegram; plan revision history; goal countdown and progress tracking on dashboard. Proactive data-request engine + pending-question matching; secured `/ingest` webhook; Health Auto Export mapping; sleep/vitals/exercise tables populated; daily_summary rollup job; Oura connector as reference.

**Phase 3 — Intelligence + adaptation + reports:**
Adaptive TDEE algorithm with weekly plan recalculation; weekly Claude digest + report; anomaly detection; blood work analysis integration; weekend under-logging detection; llm_analysis logging; reports viewable on the dashboard; Obsidian vault report writing.

**Phase 4 — Backlog polish:**
Photo meal logging, barcode, hydration, workout-by-voice, coach mode, export/backup, advanced charts, dashboard admin view.

Each phase ends with: it runs on the ARM64 box under systemd, has a short README section on how to use it, and has at least smoke tests for the new paths.

---

## 12. Deliverables & definition of done

- Working code in the module layout of §4, committed with clear messages.
- `requirements.txt` pinned and **verified to install on aarch64**.
- `.env.example` listing every required secret (see §13).
- `systemd` unit file(s) + install instructions.
- `db.py` that initializes the schema idempotently, with migrations.
- A `README.md`: setup, env vars, how to run, how to deploy to OCI, how each feature works, how to register and connect a Telegram bot.
- Smoke tests for: web registration/login, Telegram connection flow, Claude meal parsing, calorie budget tracking, voice transcription path, router classification, weight smoothing, webhook ingest.
- No secrets in the repo. No x86-only dependencies.

---

## 13. Environment variables (`.env.example`)

```
# --- Web Dashboard ---
SECRET_KEY=                    # session signing key (generate with: python -c "import secrets; print(secrets.token_hex(32))")
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
ALLOWED_ORIGINS=               # CORS origins if serving SPA separately

# --- Encryption (for bot tokens at rest) ---
ENCRYPTION_KEY=                # Fernet key for encrypting stored bot tokens (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# --- External APIs ---
OPENAI_API_KEY=                # Whisper transcription
ANTHROPIC_API_KEY=             # Claude — meal parsing, diet plans, training plans, routing, analysis

# --- Device Webhook ---
INGEST_WEBHOOK_TOKEN=          # bearer token for /ingest

# --- Optional Connectors ---
OURA_TOKEN=                    # optional, Phase 2

# --- Optional Food API (verification layer) ---
NUTRITIONIX_APP_ID=            # optional — Claude is primary; Nutritionix is fallback/verification
NUTRITIONIX_API_KEY=           # optional

# --- Database ---
DATABASE_PATH=./fitnessbot.db

# --- AI Models ---
ANALYSIS_MODEL=claude-sonnet-4-6      # weekly reports, diet plans, training plans
ROUTER_MODEL=claude-sonnet-4-6        # message classification, food parsing (fast calls)

# --- Behavior ---
TIMEZONE=America/Toronto       # server default; users set their own in profile
QUIET_HOURS=22:00-07:00        # default; users can override
```

---

## 14. Open questions for me (Alex) to answer before/while building

1. Which devices specifically — Apple Watch + which ring (Oura? Galaxy? RingConn)? Confirms which connectors to prioritize.
2. Smart scale brand (decides whether body-fat % comes through HealthKit/Health Auto Export or a vendor API).
3. Public webhook via Caddy + a domain, or Tailscale Funnel / Cloudflare Tunnel? (Affects §6.)
4. Default goal at launch — cut, bulk, or maintain — and starting targets.
5. OK to use the OpenAI Whisper API (cost per voice note), or prefer local `whisper.cpp` from day one?
6. Dashboard auth: simple email+password to start, or add OAuth (Google/GitHub) in Phase 1?
7. Expected number of users at launch — just you, or opening to a small group? (Affects connection manager scaling decisions.)
8. Blood work format — will you type results manually, upload a PDF/photo of lab results (Claude vision to parse), or both?
9. For training plans — any gym equipment constraints or home-only workout preference?
