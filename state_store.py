"""
state_store.py
--------------
Holds the last-known snapshot of every thermostat and works out what changed
between polls. Change events feed the MQTT bridge and the dashboard alert feed.

Nothing here talks to the network. It's pure bookkeeping so it's easy to reason
about and easy to test.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Iterable, Optional


def _normalize(raw: dict) -> dict:
    """Pull the fields we care about out of a raw thermostat object into a flat,
    stable shape the dashboard and MQTT layer can rely on."""
    cv = raw.get("changeableValues", {}) or {}
    return {
        "deviceID": raw.get("deviceID"),
        "name": raw.get("userDefinedDeviceName") or raw.get("name") or raw.get("deviceID"),
        "online": bool(raw.get("isAlive", True)),
        "units": raw.get("units", "Fahrenheit"),
        "indoorTemperature": raw.get("indoorTemperature"),
        "indoorHumidity": raw.get("indoorHumidity") or raw.get("displayedIndoorHumidity"),
        "outdoorTemperature": raw.get("outdoorTemperature"),
        "mode": cv.get("mode"),
        "heatSetpoint": cv.get("heatSetpoint"),
        "coolSetpoint": cv.get("coolSetpoint"),
        "setpointStatus": cv.get("thermostatSetpointStatus"),
        "autoChangeoverActive": cv.get("autoChangeoverActive"),
        "nextPeriodTime": cv.get("nextPeriodTime"),
        "allowedModes": raw.get("allowedModes", []),
        "minHeatSetpoint": raw.get("minHeatSetpoint"),
        "maxHeatSetpoint": raw.get("maxHeatSetpoint"),
        "minCoolSetpoint": raw.get("minCoolSetpoint"),
        "maxCoolSetpoint": raw.get("maxCoolSetpoint"),
        "scheduleStatus": raw.get("scheduleStatus"),
        # Onboard-schedule type (e.g. "TimedNorthAmerica"); the /devices/schedule
        # endpoint requires it as the `type` query param. Shape varies by firmware,
        # so read it defensively.
        "scheduleType": _schedule_type(raw),
        "changeableValues": cv,  # kept so control calls can merge cleanly
    }


def _schedule_type(raw: dict) -> Optional[str]:
    """The schedule type to pass to the /devices/schedule endpoint.

    A device reports scheduleType.scheduleType as a short name ("Timed"), but the
    endpoint's `type` param wants the matching entry from availableScheduleTypes
    ("TimedNorthAmerica"). Map the short name onto the capability list; fall back
    to the short name if there's no list to match against."""
    st = raw.get("scheduleType")
    cur = st.get("scheduleType") if isinstance(st, dict) else st
    if not isinstance(cur, str) or not cur:
        return None
    caps = (raw.get("scheduleCapabilities") or {}).get("availableScheduleTypes") or []
    caps = [c for c in caps if isinstance(c, str)]
    low = cur.lower()
    for c in caps:                       # exact match wins
        if c.lower() == low:
            return c
    for c in caps:                       # else the capability that includes it
        if c.lower().startswith(low) or low in c.lower():
            return c
    return cur


# Fields whose changes are worth announcing as events.
_WATCHED = ("online", "mode", "heatSetpoint", "coolSetpoint", "setpointStatus", "indoorTemperature")


class StateStore:
    def __init__(self, maxlen_alerts: int = 200):
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}          # deviceID -> normalized state
        self._location_of: dict[str, Any] = {}       # deviceID -> locationId
        self._alerts: deque[dict] = deque(maxlen=maxlen_alerts)
        self._temp_zone: dict[str, str] = {}         # deviceID -> "ok" | "high" | "low"
        self.last_poll_ts: Optional[float] = None
        self.last_poll_error: Optional[str] = None
        # Alert thresholds (edit or drive from config). None disables that check.
        self.temp_low_alert: Optional[float] = 55.0
        self.temp_high_alert: Optional[float] = 85.0

    # ------------------------------------------------------------------ update

    def ingest(self, raw_devices: Iterable[dict], location_id: Any) -> list[dict]:
        """Merge a location's devices into the store. Returns a list of change
        events (each a dict) detected against the previous snapshot."""
        events: list[dict] = []
        with self._lock:
            for raw in raw_devices:
                new = _normalize(raw)
                did = new["deviceID"]
                if not did:
                    continue
                self._location_of[did] = location_id
                old = self._devices.get(did)
                self._devices[did] = new
                events.extend(self._diff(old, new))
                self._check_temp_alert(did, new)
        for ev in events:
            self._maybe_online_alert(ev)
        return events

    def _diff(self, old: Optional[dict], new: dict) -> list[dict]:
        if old is None:
            return [{
                "type": "discovered", "deviceID": new["deviceID"],
                "name": new["name"], "ts": time.time(),
            }]
        out = []
        for field in _WATCHED:
            if old.get(field) != new.get(field):
                out.append({
                    "type": "changed", "field": field, "deviceID": new["deviceID"],
                    "name": new["name"], "old": old.get(field),
                    "new": new.get(field), "ts": time.time(),
                })
        return out

    def _maybe_online_alert(self, ev: dict) -> None:
        """Turn an online/offline change event into a human-facing alert.
        Edge-triggered by _diff, so it fires exactly once per transition."""
        if ev.get("field") != "online":
            return
        if ev["new"] is False:
            alert = {"severity": "critical", "kind": "offline",
                     "message": f"{ev['name']} went offline"}
        else:
            alert = {"severity": "info", "kind": "online",
                     "message": f"{ev['name']} is back online"}
        alert.update({"deviceID": ev["deviceID"], "ts": ev["ts"]})
        with self._lock:
            self._alerts.appendleft(alert)

    def _check_temp_alert(self, device_id: str, new: dict) -> None:
        """Alert once when a zone crosses INTO an out-of-range band, and re-arm
        when it returns to normal. Called while holding self._lock.

        This is deliberately edge-triggered on the range band (ok/high/low): a
        zone that sits at 90 degrees raises one alert, not one on every poll."""
        t = new.get("indoorTemperature")
        if t is None:
            return
        zone = "ok"
        if self.temp_high_alert is not None and t >= self.temp_high_alert:
            zone = "high"
        elif self.temp_low_alert is not None and t <= self.temp_low_alert:
            zone = "low"

        prev = self._temp_zone.get(device_id, "ok")
        self._temp_zone[device_id] = zone
        if zone == prev or zone == "ok":
            return

        name = new.get("name") or device_id
        if zone == "high":
            msg = f"{name} is {t}\u00b0 (above {self.temp_high_alert}\u00b0)"
        else:
            msg = f"{name} is {t}\u00b0 (below {self.temp_low_alert}\u00b0)"
        self._alerts.appendleft({"severity": "warning", "kind": "temp_" + zone,
                                 "message": msg, "deviceID": device_id, "ts": time.time()})

    def add_alert(self, severity: str, kind: str, message: str, device_id: str = "") -> dict:
        alert = {"severity": severity, "kind": kind, "message": message,
                 "deviceID": device_id, "ts": time.time()}
        with self._lock:
            self._alerts.appendleft(alert)
        return alert

    def mark_poll(self, error: Optional[str] = None) -> None:
        self.last_poll_ts = time.time()
        self.last_poll_error = error

    # ------------------------------------------------------------------- reads

    def devices(self) -> list[dict]:
        with self._lock:
            # Return copies without the bulky changeableValues for the wire.
            out = []
            for d in self._devices.values():
                item = {k: v for k, v in d.items() if k != "changeableValues"}
                out.append(item)
            return sorted(out, key=lambda d: (not d["online"], d["name"] or ""))

    def get(self, device_id: str) -> Optional[dict]:
        with self._lock:
            d = self._devices.get(device_id)
            return dict(d) if d else None

    def location_of(self, device_id: str) -> Optional[Any]:
        with self._lock:
            return self._location_of.get(device_id)

    def all_device_ids(self) -> list[str]:
        with self._lock:
            return list(self._devices.keys())

    def alerts(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._alerts)[:limit]
