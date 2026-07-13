"""Regression tests for app-level wiring: the schedule/rotation guard, control
field whitelisting, and store helpers used by the poller's reap guard."""
import os
import sys
import tempfile
import threading
import time
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

from automation import AutomationEngine  # noqa: E402
from scheduler import FacilityScheduler  # noqa: E402
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


class PostRestoreProgramResume(unittest.TestCase):
    """on_zones_restored must re-assert every program's active period (the
    user-visible symptom otherwise: after utility returns, zones sit at
    pre-outage setpoints until the next period boundary)."""

    def setUp(self):
        self._orig = (app_mod.scheduler, app_mod.notify)
        self.notes = []
        app_mod.notify = lambda sev, kind, msg: self.notes.append((sev, kind))

    def tearDown(self):
        app_mod.scheduler, app_mod.notify = self._orig

    def test_reasserts_active_periods_after_restore(self):
        calls = []
        class FakeScheduler:
            def apply_all_active_now(self, all_device_ids=None):
                calls.append("asserted")
        app_mod.scheduler = FakeScheduler()
        app_mod.on_zones_restored(["Z1", "Z2"])
        self.assertEqual(calls, ["asserted"])
        self.assertEqual(self.notes, [], "success must not raise alerts")

    def test_scheduler_failure_is_alerted_not_raised(self):
        class FailingScheduler:
            def apply_all_active_now(self, all_device_ids=None):
                raise RuntimeError("boom")
        app_mod.scheduler = FailingScheduler()
        app_mod.on_zones_restored(["Z1"])   # must not raise
        self.assertEqual(self.notes, [("critical", "schedule")])

    def test_noop_before_scheduler_ready(self):
        app_mod.scheduler = None
        app_mod.on_zones_restored(["Z1"])   # must not raise


class EndToEndUtilityRestore(unittest.TestCase):
    """The reported field scenario, end to end: generator ON (snapshot, shed,
    rotate) then generator OFF (stop rotation, restore) must leave the daily
    program back in control - its active period applied AFTER the restore."""

    def test_program_resumes_after_off_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            seq = []   # ordered record of every write: ("zone"|"program", target, action)

            sched = FacilityScheduler(
                apply_fn=lambda t, a: seq.append(("program", t, dict(a))),
                store_path=os.path.join(tmp, "sched.json"))
            # One period at 00:00 every day -> always the active period.
            sched.add_rule({"id": "prog", "days": [], "targets": ["Z1"],
                            "periods": [{"time": "00:00",
                                         "action": {"mode": "Heat", "heatSetpoint": 68}}]})

            eng = AutomationEngine(
                apply_fn=lambda t, v: seq.append(("zone", t, dict(v))) or [],
                resolve_fn=lambda: ["Z1", "Z2"],
                snapshot_read_fn=lambda d: {"mode": "Heat", "heatSetpoint": 62,
                                            "thermostatSetpointStatus": "PermanentHold"},
                notify_fn=lambda *a: None,
                rules_path=os.path.join(tmp, "rules.json"),
                snapshots_path=os.path.join(tmp, "snaps.json"),
                trigger_state_path=os.path.join(tmp, "trig.json"),
                rotations_path=os.path.join(tmp, "rots.json"),
                on_restored=lambda ids: sched.apply_all_active_now())
            trigger = lambda v: {"mode": "all", "retrigger": "on_change",
                                 "conditions": [{"topic": "gen", "type": "equals", "value": v}]}
            eng.add_rule({"id": "on", "name": "shed", "enabled": True, "trigger": trigger("on"),
                          "actions": [
                              {"type": "snapshot", "name": "pre", "targets": ["Z1", "Z2"]},
                              {"type": "set", "targets": ["Z2"], "values": {"mode": "Off"}},
                              {"type": "rotate", "rotation_id": "g", "targets": ["Z1"],
                               "run_count": 1, "interval_minutes": 5,
                               "on_values": {"mode": "Heat", "heatSetpoint": 66},
                               "off_values": {"mode": "Off"}}]})
            eng.add_rule({"id": "off", "name": "restore", "enabled": True, "trigger": trigger("off"),
                          "actions": [{"type": "stop_rotation", "rotation_id": "g"},
                                      {"type": "restore", "name": "pre"}]})

            eng.handle_message("gen", "on")
            seq.clear()
            eng.handle_message("gen", "off")

            program_writes = [s for s in seq if s[0] == "program"]
            self.assertEqual(len(program_writes), 1,
                             "the daily program must be re-asserted after the OFF message")
            self.assertEqual(program_writes[0][2].get("heatSetpoint"), 68)
            self.assertEqual(seq[-1][0], "program",
                             "the program's active period must be applied AFTER the restore, "
                             "so the schedule (not stale pre-outage values) has the last word")
            zone_targets = [s[1] for s in seq if s[0] == "zone"]
            self.assertEqual(sorted(zone_targets), ["Z1", "Z2"],
                             "restore must still put every snapshotted zone back first")


class ProgramChangesFollowSchedule(unittest.TestCase):
    """Saving/enabling a program must never drive zones as a side effect - the
    zones change at the scheduled period times. Immediate application is an
    explicit opt-in (apply_now), which is stripped before the rule is stored."""

    class FakeSched:
        def __init__(self):
            self.saved = None
            self.asserted = threading.Event()

        def add_rule(self, rule):
            self.saved = dict(rule)
            return dict(rule, id="r1", enabled=rule.get("enabled", True))

        def update_rule(self, rid, rule):
            self.saved = dict(rule)
            return dict(rule, id=rid, enabled=rule.get("enabled", True))

        def set_enabled(self, rid, en):
            return True

        def apply_active_now(self, rid):
            self.asserted.set()

    RULE = {"name": "p", "targets": "all",
            "periods": [{"time": "06:00", "action": {"mode": "Heat"}}]}

    def setUp(self):
        self._orig = app_mod.scheduler
        self.sched = self.FakeSched()
        app_mod.scheduler = self.sched

    def tearDown(self):
        app_mod.scheduler = self._orig

    def test_create_does_not_apply_by_default(self):
        app_mod.api_add_schedule(dict(self.RULE))
        self.assertFalse(self.sched.asserted.wait(0.3),
                         "saving a program must not write to zones by default")

    def test_update_does_not_apply_by_default(self):
        app_mod.api_update_schedule("r1", dict(self.RULE))
        self.assertFalse(self.sched.asserted.wait(0.3))

    def test_enable_does_not_apply(self):
        app_mod.api_toggle_schedule("r1", True)
        self.assertFalse(self.sched.asserted.wait(0.3))

    def test_apply_now_opt_in_applies_and_is_stripped(self):
        app_mod.api_add_schedule(dict(self.RULE, apply_now=True))
        self.assertTrue(self.sched.asserted.wait(2),
                        "apply_now must apply the active period immediately")
        self.assertNotIn("apply_now", self.sched.saved,
                         "apply_now must not persist into the stored rule")

    def test_apply_now_on_update_applies(self):
        self.sched.asserted.clear()
        app_mod.api_update_schedule("r1", dict(self.RULE, apply_now=True))
        self.assertTrue(self.sched.asserted.wait(2))


class BulkSetEndpoint(unittest.TestCase):
    """Bulk control: one values object applied to many zones at once, skipping
    zones under an active generator rotation (same guard schedule periods use,
    so a select-all can't re-energize shed zones mid-outage)."""

    def setUp(self):
        self._orig = (app_mod.client, app_mod.engine, app_mod.apply_action,
                      app_mod.notify, app_mod.store)

        class FakeClient:
            is_authorized = True
        app_mod.client = FakeClient()
        self.calls = []
        app_mod.apply_action = (lambda t, v, refresh=True:
                                self.calls.append((list(t), dict(v), refresh)) or [])
        app_mod.notify = lambda *a: None

    def tearDown(self):
        (app_mod.client, app_mod.engine, app_mod.apply_action,
         app_mod.notify, app_mod.store) = self._orig

    def test_applies_to_list_deduped_without_per_zone_refresh(self):
        app_mod.engine = FakeEngine(set())
        r = app_mod.api_bulk_set({"targets": ["Z1", "Z2", "Z1"],
                                  "values": {"mode": "Cool", "coolSetpoint": 74}})
        ids, values, refresh = self.calls[0]
        self.assertEqual(ids, ["Z1", "Z2"])
        self.assertFalse(refresh, "bulk must not spend a targeted GET per zone")
        self.assertEqual((r["ok"], r["applied"]), (True, 2))

    def test_rotated_zones_skipped(self):
        app_mod.engine = FakeEngine({"Z1"})
        r = app_mod.api_bulk_set({"targets": ["Z1", "Z2"], "values": {"mode": "Heat"}})
        self.assertEqual(self.calls[0][0], ["Z2"])
        self.assertEqual(r["skipped_rotating"], ["Z1"])

    def test_all_targets_resolve_from_store(self):
        app_mod.engine = FakeEngine(set())
        store = StateStore()
        store.ingest([{"deviceID": "A", "name": "A", "isAlive": True}], 1)
        app_mod.store = store
        app_mod.api_bulk_set({"targets": "all", "values": {"mode": "Off"}})
        self.assertEqual(self.calls[0][0], ["A"])

    def test_failed_zones_reported(self):
        from fastapi import HTTPException
        app_mod.engine = FakeEngine(set())
        app_mod.apply_action = lambda t, v, refresh=True: ["Z2"]
        r = app_mod.api_bulk_set({"targets": ["Z1", "Z2"], "values": {"mode": "Off"}})
        self.assertEqual((r["ok"], r["applied"], r["failed"]), (False, 1, ["Z2"]))

    def test_validation(self):
        from fastapi import HTTPException
        app_mod.engine = FakeEngine(set())
        for bad in ({"targets": [], "values": {"mode": "Off"}},
                    {"targets": ["Z1"], "values": {}},
                    {"targets": ["Z1"], "values": {"evilField": 1}},
                    {"targets": "some-string", "values": {"mode": "Off"}}):
            with self.assertRaises(HTTPException):
                app_mod.api_bulk_set(bad)
        self.assertEqual(self.calls, [], "invalid requests must not write anything")


class PollPartialHealth(unittest.TestCase):
    """A location that transiently reports no thermostats must mark the poll
    degraded (those zones show stale data) and must not reap those zones."""

    def setUp(self):
        self._orig = (app_mod.client, app_mod.store)

    def tearDown(self):
        app_mod.client, app_mod.store = self._orig

    def test_partial_poll_marks_error_and_skips_reap(self):
        store = StateStore()
        store.ingest([{"deviceID": "B1", "name": "B1", "isAlive": True,
                       "changeableValues": {"mode": "Off"}}], 2)
        app_mod.store = store

        thermo_a = {"deviceID": "A1", "name": "A1", "isAlive": True,
                    "changeableValues": {"mode": "Heat"}}

        class FakeClient:
            is_authorized = True
            def get_locations(self):
                return [{"locationID": 1, "devices": [thermo_a]},
                        {"locationID": 2, "devices": []}]
        app_mod.client = FakeClient()

        app_mod.poll_once()
        _, err = store.poll_status()
        self.assertIsNotNone(err, "a partial poll must not read as a healthy green poll")
        self.assertIn("no thermostats", err)
        self.assertIn("B1", store.all_device_ids(),
                      "reap must be skipped for the location that went transiently empty")


class ScheduleEnforcement(unittest.TestCase):
    """When enforcement is on, a program-covered zone that drifted from what its
    schedule says is corrected; a matching zone, a rotated zone, and the
    disabled state are all left alone."""

    def setUp(self):
        from scheduler import FacilityScheduler
        self._orig = (app_mod.scheduler, app_mod.engine, app_mod.store, app_mod.client,
                      app_mod.apply_schedule_action, app_mod.notify, app_mod._load_schedule_enforce)
        self._tmp = tempfile.TemporaryDirectory()

        class FakeClient:
            is_authorized = True
        app_mod.client = FakeClient()
        app_mod.engine = FakeEngine(set())
        app_mod.notify = lambda *a: None
        app_mod._load_schedule_enforce = lambda: True
        self.applied = []
        app_mod.apply_schedule_action = (lambda targets, action:
                                         self.applied.append((list(targets), dict(action))) or [])

        store = StateStore()
        store.ingest([{"deviceID": "Z1", "name": "Zone 1", "isAlive": True,
                       "changeableValues": {"mode": "Heat", "heatSetpoint": 70, "coolSetpoint": 76,
                                            "thermostatSetpointStatus": "PermanentHold"}}], 1)
        app_mod.store = store

        sched = FacilityScheduler(apply_fn=lambda t, a: None,
                                  store_path=os.path.join(self._tmp.name, "s.json"))
        sched.add_rule({"id": "p", "targets": ["Z1"],
                        "periods": [{"time": "00:00",
                                     "action": {"mode": "Heat", "heatSetpoint": 68,
                                                "thermostatSetpointStatus": "PermanentHold"}}]})
        app_mod.scheduler = sched

    def tearDown(self):
        (app_mod.scheduler, app_mod.engine, app_mod.store, app_mod.client,
         app_mod.apply_schedule_action, app_mod.notify, app_mod._load_schedule_enforce) = self._orig
        self._tmp.cleanup()

    def test_corrects_drifted_zone(self):
        app_mod._enforce_schedules()   # Z1 is Heat 70, program wants Heat 68 -> drift
        self.assertEqual(len(self.applied), 1)
        targets, action = self.applied[0]
        self.assertEqual(targets, ["Z1"])
        self.assertEqual(action.get("heatSetpoint"), 68)

    def test_no_correction_when_already_matching(self):
        app_mod.store.ingest([{"deviceID": "Z1", "name": "Zone 1", "isAlive": True,
                               "changeableValues": {"mode": "Heat", "heatSetpoint": 68, "coolSetpoint": 76,
                                                    "thermostatSetpointStatus": "PermanentHold"}}], 1)
        app_mod._enforce_schedules()
        self.assertEqual(self.applied, [], "a zone already on its program costs no writes")

    def test_noop_when_disabled(self):
        app_mod._load_schedule_enforce = lambda: False
        app_mod._enforce_schedules()
        self.assertEqual(self.applied, [])

    def test_skips_rotated_zone(self):
        app_mod.engine = FakeEngine({"Z1"})
        app_mod._enforce_schedules()
        self.assertEqual(self.applied, [], "a zone under an active rotation is not corrected")

    def test_skips_offline_zone(self):
        app_mod.store.ingest([{"deviceID": "Z1", "name": "Zone 1", "isAlive": False,
                               "changeableValues": {"mode": "Heat", "heatSetpoint": 70,
                                                    "thermostatSetpointStatus": "PermanentHold"}}], 1)
        app_mod._enforce_schedules()
        self.assertEqual(self.applied, [], "an offline zone can't be corrected")


class EnforcementArbitration(unittest.TestCase):
    """The reported bug: a weekday + weekend program on the same zone must not
    fight. Enforcement applies only the program in effect today; a genuine
    same-time disagreement is left alone (not guessed)."""

    def _setup(self, programs, now):
        import datetime
        from scheduler import FacilityScheduler
        self._orig = (app_mod.scheduler, app_mod.engine, app_mod.store, app_mod.client,
                      app_mod.apply_schedule_action, app_mod.notify, app_mod._load_schedule_enforce)
        self._tmp = tempfile.TemporaryDirectory()

        class FakeClient:
            is_authorized = True
        app_mod.client = FakeClient()
        app_mod.engine = FakeEngine(set())
        self.notes = []
        app_mod.notify = lambda sev, kind, msg: self.notes.append((kind, msg))
        app_mod._load_schedule_enforce = lambda: True
        self.applied = []
        app_mod.apply_schedule_action = (lambda targets, action:
                                         self.applied.append((sorted(targets), dict(action))) or [])

        store = StateStore()
        store.ingest([{"deviceID": "Z1", "name": "Zone 1", "isAlive": True,
                       "changeableValues": {"mode": "Cool", "heatSetpoint": 50, "coolSetpoint": 90,
                                            "thermostatSetpointStatus": "NoHold"}}], 1)
        app_mod.store = store

        sched = FacilityScheduler(apply_fn=lambda t, a: None,
                                  store_path=os.path.join(self._tmp.name, "s.json"),
                                  timezone="America/New_York")
        sched._now = lambda: now
        for p in programs:
            sched.add_rule(p)
        app_mod.scheduler = sched

    def tearDown(self):
        (app_mod.scheduler, app_mod.engine, app_mod.store, app_mod.client,
         app_mod.apply_schedule_action, app_mod.notify, app_mod._load_schedule_enforce) = self._orig
        self._tmp.cleanup()

    def test_weekday_wins_over_weekend_on_a_weekday(self):
        import datetime
        monday_3pm = datetime.datetime(2026, 7, 13, 15, 0)   # a Monday
        self._setup([
            {"id": "weekday", "days": ["mon", "tue", "wed", "thu", "fri"], "targets": ["Z1"],
             "periods": [{"time": "06:00", "action": {"mode": "Heat", "heatSetpoint": 70}},
                         {"time": "22:00", "action": {"mode": "Off"}}]},
            {"id": "weekend", "days": ["sat", "sun"], "targets": ["Z1"],
             "periods": [{"time": "08:00", "action": {"mode": "Heat", "heatSetpoint": 64}},
                         {"time": "23:00", "action": {"mode": "Off"}}]},
        ], monday_3pm)
        app_mod._enforce_schedules()
        self.assertEqual(len(self.applied), 1, "only the active-day program is applied")
        targets, action = self.applied[0]
        self.assertEqual(targets, ["Z1"])
        self.assertEqual((action.get("mode"), action.get("heatSetpoint")), ("Heat", 70),
                         "the weekend 'Off' must NOT be applied on a weekday")
        self.assertFalse([k for k, _ in self.notes if k == "schedule_conflict"])

    def test_genuine_same_time_conflict_is_skipped_and_warned(self):
        import datetime
        app_mod._last_conflict_zones = frozenset()   # reset debounce
        monday_3pm = datetime.datetime(2026, 7, 13, 15, 0)
        self._setup([
            {"id": "a", "name": "A", "targets": ["Z1"],
             "periods": [{"time": "06:00", "action": {"mode": "Heat", "heatSetpoint": 70}}]},
            {"id": "b", "name": "B", "targets": ["Z1"],
             "periods": [{"time": "06:00", "action": {"mode": "Off"}}]},
        ], monday_3pm)
        app_mod._enforce_schedules()
        self.assertEqual(self.applied, [], "a genuine tie must not be guessed/applied")
        self.assertTrue([k for k, _ in self.notes if k == "schedule_conflict"],
                        "the conflict must be surfaced to the operator")


class ScheduleSnapshotReadout(unittest.TestCase):
    """_schedule_snapshot() reports, per zone, which program is in effect and
    whether the zone is following it - the data behind the dashboard readout.
    It must use the SAME arbitration as enforcement, so what the operator sees
    is exactly what enforcement would act on."""

    def setUp(self):
        import datetime
        from scheduler import FacilityScheduler
        self._orig = (app_mod.scheduler, app_mod.engine, app_mod.store,
                      app_mod._load_schedule_enforce)
        self._tmp = tempfile.TemporaryDirectory()
        app_mod.engine = FakeEngine({"Z6"})            # Z6 under an active rotation
        app_mod._load_schedule_enforce = lambda: True

        store = StateStore()
        store.ingest([
            {"deviceID": "Z1", "name": "Zone 1", "isAlive": True,
             "changeableValues": {"mode": "Heat", "heatSetpoint": 68, "coolSetpoint": 76,
                                  "thermostatSetpointStatus": "PermanentHold"}},   # matches -> on_target
            {"deviceID": "Z2", "name": "Zone 2", "isAlive": True,
             "changeableValues": {"mode": "Heat", "heatSetpoint": 70, "coolSetpoint": 76,
                                  "thermostatSetpointStatus": "PermanentHold"}},   # 70 != 68 -> drifted
            {"deviceID": "Z3", "name": "Zone 3", "isAlive": True,
             "changeableValues": {"mode": "Off"}},                                 # no program -> unscheduled
            {"deviceID": "Z4", "name": "Zone 4", "isAlive": True,
             "changeableValues": {"mode": "Cool"}},                                # two programs -> conflict
            {"deviceID": "Z5", "name": "Zone 5", "isAlive": False,
             "changeableValues": {"mode": "Heat", "heatSetpoint": 70,
                                  "thermostatSetpointStatus": "PermanentHold"}},   # offline
            {"deviceID": "Z6", "name": "Zone 6", "isAlive": True,
             "changeableValues": {"mode": "Off"}},                                 # rotating
        ], 1)
        app_mod.store = store

        sched = FacilityScheduler(apply_fn=lambda t, a: None,
                                  store_path=os.path.join(self._tmp.name, "s.json"),
                                  timezone="America/New_York")
        sched._now = lambda: datetime.datetime(2026, 7, 13, 15, 0)   # a Monday
        act = {"mode": "Heat", "heatSetpoint": 68, "thermostatSetpointStatus": "PermanentHold"}
        sched.add_rule({"id": "match",  "targets": ["Z1"], "periods": [{"time": "00:00", "action": act}]})
        sched.add_rule({"id": "drift",  "targets": ["Z2"], "periods": [{"time": "00:00", "action": act}]})
        sched.add_rule({"id": "away",   "targets": ["Z5"], "periods": [{"time": "00:00", "action": act}]})
        sched.add_rule({"id": "rot",    "targets": ["Z6"],
                        "periods": [{"time": "00:00", "action": {"mode": "Heat", "heatSetpoint": 66}}]})
        sched.add_rule({"id": "a", "name": "A", "targets": ["Z4"],
                        "periods": [{"time": "00:00", "action": {"mode": "Heat", "heatSetpoint": 70}}]})
        sched.add_rule({"id": "b", "name": "B", "targets": ["Z4"],
                        "periods": [{"time": "00:00", "action": {"mode": "Off"}}]})
        app_mod.scheduler = sched

    def tearDown(self):
        (app_mod.scheduler, app_mod.engine, app_mod.store,
         app_mod._load_schedule_enforce) = self._orig
        self._tmp.cleanup()

    def test_every_state_is_reported(self):
        snap = app_mod._schedule_snapshot()
        z = snap["zones"]
        self.assertEqual(z["Z1"]["state"], "on_target")
        self.assertEqual(z["Z1"]["program"], "match")
        self.assertEqual(z["Z2"]["state"], "drifted")
        self.assertIn("68", z["Z2"]["summary"])
        self.assertEqual(z["Z3"]["state"], "unscheduled")
        self.assertEqual(z["Z4"]["state"], "conflict")
        self.assertEqual(sorted(z["Z4"]["programs"]), ["A", "B"])
        self.assertEqual(z["Z5"]["state"], "offline")
        self.assertEqual(z["Z6"]["state"], "rotating")

    def test_counts_and_enforce_flag(self):
        snap = app_mod._schedule_snapshot()
        self.assertTrue(snap["enforce"])
        self.assertEqual(snap["counts"],
                         {"on_target": 1, "drifted": 1, "unscheduled": 1,
                          "conflict": 1, "rotating": 1, "offline": 1})
        self.assertEqual([c["zone"] for c in snap["conflicts"]], ["Z4"])
        self.assertIn("New_York", snap["timezone"])

    def test_snapshot_matches_what_enforcement_would_do(self):
        # The zones the snapshot calls "drifted" are exactly the online, non-rotating
        # zones enforcement would correct - never a divergent second opinion.
        snap = app_mod._schedule_snapshot()
        drifted = {zid for zid, e in snap["zones"].items() if e["state"] == "drifted"}
        desired, _ = app_mod.scheduler.resolve_desired(app_mod.store.all_device_ids())
        rot = app_mod.engine.active_rotation_targets()
        would_correct = set()
        for zid, (action, _p) in desired.items():
            zdev = app_mod.store.get(zid)
            if zid in rot or not zdev or not zdev.get("online"):
                continue
            if not app_mod._zone_matches_action(zdev, action):
                would_correct.add(zid)
        self.assertEqual(drifted, would_correct)


class GroupEndpoints(unittest.TestCase):
    """The /api/groups CRUD wrappers surface store errors as HTTP 400/404."""

    def setUp(self):
        import tempfile
        from groups import GroupStore
        self._orig = app_mod.groups
        self._tmp = tempfile.TemporaryDirectory()
        app_mod.groups = GroupStore(store_path=os.path.join(self._tmp.name, "groups.json"))

    def tearDown(self):
        app_mod.groups = self._orig
        self._tmp.cleanup()

    def test_add_list_delete(self):
        r = app_mod.api_add_group({"name": "Arcade", "members": ["TCC-1", "TCC-1", "TCC-2"]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["group"]["members"], ["TCC-1", "TCC-2"])
        gid = r["group"]["id"]
        self.assertEqual([g["name"] for g in app_mod.api_groups()["groups"]], ["Arcade"])
        self.assertTrue(app_mod.api_delete_group(gid)["ok"])

    def test_invalid_group_is_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            app_mod.api_add_group({"name": "", "members": []})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_group_is_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            app_mod.api_delete_group("nope")
        self.assertEqual(ctx.exception.status_code, 404)


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
