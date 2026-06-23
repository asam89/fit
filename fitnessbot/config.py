"""Central configuration loaded from environment / .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    # --- Web Dashboard ---
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
    DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8000"))
    ALLOWED_ORIGINS: str = os.getenv("ALLOWED_ORIGINS", "")

    # --- Encryption (for bot tokens at rest) ---
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")

    # --- External APIs ---
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # --- Optional food API fallback ---
    NUTRITIONIX_APP_ID: str = os.getenv("NUTRITIONIX_APP_ID", "")
    NUTRITIONIX_API_KEY: str = os.getenv("NUTRITIONIX_API_KEY", "")

    # --- Device Webhook ---
    INGEST_WEBHOOK_TOKEN: str = os.getenv("INGEST_WEBHOOK_TOKEN", "")

    # --- Optional Connectors ---
    OURA_TOKEN: str = os.getenv("OURA_TOKEN", "")

    # --- Database ---
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./fitnessbot.db")

    # --- AI Models ---
    ANALYSIS_MODEL: str = os.getenv("ANALYSIS_MODEL", "claude-sonnet-4-6")
    ROUTER_MODEL: str = os.getenv("ROUTER_MODEL", "claude-sonnet-4-6")
    WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "base")

    # --- Google OAuth ---
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI: str = os.getenv("GOOGLE_REDIRECT_URI", "http://fit.140.238.131.77.nip.io/auth/google/callback")

    # --- Email (for verification) ---
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "noreply@fit.io")

    # --- Admin ---
    SUPER_ADMIN_EMAIL: str = os.getenv("SUPER_ADMIN_EMAIL", "alexsam89@gmail.com")

    # --- Behavior ---
    TIMEZONE: str = os.getenv("TIMEZONE", "America/Toronto")
    QUIET_HOURS: str = os.getenv("QUIET_HOURS", "22:00-07:00")
    BRIEFING_TIMES: str = os.getenv("BRIEFING_TIMES", "07:30,13:00,20:30")

    # --- Paths ---
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    TEMPLATE_DIR: Path = Path(__file__).resolve().parent / "web" / "templates"
    STATIC_DIR: Path = BASE_DIR / "static"
