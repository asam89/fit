# fit-ness.ca Administration Guide

Complete reference for deploying, configuring, and managing the Fitness & Health Intelligence Platform.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Deployment](#deployment)
6. [Database Management](#database-management)
7. [Telegram Bot Setup](#telegram-bot-setup)
8. [AI Provider Configuration](#ai-provider-configuration)
9. [Scheduled Jobs & Notifications](#scheduled-jobs--notifications)
10. [User Management](#user-management)
11. [Dashboard Features](#dashboard-features)
12. [Telegram Commands](#telegram-commands)
13. [Wearable Integration](#wearable-integration)
14. [Monitoring & Logs](#monitoring--logs)
15. [Backup & Recovery](#backup--recovery)
16. [Troubleshooting](#troubleshooting)
17. [Security](#security)

---

## Architecture Overview

```
                        +------------------+
                        |   fit-ness.ca    |
                        |   (Web Browser)  |
                        +--------+---------+
                                 |
                        +--------v---------+
                        |   FastAPI + Uvicorn  |
                        |   (Port 8000)        |
                        +--------+---------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v------+  +-------v-------+  +-------v--------+
     | Jinja2 HTML   |  | REST API      |  | APScheduler    |
     | Dashboard     |  | /api/*        |  | Briefings +    |
     | Settings      |  | Targets, Trends|  | Event Coaching |
     | Auth Pages    |  | Weight, Meals  |  | (per-minute)   |
     +---------------+  +-------+-------+  +-------+--------+
                                |                  |
                        +-------v------------------v--------+
                        |         SQLite Database            |
                        |         (fitnessbot.db)            |
                        +-------+---------------------------+
                                |
              +------------------+------------------+
              |                                     |
     +--------v--------+               +-----------v-----------+
     | Telegram Bot    |               | AI Inference Layer     |
     | (per-user bot   |               | Anthropic / OpenAI /   |
     |  via python-    |               | Google Gemini          |
     |  telegram-bot)  |               +-----------------------+
     +-----------------+
```

**Key components:**

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web server | FastAPI + Uvicorn | Dashboard, settings, auth, REST API |
| Templates | Jinja2 + Tailwind CSS | Server-side rendered HTML |
| Database | SQLite | All persistent data (users, meals, weights, etc.) |
| Telegram | python-telegram-bot | Per-user bot connections for meal logging and coaching |
| AI | Anthropic Claude (default) | Meal parsing, coaching replies, event coaching |
| Scheduler | APScheduler | Morning/midday/evening briefings, event check-ins |
| Auth | Session cookies + bcrypt | Email/password + optional Google OAuth |

---

## Prerequisites

- **Python 3.12+**
- **pip** or **pipenv**
- A **Telegram Bot Token** (from [@BotFather](https://t.me/BotFather))
- An **Anthropic API Key** (or OpenAI/Gemini key for alternative providers)
- A server with a public IP or domain (for production)

---

## Installation

### Local Development

```bash
# Clone the repository
git clone https://github.com/asam89/fit.git
cd fit

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your values (see Configuration section)

# Run the application
python -m fitnessbot.main
```

The app starts at `http://localhost:8000`.

### Docker

```bash
# Build and run
docker compose up -d

# Or build manually
docker build -t fitnessbot .
docker run -p 8000:8000 --env-file .env -v ./data:/app/data fitnessbot
```

The `data/` volume persists the SQLite database and uploaded meal photos.

---

## Configuration

All configuration is via environment variables. Create a `.env` file in the project root.

### Required Variables

| Variable | Description | How to Generate |
|----------|------------|-----------------|
| `SECRET_KEY` | Session signing key | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ENCRYPTION_KEY` | Fernet key for encrypting bot tokens at rest | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `ANTHROPIC_API_KEY` | Claude API key for meal parsing and coaching | Get from [console.anthropic.com](https://console.anthropic.com) |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_HOST` | `0.0.0.0` | Bind address |
| `DASHBOARD_PORT` | `8000` | HTTP port |
| `DATABASE_PATH` | `./fitnessbot.db` | Path to SQLite database |
| `ANALYSIS_MODEL` | `claude-sonnet-4-6` | Model for meal parsing and coaching |
| `ROUTER_MODEL` | `claude-sonnet-4-6` | Model for intent classification |
| `OPENAI_API_KEY` | _(empty)_ | For Whisper voice transcription |
| `GOOGLE_CLIENT_ID` | _(empty)_ | Google OAuth (enables "Sign in with Google") |
| `GOOGLE_CLIENT_SECRET` | _(empty)_ | Google OAuth secret |
| `GOOGLE_REDIRECT_URI` | `https://fit-ness.ca/auth/google/callback` | OAuth callback URL |
| `SMTP_HOST` | _(empty)_ | Email server for verification emails |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | _(empty)_ | SMTP username |
| `SMTP_PASSWORD` | _(empty)_ | SMTP password |
| `SMTP_FROM` | `noreply@fit-ness.ca` | Sender email address |
| `SUPER_ADMIN_EMAIL` | `alexsam89@gmail.com` | Email that gets super admin access |
| `BASE_URL` | `https://fit-ness.ca` | Public URL (used in Telegram links) |
| `TIMEZONE` | `America/Toronto` | Default server timezone |
| `QUIET_HOURS` | `22:00-07:00` | No notifications during these hours |
| `NUTRITIONIX_APP_ID` | _(empty)_ | Optional food database API fallback |
| `NUTRITIONIX_API_KEY` | _(empty)_ | Nutritionix API key |
| `INGEST_WEBHOOK_TOKEN` | _(empty)_ | Token for device data webhooks |

---

## Deployment

### Production Setup (systemd)

The application runs as a systemd service on an Ubuntu server.

**1. Install on server:**

```bash
cd /home/ubuntu
git clone https://github.com/asam89/fit.git
cd fit
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Create .env file:**

```bash
cp .env.example .env
nano .env  # Fill in all required values
```

**3. Install the systemd service:**

```bash
sudo cp infrastructure/fitnessbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fitnessbot
sudo systemctl start fitnessbot
```

**4. Verify:**

```bash
sudo systemctl status fitnessbot
curl http://localhost:8000
```

### Service Management

```bash
# Start / stop / restart
sudo systemctl start fitnessbot
sudo systemctl stop fitnessbot
sudo systemctl restart fitnessbot

# View status
sudo systemctl status fitnessbot

# View logs (live)
sudo journalctl -u fitnessbot -f

# View recent logs
sudo journalctl -u fitnessbot --since "1 hour ago"
```

### Deploying Updates

```bash
cd /home/ubuntu/fit
git pull origin main
sudo systemctl restart fitnessbot
```

Migrations run automatically on startup (`db.run_migrations()` in `main.py`). No manual migration steps are needed.

### Reverse Proxy (Nginx)

For HTTPS and domain routing, place Nginx in front:

```nginx
server {
    listen 443 ssl;
    server_name fit-ness.ca;

    ssl_certificate /etc/letsencrypt/live/fit-ness.ca/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/fit-ness.ca/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Uploaded meal photos
    location /uploads/ {
        proxy_pass http://127.0.0.1:8000/uploads/;
    }
}

server {
    listen 80;
    server_name fit-ness.ca;
    return 301 https://$host$request_uri;
}
```

---

## Database Management

### Schema

The application uses SQLite with automatic migrations. Key tables:

| Table | Purpose |
|-------|---------|
| `users` | User accounts, profile data (height, weight, sex, timezone) |
| `sessions` | Web login sessions |
| `telegram_connections` | Per-user Telegram bot tokens and chat IDs |
| `meals` / `meal_items` / `foods` | Meal logging and food nutrition cache |
| `body_composition` | Weight, body fat %, muscle mass entries |
| `weight_trend` | Materialized smoothed weight + 7d/30d trend data |
| `nutrition_targets` | Computed calorie/macro targets per user |
| `goals` | User goals (cut, bulk, maintain, event) |
| `exercise` | Workout logs |
| `sleep` | Sleep duration and quality logs |
| `training_plans` / `daily_workouts` | AI-generated weekly training plans |
| `event_goals` | Event-based coaching (tournaments, races, etc.) |
| `personal_bests` | Personal records for exercises |
| `briefing_log` | Dedup log for scheduled messages |
| `notification_preferences` | Per-user notification settings |
| `wearable_connections` / `device_sync_log` | Connected wearable devices |
| `friendships` / `nudge_templates` | Social features |
| `schema_version` | Tracks applied migrations |

### Migrations

Migrations are numbered sequentially (currently through migration 20). They run automatically on app startup. The system:

1. Checks `schema_version` table for the current version
2. Applies any pending migrations in order
3. Each migration adds columns, tables, or backfills data
4. Migrations are idempotent (safe to re-run)

**To check current schema version:**

```bash
sqlite3 fitnessbot.db "SELECT MAX(version) FROM schema_version;"
```

### Database Location

- **Development:** `./fitnessbot.db` (project root)
- **Production:** `/home/ubuntu/fit/fitnessbot.db`
- **Docker:** `/app/data/fitnessbot.db` (mapped to `./data/` on host)

### Manual Queries

```bash
# Open interactive SQLite shell
sqlite3 fitnessbot.db

# Useful queries
.tables                                    -- List all tables
.schema users                              -- Show table schema
SELECT count(*) FROM users;                -- User count
SELECT count(*) FROM meals;                -- Total meals logged
SELECT * FROM users WHERE is_superadmin=1; -- List admins
```

---

## Telegram Bot Setup

Each user connects their own Telegram bot. Here's the process:

### For Users (Self-Service)

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the bot token (format: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Go to `https://fit-ness.ca/settings`
5. Paste the token in the Telegram Connection section
6. Send any message to your bot in Telegram (this enables chat ID detection)
7. Click "Connect" -- the system validates the token and detects the chat ID

### For Admins

The application manages bot connections via:

- `telegram_connections` table stores encrypted bot tokens
- `bot/manager.py` starts/stops individual bot polling connections
- On startup, all active connections are started automatically
- Each bot runs its own polling loop via `python-telegram-bot`

**Bot tokens are encrypted at rest** using the `ENCRYPTION_KEY` Fernet key.

### Troubleshooting Bot Connections

```bash
# Check if bot connections are active
sudo journalctl -u fitnessbot | grep "Active bots"

# Check specific user's connection
sqlite3 fitnessbot.db "SELECT user_id, bot_username, is_active, validated_at FROM telegram_connections;"

# Restart all bot connections (restart the service)
sudo systemctl restart fitnessbot
```

---

## AI Provider Configuration

### Default Provider

The application uses **Anthropic Claude** by default. Set `ANTHROPIC_API_KEY` in `.env`.

### User-Level API Keys

Users can bring their own API keys via Settings > AI Provider:

- **Anthropic** (Claude) -- recommended
- **OpenAI** (GPT-4)
- **Google** (Gemini)

User keys are encrypted at rest and stored in `user_api_keys` table. When a user has their own key, it's used instead of the server-wide key.

### Model Selection

| Purpose | Config Variable | Default |
|---------|----------------|---------|
| Meal parsing & coaching | `ANALYSIS_MODEL` | `claude-sonnet-4-6` |
| Intent classification | `ROUTER_MODEL` | `claude-sonnet-4-6` |
| Voice transcription | `WHISPER_MODEL_SIZE` | `base` |

### Voice Transcription

Voice messages sent to the Telegram bot are transcribed using `faster-whisper` (local, no API key needed for basic models). Set `OPENAI_API_KEY` if you want to use OpenAI's Whisper API instead.

---

## Scheduled Jobs & Notifications

The scheduler runs two job types:

### 1. Briefings (every minute check)

Checks each user's notification preferences and sends messages at their configured times.

| Briefing | Default Time | Content |
|----------|-------------|---------|
| Morning Brief | 07:30 | Weight status, today's targets, yesterday's performance, training plan |
| Midday Check | 13:00 | Calorie/macro progress so far, suggestions for remaining meals |
| Evening Wrap | 20:30 | Full day summary, met/missed targets, weight trend, coaching |
| Weekly Rollup | Sunday evening | 7-day averages, trend analysis, week-over-week comparison |

Users can customize times and enable/disable each briefing type in Settings > Notifications.

### 2. Event Coaching (every 30 minutes check)

For users with active event goals (tournaments, races, etc.):

- Sends 1 motivational check-in per day
- Adapts tone based on data (training volume, sleep, nutrition adherence)
- Only sends during waking hours (8am-9pm local time)
- Respects `motivation_frequency` setting (daily or every other day)

### Quiet Hours

No notifications are sent during quiet hours (default: 10pm-7am). Configurable via `QUIET_HOURS` in `.env`.

### Notification Preferences

Users control their notifications in Settings > Notifications:

- Enable/disable each briefing type
- Set custom times for each briefing
- Toggle activity-aware prompts
- Set weekly rollup day

---

## User Management

### Registration

Users register at `https://fit-ness.ca/register` with:
- Email and password
- Optional: Google OAuth ("Sign in with Google")
- Optional: Invite link from existing user

### Super Admin

The super admin email is set via `SUPER_ADMIN_EMAIL` in `.env`. The super admin gets:

- Access to `/admin` dashboard
- View all users and their activity
- System-wide statistics

**To manually set a super admin:**

```bash
sqlite3 fitnessbot.db "UPDATE users SET is_superadmin = 1 WHERE email = 'admin@example.com';"
sudo systemctl restart fitnessbot
```

### User Profile Data

Each user's profile includes:
- Display name, email
- Height, weight, sex, birthdate
- Timezone (used for all date calculations)
- Units preference (imperial/metric)
- Activity level (sedentary through extra active)
- Dietary restrictions (JSON)

### Deleting a User

```bash
# SQLite cascading deletes handle all related data
sqlite3 fitnessbot.db "DELETE FROM users WHERE user_id = 123;"
```

This cascades to: sessions, telegram_connections, meals, meal_items, goals, body_composition, weight_trend, exercise, sleep, etc.

---

## Dashboard Features

### Today Card
- Calorie ring (consumed/target with % progress)
- Protein, carbs, fat progress bars
- Fiber, sugar, sodium, water secondary metrics
- Sodium warning when >2300mg
- Meal type badges and quick log

### Weight Tracking Card
- Current weight, 7d change, 30d change
- Distance to goal weight
- Inline trend chart (smoothed + raw + goal line)
- Status badge: On Track / Off Track / Stalled / At Goal
- Actionable suggestions

### Nutrition Targets
- TDEE with activity level selector
- Goal type and daily delta (cut/maintain/gain)
- Macro breakdown (protein, carbs, fat)
- Recalculate button
- Manual target override
- "Estimates, not medical advice" disclaimer

### Training Plan
- Weekly grid (Mon-Sun)
- Exercise type color coding
- Mark workouts complete via checkbox
- Adherence tracking

### Body Composition
- Body fat % and muscle mass tracking
- Historical entries

### Personal Bests
- Track PRs for exercises (bench, squat, deadlift, etc.)
- Historical records

### Consistency Heatmap
- 30-day calendar view
- Color-coded by daily logging activity

### Trends
- Weight trend chart (7d, 30d, quarter, year)
- Calorie balance chart
- Period selectors (week, month, quarter, year)

### Friends & Social
- Friend search by handle, name, or email
- Friend cards with activity preview
- Nudge/trash talk buttons
- Pending requests management

### Month Summary
- Daily pills with calorie status
- Green (on target), red (over), gray (missed)

### Food Diary
- Full meal history by date
- Expandable nutritional breakdown per meal
- Meal type editing
- Delete meals

---

## Telegram Commands

Users interact with the bot via natural language or these commands:

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and setup instructions |
| `/today` | Today's calorie and macro summary |
| `/weight <number>` | Log a weight entry (e.g., `/weight 225.6`) |
| `/plan` | View this week's training plan |
| `/undo` | Delete the last logged meal |
| `/delete` | Same as `/undo` |
| `/dashboard` | Get a link to the web dashboard |
| `/invite` | Generate an invite link for friends |
| `/sync` | Import health data from wearables |

### Natural Language

Beyond commands, users can:
- **Log meals:** "I had 2 eggs and toast for breakfast"
- **Log workouts:** "Did 30 min run at the gym"
- **Log sleep:** "Slept 7 hours last night"
- **Ask questions:** "How am I doing this week?"
- **Send photos:** Take a photo of food for AI-powered meal analysis
- **Send voice:** Voice messages are transcribed and processed

---

## Wearable Integration

### Supported Platforms
- Apple Health (XML/CSV export)
- Samsung Health
- Garmin
- Fitbit
- Oura Ring
- WHOOP

### How to Connect

1. Go to Settings > Wearable
2. Select your device type
3. Enter a device name
4. Click "Add Device"

### Syncing Data

**Via Telegram:**
- Send `/sync` with data pasted as text
- Or attach a CSV/JSON/XML export file

**Via Dashboard:**
- Settings > Wearable shows sync instructions per platform

### Data Tracked
- Steps
- Calories burned
- Heart rate
- Sleep data
- Weight
- Workouts

---

## Monitoring & Logs

### Application Logs

```bash
# Live logs
sudo journalctl -u fitnessbot -f

# Last 100 lines
sudo journalctl -u fitnessbot -n 100

# Filter by time
sudo journalctl -u fitnessbot --since "2026-06-22 10:00" --until "2026-06-22 12:00"

# Filter errors only
sudo journalctl -u fitnessbot -p err
```

### Key Log Patterns

| Pattern | Meaning |
|---------|---------|
| `Active bots: N` | Number of Telegram bot connections running |
| `Scheduler started` | APScheduler is running |
| `Event check-in sent for user X` | Coaching message delivered |
| `Dispatch failed for user X` | Briefing delivery error |
| `Coaching reply failed` | AI inference error in conversation |

### Health Checks

```bash
# Check if service is running
systemctl is-active fitnessbot

# Check if web server responds
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000

# Check database size
ls -lh /home/ubuntu/fit/fitnessbot.db

# Check database integrity
sqlite3 /home/ubuntu/fit/fitnessbot.db "PRAGMA integrity_check;"
```

### Briefing Log

To check what messages were sent and when:

```sql
SELECT briefing_type, content_summary, sent_at
FROM briefing_log
WHERE user_id = 1
ORDER BY sent_at DESC
LIMIT 20;
```

---

## Backup & Recovery

### Database Backup

The entire application state lives in `fitnessbot.db`. Back it up regularly:

```bash
# Simple copy (stop service first for consistency)
sudo systemctl stop fitnessbot
cp /home/ubuntu/fit/fitnessbot.db /home/ubuntu/backups/fitnessbot_$(date +%Y%m%d).db
sudo systemctl start fitnessbot

# Or use SQLite online backup (no downtime)
sqlite3 /home/ubuntu/fit/fitnessbot.db ".backup /home/ubuntu/backups/fitnessbot_$(date +%Y%m%d).db"
```

### Automated Backup (cron)

```bash
# Add to crontab: daily backup at 3am
crontab -e
0 3 * * * sqlite3 /home/ubuntu/fit/fitnessbot.db ".backup /home/ubuntu/backups/fitnessbot_$(date +\%Y\%m\%d).db"
```

### Uploaded Files

Meal photos are stored in `data/uploads/meals/`. Include this directory in backups:

```bash
tar -czf /home/ubuntu/backups/uploads_$(date +%Y%m%d).tar.gz /home/ubuntu/fit/data/uploads/
```

### Recovery

```bash
# Stop service
sudo systemctl stop fitnessbot

# Restore database
cp /home/ubuntu/backups/fitnessbot_20260622.db /home/ubuntu/fit/fitnessbot.db

# Restore uploads (if backed up)
tar -xzf /home/ubuntu/backups/uploads_20260622.tar.gz -C /

# Start service (migrations will run if needed)
sudo systemctl start fitnessbot
```

---

## Troubleshooting

### Service Won't Start

```bash
# Check logs for error
sudo journalctl -u fitnessbot -n 50

# Common causes:
# 1. Missing .env file or required variables
# 2. Python venv not found
# 3. Port 8000 already in use
# 4. Database file permissions
```

### Telegram Bot Not Responding

1. Check if bot connections are active:
   ```bash
   sudo journalctl -u fitnessbot | grep "Active bots"
   ```
2. Verify the bot token is valid:
   ```bash
   curl https://api.telegram.org/bot<TOKEN>/getMe
   ```
3. Check if chat_id is set:
   ```sql
   SELECT chat_id FROM telegram_connections WHERE user_id = 1;
   ```
4. Restart the service to re-establish connections

### Briefings Not Sending

1. Check notification preferences:
   ```sql
   SELECT * FROM notification_preferences WHERE user_id = 1;
   ```
2. Verify the user's timezone is set correctly
3. Check quiet hours aren't blocking delivery
4. Look for dispatch errors in logs:
   ```bash
   sudo journalctl -u fitnessbot | grep "Dispatch failed"
   ```
5. Check briefing_log for recent sends:
   ```sql
   SELECT * FROM briefing_log WHERE user_id = 1 ORDER BY sent_at DESC LIMIT 5;
   ```

### Calorie/Macro Calculations Seem Wrong

1. Check the user's profile data:
   ```sql
   SELECT sex, height, birthdate, activity_level FROM users WHERE user_id = 1;
   ```
2. Check stored nutrition targets:
   ```sql
   SELECT * FROM nutrition_targets WHERE user_id = 1;
   ```
3. Hit the Recalculate button on the dashboard to recompute targets
4. Verify weight is up to date (targets use current weight)

### Database Locked

SQLite can lock during concurrent writes. If you see "database is locked" errors:

```bash
# Restart the service
sudo systemctl restart fitnessbot

# If persistent, check for stuck processes
fuser /home/ubuntu/fit/fitnessbot.db
```

### Migration Errors

Migrations are designed to be idempotent. If a migration fails:

1. Check the error in logs
2. Check current schema version:
   ```sql
   SELECT MAX(version) FROM schema_version;
   ```
3. Migrations use `try/except` for `ALTER TABLE` to handle already-existing columns
4. If stuck, you can manually insert the version:
   ```sql
   INSERT INTO schema_version (version) VALUES (XX);
   ```

---

## Security

### Encryption

- **Bot tokens:** Encrypted at rest using Fernet symmetric encryption (`ENCRYPTION_KEY`)
- **User passwords:** Hashed with bcrypt (never stored in plaintext)
- **User API keys:** Encrypted at rest using Fernet
- **Sessions:** Signed with `SECRET_KEY`, stored as hashed tokens in DB

### Access Control

- **Authentication:** Session cookie-based (30-day expiry)
- **Super Admin:** Controlled via `SUPER_ADMIN_EMAIL` env var
- **User isolation:** All queries are scoped by `user_id`; users can only see their own data

### Sensitive Files

Never commit or expose:
- `.env` file (contains all secrets)
- `fitnessbot.db` (contains user data and encrypted tokens)
- `data/uploads/` (user-uploaded meal photos)

### Key Rotation

To rotate the encryption key:

1. Export all encrypted data
2. Set new `ENCRYPTION_KEY` in `.env`
3. Re-encrypt all bot tokens and API keys
4. Restart the service

**Note:** Changing `ENCRYPTION_KEY` without re-encrypting existing data will break all Telegram connections and stored API keys.

### HTTPS

Always run behind an HTTPS reverse proxy (Nginx + Let's Encrypt) in production. The application itself serves HTTP on port 8000.

---

## Quick Reference

### File Structure

```
fit/
  fitnessbot/
    main.py              # Entry point, lifespan management
    config.py            # All configuration variables
    db.py                # Database schema, migrations, DAL
    nutrition.py         # BMR/TDEE/macro calculations (Mifflin-St Jeor)
    briefings.py         # Morning/midday/evening message builders
    scheduler.py         # APScheduler job definitions
    event_coaching.py    # Event-goal coaching and motivation
    training_plan.py     # Weekly training plan logic
    metrics.py           # Weight trend calculations
    router.py            # Intent classification
    tz.py                # Timezone utilities
    voice.py             # Voice transcription (faster-whisper)
    bot/
      manager.py         # Telegram bot connection manager
      handlers.py        # Telegram command and message handlers
      conversation.py    # AI-powered conversation engine
    ai/
      food_parser.py     # AI meal parsing
      prompts.py         # System prompts
    inference/
      factory.py         # Provider selection logic
      anthropic_provider.py
      openai_provider.py
      gemini_provider.py
    web/
      app.py             # FastAPI app factory
      auth.py            # Login, register, Google OAuth
      dashboard.py       # Main dashboard routes
      settings.py        # Settings page routes
      social.py          # Friends & social features
      admin.py           # Super admin dashboard
      templates/         # Jinja2 HTML templates
  infrastructure/
    fitnessbot.service   # systemd unit file
  static/                # CSS, images, client-side assets
  tests/                 # Unit tests (142 tests)
  data/uploads/meals/    # Uploaded meal photos
  requirements.txt       # Python dependencies
  Dockerfile             # Container build
  docker-compose.yml     # Docker Compose config
  .env.example           # Template for environment variables
```

### Common Operations

| Task | Command |
|------|---------|
| Start service | `sudo systemctl start fitnessbot` |
| Stop service | `sudo systemctl stop fitnessbot` |
| Restart service | `sudo systemctl restart fitnessbot` |
| View logs | `sudo journalctl -u fitnessbot -f` |
| Deploy update | `cd /home/ubuntu/fit && git pull origin main && sudo systemctl restart fitnessbot` |
| Backup database | `sqlite3 fitnessbot.db ".backup backup.db"` |
| Run tests | `python -m pytest tests/ -x -q` |
| Check DB version | `sqlite3 fitnessbot.db "SELECT MAX(version) FROM schema_version;"` |
| Add super admin | `sqlite3 fitnessbot.db "UPDATE users SET is_superadmin=1 WHERE email='...';"` |
