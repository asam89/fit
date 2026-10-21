# Phase 0: Running Stats Tracking — Investigation Report

Status: **awaiting Alex's approval**. No feature code has been written.
Based on `main` @ `5e15f85` and a read-only look at the production DB (2026-10-21).

## 0. Spec gaps to resolve first

| Spec assumes | Reality | Proposal |
|---|---|---|
| `INVARIANTS.md` exists | It does not | Create it in PR A with a Running section, plus the existing rules (UTC storage, `tz.py` local day, Accuracy 1-4) |
| `HANDOFF.md` exists | It does not | Create it in PR A; update it in every PR |
| "Guardrails 1 to 6" work | Nothing by that name in code, tests or git history. Only **Telegram Accuracy 1-4** exists (commit `a4e6477`, `tests/test_telegram_accuracy.py`, 19 tests) | Please point me to Guardrails 1-6 (another repo? a doc?) or confirm it is Accuracy 1-4 |
| Track each workstream in Jira | Jira isn't connected to this Devin org (only GitHub) | Track it in the PRs plus a checklist in `HANDOFF.md`, unless you connect Jira |
| Metric is the default | All 38 prod users have `units_pref='imperial'` (the column default) | Add a separate `run_units` setting (`km`/`mi`), default `km`. Don't touch `units_pref`, which also drives food and weight units |
| Separate repos for the site and asamfit_bot | One repo: `asam89/fit` runs both the web app and the bot in one process | — |

## 1. Current architecture

- **Stack:** Python 3.12, FastAPI + Jinja2 (Tailwind CDN, Chart.js 4.4.7 already used on the dashboard), python-telegram-bot 21.10 (polling), APScheduler, SQLite in WAL mode (`data/fitnessbot.db`), and an LLM provider abstraction (`fitnessbot/inference/`, defaulting to Claude). It runs as one systemd service, `fitnessbot`, on OCI behind nginx.
- **Migrations:** `db.run_migrations()` runs at startup with a linear `schema_version` (prod is at **24**). It is forward-only and has no down migrations.
- **Telegram parsing:** `bot/handlers.py` sends text to `bot/conversation.process_message`. That function makes an LLM NLU call that returns intents (`meal_log`, `workout_log`, `health_metric`, `correction`, `query`, …). `_act_*` functions then act on them deterministically, and a second LLM call writes the reply. Photos go to vision meal parsing. A document is handled only when its caption starts with `/sync`; it is logged raw to `device_sync_log` and then handed to the LLM.
- **How workouts are stored today:**
  - `health_data` rows with `data_type='workout'`, holding `data_json` = `{activity, type, duration_min, source, notes}`. There is **no distance, pace or HR field**.
  - `training_plan_items` holds calendar items. `_act_workout` first tries `complete_by_title`, otherwise `log_completed_activity` creates a completed item, and `_reconcile_exercise` links it to a `health_data` row.
  - The legacy `exercise` table already has `distance`, `avg_hr` and `calories_burned` columns, but it is **unused** (0 rows in prod).
  - `personal_bests` (0 rows) stores free-form lifts and is not suitable for running PRs.
  - `wearable_connections` and the Settings "connect wearable" option are labels only and never sync anything. `INGEST_WEBHOOK_TOKEN` and `OURA_TOKEN` are defined in config but unused.
- **Prod data:** 48 workout rows, **0 runs** (none mention run or jog). Nothing needs migrating or backfilling.
- **Calories and daily totals:**
  - Food totals come from `meals`/`meal_items` over the user's local day (`tz.day_utc_range`).
  - Targets come from `nutrition.get_nutrition_targets()`: adaptive TDEE over 28 days, or Mifflin plus an activity multiplier at cold start, plus a goal delta.
  - Exercise calories come from `health_benefits.get_daily_benefits()` (MET × kg × h). They are **display-only** ("~N cal burned" in briefings and replies) and never change the target or food totals. Today a "run" would be estimated by MET from the activity name.
  - Accuracy 1 leaves the in-progress day out of averages and streaks. Accuracy 2 groups workouts into sessions (90-minute window).

## 2. Invariants this feature touches

| Existing rule | Risk from runs | How it is preserved |
|---|---|---|
| Store UTC; derive the user's local day via `tz.py` (`user_today`, `day_utc_range`) | Runs near midnight, imported runs that carry their own timezone | `started_at` is stored in UTC, and `local_date` is computed at save time from the **user's** timezone via `tz.py`. Tests cover a run at 23:30 local and a timezone change |
| Accuracy 1: exclude the in-progress day from averages and streaks | Weekly mileage and streaks | Run streaks count **weeks with ≥1 run**, so the current week counts once it has a run. The "4-week average" uses completed weeks only, consistent with Accuracy 1 |
| Accuracy 2: count distinct sessions, deduped | A run would be counted twice: once in `runs`, once in `health_data` | A run creates exactly one linked calendar item/`health_data` row with `run_id` in `data_json`. `get_daily_benefits` uses the run's own calories for that row instead of MET, so nothing is counted twice |
| Accuracy 3: targets come only from the stored profile | None, as long as run calories never touch targets | Run calories go only into the exercise line (Workstream E). The target code is unchanged |
| Accuracy 4: under/met/exceeded wording | None | Unchanged |
| Bot token and secrets are never logged (PR #65) | Strava tokens | Fernet-encrypted with the existing `ENCRYPTION_KEY` (same as Telegram tokens). The OAuth code and tokens are never logged |
| Prod deploy needs Alex's approval; `.backup()` before deploy | New migrations | Same flow. The migration is rehearsed on a `.backup()` copy of prod first |

## 3. Data model (Workstream A)

Migration 25 adds new tables only. Existing tables and queries are untouched.

```sql
CREATE TABLE runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  started_at TEXT NOT NULL,            -- UTC ISO
  local_date TEXT NOT NULL,            -- YYYY-MM-DD in user's tz at save time
  distance_m REAL NOT NULL,
  duration_s INTEGER NOT NULL,         -- elapsed
  moving_time_s INTEGER,
  elevation_gain_m REAL, avg_hr REAL, max_hr REAL, avg_cadence REAL,
  run_type TEXT CHECK (run_type IN ('easy','tempo','interval','long','race','treadmill')),
  surface  TEXT CHECK (surface IN ('road','trail','track','treadmill')),
  source   TEXT NOT NULL CHECK (source IN ('telegram','web','gpx','fit','strava','apple_health')),
  external_id TEXT,
  calories REAL, calories_source TEXT,  -- 'device'|'strava'|'estimated' (for E)
  perceived_effort INTEGER CHECK (perceived_effort BETWEEN 1 AND 10),
  notes TEXT,
  possible_duplicate_of INTEGER REFERENCES runs(run_id),
  plan_item_id INTEGER,                 -- link to training_plan_items
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE UNIQUE INDEX ux_runs_source_ext ON runs(user_id, source, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX ix_runs_user_date ON runs(user_id, local_date);

CREATE TABLE run_splits (
  run_id INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  split_index INTEGER NOT NULL,
  distance_m REAL NOT NULL, duration_s REAL NOT NULL,
  avg_hr REAL, elevation_change_m REAL,
  PRIMARY KEY (run_id, split_index)
);

CREATE TABLE run_drafts (            -- pending Telegram confirmations (B)
  draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  expires_at TEXT NOT NULL
);
```

- I added three columns not in the spec: `calories`/`calories_source` (needed by E), `plan_item_id` (needed for the session dedup above) and `possible_duplicate_of`. I also added a `run_drafts` table, because a draft has to survive a bot restart between the confirmation card and the Save tap.
- **Dedup:** the unique index on `(user_id, source, external_id)` makes imports idempotent. Duplicates across sources are flagged (start within 2 minutes and distance within 3%) via `possible_duplicate_of`; they are not silently dropped. The user is asked which copy to keep.
- **"Forward and back":** the repo has no down migrations. I'll add a `migrate_down_25()` helper (drops the three new tables, resets `schema_version` to 24) and test both directions on a `.backup()` copy of prod. It is not wired into startup.

## 4. Import options

| Option | Effort | Reliability | Auth / needs | Notes |
|---|---|---|---|---|
| **GPX/FIT file** (sent to the bot or uploaded on the site) | **Low–medium**, ~1 PR | High; deterministic parsing | None | GPX: parse with the existing `defusedxml`, no new dependency. FIT: add `fitdecode` (pure Python, maintained). The Telegram bot API downloads files up to 20 MB, plenty for one activity. Files come from Garmin Connect, Coros and Strava's "Export original". It's manual: you export each run |
| **Strava API** | **Medium**, ~1–2 PRs | High once set up; covers almost any watch, since devices sync to Strava | **You** register a Strava API app (client ID/secret, callback domain `fit-ness.ca`). OAuth, refresh tokens, a webhook subscription on a public HTTPS endpoint (we have one) | Default rate limits are small (per-15-min and daily caps). Fine for a few users and a 90-day backfill; webhook events only send IDs, so we fetch each activity. Streams give per-km splits |
| **Apple Health** | **High** for the full export; **medium** for a Shortcut push | Export: poor (the `export.zip` is often hundreds of MB, over Telegram's 20 MB limit, and the XML is huge). Shortcut: OK but fiddly to set up on the iPhone | Export: none. Shortcut: per-user ingest token | Follow-up only. If you run with an Apple Watch, Strava picks those runs up anyway |

**Recommendation:** build GPX/FIT first (C1), because it needs no third-party setup and gives us real split data to test the stats engine. Then Strava (C2), which is the "appears automatically" path in the Definition of Done. Apple Health is deferred.

**To unblock C2:** before C2 starts, please create the Strava app at strava.com/settings/api and put `STRAVA_CLIENT_ID`/`STRAVA_CLIENT_SECRET` in the prod `.env`.

## 5. Units and time zones

- Storage is metres and seconds only. Pace, speed, km and miles are derived at read time in `running/stats.py`.
- Display uses a new `users.run_units` column (`km` default, `mi` allowed), set on the Profile page and with `/runits km|mi`. The parser accepts either unit in a message regardless of the setting ("ran 3 miles" is stored as 4828 m).
- `local_date`: the date "today/yesterday/Oct 12" from Telegram means the user's local date via `tz.py`. For an import, it is the run's UTC start converted to the user's **current** timezone. It is computed once at save, so a later timezone change doesn't move past runs. Weekly stats use local Monday–Sunday weeks, matching the training calendar.

## 6. UX

**Telegram**
- Free text such as "ran 5k in 27:30" works like this:
  1. NLU adds a `run_log` intent, and the LLM returns only a structured draft.
  2. Deterministic code re-validates the draft and computes pace.
  3. The bot replies with a card: `🏃 Run — Tue Oct 21 · 5.00 km · 27:30 · 5:30/km · easy`, with buttons **[Save] [Edit] [Cancel]**.
  4. Only Save writes. On Save the bot replies `Saved ✅` plus any new PR (`🏆 New 5K PR: 27:30 (was 28:10)`).
- Interval workouts like "6x800 at 3:55 pace" become distance = 4.8 km of work, type `interval`, with the reps stored as splits. The card says it covers work reps only.
- Guardrails: a pace outside 2:30–15:00 /km, over 100 km or over 12 hours gets a clarifying question instead of a card ("Did you mean 5 km in 27:30, not 2:30?").
- Commands:
  - `/run` starts a guided log: distance, then time, then type.
  - `/runs` shows the last 5 runs with pace.
  - `/runstats` shows this week, the last 4 weeks, PRs, streak, and the load note.
  - `/editrun` and `/deleterun` act on the latest run, with confirmation.
- Sending a `.gpx`/`.fit` file with no caption is handled as an import. The `/sync` caption path stays unchanged.
- The Sunday-evening summary is behind `RUN_WEEKLY_SUMMARY_ENABLED=0`, so it is off by default.

**Website:** a new **Running** page (`/running`, linked from the header):
- weekly distance bar chart (12 weeks)
- easy-pace trend line (8 weeks)
- PR table (1K, 5K, 10K, half, full, with date and a link to the run)
- run history table filtered by type and date range; each row opens a run detail with splits and HR zones
- GPX/FIT upload, and later a Strava connect button

The page and `/runstats` both call the same `running/stats.py` functions, so their numbers are identical by construction. A test asserts that both render the same values.

## 7. Risks, open questions, estimates

**Risks**
- **Double counting run calories:** handled by the `run_id` link, with E's regression tests as a gate.
- **NLU confusion:** "ran errands" or "ran out of eggs" could be read as runs. The confirmation card stops anything being saved by mistake, and the parser test set includes negatives like these.
- **PR accuracy without splits:** a manual "10k in 50:00" also counts as the 5K best only if splits exist. Otherwise PRs come only from whole-run distances within ±2% of the target distance. PRs from GPS split streams use a rolling best-effort window.
- **Strava API terms, rate limits and token expiry:** tokens are refreshed before every call. An unreachable API is logged and retried, and the user gets at most one notice.
- **Old Telegram token:** still recommend rotating it (from PR #65's follow-up) before adding more integrations.

**Open questions for Alex**
1. What are "Guardrails 1-6"? See section 0.
2. Is Jira required? If so, please connect it in Devin. Otherwise the plan is PRs plus `HANDOFF.md`.
3. Is `run_units` defaulting to km OK, even though your profile is imperial?
4. Should running be enabled for all users, or only you behind a flag, at first?
5. Is it OK to add the `fitdecode` dependency for FIT files?
6. For E, should the run calorie estimate be the spec's ~1 kcal/kg/km or the existing MET model? I recommend 1.0 kcal/kg/km for runs, so it is distance-based, simpler and documented.

**Estimates** (Devin sessions; one PR each, in the order A → B → D → C1 → C2 → E)

| Workstream | Estimate | Notes |
|---|---|---|
| A: data model and migration, plus creating INVARIANTS.md and HANDOFF.md | 0.5 | |
| B: Telegram logging (parser and 30+ variants, card, commands) | 1 | |
| D: stats engine, `/runstats` and Running page | 1–1.5 | |
| C1: GPX/FIT import | 0.5–1 | |
| C2: Strava | 1 | Blocked on the Strava app credentials |
| E: calorie integration (flagged off) | 0.5 | |
| Apple Health | follow-up | |
| **Total** | **~5 sessions** | |
