"""Regression tests for the Slack offline/online notifier: message formatting,
transient-failure retry (429 with Retry-After / 5xx / network), terminal API
errors (a misconfiguration must NOT be retried forever), the give-up bound, and
that in-order delivery works end to end through the worker thread. No network:
the HTTP session and the sleep function are injected."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slack_notifier import SlackNotifier


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        # None body -> a healthy {"ok": true}; pass explicit {} / dict to override,
        # or the sentinel "NOJSON" to simulate an unparseable body.
        self._body = {"ok": True} if body is None else body
        self.headers = headers or {}

    def json(self):
        if self._body == "NOJSON":
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Records every post() and returns queued responses (or raises queued
    exceptions) in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": headers,
                           "payload": json.loads(data), "timeout": timeout})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def make(responses, **kw):
    """A notifier wired to a FakeSession and a non-sleeping sleep_fn (records the
    requested waits so backoff/Retry-After handling can be asserted)."""
    sess = FakeSession(responses)
    slept = []
    n = SlackNotifier("xoxb-test", "C123", session=sess,
                      sleep_fn=lambda s: slept.append(s), **kw)
    return n, sess, slept


class Formatting(unittest.TestCase):
    def test_offline_gets_red_circle_and_keeps_message(self):
        text = SlackNotifier._format({"kind": "offline", "message": "Arcade 1 went offline"})
        self.assertIn("🔴", text)
        self.assertIn("Arcade 1 went offline", text)

    def test_online_gets_green_circle(self):
        text = SlackNotifier._format({"kind": "online", "message": "Arcade 1 is back online"})
        self.assertIn("🟢", text)
        self.assertIn("back online", text)

    def test_unknown_kind_falls_back(self):
        text = SlackNotifier._format({"kind": "temp_high", "message": "hot"})
        self.assertIn("hot", text)


class DeliverySuccess(unittest.TestCase):
    def test_posts_channel_text_and_auth_header(self):
        n, sess, slept = make([FakeResp()])
        self.assertTrue(n._deliver({"channel": "C123", "text": "🔴 x went offline"}))
        self.assertEqual(len(sess.calls), 1)
        call = sess.calls[0]
        self.assertTrue(call["url"].endswith("/chat.postMessage"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer xoxb-test")
        self.assertEqual(call["payload"]["channel"], "C123")
        self.assertEqual(call["payload"]["text"], "🔴 x went offline")
        self.assertEqual(slept, [], "a first-try success must not sleep")


class RetryBehavior(unittest.TestCase):
    def test_429_honors_retry_after_then_succeeds(self):
        n, sess, slept = make([FakeResp(429, headers={"Retry-After": "2"}), FakeResp()])
        self.assertTrue(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 2)
        self.assertEqual(slept, [2.0])

    def test_429_retry_after_is_capped(self):
        n, sess, slept = make([FakeResp(429, headers={"Retry-After": "9999"}), FakeResp()],
                              retry_max_sleep=30)
        self.assertTrue(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(slept, [30.0], "a huge Retry-After must be clamped")

    def test_5xx_backs_off_then_succeeds(self):
        n, sess, slept = make([FakeResp(503), FakeResp()])
        self.assertTrue(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 2)
        self.assertEqual(len(slept), 1)

    def test_network_error_then_succeeds(self):
        n, sess, slept = make([ConnectionError("boom"), FakeResp()])
        self.assertTrue(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 2)

    def test_gives_up_after_max_retries(self):
        n, sess, slept = make([FakeResp(500)] * 10, max_retries=2)
        self.assertFalse(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 3, "1 initial try + 2 retries, then stop")

    def test_unparseable_body_is_not_a_confirmed_success(self):
        # A 2xx with an unparseable body is NOT proof the message posted (Slack
        # always returns {"ok": ...} JSON); it's retried, then dropped - never
        # silently counted as sent.
        n, sess, slept = make([FakeResp(200, body="NOJSON")] * 5, max_retries=1)
        self.assertFalse(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 2, "1 try + 1 retry")

    def test_4xx_is_retried_then_dropped(self):
        n, sess, slept = make([FakeResp(404, body="NOJSON")] * 5, max_retries=1)
        self.assertFalse(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 2, "1 try + 1 retry")


class TerminalErrors(unittest.TestCase):
    """A misconfiguration (bad token / wrong channel / bot not invited) must be
    logged once and dropped, never retried - retrying can't fix it and would just
    burn the rate limit."""

    def test_channel_not_found_is_not_retried(self):
        n, sess, slept = make([FakeResp(200, {"ok": False, "error": "channel_not_found"})])
        self.assertFalse(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 1)
        self.assertEqual(slept, [])

    def test_invalid_auth_is_not_retried(self):
        n, sess, slept = make([FakeResp(200, {"ok": False, "error": "invalid_auth"})])
        self.assertFalse(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 1)

    def test_not_in_channel_is_not_retried(self):
        n, sess, slept = make([FakeResp(200, {"ok": False, "error": "not_in_channel"})])
        self.assertFalse(n._deliver({"channel": "C123", "text": "hi"}))
        self.assertEqual(len(sess.calls), 1)


class Validation(unittest.TestCase):
    def test_requires_token_and_channel(self):
        with self.assertRaises(ValueError):
            SlackNotifier("", "C123")
        with self.assertRaises(ValueError):
            SlackNotifier("xoxb-x", "")


class SendNow(unittest.TestCase):
    """send_now() is the synchronous path behind the 'Send test message' button:
    one POST, no retry, and it reports the real (ok, error)."""

    def test_success(self):
        n, sess, _ = make([FakeResp()])
        self.assertEqual(n.send_now("hi"), (True, ""))
        self.assertEqual(len(sess.calls), 1)

    def test_reports_slack_error(self):
        n, sess, _ = make([FakeResp(200, {"ok": False, "error": "channel_not_found"})])
        ok, err = n.send_now("hi")
        self.assertFalse(ok)
        self.assertEqual(err, "channel_not_found")

    def test_network_error_reported(self):
        n, sess, _ = make([ConnectionError("boom")])
        ok, err = n.send_now("hi")
        self.assertFalse(ok)
        self.assertIn("boom", err)

    def test_single_attempt_no_retry(self):
        n, sess, _ = make([FakeResp(500, body="NOJSON"), FakeResp()])
        ok, err = n.send_now("hi")
        self.assertFalse(ok)
        self.assertEqual(len(sess.calls), 1, "a test send must not retry")


class WorkerIntegration(unittest.TestCase):
    """End to end through the real worker thread: start -> send_alert -> stop
    delivers the queued messages, in order, formatted, to the right channel."""

    def test_start_send_stop_delivers_in_order(self):
        n, sess, _ = make([FakeResp(), FakeResp()])
        n.start()
        n.send_alert({"kind": "offline", "message": "Z1 went offline"})
        n.send_alert({"kind": "online", "message": "Z1 is back online"})
        n.stop()   # drains queued messages ahead of the stop sentinel, then joins
        self.assertEqual(len(sess.calls), 2)
        self.assertEqual(sess.calls[0]["payload"]["channel"], "C123")
        self.assertIn("went offline", sess.calls[0]["payload"]["text"])
        self.assertIn("🔴", sess.calls[0]["payload"]["text"])
        self.assertIn("back online", sess.calls[1]["payload"]["text"])
        self.assertIn("🟢", sess.calls[1]["payload"]["text"])

    def test_empty_text_is_not_queued(self):
        n, sess, _ = make([])
        n.post("")   # nothing to say -> no work enqueued
        self.assertEqual(n._work.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
