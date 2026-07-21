"""
slack_notifier.py
-----------------
Best-effort Slack notifications for operator alerts - specifically a unit going
offline and coming back online - delivered through Slack's chat.postMessage Web
API with a bot (``xoxb-``) token.

Why it looks the way it does:

* Delivery runs on a single background worker thread fed by a queue, exactly
  like the MQTT bridge. Offline/online alerts are produced on the poller thread
  while it ingests a poll; a Slack POST can take seconds (or block on a slow
  network), so doing it inline would stall polling and, worse, hold up the very
  poll cycle that detects further state changes. Enqueue-and-return keeps the
  poller moving.

* Every failure is swallowed. A missed Slack ping must never take down the
  control loop, so network errors, timeouts and API errors are logged, retried
  where retrying can actually help (429 rate-limit / 5xx / network blips), and
  otherwise dropped. Misconfiguration errors (bad token, wrong channel, bot not
  invited) are terminal - they're logged once, loudly, instead of being retried
  pointlessly.

* The "exactly once per transition" edge logic lives in state_store's alert
  generation, not here. This module just delivers whatever alert it's handed, so
  that guarantee stays in one place with its own regression tests.

The delivery seam (``_deliver``) and the HTTP session are injectable so the
retry/format behavior can be tested without touching the network.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

try:
    import requests
except ImportError:  # pragma: no cover - requests is a hard dependency
    requests = None  # type: ignore

log = logging.getLogger("honeywell.slack")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Slack returns HTTP 200 with {"ok": false, "error": ...} for logical failures.
# These particular errors mean the setup is wrong and no retry will fix it, so we
# log them once (loudly) and drop the message instead of hammering the API.
_TERMINAL_SLACK_ERRORS = frozenset({
    "invalid_auth", "not_authed", "account_inactive", "token_revoked",
    "token_expired", "channel_not_found", "not_in_channel", "is_archived",
    "no_permission", "org_login_required", "missing_scope", "restricted_action",
})

# Prepended to the message so an offline/online alert reads at a glance in Slack.
_KIND_EMOJI = {"offline": "🔴", "online": "🟢"}


class SlackNotifier:
    """Posts alert messages to a Slack channel via a bot token, off-thread.

    Call :meth:`start` once (spawns the worker), then :meth:`send_alert` /
    :meth:`post` from any thread - they enqueue and return immediately. Call
    :meth:`stop` on shutdown to drain the queue and join the worker.
    """

    _STOP = object()   # sentinel that shuts the worker thread down

    def __init__(
        self,
        token: str,
        channel: str,
        *,
        timeout: Any = (5, 10),
        max_retries: int = 3,
        retry_max_sleep: float = 30.0,
        session: Any = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if requests is None:
            raise RuntimeError("requests is not installed. `pip install requests`")
        if not token:
            raise ValueError("Slack bot token is required")
        if not channel:
            raise ValueError("Slack channel is required")
        self.token = token
        self.channel = channel
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_max_sleep = max(1.0, float(retry_max_sleep))
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep_fn
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        # Inbound messages run on this single worker thread, in arrival order, so
        # a slow Slack API never blocks the poller. Unbounded on purpose: a short
        # backlog is cheaper than dropping an offline/online notification.
        self._work: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._started = False
        self._backlog_warned = 0.0

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker = threading.Thread(target=self._drain, name="slack-notify", daemon=True)
        self._worker.start()
        log.info("Slack notifier started (channel %s).", self.channel)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to finish the queued messages, then shut down.

        The sentinel is enqueued behind whatever is already queued, so messages
        posted before ``stop()`` are still delivered (best effort) before the
        worker exits."""
        if not self._started:
            return
        self._work.put(self._STOP)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
        self._started = False

    # ------------------------------------------------------------- enqueue

    def send_alert(self, alert: dict) -> None:
        """Format an alert dict (offline/online, but any alert works) and queue
        it for delivery. Safe to call from any thread; returns immediately."""
        self.post(self._format(alert))

    def post(self, text: str) -> None:
        """Queue a plain-text message for the worker thread. Returns at once."""
        if not text:
            return
        self._work.put({"channel": self.channel, "text": text})
        depth = self._work.qsize()
        # A growing backlog means Slack is unreachable or throttling us; warn at
        # most once a minute rather than on every enqueue.
        if depth > 100 and time.monotonic() - self._backlog_warned > 60:
            self._backlog_warned = time.monotonic()
            log.warning("Slack notify backlog is %d deep; Slack may be unreachable.", depth)

    @staticmethod
    def _format(alert: dict) -> str:
        kind = alert.get("kind", "")
        emoji = _KIND_EMOJI.get(kind, "⚠️")
        msg = alert.get("message") or kind or "alert"
        return f"{emoji} {msg}"

    # ------------------------------------------------------------- worker

    def _drain(self) -> None:
        """Worker loop: deliver queued messages in order. A failure in one
        message is logged and never kills the worker."""
        while True:
            item = self._work.get()
            if item is self._STOP:
                return
            try:
                self._deliver(item)
            except Exception as exc:  # pragma: no cover - _deliver already guards
                log.error("Slack delivery raised unexpectedly: %s", exc)

    def _deliver(self, payload: dict) -> bool:
        """POST one message, retrying transient failures with capped backoff.
        Returns True on success. Never raises - a failed Slack ping must not
        affect anything upstream."""
        attempt = 0
        while True:
            try:
                resp = self._session.post(POST_MESSAGE_URL, headers=self._headers,
                                          data=json.dumps(payload), timeout=self.timeout)
                status = getattr(resp, "status_code", 0)

                if status == 429:   # rate limited - honor Retry-After if present
                    if attempt >= self.max_retries:
                        log.warning("Slack rate-limited; giving up after %d retr%s.",
                                    attempt, "y" if attempt == 1 else "ies")
                        return False
                    wait = self._retry_after(resp, attempt)
                    log.info("Slack rate-limited; retrying in %.1fs.", wait)
                    self._sleep(wait)
                    attempt += 1
                    continue

                if 500 <= status < 600:   # transient server error - back off
                    if attempt >= self.max_retries:
                        log.warning("Slack server error %d; giving up after %d retr%s.",
                                    status, attempt, "y" if attempt == 1 else "ies")
                        return False
                    self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue

                # 2xx (Slack signals logical errors here too) or a non-429 4xx.
                ok, err = self._parse(resp)
                if ok:
                    return True
                if err in _TERMINAL_SLACK_ERRORS:
                    log.error("Slack rejected the message (%s) - check SLACK_BOT_TOKEN and "
                              "SLACK_CHANNEL, and that the bot has been invited to the "
                              "channel with the chat:write scope.", err)
                    return False
                # Unknown non-ok response: a couple of retries, then drop.
                if attempt >= self.max_retries:
                    log.warning("Slack post failed (%s); giving up after %d retr%s.",
                                err or status, attempt, "y" if attempt == 1 else "ies")
                    return False
                self._sleep(self._backoff(attempt))
                attempt += 1

            except Exception as exc:   # network error, timeout, DNS, TLS, ...
                if attempt >= self.max_retries:
                    log.warning("Slack post errored (%s); giving up after %d retr%s.",
                                exc, attempt, "y" if attempt == 1 else "ies")
                    return False
                self._sleep(self._backoff(attempt))
                attempt += 1

    # ------------------------------------------------------------- helpers

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff (1s, 2s, 4s, ...) capped at retry_max_sleep."""
        return min(self.retry_max_sleep, 2.0 ** attempt)

    def _retry_after(self, resp: Any, attempt: int) -> float:
        """Seconds to wait after a 429: the server's Retry-After header if it's a
        sane number (capped), otherwise plain backoff."""
        try:
            ra = resp.headers.get("Retry-After")
        except Exception:
            ra = None
        if ra is not None:
            try:
                return min(self.retry_max_sleep, max(0.0, float(ra)))
            except (TypeError, ValueError):
                pass
        return self._backoff(attempt)

    @staticmethod
    def _parse(resp: Any) -> tuple:
        """(ok, error) from a Slack response. Success is a POSITIVELY confirmed
        ``{"ok": true}`` - never merely a 2xx status. A body we can't parse (an
        unexpected proxy/gateway page) counts as a non-terminal failure, so it's
        retried a couple of times and then dropped rather than being mistaken for
        a delivered message."""
        status = getattr(resp, "status_code", 0)
        try:
            body = resp.json()
        except Exception:
            return False, f"unparseable_http_{status}"
        if isinstance(body, dict):
            return bool(body.get("ok")), (body.get("error") or f"http_{status}")
        return False, f"bad_response_http_{status}"
