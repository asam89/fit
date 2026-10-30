# Invariants

Rules that must hold across every change. A change that would break one stops and asks Alex first.

## Dates and time
1. Timestamps are stored in UTC. Every user-facing date ("today", a day's totals, week boundaries) is derived from the user's `users.timezone` via `fitnessbot/tz.py` (`user_now`, `user_today`, `day_utc_range`). Never use the server clock or SQLite `date('now')` for "today".
2. LLM prompts get the user's local clock (`compose_prompt(..., now=user_now(user_id))`).

## Telegram accuracy (Accuracy 1-4, `tests/test_telegram_accuracy.py`)
3. The in-progress day is left out of averages, target-hit counts and streaks.
4. Workouts are counted as distinct sessions (entries within 90 minutes count as one session), not as individual exercises.
5. Calorie and macro targets come only from the stored profile via `get_nutrition_targets()`. A number in a message is never adopted as a goal.
6. Target wording: under target is < 95%, met target is within 5%, exceeded target is > 105%.

## Calories
7. Exercise calories never change calorie targets, food totals or partial-day aggregation. They are shown on a separate exercise line.

## Secrets
8. Secrets and tokens (Telegram bot token, third-party OAuth tokens) are stored Fernet-encrypted with `ENCRYPTION_KEY` and never logged. `httpx` INFO logging stays silenced (`fitnessbot/main.py`).

## Running
9. Runs are stored in metres and seconds. Pace, speed and display units (`users.run_units`, `km`/`mi`) are derived at read time and never stored.
10. No run is saved from Telegram text without the user's explicit Save. Parsed runs wait in `run_drafts`.
11. All running stats are computed by the running stats module only. Handlers and templates never duplicate the calculations.
12. Run calories enter the day only through the single integration function (Workstream E), behind `RUN_CALORIES_ENABLED` (off by default). Invariant 7 still applies.
13. Imports are idempotent on `(user_id, source, external_id)`, enforced by the unique index `ux_runs_source_ext`. Likely duplicates from different sources (start within 2 minutes, distance within 3%) are flagged in `possible_duplicate_of`, never silently dropped.
14. A run's `local_date` is computed once at save time from the user's timezone and does not change if the user's timezone changes later.
15. New scheduled messages and integrations are off by default and enabled only by an environment variable.

## Enhancements (DRAFT for review, Phase 0 of the Enhancements spec)
Spec rule numbers are in brackets. Amendments to existing rules:
- **1** [S1] gains: streaks, challenges, recaps and adaptive targets use the same day function as the dashboard (`tz.py`). There is no second implementation; the `utc_offset_hours` and `DATE(logged_at)` queries in `db.py` move onto it.
- **3** [S3] gains: an unfinished today never counts as a missed day or a broken streak until it has ended in the user's timezone.
- **7** gains: *except* the flagged adaptive-target adjustment (rule 18), which is stored separately and never alters food totals.
- **13** [S8] gains: deduplication spans all sources (wearable, Strava, GPX/FIT, Telegram); the same activity is counted once.
- **15** [S10] gains: every new feature is behind a flag that defaults to off, and turning it off returns behaviour to the current production state.

New rules:
16. [S2] Daily totals shown in the dashboard, Telegram replies, streaks, leaderboards and recap cards always match for the same user and day.
17. [S4] Logging a meal never changes the user's base target. Adaptive targets are stored as a separate daily adjustment, and the base target is always recoverable.
18. [S5] An adaptive adjustment is never negative and never more than +30% of the base target on any day, and suggested intake is never below the safety floor (`nutrition.SAFETY_FLOOR`).
19. [S6] Group leaderboards show only streak days, goal hit yes or no, and challenge points. Never calories, weight, meals or photos unless the user explicitly opts in per challenge.
20. [S7] A user can leave a challenge or revoke Strava at any time; their data stops being shown or imported immediately and stored third-party tokens are deleted.
21. [S9] At most one scheduled leaderboard post per group per day, enforced by a unique key, not by timing.
22. A Telegram bot acts only for the account it is linked to: a message is processed only when its sender is mapped to that account.
