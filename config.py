"""Configuration, loaded from environment (.env supported)."""

from __future__ import annotations

import logging
import os

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    try:
        load_dotenv()
    except OSError:
        # .env exists but isn't readable as this user - e.g. running as a service
        # account while .env is owned by another user. Not fatal: values are also
        # supplied via the process environment (systemd EnvironmentFile), so we
        # just skip loading the file rather than crashing at import.
        pass

log = logging.getLogger("honeywell.config")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


class Config:
    # --- Honeywell / Resideo credentials (REQUIRED) ---
    API_KEY = os.getenv("HONEYWELL_API_KEY", "")
    API_SECRET = os.getenv("HONEYWELL_API_SECRET", "")
    REDIRECT_URI = os.getenv("HONEYWELL_REDIRECT_URI", "http://localhost:8010/auth/callback")

    # --- Polling ---
    # Basic plan is sized for ~20 devices every 5 minutes. Don't go below this
    # without a higher rate limit from Resideo.
    POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 300)

    # --- Rate limiter guardrails ---
    RL_MIN_INTERVAL = _float("RL_MIN_INTERVAL", 1.0)
    RL_HOURLY_CAP = _int("RL_HOURLY_CAP", 250)

    # --- MQTT (optional) ---
    MQTT_ENABLED = _bool("MQTT_ENABLED", False)
    MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
    MQTT_PORT = _int("MQTT_PORT", 1883)
    MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
    MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "honeywell")

    # --- Scheduler ---
    SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "")  # e.g. "America/New_York"; blank = system tz

    # --- Sole Controller mode ---
    # When on, the app continuously holds every zone under a permanent hold so the
    # thermostats' onboard schedule and the Resideo app never drive them - this app
    # is the single top controller. On by default; this is the intended operating
    # mode. Can be turned off at runtime from the dashboard (persisted), which
    # overrides this startup default.
    SOLE_CONTROLLER = _bool("SOLE_CONTROLLER", True)

    # --- Server ---
    # Default port is 8010 (not 8000) so this can run on the same host as the
    # GenWatch generator monitor, which listens on 8000.
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 8010)

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
