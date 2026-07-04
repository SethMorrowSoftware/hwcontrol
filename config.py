"""Configuration, loaded from environment (.env supported)."""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # --- Honeywell / Resideo credentials (REQUIRED) ---
    API_KEY = os.getenv("HONEYWELL_API_KEY", "")
    API_SECRET = os.getenv("HONEYWELL_API_SECRET", "")
    REDIRECT_URI = os.getenv("HONEYWELL_REDIRECT_URI", "http://localhost:8000/auth/callback")

    # --- Polling ---
    # Basic plan is sized for ~20 devices every 5 minutes. Don't go below this
    # without a higher rate limit from Resideo.
    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

    # --- Rate limiter guardrails ---
    RL_MIN_INTERVAL = float(os.getenv("RL_MIN_INTERVAL", "1.0"))
    RL_HOURLY_CAP = int(os.getenv("RL_HOURLY_CAP", "250"))

    # --- MQTT (optional) ---
    MQTT_ENABLED = _bool("MQTT_ENABLED", False)
    MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
    MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
    MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
    MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "honeywell")

    # --- Scheduler ---
    SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "")  # e.g. "America/New_York"; blank = system tz

    # --- Server ---
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8000"))

    # --- Simple dashboard gate (optional) ---
    # If set, the dashboard and API require ?token=... or an X-Token header.
    DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

    @classmethod
    def require_credentials(cls) -> None:
        missing = [n for n in ("API_KEY", "API_SECRET") if not getattr(cls, n)]
        if missing:
            raise SystemExit(
                "Missing required config: "
                + ", ".join("HONEYWELL_" + m for m in missing)
                + ". Copy .env.example to .env and fill it in."
            )
