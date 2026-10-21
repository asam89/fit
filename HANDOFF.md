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
- [ ] **B: Telegram logging.** Add a `run_log` intent that produces a draft, a confirmation card with Save/Edit/Cancel, guardrails, `/run`, `/runs`, `/editrun`, `/deleterun`, and 30+ parser variants. On Save, link the run to a calendar item (`plan_item_id`) so session counts and calories aren't doubled.
- [ ] **D: stats engine.** Write `fitnessbot/running/stats.py` (pure functions), then `/runstats`, the `/running` page and the PR table.
- [ ] **C1: GPX/FIT import.** Files come from the bot (no caption) and from upload on the site.
- [ ] **C2: Strava sync.** Blocked on Alex creating a Strava API app and putting `STRAVA_CLIENT_ID`/`STRAVA_CLIENT_SECRET` in the prod `.env`.
- [ ] **E: calorie integration.** Behind `RUN_CALORIES_ENABLED` (off by default).

## How to run
- Run tests with `./venv/bin/pip install pytest` and then `./venv/bin/python -m pytest`. Before PR A, there was one known failure: `tests/test_workout_web.py::TestAdjust::test_repeated_adjusting_does_not_force_a_deload` (it fails on `main` too).
- Deploy only after Alex approves: back up with sqlite `.backup()`, then on the host run `git pull --ff-only && sudo systemctl restart fitnessbot`. Migrations run at startup.
