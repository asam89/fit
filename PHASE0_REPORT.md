# Phase 0: fit-ness Enhancements (streaks, challenges, recaps, adaptive targets, Strava)

Status: **awaiting Alex's written approval**. No feature code, migrations or dependency changes have been made.
Based on `main` @ `4be6cf0` (running stats A+B deployed to prod 2026-10-30, schema 25) and a read-only look at the production DB.

## 0. Findings that change the plan

| # | Spec assumes | Reality | Proposal |
|---|---|---|---|
| F1 | One fit-ness bot that can join Telegram groups | Every user brings **their own BotFather bot**. The token goes in `telegram_connections.bot_token_encrypted` (`fitnessbot/web/connections.py`), and `fitnessbot/bot/manager.py` polls one `Application` per user. Prod has **1** connection (Alex, `@Asamfit_bot`) | B needs **one shared platform bot** (`@fitness_ca_bot` or similar) that is added to groups and maps `telegram_user_id` to `users.user_id`. Today there is no such mapping, only one `chat_id` per owner. This is the biggest design decision (Q1) |
| F2 | The bot only talks to its owner | `register_handlers(app, user_id)` binds every handler to the owner's `user_id` and **never checks who sent the message** (`fitnessbot/bot/handlers.py`; no `effective_user`/`chat_id` check, no `filters.Chat`). Anyone who finds `@Asamfit_bot` can DM it, log meals into your account or read `/today` | **Fix this before any group work.** Restrict every handler to `filters.Chat(int(conn["chat_id"]))`. It is a small security PR, separate from this spec (Q2) |
| F3 | Wearable data exists (steps, active calories, HR) | `device_sync_log` has **0 rows** and `wearable_connections` has **0 rows**. `/sync` (`handlers.py` `cmd_sync`) hands the raw text or file to the LLM via `process_message("[HEALTH_SYNC] …")`. No steps or active-calorie table exists; `vitals`, `sleep`, `exercise` and `daily_summary` are all empty | D has **no input today** except manual and Telegram workouts, runs, and later Strava. Propose a structured `daily_activity` table filled by Strava (E) and an ingest endpoint. Build D after E, or have D use MET estimates from `health_benefits.py` first (Q3) |
| F4 | "No second implementation" of the day | **Three** day implementations exist: (a) `tz.day_utc_range` (correct, DST-aware), (b) fixed `utc_offset_hours` string offsets in `db.get_calorie_history`, `get_logging_heatmap` and `get_meal_dates_with_counts` (wrong across a DST change; used by the dashboard, `web/dashboard.py` lines 98-112 and 318-323), (c) bare `DATE(logged_at)` (UTC day) fallbacks, 15 in `db.py`. `db.get_friend_summary` uses `logged_at >= today`, which has no upper bound and compares a local date to a UTC timestamp | Workstream A0 (before A): add `tz.local_day_expr`/`days_for_range` and move (b) and (c) onto it. Golden tests first, so totals provably don't change except at DST boundaries |
| F5 | Existing INVARIANTS.md rules 1-10 | `INVARIANTS.md` already has **15** rules (dates, Accuracy 1-4, calories, secrets, running). Spec rules 1, 3, 8 and 10 overlap existing 1, 3, 13 and 15 | The draft (§11) amends 1, 3, 13 and 15 and adds 16-22. Spec numbers are cross-referenced |
| F6 | Strava is new | Strava is already **running stats C2** (`HANDOFF.md`), blocked on you creating the Strava app | Merge: one Strava integration, E here = running C2 + non-run activities. Running C1 (GPX/FIT) and D (stats) stay as planned |
| F7 | Feature flags `streaks_v1` etc. | No flag system. Flags are env vars read in `fitnessbot/config.py` (Invariant 15, e.g. `RUN_CALORIES_ENABLED`) | Add `Config.flag(name)` reading `FLAG_STREAKS_V1=1` etc. (global, default off) plus an optional per-user allowlist `FLAG_<NAME>_USERS=1,4` for dogfooding |
| F8 | Users in several timezones | All **42** prod users are `America/Toronto`. 23 were active in the last 30 days, but only **4 have ever logged a meal** (234 meals, 225 of them yours) and only you have Telegram connected | Seed Toronto/Vancouver/Dubai test users as the spec says. Retention metrics will be noise until there are more Telegram users; F1 (a shared bot) is also an onboarding fix |
| F9 | Jira tracking | Jira isn't connected to this Devin org | Track in PRs and the `HANDOFF.md` checklist |

## 1. Inventory

36 files touch the listed areas (out of 62 tracked files under `fitnessbot/`, 103 in the repo). A file is listed under every area it touches.

| Area | Files |
|---|---|
| Meal logging (text, voice, photo) | `ai/food_parser.py`, `ai/prompts.py`, `bot/conversation.py`, `bot/handlers.py` (`handle_photo`, `handle_voice`), `voice.py`, `inference/factory.py` (vision), `meal_quality.py`, `db.py` (`insert_meal`, `insert_meal_item`), `web/dashboard.py` |
| Workout logging | `bot/conversation.py` (`_act_workout`), `training_plan.py` (`log_completed_activity`, `complete_by_title`), `workout_store.py`, `workout.py`, `health_benefits.py`, `db.py` (`health_data` `data_type='workout'`), `web/plan.py`, `web/workout.py`, `web/templates/dashboard.html`, `running/telegram.py`, `running/store.py` |
| Wearable /sync | `bot/handlers.py` (`cmd_sync`, `handle_sync_document`), `db.py` (`device_sync_log`, `wearable_connections`), `web/settings.py` (`/settings/wearable/*`, labels only), `web/templates/settings.html`, `config.py` (`INGEST_WEBHOOK_TOKEN`, `OURA_TOKEN`, both unused) |
| Targets | `nutrition.py` (`compute_targets`, `get_nutrition_targets`, `SAFETY_FLOOR`), `db.py` (`nutrition_targets`), `briefings.py`, `bot/conversation.py`, `bot/handlers.py`, `event_coaching.py`, `workout.py`, `web/dashboard.py` |
| Dashboard | `web/dashboard.py`, `web/templates/dashboard.html`, `web/templates/diary.html`, `web/templates/base.html`, `web/app.py`, `metrics.py` |
| Friends / social | `web/social.py`, `db.py` (`friendships`, `share_settings`, `nudges`, `blocks`, `reports`, `invite_links`, `get_friend_summary`), `web/auth.py` (`?invite=` sets `users.invited_by`), `web/settings.py`, `web/templates/dashboard.html`, `web/templates/settings.html`, `web/templates/register.html`, `bot/handlers.py` (`/invite`) |
| Telegram | `bot/handlers.py`, `bot/manager.py`, `bot/conversation.py`, `router.py`, `briefings.py` (`_send_telegram`), `scheduler.py`, `web/connections.py`, `web/settings.py`, `running/telegram.py` |
| Day computation | `tz.py`, `db.py`, `nutrition.py`, `metrics.py`, `briefings.py`, `health_benefits.py`, `web/dashboard.py` |

## 2. Data model (prod row counts)

- `users` (42): `timezone` (IANA, default `America/Toronto`), `units_pref`, `run_units`, `invited_by` (2 set), `handle`, `activity_level`, `sex`. Defined in `db.py` `SCHEMA` + migrations.
- `meals` (234) / `meal_items` / `foods`: `logged_at` UTC ISO; per-meal `total_calories`, `total_protein`, … (`db.py`).
- Workouts: `health_data` (49 `workout` rows, `data_json` = `{activity, type, duration_min, source}`), `training_plan_items` (49), `runs` (0, new). `exercise` and `personal_bests` exist but are empty.
- Daily totals: **computed on read** from `meals` (`db.get_today_totals` with `tz.day_utc_range`). `daily_summary`, `weekly_summary` and `monthly_summary` exist but are **empty**.
- Targets: `nutrition_targets` (41 rows, one per user, `UNIQUE(user_id)`), recomputed by `nutrition.compute_targets`. There is no per-day history, so there is nowhere to store an adjustment yet.
- Social: `friendships` (4), `share_settings` (1; defaults share **diet** and workouts with friends, `share_weight=0`), `nudges` (6), `invite_links` (13; `code`, `uses`, `max_uses`, `expires_at`, no `source` field).
- Timezones: stored per user, applied via `tz.py` for "today" and day ranges; F4 lists the exceptions.

## 3. Telegram layer

- Routing (`bot/handlers.py` `register_handlers`): `CommandHandler`s (`/today`, `/plan`, `/run`, …), then `MessageHandler`s for documents (`/sync` caption), photos, text (`TEXT & ~COMMAND`) and voice.
  - Text goes first to `running/telegram.handle_text` (runs).
  - Then `conversation.process_message`, which runs fast-path regexes (`_WEIGHT_PAT`, …, `router.py` `_PB_PATTERN`), the LLM NLU, the `_act_*` handlers and the LLM reply.
- Groups: **not supported.** No `ChatType` filter, and a user's private bot added to a group would act on that group's messages as its owner (F2). Privacy mode is BotFather's default, so the bot only sees commands and mentions, but those are enough.
- Identity mapping: one row per owner in `telegram_connections` (`user_id` UNIQUE, `chat_id`). There is no `telegram_user_id` column, so group members can't be identified.

## 4. Daily aggregation

- "Today" = `tz.user_today(user_id)`; totals = `meals` in `tz.day_utc_range(date)` (the fix was commit `52f5518` "timezone-aware date filtering for consistent daily totals", PR #33).
- Partial day: Accuracy 1 (commit `a4e6477`, PR #38) leaves today out of averages and hit counts. See `nutrition.build_month_summary` (line ~548), `briefings.build_weekly_rollup` (line ~292) and `conversation.py` (lines ~676 and ~928). Tests are in `tests/test_telegram_accuracy.py`.
- Late-night logs: a meal is assigned by its UTC `logged_at` falling inside the user's local day, so 23:50 local counts toward that day. Telegram meal logging has no back-dating ("last night"); workouts can be back-dated (`tests/test_activity_day_assign.py`).
- The bot's local date fix was commit `ec7b3a2`, PR #62.
- Weak spots: see F4. These matter for Invariant 2, because the dashboard charts and Telegram must match.

## 5. Wearable sync

- Today the only path is `/sync`: text, or a CSV/JSON/XML file up to Telegram's 20 MB. The LLM reads it and may log weight or workouts through the normal intents.
- Nothing structured is stored for steps, active calories or HR, and freshness is not tracked.
- Prod has never received a sync (`device_sync_log` = 0). The Settings "connect wearable" option is a label (`web/settings.py` `/settings/wearable/connect`).
- Running stats C1 adds GPX/FIT for runs.

## 6. Jobs and scheduling

- `scheduler.py` runs APScheduler in-process, with two jobs:
  - `dispatch_briefings`: every minute; morning, midday and evening briefs per user in their timezone, and the weekly rollup appended on `prefs["weekly_rollup_day"]`.
  - `event_checkins`: at :00 and :30.
- Dedupe: `briefing_log` (452 rows) via `db.get_briefings_sent_today`.
- Quiet hours: `briefings._in_quiet_hours`, a global `QUIET_HOURS=22:00-07:00` that defaults to Toronto time rather than each user's timezone.
- Proposed new jobs:
  - The daily leaderboard and the weekly recap both become checks inside the existing per-minute dispatcher, keyed on a group's or user's local time.
  - Both are deduped with a new `scheduled_post_log(scope, scope_id, kind, local_date)` UNIQUE table. That gives Invariant 9 by construction, not by timing.
  - Recap rendering runs in a thread pool (`asyncio.to_thread`) so the bot never blocks.

## 7. Image generation

- There is no server-side rendering. `requirements.txt` has no Pillow; matplotlib is commented out.
- Recommendation: **Pillow** (one battle-tested dependency, ~4 MB) with bundled TTF fonts, drawing a 1080×1920 PNG directly. That's about 50 ms per card, so it's fine for every user on one VM.
- Alternatives:
  - HTML → headless Chrome: heavy on a VM that's at 93% disk.
  - SVG → cairosvg: needs system cairo.
- Golden-image tests compare against stored PNG hashes, for zero, one and seven days of logs.

## 8. Existing tests

- Run `./venv/bin/pip install pytest && ./venv/bin/python -m pytest`. That's 603 tests in 18 files, with no CI.
- One known failure: `test_workout_web.py::TestAdjust::test_repeated_adjusting_does_not_force_a_deload`. It fails on `main` too.
- Coverage is strongest in workouts (`test_workout*.py`, 156), nutrition math (`test_nutrition.py`, 54), health benefits (46), running (40) and Accuracy 1-4 (19).
- Gaps the spec needs:
  - **no** golden tests for meal logging from text, voice or photo (0 tests mention voice or photo)
  - no dashboard-vs-Telegram totals parity test
  - no DST or cross-midnight tests for the `utc_offset_hours` queries
  - no `/sync` tests
  - only one timezone in fixtures (`test_user_local_dates.py`, 5 tests)
- Proposal: add CI (GitHub Actions, pytest) in A0 so "every PR runs the suite" is enforced, not manual.

## 9. Proposed design per workstream

Common to every workstream: a feature flag via `Config.flag()` (default off), one migration with a `migrate_down_N()` rehearsed on a `.backup()` prod copy, golden tests run with the flag on and off, and `HANDOFF.md` updated.

**A0. Foundations (new, before A).** This adds no tables.
- Fix F2 (sender check), if it isn't done separately.
- Add `tz.py` helpers and move the F4 queries onto them.
- Add golden tests: meal logging from text, voice and photo with the LLM mocked at the `inference` boundary; daily totals; partial day; targets; `/sync`.
- Seed fixtures for Toronto, Vancouver and Dubai, with logs that cross midnight.
- Add `Config.flag()`, a CI workflow, and a parity test (dashboard totals == Telegram `/today` == `metrics`).

**A. Streaks and PRs (`streaks_v1`).**
- New module `fitnessbot/streaks.py`, with pure functions over a list of `(local_date, logged, protein_hit, workouts_done, workouts_planned)` built from one `day_facts(user_id, start, end)` that uses the shared day function (Invariant 1).
  - Logging and protein streaks are counted in days. The workout streak is counted in weeks where completed ≥ planned.
  - Today counts only if it's already met; otherwise it's skipped, never a break (Invariant 3).
- Tables:
  - `streak_events(user_id, kind, milestone, local_date, sent_at, UNIQUE(user_id, kind, milestone, period_key))` gives one message per event.
  - `streak_freezes(user_id, used_on)` enforces one freeze per rolling 30 days.
- Streaks are **computed on read**, not stored, so backfill is automatic and a timezone change recomputes. PR values ("longest", "best week") are also computed; only the sent milestones are stored.
- Surfaces: `/streaks`, a dashboard card (`web/dashboard.py` + template), and milestone messages via `_send_telegram` from the per-minute dispatcher.
- Rollback: drop the two tables.

**B. Group challenges (`challenges_v1`).** This depends on Q1.
- Platform bot token in `.env` (`PLATFORM_BOT_TOKEN`), run as one more `Application` in `manager.py`, with group-only handlers (`filters.ChatType.GROUPS`).
- Tables:
  - `telegram_identities(telegram_user_id UNIQUE, user_id)`, linked by `/join`: a deep link `t.me/<bot>?start=link_<nonce>` → web login → bind.
  - `challenges(challenge_id, chat_id, template, length_days, start_local_date, post_time, tz, status)`
  - `challenge_members(challenge_id, user_id, joined_at, left_at, share_opt_in_json)`
- Points: computed on read from A's `day_facts`, so they equal the member's own streak data (acceptance criterion and Invariant 2).
- Leaderboard: shows only days done, goal hit yes/no, and points (Invariant 6).
- Posting: daily and final posts are deduped by `scheduled_post_log` (Invariant 9). `/leave` sets `left_at`, and posts filter it immediately (Invariant 7).
- Signups: non-users get `/register?invite=<code>&src=challenge:<id>`. A new `users.referral_source` column records the source.
- Web: `/challenges/<id>` uses the same function as the bot post.
- Steps template: hidden until a steps source exists (F3).
- Rollback: drop the tables and the column.

**C. Weekly recap (`recap_v1`).**
- `fitnessbot/recap.py`: `recap_data(user_id, week_start)` from `day_facts` (Invariant 2), and `render_png(data, prefs) -> bytes` with Pillow.
- Data and prefs:
  - `recap_prefs(user_id, stats_json)`, with calories and weight off by default.
  - `scheduled_post_log` for "Sunday 19:00 local".
- Sending: `bot.send_photo` with a caption that includes the user's invite link (`invite_links`, plus a new `source` column, so signups are attributed via `users.invited_by`).
- Rollback: drop the tables and the column, and remove Pillow.

**D. Adaptive targets (`adaptive_targets_v1`).**
- `target_adjustments(user_id, local_date, kcal_delta, protein_delta, reason, source, computed_at, UNIQUE(user_id, local_date))`. `nutrition_targets` is never written (Invariant 4).
- `nutrition.get_effective_targets(user_id, date)` = base + adjustment, clamped to `0 ≤ delta ≤ 0.30 × base` and to `≥ SAFETY_FLOOR` (Invariant 5).
- With the flag off, it returns `get_nutrition_targets()` unchanged.
- Recompute on new activity for that day only. Days before yesterday are "closed" and frozen.
- Input:
  - activity calories from Strava/`daily_activity` when present
  - otherwise the MET estimate from `health_benefits.get_daily_benefits`, discounted (e.g. 50%) because it's an estimate
- Today, Invariant 7 says exercise calories never change targets. D deliberately changes that, only behind the flag and only as a separate adjustment row; Invariant 7 is amended in the draft. **Needs your explicit sign-off (Q4).**
- Evening protein nudge: opt-in, from previously logged `foods`. It respects per-user quiet hours (fixes the global-tz gap from §6) and is sent at most once a day via `briefing_log`.
- Rollback: drop the table.

**E. Strava (`strava_v1`, merges running C2).**
- OAuth on `/settings/strava`.
- `strava_connections(user_id, athlete_id UNIQUE, access_token_enc, refresh_token_enc, expires_at, scopes, writeback_enabled)`, Fernet-encrypted (Invariant 8).
- Webhook: `POST /strava/webhook` plus the subscription verify handshake. Events carry only IDs, so each activity is fetched.
- Storage:
  - runs → `runs` (source `strava`, `ux_runs_source_ext` makes it idempotent)
  - other types → a `health_data` workout plus a `training_plan_items` entry, with `external_id`
  - day activity totals → `daily_activity`, which feeds D
- Dedupe across sources: start within 2 minutes plus the same type, flagged as `possible_duplicate_of` and counted once (Invariant 13, extended).
- Disconnect: deauthorizes at Strava, deletes the tokens, and stops imports immediately.
- Write-back: needs the `activity:write` scope and is opt-in.
- Strava limits:
  - The default rate limit is 200 requests per 15 minutes and 2,000 per day per app.
  - New apps are limited to **1 connected athlete** (the developer) until Strava approves a capacity increase, which needs a review and their brand guidelines ("Powered by Strava", the Connect button).
  - **Phase 0 can't fully confirm current limits without your app dashboard. Please check them when you create the app.**
- Rollback: drop the tables, revoke the webhook subscription.

**Suggested order:** F2 fix → A0 → A → C → B (after Q1) → E → D. D moves after E because without Strava it only has MET estimates (F3).

## 10. Risks and open questions

**Risks**
- F2 is a live privacy issue today, independent of this spec.
- A single shared bot means one token for all group traffic. Telegram's limit is ~30 messages/sec globally and 20/min per group, far above our scale.
- Changing the F4 queries can move a meal across a day boundary only around DST changes. The golden tests will show the exact diff before merge.
- Prod disk is at **93%** (3.5 GB free; journald 3.5 GB, deploy backups). Recap PNGs aren't stored (rendered and sent), but the disk should be cleaned before adding Pillow, fonts and CI artifacts. `journalctl --vacuum-size=500M` would free ~3 GB.
- The Strava single-athlete limit means E can't go beyond you without Strava's review. Allow weeks.
- Group leaderboards on retention: with 1 Telegram user today, B mostly depends on Q1 for onboarding.

**Questions for Alex**
1. **Q1:** OK to add one shared platform bot for groups (and optionally as the default bot for new users, instead of BotFather), with personal bots kept for existing users? Without it, B isn't possible.
2. **Q2:** OK to ship the F2 sender-check fix now as a small standalone PR, before anything else?
3. **Q3:** For D's input, build after E (Strava), or ship D first using discounted MET estimates from logged workouts?
4. **Q4:** D intentionally lets activity raise the day's target (behind the flag). Confirm that's OK, since it amends Invariant 7 ("exercise calories never change targets").
5. **Q5:** Workout streak = weeks where completed ≥ planned. What counts for a week with no plan: any workout, or skip the week?
6. **Q6:** Protein streak "hit" = within 5% or above (Accuracy 4 "met"). OK?
7. **Q7:** Recap send time Sunday 19:00 local, not in quiet hours. OK?
8. **Q8:** `share_settings` currently shares **diet** (calories, macros) with friends by default. Should that default flip to off as part of Invariant 6, or stay as is (friends ≠ groups)?

## Estimate

| Step | Sessions |
|---|---|
| F2 sender check | 0.25 |
| A0 foundations (golden tests, day function, flags, CI) | 1 |
| A streaks + PRs | 1 |
| C recap cards | 1 |
| B group challenges (shared bot, linking, leaderboard) | 1.5–2 |
| E Strava (incl. running C2) | 1.5 |
| D adaptive targets | 1 |
| **Total** | **~7–8** |
