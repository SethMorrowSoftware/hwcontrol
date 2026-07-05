"""Regression tests for app-level wiring: the schedule/rotation guard, control
field whitelisting, and store helpers used by the poller's reap guard."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.py requires credentials at import time; dummies are fine (no network).
os.environ.setdefault("HONEYWELL_API_KEY", "test-key")
os.environ.setdefault("HONEYWELL_API_SECRET", "test-secret")

# Import from a temp cwd so a stray tokens.json in the repo is never touched.
_prev = os.getcwd()
_tmp = tempfile.mkdtemp()
os.chdir(_tmp)
import app as app_mod  # noqa: E402
os.chdir(_prev)

from state_store import StateStore  # noqa: E402


class FakeEngine:
    def __init__(self, active):
        self._active = set(active)

    def active_rotation_targets(self):
        return set(self._active)


class ScheduleRotationGuard(unittest.TestCase):
    """A schedule period boundary firing mid-outage must not re-energize zones
    under an active generator rotation."""

    def setUp(self):
        self.calls, self.notes = [], []
        self._orig = (app_mod.apply_action, app_mod.engine, app_mod.notify)
        app_mod.apply_action = lambda t, a, refresh=True: self.calls.append(list(t) if isinstance(t, list) else t) or []
        app_mod.notify = lambda sev, kind, msg: self.notes.append(kind)

    def tearDown(self):
        app_mod.apply_action, app_mod.engine, app_mod.notify = self._orig

    def test_rotated_zones_are_skipped(self):
        app_mod.engine = FakeEngine({"Z1"})
        app_mod.apply_schedule_action(["Z1", "Z2"], {"mode": "Heat"})
        self.assertEqual(self.calls, [["Z2"]])
        self.assertIn("schedule_deferred", self.notes)

    def test_fully_rotated_target_applies_nothing(self):
        app_mod.engine = FakeEngine({"Z1"})
        failed = app_mod.apply_schedule_action(["Z1"], {"mode": "Heat"})
        self.assertEqual(self.calls, [])
        self.assertEqual(failed, [])

    def test_no_rotation_passes_through(self):
        app_mod.engine = FakeEngine(set())
        app_mod.apply_schedule_action(["Z1"], {"mode": "Heat"})
        self.assertEqual(self.calls, [["Z1"]])
        self.assertEqual(self.notes, [])


class ControlFieldWhitelist(unittest.TestCase):
    """Junk keys must never ride into the changeableValues body POSTed to
    Resideo (from MQTT payloads, hand-edited rules, or API callers)."""

    def setUp(self):
        self.writes = []
        self._store, self._client = app_mod.store, app_mod.client
        store = StateStore()
        store.ingest([{"deviceID": "D1", "name": "Zone 1", "isAlive": True,
                       "changeableValues": {"mode": "Off", "heatSetpoint": 60,
                                            "coolSetpoint": 80,
                                            "thermostatSetpointStatus": "NoHold"}}], 99)
        app_mod.store = store

        outer = self
        class FakeClient:
            is_authorized = True
            def set_thermostat(self, did, loc, overrides, current_changeable=None):
                outer.writes.append(("set", did, dict(overrides)))
            def set_fan(self, did, loc, mode):
                outer.writes.append(("fan", did, mode))
        app_mod.client = FakeClient()

    def tearDown(self):
        app_mod.store, app_mod.client = self._store, self._client

    def test_unknown_fields_dropped_and_hold_defaulted(self):
        failed = app_mod.apply_action("D1", {"mode": "Heat", "evilField": "x"}, refresh=False)
        self.assertEqual(failed, [])
        kind, did, overrides = self.writes[0]
        self.assertNotIn("evilField", overrides)
        self.assertEqual(overrides.get("mode"), "Heat")
        # Programmatic writes default to PermanentHold (sole source of truth).
        self.assertEqual(overrides.get("thermostatSetpointStatus"), "PermanentHold")

    def test_fan_routed_separately(self):
        app_mod.apply_action("D1", {"fan": "Circulate"}, refresh=False)
        self.assertEqual(self.writes, [("fan", "D1", "Circulate")],
                         "a fan-only command must not trigger a setpoint write")


class StoreLocationHelpers(unittest.TestCase):
    def test_device_ids_at_and_reap(self):
        s = StateStore()
        s.ingest([{"deviceID": "A", "name": "A", "isAlive": True}], 1)
        s.ingest([{"deviceID": "B", "name": "B", "isAlive": True}], 2)
        self.assertEqual(s.device_ids_at(1), ["A"])
        self.assertEqual(s.device_ids_at(2), ["B"])
        self.assertEqual(s.device_ids_at(3), [])
        events = s.reap({"A"})   # B no longer reported by a complete poll
        self.assertEqual([e["deviceID"] for e in events], ["B"])
        self.assertEqual(s.device_ids_at(2), [])

    def test_online_carries_forward_when_unreported(self):
        s = StateStore()
        s.ingest([{"deviceID": "A", "name": "A", "isAlive": False}], 1)
        s.ingest([{"deviceID": "A", "name": "A"}], 1)   # isAlive omitted this poll
        self.assertFalse(s.get("A")["online"],
                         "an unreported isAlive must not read as 'came back online'")


if __name__ == "__main__":
    unittest.main()
