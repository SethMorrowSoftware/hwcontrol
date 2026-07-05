"""Regression tests for the automation engine's safety-critical behavior:
rotation window math, break-before-make under failure, tick serialization,
trigger edge latching, and trigger-state write debouncing.

Run:  python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation as automation_mod
from automation import AutomationEngine


def make_engine(tmpdir, apply_fn, notify_fn=None, resolve=("Z1", "Z2", "Z3", "Z4"),
                on_restored=None):
    return AutomationEngine(
        apply_fn=apply_fn,
        resolve_fn=lambda: list(resolve),
        snapshot_read_fn=lambda d: {"mode": "Heat", "heatSetpoint": 70, "coolSetpoint": 76,
                                    "thermostatSetpointStatus": "PermanentHold"},
        notify_fn=notify_fn or (lambda *a: None),
        rules_path=os.path.join(tmpdir, "rules.json"),
        snapshots_path=os.path.join(tmpdir, "snaps.json"),
        trigger_state_path=os.path.join(tmpdir, "trig.json"),
        rotations_path=os.path.join(tmpdir, "rots.json"),
        on_restored=on_restored,
    )


class RotationWindowMath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = make_engine(self.tmp.name, lambda t, v: [])

    def tearDown(self):
        self.tmp.cleanup()

    def test_count_window_slides_and_wraps(self):
        st = {"targets": ["A", "B", "C", "D"], "run_count": 2}
        self.assertEqual(self.eng._window_at(st, 0), ["A", "B"])
        self.assertEqual(self.eng._window_at(st, 3), ["D", "A"])

    def test_power_budget_fills_until_next_unit_would_exceed(self):
        st = {"targets": ["A", "B", "C", "D"], "run_count": 4, "max_power": 20.0,
              "power": {"A": 5, "B": 5, "C": 10, "D": 10}}
        self.assertEqual(self.eng._window_at(st, 0), ["A", "B", "C"])   # 5+5+10 = 20
        self.assertEqual(self.eng._window_at(st, 2), ["C", "D"])        # 10+10 = 20
        st19 = dict(st, max_power=19.0)
        self.assertEqual(self.eng._window_at(st19, 0), ["A", "B"])      # C would exceed

    def test_oversized_single_unit_still_runs(self):
        st = {"targets": ["A", "B"], "run_count": 2, "max_power": 3.0,
              "power": {"A": 5, "B": 5}}
        self.assertEqual(self.eng._window_at(st, 0), ["A"])  # never run nothing


class BreakBeforeMakeUnderFailure(unittest.TestCase):
    """A failed OFF (break) write may leave that unit physically running.
    The make phase must shrink so the group can't exceed its cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_writes_held_back_when_off_fails(self):
        calls, notes = [], []
        def apply_fn(target, values):
            calls.append((target, values.get("mode")))
            if target == "Z3" and values.get("mode") == "Off":
                return [target]                     # Z3 refuses to switch off
            return []
        eng = make_engine(self.tmp.name, apply_fn,
                          notify_fn=lambda sev, kind, msg: notes.append(kind))
        with eng._action_lock:                      # as the action runner would
            eng._rotations["r"] = {"targets": ["Z1", "Z2", "Z3", "Z4"], "run_count": 2,
                                   "interval": 5, "on_values": {"mode": "Heat"},
                                   "off_values": {"mode": "Off"}, "on_fraction": None,
                                   "max_power": None, "power": {}, "index": 0,
                                   "current_on": set(), "job_id": "rotation:r"}
            eng._advance_and_drive("r")
        ons = [t for t, m in calls if m == "Heat"]
        self.assertEqual(ons, ["Z1"], "one incoming zone must be held back for the failed off")
        self.assertIn("rotation_degraded", notes)
        self.assertEqual(eng._rotations["r"]["current_on"], {"Z1"})

    def test_power_weighted_holdback(self):
        # Failed-off unit draws 10; both incoming draw 5 each -> hold back BOTH.
        calls = []
        def apply_fn(target, values):
            calls.append((target, values.get("mode")))
            if target == "Z3" and values.get("mode") == "Off":
                return [target]
            return []
        eng = make_engine(self.tmp.name, apply_fn)
        with eng._action_lock:
            eng._rotations["r"] = {"targets": ["Z1", "Z2", "Z3"], "run_count": 2,
                                   "interval": 5, "on_values": {"mode": "Heat"},
                                   "off_values": {"mode": "Off"}, "on_fraction": None,
                                   "max_power": None,
                                   "power": {"Z1": 5, "Z2": 5, "Z3": 10}, "index": 0,
                                   "current_on": set(), "job_id": "rotation:r"}
            eng._advance_and_drive("r")
        self.assertEqual([t for t, m in calls if m == "Heat"], [],
                         "held-back draw must cover the whole failed-off draw")

    def test_full_state_driven_on_clean_tick(self):
        calls = []
        eng = make_engine(self.tmp.name, lambda t, v: calls.append((t, v.get("mode"))) or [])
        with eng._action_lock:
            eng._rotations["r"] = {"targets": ["Z1", "Z2", "Z3", "Z4"], "run_count": 2,
                                   "interval": 5, "on_values": {"mode": "Heat"},
                                   "off_values": {"mode": "Off"}, "on_fraction": None,
                                   "max_power": None, "power": {}, "index": 0,
                                   "current_on": set(), "job_id": "rotation:r"}
            eng._advance_and_drive("r")
        self.assertEqual(calls, [("Z3", "Off"), ("Z4", "Off"), ("Z1", "Heat"), ("Z2", "Heat")],
                         "break (all offs) must come before make (all ons)")
        self.assertEqual(eng._rotations["r"]["current_on"], {"Z1", "Z2"})


class TickSerialization(unittest.TestCase):
    """Scheduled ticks must serialize with rule actions (_action_lock) so an
    in-flight tick can't land an Off write after a concurrent restore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tick_waits_for_action_lock(self):
        calls = []
        eng = make_engine(self.tmp.name, lambda t, v: calls.append(t) or [])
        eng._rotations["r"] = {"targets": ["Z1", "Z2"], "run_count": 1, "interval": 5,
                               "on_values": {"mode": "Heat"}, "off_values": {"mode": "Off"},
                               "on_fraction": None, "max_power": None, "power": {},
                               "index": 0, "current_on": set(), "job_id": "rotation:r"}
        eng._action_lock.acquire()                  # a restore rule is mid-flight
        t = threading.Thread(target=eng._rotation_tick, args=("r",))
        t.start()
        time.sleep(0.4)
        self.assertEqual(calls, [], "tick must not write while rule actions hold the lock")
        eng._action_lock.release()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertTrue(calls, "tick must proceed once the lock frees")

    def test_first_tick_inside_run_actions_does_not_deadlock(self):
        # run_rule_now -> _run_actions holds _action_lock -> rotate action drives
        # the first tick; re-acquiring the (non-reentrant) lock would deadlock.
        eng = make_engine(self.tmp.name, lambda t, v: [])
        eng.add_rule({"id": "r-rot", "name": "rot", "enabled": True,
                      "trigger": {"mode": "all", "retrigger": "on_change",
                                  "conditions": [{"topic": "gen", "type": "any"}]},
                      "actions": [{"type": "rotate", "rotation_id": "g",
                                   "targets": ["Z1", "Z2"], "run_count": 1,
                                   "interval_minutes": 5,
                                   "on_values": {"mode": "Heat"},
                                   "off_values": {"mode": "Off"}}]})
        t = threading.Thread(target=eng.run_rule_now, args=("r-rot",))
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "run_rule_now with a rotate action deadlocked")
        eng._stop_rotation("g")


class TriggerEdgeSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_actions_retry_then_latch_then_rearm(self):
        fires = []
        state = {"n": 0}
        def apply_fn(target, values):
            fires.append(target)
            state["n"] += 1
            return [target] if state["n"] == 1 else []   # first attempt fails
        eng = make_engine(self.tmp.name, apply_fn)
        eng.add_rule({"id": "r1", "name": "gen", "enabled": True,
                      "trigger": {"mode": "all", "retrigger": "on_change",
                                  "conditions": [{"topic": "gen", "type": "equals", "value": "on"}]},
                      "actions": [{"type": "set", "targets": ["Z1"], "values": {"mode": "Off"}}]})
        eng.handle_message("gen", "on")     # fires, action FAILS -> not latched
        eng.handle_message("gen", "on")     # retained re-announce -> retries
        eng.handle_message("gen", "on")     # latched -> no fire
        eng.handle_message("gen", "off")    # falling edge re-arms
        eng.handle_message("gen", "on")     # fires again
        self.assertEqual(len(fires), 3)

    def test_and_across_topics(self):
        fires = []
        eng = make_engine(self.tmp.name, lambda t, v: fires.append(t) or [])
        eng.add_rule({"id": "r2", "name": "and", "enabled": True,
                      "trigger": {"mode": "all", "retrigger": "on_change",
                                  "conditions": [
                                      {"topic": "gen", "type": "equals", "value": "on"},
                                      {"topic": "load", "type": "gt", "value": 85, "field": "pct"}]},
                      "actions": [{"type": "set", "targets": ["Z1"], "values": {"mode": "Off"}}]})
        eng.handle_message("gen", "on")
        self.assertEqual(fires, [], "one true condition must not fire an AND rule")
        eng.handle_message("load", '{"pct": 90}')
        self.assertEqual(len(fires), 1)
        eng.handle_message("load", '{"pct": 50}')   # falls false -> re-arms
        eng.handle_message("load", '{"pct": 99}')
        self.assertEqual(len(fires), 2)


class TriggerStateDebounce(unittest.TestCase):
    """Trigger state is fsync'd to disk; a chatty topic must not force a write
    per message when nothing edge-relevant changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        automation_mod.atomic_write_json = self._orig
        self.tmp.cleanup()

    def test_only_matched_transitions_persist(self):
        writes = {"n": 0}
        self._orig = automation_mod.atomic_write_json
        def counting(path, data, **kw):
            if "trig" in str(path):
                writes["n"] += 1
            return self._orig(path, data, **kw)
        automation_mod.atomic_write_json = counting

        eng = make_engine(self.tmp.name, lambda t, v: [])
        eng.add_rule({"id": "r3", "name": "chatty", "enabled": True,
                      "trigger": {"mode": "all", "retrigger": "on_change",
                                  "conditions": [{"topic": "load", "type": "gt", "value": 100}]},
                      "actions": [{"type": "set", "targets": ["Z1"], "values": {"mode": "Off"}}]})
        writes["n"] = 0
        for i in range(50):
            eng.handle_message("load", str(i))      # value wobbles, matched stays False
        self.assertLessEqual(writes["n"], 1,
                             f"50 non-crossing messages caused {writes['n']} disk writes")
        eng.handle_message("load", "150")           # crossing -> must persist
        self.assertGreaterEqual(writes["n"], 2)


class SnapshotRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_is_non_clobbering_and_restore_clears_on_success(self):
        applied = []
        fail = {"on": False}
        def apply_fn(target, values):
            applied.append((target, dict(values)))
            return [target] if fail["on"] else []
        eng = make_engine(self.tmp.name, apply_fn, resolve=("Z1",))
        with eng._action_lock:
            eng._run_action({"type": "snapshot", "name": "pre", "targets": ["Z1"]})
            self.assertIn("Z1", eng._snapshots["pre"])
            captured = dict(eng._snapshots["pre"]["Z1"])
            # A replayed snapshot (retained "on" after restart) must keep the original.
            eng._snapshots["pre"]["Z1"] = {"marker": True}
            eng._run_action({"type": "snapshot", "name": "pre", "targets": ["Z1"]})
            self.assertEqual(eng._snapshots["pre"]["Z1"], {"marker": True})
            eng._snapshots["pre"]["Z1"] = captured

            fail["on"] = True                        # failed restore keeps the snapshot
            eng._run_action({"type": "restore", "name": "pre"})
            self.assertIn("pre", eng._snapshots)
            fail["on"] = False                       # successful restore clears it
            eng._run_action({"type": "restore", "name": "pre"})
            self.assertNotIn("pre", eng._snapshots)


class PostRestoreScheduleResume(unittest.TestCase):
    """A successful restore must fire on_restored so the app can immediately
    re-assert daily programs (boundaries fired during the outage were skipped -
    zones would otherwise sit at pre-outage setpoints until the NEXT boundary)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.restored_calls = []
        self.fail = {"on": False}

        def apply_fn(target, values):
            return [target] if self.fail["on"] else []
        self.eng = make_engine(self.tmp.name, apply_fn,
                               on_restored=lambda ids: self.restored_calls.append(sorted(ids)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_hook_fires_with_restored_ids_on_full_success(self):
        with self.eng._action_lock:
            self.eng._run_action({"type": "snapshot", "name": "pre", "targets": ["Z1", "Z2"]})
            self.eng._run_action({"type": "restore", "name": "pre"})
        self.assertEqual(self.restored_calls, [["Z1", "Z2"]])

    def test_hook_skipped_when_nothing_saved(self):
        with self.eng._action_lock:
            self.eng._run_action({"type": "restore", "name": "never-captured"})
        self.assertEqual(self.restored_calls, [],
                         "a no-op restore (retained 'off' replay) must not rewrite programs")

    def test_hook_skipped_on_partial_failure_then_fires_on_retry(self):
        with self.eng._action_lock:
            self.eng._run_action({"type": "snapshot", "name": "pre", "targets": ["Z1"]})
            self.fail["on"] = True
            self.eng._run_action({"type": "restore", "name": "pre"})
            self.assertEqual(self.restored_calls, [], "failed restore must not assert programs")
            self.fail["on"] = False
            self.eng._run_action({"type": "restore", "name": "pre"})   # the retry
        self.assertEqual(self.restored_calls, [["Z1"]])

    def test_hook_failure_is_contained_and_alerted(self):
        notes = []
        def boom(ids):
            raise RuntimeError("scheduler exploded")
        eng = make_engine(self.tmp.name, lambda t, v: [],
                          notify_fn=lambda sev, kind, msg: notes.append(kind),
                          on_restored=boom)
        with eng._action_lock:
            eng._run_action({"type": "snapshot", "name": "pre", "targets": ["Z1"]})
            text, ok = eng._run_action({"type": "restore", "name": "pre"})
        self.assertTrue(ok, "a hook failure must not mark the (successful) restore failed")
        self.assertIn("restore_followup", notes)


class ResumeReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rotation_resumes_and_reconciles_after_restart(self):
        import json
        rot = {"g": {"targets": ["Z1", "Z2", "Z3"], "run_count": 1, "interval": 5,
                     "on_values": {"mode": "Heat"}, "off_values": {"mode": "Off"},
                     "index": 2, "current_on": ["Z2"], "job_id": "rotation:g"}}
        with open(os.path.join(self.tmp.name, "rots.json"), "w") as fh:
            json.dump(rot, fh)
        calls = []
        eng = make_engine(self.tmp.name, lambda t, v: calls.append((t, v.get("mode"))) or [])
        eng.start()
        try:
            # Reconciles to the last-applied window WITHOUT advancing: Z2 on, others off.
            self.assertEqual([t for t, m in calls if m == "Heat"], ["Z2"])
            self.assertEqual(sorted(t for t, m in calls if m == "Off"), ["Z1", "Z3"])
            self.assertEqual(eng._rotations["g"]["index"], 2, "resume must not advance the window")
        finally:
            eng.stop()


if __name__ == "__main__":
    unittest.main()
