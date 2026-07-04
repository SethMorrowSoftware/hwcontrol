"""
honeywell_client.py
-------------------
A synchronous client for the Resideo / Honeywell Home v2 API.

Design notes (why it looks the way it does):

* It uses the OAuth2 *Authorization Code* flow and then keeps a valid user
  access token alive by refreshing it. The docs confirm the user access token
  works for both reads (/locations, /devices/thermostats) and writes
  (POST /devices/thermostats/{id}). This avoids the client-credentials +
  UserRefID dance and the (non-existent) users/me endpoint.

* Honeywell ROTATES the refresh token on every refresh. If you don't persist
  the new refresh token you get one refresh and then failures. We always save
  whatever comes back.

* All token acquisition is guarded by a lock so the FastAPI threadpool, the
  poller thread, the scheduler thread and MQTT callbacks can't refresh at the
  same time and clobber each other's rotated refresh token.

* A tiny rate limiter enforces a minimum gap between calls and an hourly cap,
  because the Basic plan is sized for ~20 devices polled every 5 minutes.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger("honeywell.client")

AUTH_BASE = "https://api.honeywellhome.com"
API_BASE = "https://api.honeywellhome.com/v2"
AUTHORIZE_URL = f"{AUTH_BASE}/oauth2/authorize"
TOKEN_URL = f"{AUTH_BASE}/oauth2/token"

# Refresh this many seconds before the token actually expires.
TOKEN_SAFETY_WINDOW = 90


class HoneywellError(RuntimeError):
    """Raised when the API returns an error or we have no usable token."""


class NotAuthorized(HoneywellError):
    """Raised when no token is stored yet and the user must complete OAuth."""


class _RateLimiter:
    """Minimum-interval + rolling hourly cap. Blocks (sleeps) when needed."""

    def __init__(self, min_interval: float = 1.0, hourly_cap: int = 250):
        self.min_interval = min_interval
        self.hourly_cap = hourly_cap
        self._last = 0.0
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            # enforce minimum spacing
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            # enforce hourly cap
            cutoff = now - 3600
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()
            if len(self._calls) >= self.hourly_cap:
                sleep_for = self._calls[0] + 3600 - now
                log.warning("Hourly rate cap reached; sleeping %.0fs", sleep_for)
                time.sleep(max(sleep_for, 0))
                now = time.monotonic()
            self._calls.append(now)
            self._last = now


class HoneywellClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: str,
        token_path: str = "tokens.json",
        min_interval: float = 1.0,
        hourly_cap: int = 250,
    ):
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri
        self.token_path = Path(token_path)

        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: float = 0.0

        self._session = requests.Session()
        self._limiter = _RateLimiter(min_interval, hourly_cap)

        self._load_tokens()

    # ------------------------------------------------------------------ OAuth

    def _basic_auth_header(self) -> str:
        raw = f"{self.api_key}:{self.api_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def authorize_url(self, state: Optional[str] = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
        }
        if state:
            params["state"] = state
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> None:
        """Exchange an authorization code for tokens and persist them."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        self._token_request(data)
        log.info("OAuth authorization complete; tokens stored.")

    def _refresh(self) -> None:
        if not self._refresh_token:
            raise NotAuthorized("No refresh token available. Complete OAuth login first.")
        log.info("Refreshing access token...")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        self._token_request(data)

    def _token_request(self, data: dict[str, str]) -> None:
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = self._session.post(TOKEN_URL, headers=headers, data=data, timeout=30)
        if not resp.ok:
            raise HoneywellError(f"Token request failed ({resp.status_code}): {resp.text}")
        payload = resp.json()
        self._access_token = payload["access_token"]
        # Honeywell rotates the refresh token: keep whatever came back, but fall
        # back to the old one if (rarely) none was returned.
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        expires_in = int(payload.get("expires_in", "599"))
        self._expires_at = time.time() + expires_in
        self._save_tokens()

    def _valid_token(self) -> str:
        """Return a valid access token, refreshing if necessary. Thread-safe."""
        with self._lock:
            if not self._access_token:
                raise NotAuthorized("Not authorized yet. Visit /auth/login to connect the account.")
            if time.time() >= (self._expires_at - TOKEN_SAFETY_WINDOW):
                self._refresh()
            return self._access_token

    @property
    def is_authorized(self) -> bool:
        return bool(self._refresh_token or self._access_token)

    # ------------------------------------------------------------- persistence

    def _save_tokens(self) -> None:
        try:
            self.token_path.write_text(
                json.dumps(
                    {
                        "access_token": self._access_token,
                        "refresh_token": self._refresh_token,
                        "expires_at": self._expires_at,
                    }
                )
            )
            # Tokens are secrets; keep them owner-readable only.
            try:
                self.token_path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:  # pragma: no cover
            log.error("Could not persist tokens: %s", exc)

    def _load_tokens(self) -> None:
        if not self.token_path.exists():
            return
        try:
            data = json.loads(self.token_path.read_text())
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._expires_at = float(data.get("expires_at", 0))
            log.info("Loaded stored tokens from %s", self.token_path)
        except (OSError, ValueError) as exc:
            log.warning("Could not load stored tokens: %s", exc)

    # ---------------------------------------------------------------- requests

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        token = self._valid_token()
        params = dict(params or {})
        params["apikey"] = self.api_key  # required as a query param on every call
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{API_BASE}/{path.lstrip('/')}"
        self._limiter.acquire()
        resp = self._session.request(
            method, url, headers=headers, params=params, json=json_body, timeout=30
        )

        # One automatic retry on 401 in case the token died early.
        if resp.status_code == 401:
            log.info("Got 401; forcing a refresh and retrying once.")
            with self._lock:
                self._refresh()
                token = self._access_token
            headers["Authorization"] = f"Bearer {token}"
            self._limiter.acquire()
            resp = self._session.request(
                method, url, headers=headers, params=params, json=json_body, timeout=30
            )

        if resp.status_code == 429:
            raise HoneywellError("Rate limited by Honeywell (429). Slow down polling / request a higher limit.")
        if not resp.ok:
            raise HoneywellError(f"{method} {path} failed ({resp.status_code}): {resp.text}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ----------------------------------------------------------------- reads

    def get_locations(self) -> list[dict]:
        """All locations for the account, each with its devices inline."""
        return self._request("GET", "locations") or []

    def get_thermostats(self, location_id: int | str) -> list[dict]:
        """Every thermostat at one location, with full state. One call per location."""
        return self._request("GET", "devices/thermostats", params={"locationId": location_id}) or []

    def get_thermostat(self, device_id: str, location_id: int | str) -> dict:
        return self._request(
            "GET", f"devices/thermostats/{device_id}", params={"locationId": location_id}
        )

    # ----------------------------------------------------------------- writes

    def set_thermostat(
        self,
        device_id: str,
        location_id: int | str,
        overrides: dict,
        current_changeable: Optional[dict] = None,
    ) -> None:
        """
        Change setpoints / mode / hold.

        Best practice per the docs: take the device's existing changeableValues,
        modify only what you want, and POST the whole object back. Pass
        `current_changeable` (from cached state) so we don't accidentally reset
        fields we didn't mean to touch. If it's missing we fetch it first.
        """
        if current_changeable is None:
            device = self.get_thermostat(device_id, location_id)
            current_changeable = device.get("changeableValues", {}) or {}

        body = {**current_changeable, **overrides}

        # HoldUntil requires nextPeriodTime; other hold types must not send it.
        status = body.get("thermostatSetpointStatus")
        if status and status != "HoldUntil":
            body.pop("nextPeriodTime", None)

        self._request(
            "POST",
            f"devices/thermostats/{device_id}",
            params={"locationId": location_id},
            json_body=body,
        )

    def set_fan(self, device_id: str, location_id: int | str, mode: str) -> None:
        """Set fan mode (e.g. 'Auto', 'On', 'Circulate'). Endpoint per Resideo v2."""
        self._request(
            "POST",
            f"devices/thermostats/{device_id}/fan",
            params={"locationId": location_id},
            json_body={"mode": mode},
        )

    # ------------------------------------------------- onboard (device) schedule

    def get_schedule(self, device_id: str, location_id: int | str) -> Any:
        """Read a device's onboard 7-day schedule. Present on T-series/LCC units;
        round (TCC-) devices generally return an error (no /schedule resource)."""
        return self._request(
            "GET", f"devices/schedule/{device_id}", params={"locationId": location_id}
        )

    def set_schedule(self, device_id: str, location_id: int | str, schedule: dict) -> None:
        """Write a device's onboard schedule back (used to disable/restore it)."""
        self._request(
            "POST", f"devices/schedule/{device_id}",
            params={"locationId": location_id}, json_body=schedule,
        )
