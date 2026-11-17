# Handoff

Read this at the start of every session. Update it at every checkpoint and before every PR.

## Current project: Running stats
- Spec: Alex's "Running Stats Tracking" spec. Phase 0 report: `PHASE0_RUNNING_STATS.md` (approved 2026-10-21).
- Approved defaults:
  - `run_units` defaults to km, separate from `units_pref`.
  - Running is on for all users. The weekly summary and run calories are behind env flags, off by default.
  - `fitdecode` is OK for FIT files.
  - Run calories are about 1 kcal per kg per km.
- Guardrails 1-6 from the spec don't exist in the repo. Treat the Telegram Accuracy 1-4 rules in `INVARIANTS.md` as the baseline unless Alex says otherwise.
- Tracking: Jira isn't connected, so progress is tracked here and in the PRs.

## Workstreams (one PR each, in order)
- [x] **A: data model.** Migration 25 adds `runs`, `run_splits`, `run_drafts` and `users.run_units`. Code is in `fitnessbot/running/store.py`, with manual rollback via `db.migrate_down_25()`. Tests are in `tests/test_running_store.py`. Not deployed until Alex approves.
- [x] **B: Telegram logging.** Deterministic parser in `fitnessbot/running/parser.py` (no LLM), draft/card/Save flow in `fitnessbot/running/telegram.py`, wired in `bot/handlers.py` ahead of the NLU so run messages never also log a generic workout. `/run`, `/runs`, `/editrun`, `/deleterun`. Save creates a calendar item and stores it in `plan_item_id`. Personal-best messages ("new PB 5K 24:30") keep their old flow. Tests: `tests/test_running_telegram.py`. Needs migration 25 deployed (A + B deploy together). PR celebration on save waits for D's PR table.
- [x] **D: stats engine.** Pure functions in `fitnessbot/running/stats.py`; `build_summary()`/`user_summary()` is the one entry point, and `present()` makes the display strings both surfaces render. `/runstats` (Telegram) and the `/running` page plus `/running/{id}` run detail (`fitnessbot/web/running.py`, linked from the header). Save now announces new PRs ("🏆 New 5K PR: 27:30 (was 28:10)"). Tests: `tests/test_running_stats.py`, including one that checks `/runstats` and the page show identical numbers. Choices Alex hasn't confirmed:
  - **Load note:** shown when this week's distance is more than 30% above the 4-week average, and only when that average is at least 5 km.
  - **4-week average:** completed weeks only, starting from the user's first run week (empty weeks after that count as 0).
  - **PRs:** a whole run counts within ±2% of the distance (time scaled to the exact distance); longer runs count via the fastest consecutive splits. Interval reps never count.
  - **HR zones:** 50/60/70/80/90% of max HR, from per-split average HR. Max HR is the highest seen on a run, else 220 − age from the profile birthdate.
  - History lists up to 200 runs. GPX/FIT upload on the page arrives with C1.
- [ ] **C1: GPX/FIT import.** Files come from the bot (no caption) and from upload on the site.
- [ ] **C2: Strava sync.** Blocked on Alex creating a Strava API app and putting `STRAVA_CLIENT_ID`/`STRAVA_CLIENT_SECRET` in the prod `.env`.
- [ ] **E: calorie integration.** Behind `RUN_CALORIES_ENABLED` (off by default).

## How to run
- Run tests with `./venv/bin/pip install pytest` and then `./venv/bin/python -m pytest`. As of D, the full suite passes (629 tests).
- Deploy only after Alex approves: back up with sqlite `.backup()`, then on the host run `git pull --ff-only && sudo systemctl restart fitnessbot`. Migrations run at startup.
