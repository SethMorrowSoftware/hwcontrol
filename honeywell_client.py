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
  whatever comes back, and we save it *durably* (atomic write + .bak) so a crash
  or power-loss mid-write can't corrupt tokens.json and brick auth.

* Token acquisition is single-flighted: a refresh runs the network POST WITHOUT
  holding the state lock, so a slow token endpoint can't freeze every other
  thread that just needs to read the current token. Concurrent 401s collapse to
  one refresh instead of burning several rotations.

* A tiny rate limiter enforces a minimum gap between calls and an hourly cap.
  Crucially it computes how long to wait while holding its lock but *sleeps
  outside it*, so hitting the hourly cap throttles the calling thread without
  freezing every other API call (a generator load-shed must not wait behind a
  poll that happened to trip the cap).

* Every network/parse failure is surfaced as HoneywellError so callers can react
  uniformly (raise an alert, skip a device) instead of an unexpected exception
  tearing out of a bulk control loop and silently skipping the rest of the zones.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from storage import atomic_write_json, load_json

log = logging.getLogger("honeywell.client")

AUTH_BASE = "https://api.honeywellhome.com"
API_BASE = "https://api.honeywellhome.com/v2"
AUTHORIZE_URL = f"{AUTH_BASE}/oauth2/authorize"
TOKEN_URL = f"{AUTH_BASE}/oauth2/token"

# Refresh this many seconds before the token actually expires.
TOKEN_SAFETY_WINDOW = 90
# How long to wait for HTTP; (connect, read) so a stalled read can't hang forever.
HTTP_TIMEOUT = (10, 30)


class HoneywellError(RuntimeError):
    """Raised when the API returns an error or we have no usable token."""


class NotAuthorized(HoneywellError):
    """Raised when no token is stored yet and the user must complete OAuth."""


class _RateLimiter:
    """Minimum-interval + rolling hourly cap.

    Computes the required wait while holding the lock, then sleeps OUTSIDE it and
    re-checks, so one thread parked waiting on the hourly cap never blocks other
    threads from making progress the instant a slot frees up.
    """

    def __init__(self, min_interval: float = 1.0, hourly_cap: int = 250):
        self.min_interval = max(0.0, min_interval)
        self.hourly_cap = max(1, hourly_cap)
        self._last = 0.0
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                # enforce minimum spacing
                wait = self.min_interval - (now - self._last)
                # enforce hourly cap
                cutoff = now - 3600
                while self._calls and self._calls[0] < cutoff:
                    self._calls.popleft()
                if len(self._calls) >= self.hourly_cap:
                    cap_wait = self._calls[0] + 3600 - now
                    if cap_wait > wait:
                        wait = cap_wait
                        log.warning("Hourly rate cap reached; throttling %.0fs", cap_wait)
                if wait <= 0:
                    # Reserve the slot now, while still holding the lock.
                    self._calls.append(now)
                    self._last = now
                    return
            # Sleep without the lock held, then loop and re-check.
            time.sleep(wait)


class HoneywellClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: str,
        token_path: str = "tokens.json",
        min_interval: float = 1.0,
        hourly_cap: int = 250,
        max_retries: int = 4,
        retry_max_sleep: float = 120.0,
    ):
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri
        self.token_path = Path(token_path)
        # Bounded retry for transient failures (429/5xx/network). Writes here set
        # absolute state (setpoints, hold, mode), so they're idempotent and safe
        # to re-send. Retries still pass through the rate limiter.
        self.max_retries = max(0, int(max_retries))
        self.retry_max_sleep = max(1.0, float(retry_max_sleep))

        self._lock = threading.Lock()          # guards the token fields below
        self._refresh_lock = threading.Lock()  # single-flights the refresh network call
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
        """Run a refresh network POST. Does NOT hold self._lock across the call."""
        with self._lock:
            rt = self._refresh_token
        if not rt:
            raise NotAuthorized("No refresh token available. Complete OAuth login first.")
        log.info("Refreshing access token...")
        self._token_request({"grant_type": "refresh_token", "refresh_token": rt})

    def _token_request(self, data: dict[str, str]) -> None:
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = self._session.post(TOKEN_URL, headers=headers, data=data, timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise HoneywellError(f"Token request network error: {exc}") from exc
        if not resp.ok:
            raise HoneywellError(f"Token request failed ({resp.status_code}): {resp.text}")
        try:
            payload = resp.json()
            access = payload["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HoneywellError(f"Malformed token response: {exc}") from exc
        # Honeywell rotates the refresh token: keep whatever came back, but fall
        # back to the old one if (rarely) none was returned.
        new_refresh = payload.get("refresh_token")
        try:
            expires_in = int(payload.get("expires_in", 599))
        except (TypeError, ValueError):
            expires_in = 599
        with self._lock:
            self._access_token = access
            if new_refresh:
                self._refresh_token = new_refresh
            self._expires_at = time.time() + expires_in
            snapshot = {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "expires_at": self._expires_at,
            }
        self._save_tokens(snapshot)

    def _valid_token(self) -> str:
        """Return a valid access token, refreshing if necessary. Thread-safe and
        single-flighted: the refresh network call does not hold self._lock."""
        with self._lock:
            if not self._access_token:
                raise NotAuthorized("Not authorized yet. Visit /auth/login to connect the account.")
            if time.time() < (self._expires_at - TOKEN_SAFETY_WINDOW):
                return self._access_token
        return self._refresh_and_get()

    def _refresh_and_get(self, used_token: Optional[str] = None) -> str:
        """Single-flight a refresh. If another thread already produced a fresh
        token while we waited for the refresh lock, use that instead of burning
        another rotation.

        ``used_token`` is passed from the 401 path: the server rejected that exact
        token, so we must refresh even though it may not be expired by the clock -
        UNLESS another thread has already rotated to a different token in the
        meantime (then that new token is what we retry with)."""
        with self._refresh_lock:
            with self._lock:
                if used_token is None:
                    # Proactive (near-expiry) refresh: skip if another thread just did it.
                    if self._access_token and time.time() < (self._expires_at - TOKEN_SAFETY_WINDOW):
                        return self._access_token
                else:
                    # 401 path: skip our refresh only if the current token already
                    # differs from the one that got rejected.
                    if self._access_token and self._access_token != used_token:
                        return self._access_token
            self._refresh()
            with self._lock:
                if not self._access_token:
                    raise NotAuthorized("Refresh produced no token.")
                return self._access_token

    @property
    def is_authorized(self) -> bool:
        return bool(self._refresh_token or self._access_token)

    # ------------------------------------------------------------- persistence

    def _save_tokens(self, snapshot: dict) -> None:
        try:
            atomic_write_json(self.token_path, snapshot, mode=0o600)
        except OSError as exc:
            # A failed save of a freshly *rotated* refresh token is dangerous:
            # the in-memory token works now, but on the next restart the on-disk
            # copy holds a token Honeywell has already invalidated -> auth bricked.
            # Make it loud rather than a buried debug line.
            log.error("CRITICAL: could not persist rotated tokens to %s: %s -- "
                      "auth will break on restart until this is fixed.",
                      self.token_path, exc)

    def _load_tokens(self) -> None:
        data = load_json(self.token_path)
        if not isinstance(data, dict):
            return
        try:
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._expires_at = float(data.get("expires_at", 0))
            log.info("Loaded stored tokens from %s", self.token_path)
        except (ValueError, TypeError) as exc:
            log.warning("Stored tokens malformed: %s", exc)

    # ---------------------------------------------------------------- requests

    @staticmethod
    def _parse_retry_after(resp) -> Optional[float]:
        """Seconds to wait per the server's Retry-After header (bare seconds or an
        HTTP-date), or None if absent/unparseable. Lets us cooperate with a 429
        instead of guessing the backoff."""
        val = (resp.headers.get("Retry-After") or "").strip()
        if not val:
            return None
        try:
            return max(0.0, float(val))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime
            import datetime as _dt
            dt = parsedate_to_datetime(val)
            if dt is not None:
                now = _dt.datetime.now(dt.tzinfo) if dt.tzinfo else _dt.datetime.now()
                return max(0.0, (dt - now).total_seconds())
        except Exception:
            pass
        return None

    def _retry_wait(self, attempt: int, retry_after: Optional[float]) -> float:
        """How long to sleep before retry `attempt` (1-based): honor Retry-After
        when the server sent one, else exponential backoff (2,4,8,16…), capped at
        retry_max_sleep either way so a bad header can't park a thread for an hour."""
        base = retry_after if retry_after is not None else 2.0 * (2 ** (attempt - 1))
        return min(self.retry_max_sleep, max(0.0, base))

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

        attempt = 0          # counts transient (429/5xx/network) retries
        did_401_refresh = False
        while True:
            self._limiter.acquire()
            try:
                resp = self._session.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=HTTP_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                # Network blip: retry (writes are idempotent) up to the bound.
                if attempt < self.max_retries:
                    attempt += 1
                    wait = self._retry_wait(attempt, None)
                    log.warning("%s %s network error (%s); retry %d/%d in %.0fs",
                                method, path, exc, attempt, self.max_retries, wait)
                    time.sleep(wait)
                    token = self._valid_token()  # token may have expired during backoff
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                raise HoneywellError(f"{method} {path} network error after {attempt} retries: {exc}") from exc

            # One automatic refresh+retry on 401 in case the token died early. This
            # is separate from the transient-retry budget.
            if resp.status_code == 401 and not did_401_refresh:
                did_401_refresh = True
                log.info("Got 401; forcing a refresh and retrying.")
                token = self._refresh_and_get(used_token=token)
                headers["Authorization"] = f"Bearer {token}"
                continue

            # Retry rate-limit (429) and server errors (5xx). Honors Retry-After on
            # 429 so we back off exactly as long as Honeywell asks, then try again -
            # so a control action eventually lands instead of failing on a throttle.
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt < self.max_retries:
                    attempt += 1
                    wait = self._retry_wait(attempt, self._parse_retry_after(resp))
                    log.warning("%s %s -> HTTP %s; retry %d/%d in %.0fs",
                                method, path, resp.status_code, attempt, self.max_retries, wait)
                    time.sleep(wait)
                    token = self._valid_token()
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                if resp.status_code == 429:
                    raise HoneywellError(
                        f"Rate limited by Honeywell (429) after {attempt} retries. "
                        f"Raise POLL_INTERVAL_SECONDS / request a higher limit.")
                raise HoneywellError(f"{method} {path} failed ({resp.status_code}) after {attempt} retries: {resp.text}")

            if not resp.ok:
                # A real 4xx (bad request, not found, …) - not worth retrying.
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
        device = self._request(
            "GET", f"devices/thermostats/{device_id}", params={"locationId": location_id}
        )
        if not isinstance(device, dict):
            raise HoneywellError(f"Unexpected empty response reading thermostat {device_id}")
        return device

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

        # HoldUntil requires nextPeriodTime; every other hold (and an absent hold)
        # must NOT carry a stale nextPeriodTime inherited from the cached object,
        # or Resideo may reject it or apply an unintended timed hold.
        status = body.get("thermostatSetpointStatus")
        if status != "HoldUntil":
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

    def get_schedule(self, device_id: str, location_id: int | str,
                     schedule_type: Optional[str] = None) -> Any:
        """Read a device's onboard 7-day schedule. Present on T-series/LCC units;
        round (TCC-) devices generally return an error (no /schedule resource).

        The endpoint requires the device's schedule type as a query param (e.g.
        "TimedNorthAmerica"); without it Resideo returns 400. The exact param name
        isn't in the public docs, so we send both `type` and `scheduleType` — the
        gateway ignores the unrecognized one."""
        params: dict[str, Any] = {"locationId": location_id}
        if schedule_type:
            params["type"] = schedule_type
            params["scheduleType"] = schedule_type
        return self._request("GET", f"devices/schedule/{device_id}", params=params)

    def set_schedule(self, device_id: str, location_id: int | str, schedule: dict,
                     schedule_type: Optional[str] = None) -> None:
        """Write a device's onboard schedule back (used to disable/restore it)."""
        params: dict[str, Any] = {"locationId": location_id}
        if schedule_type:
            params["type"] = schedule_type
            params["scheduleType"] = schedule_type
        self._request("POST", f"devices/schedule/{device_id}",
                      params=params, json_body=schedule)
