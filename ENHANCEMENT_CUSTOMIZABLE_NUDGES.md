# Enhancement — Customizable Message Frequency & Activity Prompts

## Summary
Allow users to configure how often the bot sends check-in messages (briefings/nudges) from the dashboard, and have those messages prompt for the user's common activities based on their fitness profile.

## Requirements

### Customizable frequency
- Dashboard Settings section: slider or dropdown for message frequency
  - Options: Off, 1x/day (evening only), 2x/day (morning + evening), 3x/day (morning + midday + evening), Custom times
- Per-message-type toggles: morning brief, midday check, evening wrap, weekly rollup
- Custom time picker for each enabled message type
- Persist in user profile; scheduler reads per-user preferences

### Activity-aware prompts
- Messages reference the user's common activities from their training plan and logged history
- Example: if user regularly plays basketball on Mondays, Monday morning brief says "Basketball today — are you playing?"
- Evening nudge: "Did you do your planned workout today?" with inline complete buttons
- Learn activity patterns from logged workouts (frequency, time of day, duration)
- Suggest activities the user hasn't done in a while: "You haven't run in 10 days — want to add one this week?"

### Fitness profile integration
- Pull from: training plan (scheduled activities), workout history (past patterns), health data (sleep, weight trends)
- Prompt content adapts: rest day → recovery tips; heavy training day → nutrition focus; weigh-in day → reminder

## Data model
- Add to `users` table or new `notification_preferences`:
  - `morning_brief_enabled` (bool, default true)
  - `morning_brief_time` (TIME, default '07:30')
  - `midday_check_enabled` (bool, default true)
  - `midday_check_time` (TIME, default '13:00')
  - `evening_wrap_enabled` (bool, default true)
  - `evening_wrap_time` (TIME, default '20:30')
  - `weekly_rollup_enabled` (bool, default true)
  - `weekly_rollup_day` (int 0-6, default 6 = Sunday)

## UI
- Settings page: "Notifications" section with toggles + time pickers
- Dashboard: no new section — messages just become smarter based on profile
