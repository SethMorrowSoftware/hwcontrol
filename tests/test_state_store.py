"""Regression tests for live equipment state (operationStatus) ingestion:
normalization, change events, and the commanded-vs-actual mismatch alert."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_store import StateStore


def raw(mode="Off", equip=None, name="Zone", did="D1", **extra):
    d = {"deviceID": did, "name": name, "isAlive": True,
         "changeableValues": {"mode": mode, "heatSetpoint": 66, "coolSetpoint": 78,
                              "thermostatSetpointStatus": "PermanentHold"}}
    if equip is not None:
        d["operationStatus"] = {"mode": equip, "fanRequest": extra.pop("fanRequest", False),
                                "circulationFanRequest": False}
    d.update(extra)
    return d


class Normalization(unittest.TestCase):
    def test_equipment_fields_extracted(self):
        s = StateStore()
        s.ingest([raw(mode="Heat", equip="Heat", fanRequest=True)], 1)
        d = s.get("D1")
        self.assertEqual(d["equipmentStatus"], "Heat")
        self.assertTrue(d["fanRequest"])
        self.assertFalse(d["circulationFanRequest"])

    def test_absent_operation_status_is_none_not_crash(self):
        s = StateStore()
        s.ingest([raw(mode="Heat")], 1)
        d = s.get("D1")
        self.assertIsNone(d["equipmentStatus"])
        self.assertIsNone(d["fanRequest"])


class EquipmentChangeEvents(unittest.TestCase):
    def test_equipment_transitions_emit_change_events(self):
        s = StateStore()
        s.ingest([raw(mode="Heat", equip="EquipmentOff")], 1)
        events = s.ingest([raw(mode="Heat", equip="Heat")], 1)
        eq = [e for e in events if e.get("field") == "equipmentStatus"]
        self.assertEqual(len(eq), 1)
        self.assertEqual((eq[0]["old"], eq[0]["new"]), ("EquipmentOff", "Heat"))
        # No change -> no event.
        events = s.ingest([raw(mode="Heat", equip="Heat")], 1)
        self.assertFalse([e for e in events if e.get("field") == "equipmentStatus"])


class CelsiusTempAlerts(unittest.TestCase):
    """Out-of-range thresholds are configured in Fahrenheit; a device reporting
    Celsius must have them converted, not compared raw (a normal 21°C room would
    otherwise read as 'below 55°' and alarm forever)."""

    def temp_alerts(self, s):
        return [a for a in s.alerts(limit=50) if a["kind"].startswith("temp_")]

    def test_normal_celsius_room_does_not_false_alarm(self):
        s = StateStore()
        s.ingest([raw(mode="Heat", units="Celsius", indoorTemperature=21)], 1)
        self.assertEqual(self.temp_alerts(s), [])

    def test_celsius_excursion_still_alerts(self):
        s = StateStore()
        s.ingest([raw(mode="Heat", units="Celsius", indoorTemperature=8)], 1)  # ~46°F
        kinds = [a["kind"] for a in self.temp_alerts(s)]
        self.assertIn("temp_low", kinds)

    def test_fahrenheit_thresholds_unchanged(self):
        s = StateStore()
        s.ingest([raw(mode="Heat", units="Fahrenheit", indoorTemperature=50)], 1)
        kinds = [a["kind"] for a in self.temp_alerts(s)]
        self.assertIn("temp_low", kinds)


class MismatchAlert(unittest.TestCase):
    """'Set to Off but actively heating' must alert on the SECOND consecutive
    poll (one poll of grace for post-mode-change run-out), exactly once per
    episode, and re-arm when it clears."""

    def mismatch_alerts(self, s):
        return [a for a in s.alerts(limit=50) if a["kind"] == "equipment_mismatch"]

    def test_debounce_alert_once_and_rearm(self):
        s = StateStore()
        s.ingest([raw(mode="Off", equip="Heat")], 1)         # poll 1: grace
        self.assertEqual(len(self.mismatch_alerts(s)), 0)
        s.ingest([raw(mode="Off", equip="Heat")], 1)         # poll 2: alert
        self.assertEqual(len(self.mismatch_alerts(s)), 1)
        self.assertIn("actively heating", self.mismatch_alerts(s)[0]["message"])
        s.ingest([raw(mode="Off", equip="Heat")], 1)         # poll 3: no repeat
        self.assertEqual(len(self.mismatch_alerts(s)), 1)
        s.ingest([raw(mode="Off", equip="EquipmentOff")], 1) # clears -> re-arms
        s.ingest([raw(mode="Off", equip="Heat")], 1)
        s.ingest([raw(mode="Off", equip="Heat")], 1)
        self.assertEqual(len(self.mismatch_alerts(s)), 2)

    def test_cross_mode_mismatch(self):
        s = StateStore()
        s.ingest([raw(mode="Heat", equip="Cool")], 1)
        s.ingest([raw(mode="Heat", equip="Cool")], 1)
        self.assertEqual(len(self.mismatch_alerts(s)), 1)
        self.assertIn("actively cooling", self.mismatch_alerts(s)[0]["message"])

    def test_legitimate_states_never_alert(self):
        s = StateStore()
        for _ in range(3):
            s.ingest([raw(mode="Heat", equip="Heat", did="A", name="A")], 1)          # heating as told
            s.ingest([raw(mode="Auto", equip="Cool", did="B", name="B")], 1)          # auto may do either
            s.ingest([raw(mode="Off", equip="EquipmentOff", did="C", name="C",
                          fanRequest=True)], 1)                                       # fan-only in Off
            s.ingest([raw(mode="Heat", equip="EmergencyHeat", did="E", name="E")], 1) # aux heat in Heat
            s.ingest([raw(mode="Heat", did="F", name="F")], 1)                        # no equipment data
        self.assertEqual(len(self.mismatch_alerts(s)), 0)


if __name__ == "__main__":
    unittest.main()
