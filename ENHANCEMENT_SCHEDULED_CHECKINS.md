# Enhancement Spec — Scheduled Check-ins, Missing-Data Nudges & Long-Horizon Dashboard

**Target executor:** Devin AI
**Parent spec:** `fitness-bot-requirements.md` (this is an enhancement to that build)
**Owner:** Alex
**Last updated:** 2026-06-22
**Queue status:** Enhancement — schedule after Phase 1/2 of the parent spec are running.

---

## 0. TL;DR for the agent

Add three things to the existing OCI-hosted fitness bot:

1. **Three scheduled daily Telegram briefings** — morning, midday, evening — each a health summary tailored to that time of day.
2. **Missing-data nudges** — if I haven't logged meals, weight, or been active, the bot proactively reminds me (to log food, weigh in, or get moving). These are coordinated with the scheduled briefings so I'm never double-pinged.
3. **A long-horizon visual dashboard** — week / month / quarter / year / all-time views of weight, intake, activity, sleep, and vitals. Glanceable charts pushed into Telegram daily, plus a full interactive web dashboard served from the OCI box.

Everything runs on the existing **OCI Ampere (ARM64, Ubuntu 24.04)** instance under systemd, reusing the existing DB, scheduler, and webhook server.

---

## 1. How this fits the existing system

- Reuse `scheduler.py` (APScheduler) for all timed jobs.
- Reuse the `daily_summary` rollup table from the parent schema as the primary data source for briefings and dashboard.
- Reuse the `data_requests` table so nudges and their answers flow through the same pending-question mechanism (a bare reply like `181.4` still gets matched to the question that prompted it).
- Reuse the FastAPI/aiohttp server already standing up for the device webhook to also serve the dashboard — no second web process.
- Respect the existing `QUIET_HOURS` and per-category snooze settings. Nothing in this enhancement may violate quiet hours.

---

## 2. Feature A — Three scheduled daily briefings

### 2.1 Times (configurable, these are the defaults)

| Briefing | Default time (America/Toronto) | Purpose |
|---|---|---|
| **Morning brief** | **07:30** | Overnight recovery + the day's plan |
| **Midday check** | **13:00** | Mid-day pace + first nudge if data is missing |
| **Evening wrap** | **20:30** | Full-day totals + close the loops + periodic rollups |

- Times live in config (`BRIEFING_TIMES=07:30,13:00,20:30`) and are individually overridable via a `/briefings` command.
- Each briefing is a single Telegram message (Markdown), optionally with one attached chart PNG.
- **Graceful degradation:** any section with no data is silently omitted, not shown as "N/A". A briefing never sends an empty shell — if literally nothing is available, it sends the relevant nudge instead (see Feature B).

### 2.2 Morning brief (07:30) — content

In priority order, include whatever data exists:
- Greeting + date.
- **Sleep** last night: duration, efficiency, vs. my 7-day average (from device data).
- **Recovery:** resting HR and HRV vs. baseline, with a one-line read ("HRV up, good to train" / "resting HR elevated, maybe ease off"). Informational only, never medical.
- **Weight:** latest smoothed trend value + 7-day delta. If no weigh-in in ≥2 days → append a weigh-in nudge.
- **Today's targets:** adaptive calorie + macro goal for the day.
- **Plan nudge:** "Log breakfast when you eat."

### 2.3 Midday check (13:00) — content

- **Intake so far:** calories + macros logged today vs. target, with a simple pace read ("on track" / "light so far" / "ahead").
- **Activity so far:** steps and active minutes vs. daily goal.
- **At most one nudge**, prioritized (see Feature B priority order). Typical: "No meals logged yet — what'd you have?" or "You're at 1,800 steps — a short walk would help."

### 2.4 Evening wrap (20:30) — content

- **Full-day totals** vs. target: calories, protein / carbs / fat, fiber.
- **Activity summary:** steps, active minutes, any workouts.
- **Remaining macros** if I'm short ("~30g protein short of target").
- **Loop-closing nudges:** "No dinner logged?" / "No weigh-in today — try tomorrow morning."
- **Periodic rollups appended here:**
  - **Sundays** → weekly rollup + dashboard link.
  - **1st of month** → monthly rollup.
  - **Jan 1 (and optionally my birthday)** → yearly review.

### 2.5 Narrative quality

Use Claude to turn the structured digest into a short, natural, non-repetitive message — not a robotic stat dump. Keep it tight (a few lines), warm, never alarmist, never medical-advice framed. Cache/batch the call; log it to `llm_analysis`. Provide a deterministic template fallback if the Claude call fails so a briefing always sends.

---

## 3. Feature B — Missing-data & inactivity nudges

### 3.1 What counts as "missing" (defaults, all configurable)

| Trigger | Condition | Nudge |
|---|---|---|
| No meals | Nothing logged by **13:00** | "Haven't seen any meals today — send me what you ate." |
| No dinner | < 2 meals logged by **20:30** | "No dinner yet? Log it when you can." |
| Stale weight | No weigh-in in **≥ 2 days** | "Hop on the scale tomorrow morning?" |
| Sedentary | Steps < **30% of daily goal** by **15:00** and not currently in a workout | "You're at {steps} — a 15-min walk would help hit your goal." |
| Low day | Active minutes < goal by **18:00** | "Light activity today — quick movement before bed?" |
| Missing device data | No sleep/HR sync by **10:00** | "No sleep data synced — check the watch/ring connection." |
| Undescribed workout | Watch logged a workout but no note from me | "Saw a {activity} workout — want to log how it felt?" |

Defaults: daily step goal **8,000**, active-minutes goal **30**. Pull these from `goals`.

### 3.2 Coordination rules (critical — don't be annoying)

- **Bundle into briefings first.** A nudge that's relevant at a briefing time rides inside that briefing rather than firing separately.
- **Standalone nudges** are allowed only for time-sensitive items between briefings (e.g. the 15:00 sedentary nudge, the 18:00 low-activity nudge). Cap standalone nudges at **2 per day**.
- Every nudge writes a `data_requests` row (`category`, `question_text`, `status=pending`). The router matches my next reply against pending requests **before** any other intent.
- **Never nudge during quiet hours.** Never nudge about a category that's snoozed. Never repeat the same nudge twice in one day.
- If I've already provided the data (logged the meal, weighed in), the trigger is suppressed.

### 3.3 Snooze / control

- `/snooze meals 1d`, `/snooze activity`, `/quiet 22:00-07:00`, `/briefings` to view & edit everything.
- A one-tap "🔕 mute today" inline button on any nudge.

---

## 4. Feature C — Long-horizon visual dashboard

Two surfaces, same data: **glanceable charts in Telegram** + a **full interactive web dashboard**.

### 4.1 Timeframes

Every view supports: **Week · Month · Quarter · Year · All-time.** Default landing view = Month.

### 4.2 Metrics & charts

| Chart | Description | Timeframes |
|---|---|---|
| **Weight trend** | Smoothed weight line + raw scatter + goal line | all |
| **Calorie balance** | Daily intake vs. estimated TDEE, with deficit/surplus shading | all |
| **Macro adherence** | % of days hitting protein target; avg macro split | week/month/year |
| **Activity** | Steps + active minutes per day/week; workouts per week | all |
| **Sleep** | Duration + efficiency trend, vs. baseline band | all |
| **Vitals** | Resting HR & HRV trend lines | all |
| **Body composition** | Body-fat % and lean mass over time (when available) | month/year/all |
| **Consistency heatmap** | GitHub-style calendar heatmap of logging days over the past year | year/all |

The **consistency heatmap** is a priority — it's the best single visual for "have I been keeping up over months/years."

### 4.3 Telegram surface (glanceable)

- The **evening wrap** attaches one rotating chart PNG (e.g. weight trend on weekdays, activity on weekends).
- The **weekly/monthly/yearly rollups** attach the relevant summary chart(s) + a dashboard link.
- A `/dashboard` command returns a secured link to the full web dashboard.
- A `/chart <metric> <timeframe>` command renders any chart on demand (e.g. `/chart weight year`).
- PNGs rendered with **matplotlib (Agg backend)** — confirmed to work headless on ARM64. Keep styling clean and readable on a phone screen.

### 4.4 Web dashboard

- Served by the existing FastAPI app: a `/dashboard` HTML route + `/api/stats?metric=&timeframe=` JSON endpoints.
- **Single self-contained HTML page**, no build step (keeps ARM64 deployment trivial). Charts via **Chart.js** or **Plotly** (CDN or vendored), interactive timeframe toggle, dark theme.
- **Auth:** access restricted to the Tailscale tailnet, OR a signed time-limited token link (`/dashboard?t=<signed>`), so it's not publicly open. Reuse the webhook's TLS setup.
- Mobile-responsive — I'll mostly open it from my phone via the Telegram link.

### 4.5 Data source & performance

- Read from `daily_summary` for everything ≥ daily granularity. A full year is ~365 rows — query directly, no heavy pre-aggregation needed.
- Add lightweight **`weekly_summary`** and **`monthly_summary`** materialized tables only for the rollup narratives and fast year/all-time charts (computed by the rollup jobs in §5). Backfill them from existing `daily_summary` on first run.

---

## 5. Scheduler jobs to add

| Job | Schedule | Action |
|---|---|---|
| `morning_brief` | daily 07:30 | build + send morning briefing |
| `midday_check` | daily 13:00 | build + send midday briefing (incl. nudge) |
| `evening_wrap` | daily 20:30 | build + send evening briefing (+ rollups on schedule) |
| `sedentary_nudge` | daily 15:00 | standalone activity nudge if triggered |
| `low_activity_nudge` | daily 18:00 | standalone low-activity nudge if triggered |
| `rollup_daily` | daily 00:10 | finalize yesterday's `daily_summary` |
| `rollup_weekly` | Mon 00:20 | compute `weekly_summary` for prior week |
| `rollup_monthly` | 1st 00:30 | compute `monthly_summary` for prior month |

All jobs: timezone-aware (America/Toronto), idempotent (safe to re-run), and they no-op silently during quiet hours where applicable. Wrap each in error handling so one failing job never kills the scheduler.

---

## 6. Schema additions

- **`weekly_summary`** — week_start, user_id, avg_calories, avg_protein/carbs/fat, weight_start, weight_end, weight_change, avg_sleep_min, avg_resting_hr, total_steps, workouts, logging_days, days_on_target.
- **`monthly_summary`** — month, user_id, + same aggregate columns as weekly.
- **`briefing_log`** — id, user_id, sent_at, type (morning/midday/evening/weekly/monthly/yearly), content_summary, had_nudge (bool). (Prevents duplicate sends and gives an audit trail.)

`data_requests` and `llm_analysis` already exist in the parent schema — reuse them.

---

## 7. New commands

| Command | Action |
|---|---|
| `/briefings` | view/edit the three briefing times & toggle each on/off |
| `/dashboard` | secured link to the web dashboard |
| `/chart <metric> <timeframe>` | render a chart on demand |
| `/snooze <category> [duration]` | mute a nudge category |
| `/quiet <range>` | set quiet hours |
| `/week`, `/month`, `/year` | on-demand text rollup for that period |

---

## 8. Acceptance criteria (definition of done)

- Three briefings fire at the configured times in **America/Toronto**, render correctly with real data, and **degrade gracefully** when sections are missing.
- **No message ever sends during quiet hours.** Snooze and "mute today" verifiably suppress the right messages.
- Missing-data triggers fire **only** when the data is genuinely absent, are **deduped** (never twice/day), and standalone nudges are **capped at 2/day**.
- A bare reply to a nudge (e.g. `181.4`, "had a chicken wrap") is correctly attributed to the pending `data_requests` row and logged to the right table.
- `/chart weight year` and the other metric/timeframe combos render readable PNGs on the **ARM64** box (matplotlib Agg, headless).
- The web dashboard loads on mobile, switches across **week/month/quarter/year/all-time**, shows all §4.2 charts including the **consistency heatmap**, and is **not publicly accessible** (Tailscale-only or signed-token).
- Rollup tables backfill correctly from existing `daily_summary` and stay current via the scheduled jobs.
- All new jobs are idempotent and isolated — a failure in one doesn't crash the scheduler or the bot.
- Every Claude-generated briefing has a deterministic template fallback so a briefing always sends even if the API call fails.

---

## 9. Suggested build order within this enhancement

1. **Schema + rollup jobs** (`weekly_summary`, `monthly_summary`, `briefing_log`) with backfill.
2. **Three briefings** with template (non-LLM) content + graceful degradation + quiet-hours guard.
3. **Missing-data nudge engine** + coordination/dedup + `data_requests` wiring + snooze controls.
4. **Telegram charts** (matplotlib PNGs) + `/chart` command, attached to evening wrap & rollups.
5. **Web dashboard** (FastAPI route + JSON API + single-page Chart.js/Plotly UI + consistency heatmap + auth).
6. **Claude narrative layer** over the briefings + the weekly/monthly/yearly review prose.

Ship 1–4 first (fully working, in-chat). 5–6 are the polish layer.

---

## 10. Open questions for me (Alex)

1. Confirm the three default times (07:30 / 13:00 / 20:30) or adjust — e.g. earlier morning, later evening?
2. Daily step / active-minute goals — keep 8,000 / 30 min or set your own?
3. Dashboard access: **Tailscale-only** (simplest, secure, phone must be on tailnet) or **signed-token public link** (open from any browser)? 
4. Want the yearly review tied to **Jan 1**, my **birthday**, or both?
5. For the "get active" nudge — base it on **steps**, **active minutes**, or **both** (whichever is behind)?