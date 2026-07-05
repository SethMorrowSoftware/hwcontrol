"""
state_store.py
--------------
Holds the last-known snapshot of every thermostat and works out what changed
between polls. Change events feed the MQTT bridge and the dashboard alert feed.

Nothing here talks to the network. It's pure bookkeeping so it's easy to reason
about and easy to test.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Iterable, Optional

log = logging.getLogger("honeywell.store")


def _num(v: Any) -> Optional[float]:
    """Coerce a value to a float, or None if it isn't a number. Resideo has been
    seen to return numeric fields as strings; a raw comparison then raises and,
    before this, could blow up a whole poll. Booleans are not numbers here."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize(raw: dict) -> dict:
    """Pull the fields we care about out of a raw thermostat object into a flat,
    stable shape the dashboard and MQTT layer can rely on."""
    cv = raw.get("changeableValues", {}) or {}
    st_api, st_short, st_sub = _schedule_type_info(raw)
    humidity = _num(raw.get("indoorHumidity"))
    if humidity is None:
        humidity = _num(raw.get("displayedIndoorHumidity"))
    # Fan lives under settings.fan on LCC devices; absent elsewhere. Guard every
    # level so a device without fan data just reports None/[] and the dashboard
    # hides its fan control.
    fan = (raw.get("settings") or {}).get("fan") or {}
    fan_cv = fan.get("changeableValues") or {}
    return {
        "deviceID": raw.get("deviceID"),
        "name": raw.get("userDefinedDeviceName") or raw.get("name") or raw.get("deviceID"),
        # None here means "device didn't report reachability this poll"; ingest()
        # resolves it (carry forward the prior value) rather than assuming online.
        "online": raw.get("isAlive"),
        "units": raw.get("units", "Fahrenheit"),
        "indoorTemperature": _num(raw.get("indoorTemperature")),
        "indoorHumidity": humidity,
        "outdoorTemperature": _num(raw.get("outdoorTemperature")),
        "mode": cv.get("mode"),
        "heatSetpoint": cv.get("heatSetpoint"),
        "coolSetpoint": cv.get("coolSetpoint"),
        "setpointStatus": cv.get("thermostatSetpointStatus"),
        "autoChangeoverActive": cv.get("autoChangeoverActive"),
        "nextPeriodTime": cv.get("nextPeriodTime"),
        "fanMode": fan_cv.get("mode"),
        "fanRunning": fan.get("fanRunning"),
        "allowedFanModes": fan.get("allowedModes", []),
        "allowedModes": raw.get("allowedModes", []),
        "minHeatSetpoint": raw.get("minHeatSetpoint"),
        "maxHeatSetpoint": raw.get("maxHeatSetpoint"),
        "minCoolSetpoint": raw.get("minCoolSetpoint"),
        "maxCoolSetpoint": raw.get("maxCoolSetpoint"),
        "scheduleStatus": raw.get("scheduleStatus"),
        # Onboard-schedule identity, used to disable/restore a device's own schedule:
        #   scheduleType      - API name for the `type` query param ("TimedNorthAmerica")
        #   scheduleTypeShort - short name used in the request body ("Timed")
        #   scheduleSubType   - e.g. "NA"
        "scheduleType": st_api,
        "scheduleTypeShort": st_short,
        "scheduleSubType": st_sub,
        "changeableValues": cv,  # kept so control calls can merge cleanly
    }


def _schedule_type_info(raw: dict):
    """Return (api_name, short_name, sub_type) for a device's onboard schedule.

    Devices report scheduleType.scheduleType as a short name ("Timed"), but the
    /devices/schedule endpoint's `type` query param wants the matching entry from
    availableScheduleTypes ("TimedNorthAmerica"). Returns (None, None, None) when
    the device doesn't report a usable schedule type. Never raises - schedule
    metadata is optional and must not break device ingestion."""
    try:
        st = raw.get("scheduleType")
        if isinstance(st, dict):
            short, sub = st.get("scheduleType"), st.get("scheduleSubType")
        else:
            short, sub = st, None
        if not isinstance(short, str) or not short:
            return (None, None, None)
        caps_obj = raw.get("scheduleCapabilities")
        avail = caps_obj.get("availableScheduleTypes") if isinstance(caps_obj, dict) else None
        caps = [c for c in avail if isinstance(c, str)] if isinstance(avail, list) else []
        low = short.lower()
        api = next((c for c in caps if c.lower() == low), None)
        if api is None:
            # Prefer the most specific (longest) partial match so ambiguous lists
            # don't pick an arbitrary first entry.
            partials = sorted((c for c in caps if c.lower().startswith(low) or low in c.lower()),
                              key=len, reverse=True)
            api = partials[0] if partials else None
        return (api or short, short, sub if isinstance(sub, str) else None)
    except Exception:  # optional metadata; never let it break ingestion
        return (None, None, None)


# Fields whose changes are worth announcing as events. indoorTemperature is
# deliberately excluded: it drifts every poll and would flood the event stream
# (the temperature *band* is covered separately by _check_temp_alert).
_WATCHED = ("online", "mode", "heatSetpoint", "coolSetpoint", "setpointStatus")


class StateStore:
    def __init__(self, maxlen_alerts: int = 200):
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}          # deviceID -> normalized state
        self._location_of: dict[str, Any] = {}       # deviceID -> locationId
        self._alerts: deque[dict] = deque(maxlen=maxlen_alerts)
        self._temp_zone: dict[str, str] = {}         # deviceID -> "ok" | "high" | "low"
        self._last_poll_ts: Optional[float] = None
        self._last_poll_error: Optional[str] = None
        # Alert thresholds (edit or drive from config). None disables that check.
        self.temp_low_alert: Optional[float] = 55.0
        self.temp_high_alert: Optional[float] = 85.0

    # ------------------------------------------------------------------ update

    def ingest(self, raw_devices: Iterable[dict], location_id: Any) -> list[dict]:
        """Merge a location's devices into the store. Returns a list of change
        events (each a dict) detected against the previous snapshot."""
        events: list[dict] = []
        pending_alerts: list[dict] = []
        with self._lock:
            for raw in raw_devices:
                try:
                    new = _normalize(raw)
                    did = new["deviceID"]
                    if not did:
                        log.warning("Skipping a device with no deviceID: name=%r", new.get("name"))
                        continue
                    # Resolve reachability: a poll that omits isAlive shouldn't be
                    # read as "came back online" - carry the prior value forward.
                    old = self._devices.get(did)
                    if new["online"] is None:
                        new["online"] = old["online"] if old else True
                    new["online"] = bool(new["online"])

                    self._location_of[did] = location_id
                    self._devices[did] = new
                    device_events = self._diff(old, new)
                    events.extend(device_events)
                    pending_alerts.extend(self._alerts_for(old, new, device_events))
                    self._check_temp_alert(did, new, pending_alerts)
                except Exception as exc:
                    # A single malformed device must never blank the whole poll.
                    log.warning("Skipping a device that failed to ingest: %s", exc)
                    continue
            # Append alerts under the same lock so alert order matches state order.
            for alert in pending_alerts:
                self._alerts.appendleft(alert)
        return events

    def reap(self, seen_ids: Iterable[str]) -> list[dict]:
        """Remove devices that were NOT seen in a *complete* poll of the whole
        account (a decommissioned/relocated thermostat). Only call this after a
        poll that reached every location, never after a single-device refresh, or
        it would wrongly evict everything else. Emits a removal event + alert per
        dropped device."""
        seen = set(seen_ids)
        events: list[dict] = []
        with self._lock:
            gone = [did for did in self._devices if did not in seen]
            for did in gone:
                dev = self._devices.pop(did, None)
                self._location_of.pop(did, None)
                self._temp_zone.pop(did, None)
                name = (dev or {}).get("name") or did
                events.append({"type": "removed", "deviceID": did, "name": name, "ts": time.time()})
                self._alerts.appendleft({
                    "severity": "warning", "kind": "removed",
                    "message": f"{name} is no longer reported by the account (removed)",
                    "deviceID": did, "ts": time.time(),
                })
                log.info("Reaped device %s (no longer in account).", did)
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

    def _alerts_for(self, old: Optional[dict], new: dict, events: list[dict]) -> list[dict]:
        """Build online/offline alerts for this device's transitions. Returns a
        list (appended under the caller's lock so alert order tracks state order).
        Edge-triggered: a first-seen offline device alerts once, and each later
        online<->offline flip alerts exactly once."""
        out = []
        if old is None:
            # First time we've seen this device: alert if it's already offline so a
            # zone that's dead at startup isn't silently unmonitored.
            if new["online"] is False:
                out.append({"severity": "critical", "kind": "offline",
                            "message": f"{new['name']} is offline",
                            "deviceID": new["deviceID"], "ts": time.time()})
            return out
        for ev in events:
            if ev.get("field") != "online":
                continue
            if ev["new"] is False:
                out.append({"severity": "critical", "kind": "offline",
                            "message": f"{new['name']} went offline",
                            "deviceID": new["deviceID"], "ts": ev["ts"]})
            else:
                out.append({"severity": "info", "kind": "online",
                            "message": f"{new['name']} is back online",
                            "deviceID": new["deviceID"], "ts": ev["ts"]})
        return out

    def _check_temp_alert(self, device_id: str, new: dict, out: list[dict]) -> None:
        """Append an alert when a zone crosses INTO an out-of-range band, and
        re-arm when it returns to normal. Called while holding self._lock.

        Edge-triggered on the range band (ok/high/low): a zone that sits at 90
        degrees raises one alert, not one on every poll."""
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
            msg = f"{name} is {t}° (above {self.temp_high_alert}°)"
        else:
            msg = f"{name} is {t}° (below {self.temp_low_alert}°)"
        out.append({"severity": "warning", "kind": "temp_" + zone,
                    "message": msg, "deviceID": device_id, "ts": time.time()})

    def add_alert(self, severity: str, kind: str, message: str, device_id: str = "") -> dict:
        alert = {"severity": severity, "kind": kind, "message": message,
                 "deviceID": device_id, "ts": time.time()}
        with self._lock:
            self._alerts.appendleft(alert)
        return alert

    def mark_poll(self, error: Optional[str] = None) -> None:
        with self._lock:
            self._last_poll_ts = time.time()
            self._last_poll_error = error

    def poll_status(self) -> tuple[Optional[float], Optional[str]]:
        with self._lock:
            return self._last_poll_ts, self._last_poll_error

    # Backwards-compatible read-only properties (guarded).
    @property
    def last_poll_ts(self) -> Optional[float]:
        with self._lock:
            return self._last_poll_ts

    @property
    def last_poll_error(self) -> Optional[str]:
        with self._lock:
            return self._last_poll_error

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
            if not d:
                return None
            # Deep-ish copy so callers (snapshots, control merges) can't mutate the
            # stored changeableValues behind the lock's back.
            copy = dict(d)
            if isinstance(copy.get("changeableValues"), dict):
                copy["changeableValues"] = dict(copy["changeableValues"])
            return copy

    def apply_local_override(self, device_id: str, overrides: dict) -> None:
        """Merge a just-applied control write into the cached device state so a
        serialized follow-up write (and the dashboard) sees it immediately, without
        spending an API GET. Keeps the flat fields and changeableValues coherent."""
        if not overrides:
            return
        with self._lock:
            d = self._devices.get(device_id)
            if not d:
                return
            cv = dict(d.get("changeableValues") or {})
            cv.update(overrides)
            d["changeableValues"] = cv
            # Mirror the flat fields _normalize derives from changeableValues.
            if "mode" in overrides:
                d["mode"] = cv.get("mode")
            if "heatSetpoint" in overrides:
                d["heatSetpoint"] = cv.get("heatSetpoint")
            if "coolSetpoint" in overrides:
                d["coolSetpoint"] = cv.get("coolSetpoint")
            if "thermostatSetpointStatus" in overrides:
                d["setpointStatus"] = cv.get("thermostatSetpointStatus")
            if "autoChangeoverActive" in overrides:
                d["autoChangeoverActive"] = cv.get("autoChangeoverActive")

    def location_of(self, device_id: str) -> Optional[Any]:
        with self._lock:
            return self._location_of.get(device_id)

    def device_ids_at(self, location_id: Any) -> list[str]:
        """Device IDs last seen at a location. The poller uses this to detect a
        location that previously had zones but transiently reported none - which
        must block reaping, not evict the whole location."""
        with self._lock:
            return [d for d, l in self._location_of.items() if l == location_id]

    def all_device_ids(self) -> list[str]:
        with self._lock:
            return list(self._devices.keys())

    def alerts(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._alerts)[:limit]
