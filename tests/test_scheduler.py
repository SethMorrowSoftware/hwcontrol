"""Regression tests for the facility scheduler: active-period walkback,
timezone fallback, and program validation."""
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import FacilityScheduler


def sched(tmpdir, timezone=None):
    return FacilityScheduler(apply_fn=lambda t, a: None,
                             store_path=os.path.join(tmpdir, "schedules.json"),
                             timezone=timezone)


def at(weekday, hour, minute=0):
    """A datetime on the given weekday (0=Mon) of a fixed week."""
    base = datetime.datetime(2026, 7, 6)  # a Monday
    return base + datetime.timedelta(days=weekday, hours=hour, minutes=minute)


RULE = {"id": "p1", "enabled": True, "days": ["mon"], "targets": "all",
        "periods": [{"time": "06:00", "action": {"mode": "Heat"}},
                    {"time": "22:00", "action": {"mode": "Off"}}]}


class ActivePeriodWalkback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = sched(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mid_morning_uses_todays_earlier_period(self):
        p = self.s._active_period(RULE, at(0, 7))
        self.assertEqual(p["time"], "06:00")

    def test_before_first_period_walks_back_to_prior_active_day(self):
        p = self.s._active_period(RULE, at(0, 5))     # Mon 05:00 -> last Mon 22:00
        self.assertEqual(p["time"], "22:00")

    def test_inactive_day_uses_last_period_of_most_recent_active_day(self):
        p = self.s._active_period(RULE, at(1, 3))     # Tue 03:00 -> Mon 22:00
        self.assertEqual(p["time"], "22:00")

    def test_everyday_program_before_first_period(self):
        every = dict(RULE, days=[])
        p = self.s._active_period(every, at(3, 5))    # 05:00 -> yesterday 22:00
        self.assertEqual(p["time"], "22:00")


class TimezoneFallback(unittest.TestCase):
    """A typo in SCHEDULE_TZ must degrade to server-local time with an error
    surfaced, not crash the app into a systemd restart loop."""

    def test_invalid_timezone_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = sched(tmp, timezone="America/Notaplace")
            self.assertIsNotNone(s.timezone_error)
            self.assertIn("Notaplace", s.timezone_error)

    def test_valid_timezone_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = sched(tmp, timezone="America/New_York")
            self.assertIsNone(s.timezone_error)
            self.assertIn("New_York", s.timezone_name())


class GroupTargets(unittest.TestCase):
    """A program can target any custom group of zones (a non-empty list of
    deviceIDs), not just one zone or 'all'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = sched(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def periods():
        return [{"time": "06:00", "action": {"mode": "Heat"}}]

    def test_group_accepted_and_deduped(self):
        r = self.s.add_rule({"targets": ["A", "B", "A"], "periods": self.periods()})
        self.assertEqual(r["targets"], ["A", "B"],
                         "a zone picked twice must not be written twice per period")

    def test_empty_group_rejected(self):
        with self.assertRaises(ValueError):
            self.s.add_rule({"targets": [], "periods": self.periods()})

    def test_bad_targets_rejected(self):
        with self.assertRaises(ValueError):
            self.s.add_rule({"targets": 123, "periods": self.periods()})
        with self.assertRaises(ValueError):
            self.s.add_rule({"targets": ["A", ""], "periods": self.periods()})
        with self.assertRaises(ValueError):
            self.s.add_rule({"targets": "  ", "periods": self.periods()})

    def test_group_flows_through_to_apply(self):
        calls = []
        s2 = FacilityScheduler(apply_fn=lambda t, a: calls.append((t, dict(a))),
                               store_path=os.path.join(self.tmp.name, "s2.json"))
        r = s2.add_rule({"targets": ["A", "B"],
                         "periods": [{"time": "00:00", "action": {"mode": "Heat"}}]})
        self.assertTrue(s2.apply_active_now(r["id"]))
        self.assertEqual(calls[0][0], ["A", "B"],
                         "the whole group must be handed to the apply function")


class ActiveAssertions(unittest.TestCase):
    """active_assertions() reports what each enabled program says right now, for
    the poller's schedule-enforcement pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = sched(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reports_enabled_active_programs(self):
        self.s.add_rule({"id": "p1", "targets": ["Z1"], "days": [],
                         "periods": [{"time": "00:00", "action": {"mode": "Heat", "heatSetpoint": 68}}]})
        self.s.add_rule({"id": "p2", "enabled": False, "targets": "all",
                         "periods": [{"time": "00:00", "action": {"mode": "Off"}}]})
        out = self.s.active_assertions()
        self.assertEqual(len(out), 1, "disabled programs are excluded")
        targets, action = out[0]
        self.assertEqual(targets, ["Z1"])
        self.assertEqual(action.get("heatSetpoint"), 68)


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = sched(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_duplicate_period_times(self):
        bad = {"periods": [{"time": "06:00", "action": {"mode": "Heat"}},
                           {"time": "06:00", "action": {"mode": "Off"}}]}
        with self.assertRaises(ValueError):
            self.s.add_rule(bad)

    def test_rejects_bad_time_and_days(self):
        with self.assertRaises(ValueError):
            self.s.add_rule({"periods": [{"time": "25:00", "action": {"mode": "Off"}}]})
        with self.assertRaises(ValueError):
            self.s.add_rule({"days": ["funday"],
                             "periods": [{"time": "06:00", "action": {"mode": "Off"}}]})

    def test_legacy_single_time_rule_migrates(self):
        r = self.s.add_rule({"time": "06:00", "action": {"mode": "Heat"}})
        self.assertEqual(len(r["periods"]), 1)
        self.assertEqual(r["periods"][0]["time"], "06:00")


if __name__ == "__main__":
    unittest.main()
