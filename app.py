"""
app.py
------
Ties everything together and serves the dashboard.

Run:  uvicorn app:app --host 0.0.0.0 --port 8010
(or)  python app.py

Flow on startup:
  1. Build the Honeywell client (loads any stored tokens).
  2. Start MQTT bridge (if enabled) and the facility scheduler.
  3. Start a background poller thread that refreshes all devices on an interval,
     pushes state to MQTT, and raises alerts.
  4. Serve the dashboard + REST API.

If the account isn't authorized yet, everything still runs - the dashboard shows
a "Connect account" button that kicks off the OAuth flow.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from automation import AutomationEngine
from config import Config
from groups import GroupStore
from honeywell_client import HoneywellClient, HoneywellError, NotAuthorized
from scheduler import FacilityScheduler
from slack_notifier import SlackNotifier
from state_store import StateStore
from storage import atomic_write_json, load_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("honeywell.app")

STATIC_DIR = Path(__file__).parent / "static"

# ----------------------------------------------------------------- singletons

Config.require_credentials()

client = HoneywellClient(
    api_key=Config.API_KEY,
    api_secret=Config.API_SECRET,
    redirect_uri=Config.REDIRECT_URI,
    min_interval=Config.RL_MIN_INTERVAL,
    hourly_cap=Config.RL_HOURLY_CAP,
    max_retries=Config.RL_MAX_RETRIES,
    retry_max_sleep=Config.RL_RETRY_MAX_SLEEP,
)
store = StateStore()
# Named, reusable zone groups (a picker convenience; see groups.py). Loaded at
# import so the CRUD endpoints work whether or not the account is authorized.
groups = GroupStore()
bridge = None          # set up in lifespan if MQTT enabled
scheduler: Optional[FacilityScheduler] = None
engine: Optional[AutomationEngine] = None
slack: Optional[SlackNotifier] = None   # set up in lifespan if Slack enabled
_poller_stop = threading.Event()
_schedules_asserted = False   # assert program setpoints once, after the first poll
_deferral_notified = False    # one alert per deferral episode, not one per poll

# Serializes a full poll so /api/refresh + /auth/callback + the poller thread
# can't stack N concurrent polls all hammering the rate limit.
_poll_lock = threading.Lock()

# Per-device write locks so two writers (scheduler + automation + sole-control +
# API) to the SAME zone serialize: each sees the previous write's result instead
# of both merging onto the same stale cache and losing an update.
_device_locks: dict[str, threading.Lock] = {}
_device_locks_meta = threading.Lock()

# OAuth CSRF: a random state per login, verified on the callback.
_oauth_states: dict[str, float] = {}
_oauth_states_lock = threading.Lock()


def _device_lock(device_id: str) -> threading.Lock:
    with _device_locks_meta:
        lk = _device_locks.get(device_id)
        if lk is None:
            lk = threading.Lock()
            _device_locks[device_id] = lk
        return lk


def _local_now_str() -> str:
    """Server's current time in the scheduler's timezone (what program times are
    interpreted in), formatted for the status endpoint so a wrong clock/timezone
    is easy to spot."""
    if scheduler is not None:
        try:
            return scheduler._now().strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            pass
    return time.strftime("%Y-%m-%d %H:%M:%S")


def notify(severity: str, kind: str, message: str) -> None:
    """Raise an operator-facing alert and mirror it to MQTT if connected."""
    alert = store.add_alert(severity, kind, message)
    if bridge and bridge.connected:
        bridge.publish_alert(alert)


# Alert kinds mirrored to Slack. Deliberately just the unit up/down transitions
# the operator asked for: state_store's alert generation is edge-triggered, so
# each unit yields exactly one "offline" when it drops and one "online" when it
# returns - no repeats while the state is unchanged. Widen this set to forward
# more alert kinds (temp excursions, equipment faults) to Slack later.
_SLACK_ALERT_KINDS = frozenset({"offline", "online"})


def _on_new_alert(alert: dict) -> None:
    """Sink invoked by the store for every newly-raised alert. Forwards unit
    offline/online transitions to Slack (best-effort, non-blocking). Runs on the
    poller thread inside the store's alert path, so it must never raise."""
    notifier = slack
    if notifier is None or alert.get("kind") not in _SLACK_ALERT_KINDS:
        return
    try:
        notifier.send_alert(alert)
    except Exception as exc:
        log.error("Slack notify failed for %s alert: %s", alert.get("kind"), exc)


def snapshot_read(device_id: str) -> Optional[dict]:
    """Current changeableValues for a device from the latest poll (for snapshots)."""
    cached = store.get(device_id)
    return cached.get("changeableValues") if cached else None


def _zone_is_heating(device_id: str) -> bool:
    """True only if a zone is LOCKED to heating — its mode is Heat. Heat runs on
    natural gas, so it draws no generator power, and the load-shed rotation leaves
    a Heat zone running instead of cycling it.

    Deliberately NOT based on live equipment state. The rotation classifies zones
    ONCE, at outage start, so whatever we exempt now stays exempt for the outage —
    dropped from the rotation, never counted against the cap, never shed. That's
    only safe for a mode that CANNOT draw cooling load: an "Auto" zone can report
    its furnace firing at this instant yet autonomously start its AC compressor
    later when the space warms, and an exempted Auto zone's compressor would then
    run UNCOUNTED and overload the generator. Only Heat mode can never run the
    compressor, so only Heat mode is exempt; Auto / Cool / Off zones stay cyclable
    — the safe default for the generator. (Set a zone to Heat, not Auto, if you
    want it left running.) Reads the cache only (no API call); an unreadable zone
    reads as not-heating and is therefore cyclable."""
    return ((store.get(device_id) or {}).get("mode") or "") == "Heat"


def sync_automation_topics() -> None:
    """Keep the MQTT bridge subscribed to exactly the topics our rules watch."""
    if bridge and engine:
        bridge.sync_trigger_topics(engine.subscribed_topics())


def _mqtt_connection_change(connected: bool) -> None:
    """Raise an operator alert when the broker link changes state. Without this a
    dropped broker is invisible - and the generator load-shed rides on MQTT."""
    if connected:
        notify("info", "mqtt", "Automation link connected.")
    else:
        notify("critical", "mqtt",
               "Automation link disconnected — automations and external commands "
               "are offline until it reconnects.")


# ------------------------------------------------ sole controller (takeover)
# Making this app the single top controller is done with a permanent hold, not by
# editing the device's onboard schedule: a PermanentHold suspends the onboard
# (Resideo-app) schedule indefinitely, and unlike the /devices/schedule endpoint
# it works on every unit (that endpoint 404s on LCC devices). In Sole Controller
# mode the poller re-asserts a permanent hold on any zone that isn't already held,
# so nothing on the thermostat or in the Resideo app ever changes a zone.
#
# The mode is on by default (Config.SOLE_CONTROLLER) and can be toggled at runtime
# from the dashboard; the runtime choice is persisted here so it survives restarts.

SOLE_CONTROL_FILE = Path("sole_control.json")

# Don't re-issue a takeover to the same device more often than this, so a device
# that (for any reason) won't report PermanentHold can't make us hammer the API.
_TAKEOVER_COOLDOWN_SECONDS = 900
_takeover_cooldown: dict[str, float] = {}
_sole_control_lock = threading.Lock()      # guards the cooldown dict + the flag file
_sole_enforce_lock = threading.Lock()      # single-flights an enforcement pass


def _load_sole_control() -> bool:
    """Current Sole Controller setting: the persisted runtime choice if present,
    otherwise the startup default from config."""
    data = load_json(SOLE_CONTROL_FILE)
    if isinstance(data, dict) and "enabled" in data:
        return bool(data.get("enabled"))
    return Config.SOLE_CONTROLLER


def _save_sole_control(enabled: bool) -> None:
    try:
        atomic_write_json(SOLE_CONTROL_FILE, {"enabled": bool(enabled)})
    except OSError as exc:
        log.error("Could not persist sole-control setting: %s", exc)


# ---------------------------------------------- schedule enforcement (anti-tamper)
# When on, each poll re-checks every program-covered zone against what its
# schedule says right now and corrects the ones that drifted (someone changed the
# temp at the thermostat or in the Resideo app). It's drift-aware: a zone that
# already matches its program is left alone, so a steady state costs no API calls.
# Zones under an active generator rotation are skipped (via apply_schedule_action).

SCHEDULE_ENFORCE_FILE = Path("schedule_enforce.json")
_schedule_enforce_lock = threading.Lock()   # single-flights an enforcement pass
_last_conflict_zones: frozenset = frozenset()   # debounce the conflict warning


def _load_schedule_enforce() -> bool:
    data = load_json(SCHEDULE_ENFORCE_FILE)
    if isinstance(data, dict) and "enabled" in data:
        return bool(data.get("enabled"))
    return Config.SCHEDULE_ENFORCE


def _save_schedule_enforce(enabled: bool) -> None:
    try:
        atomic_write_json(SCHEDULE_ENFORCE_FILE, {"enabled": bool(enabled)})
    except OSError as exc:
        log.error("Could not persist schedule-enforce setting: %s", exc)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _zone_matches_action(zone: dict, action: dict) -> bool:
    """True if the zone's current state already matches what a program period
    says. Only the fields the action specifies are compared, so an unrelated
    field never triggers a needless correction."""
    if "mode" in action and (zone.get("mode") or "") != action["mode"]:
        return False
    for field in ("heatSetpoint", "coolSetpoint"):
        if field in action:
            want = _num(action[field])
            if want is not None and _num(zone.get(field)) != want:
                return False
    if "thermostatSetpointStatus" in action:
        if (zone.get("setpointStatus") or "") != action["thermostatSetpointStatus"]:
            return False
    return True


def _action_summary(action: dict) -> str:
    """Short human description of what a program period sets, for alerts and the
    schedule readout. The setpoint labels ("Heat 68°") already name the mode, so
    we don't repeat it - "Cool 68°", not "Cool Cool 68°"."""
    mode = action.get("mode")
    heat, cool = action.get("heatSetpoint"), action.get("coolSetpoint")
    if mode == "Off":
        return "Off"
    if mode == "Heat":
        return f"Heat {heat}°" if heat is not None else "Heat"
    if mode == "Cool":
        return f"Cool {cool}°" if cool is not None else "Cool"
    if mode == "Auto":
        span = [f"{v}°" for v in (heat, cool) if v is not None]
        return "Auto " + "–".join(span) if span else "Auto"
    # No/unknown mode: just show whatever setpoints the period specifies.
    bits = ([mode] if mode else []) + \
           ([f"Heat {heat}°"] if heat is not None else []) + \
           ([f"Cool {cool}°"] if cool is not None else [])
    return " ".join(bits) or "(no change)"


def _enforce_schedules() -> None:
    """Correct any online, program-covered zone that has drifted from what its
    schedule says right now. Single-flighted; skips zones under an active
    rotation; raises one operator alert per correction batch as a tamper log.

    When two enabled programs cover the same zone, the one whose boundary took
    effect most recently wins (a today program beats an off-day carry-over). If
    two programs tie at the same boundary but disagree, the zone is skipped and a
    conflict is surfaced rather than guessed - so enforcement never fights itself.
    """
    global _last_conflict_zones
    if not _load_schedule_enforce() or scheduler is None or not client.is_authorized:
        return
    if not _schedule_enforce_lock.acquire(blocking=False):
        return
    try:
        desired, conflicts = scheduler.resolve_desired(store.all_device_ids())
        active_rot = engine.active_rotation_targets() if engine else set()

        # Group the zones that actually drifted by their target action, so a set
        # of zones needing the same correction is one write, not N.
        by_action: dict[str, tuple] = {}
        for zid, (action, _prog) in desired.items():
            if zid in active_rot:
                continue   # under outage control; apply_schedule_action would skip it anyway
            z = store.get(zid)
            if not z or not z.get("online"):
                continue
            if _zone_matches_action(z, action):
                continue
            key = json.dumps(action, sort_keys=True)
            by_action.setdefault(key, (action, []))[1].append(zid)

        corrected: list[str] = []
        for action, zids in by_action.values():
            failed = set(apply_schedule_action(zids, action))
            corrected.extend(z for z in zids if z not in failed)

        if corrected:
            def one(z):
                nm = (store.get(z) or {}).get("name") or z
                act, prog = desired[z]
                return f"{nm} → {_action_summary(act)} ({prog})"
            shown = "; ".join(one(z) for z in corrected[:6]) + (
                f"; +{len(corrected) - 6} more" if len(corrected) > 6 else "")
            log.info("Schedule enforcement corrected %d zone(s): %s", len(corrected), shown)
            notify("info", "schedule_enforced",
                   f"Schedule enforcement put {len(corrected)} zone(s) back to their program "
                   f"(a change was made at the thermostat or in the Resideo app): {shown}.")

        # Surface conflicting programs once per change of the conflict set, so the
        # operator can fix the overlap instead of enforcement silently oscillating.
        conflict_zones = frozenset(c["zone"] for c in conflicts)
        if conflict_zones != _last_conflict_zones:
            _last_conflict_zones = conflict_zones
            if conflicts:
                detail = "; ".join(
                    f"{(store.get(c['zone']) or {}).get('name') or c['zone']} "
                    f"({', '.join(c['programs'])})" for c in conflicts[:8])
                notify("warning", "schedule_conflict",
                       f"Schedule enforcement left {len(conflicts)} zone(s) alone because two "
                       f"programs set them differently at the same time - please fix the "
                       f"overlap: {detail}.")
    except Exception as exc:
        log.exception("Schedule enforcement pass failed: %s", exc)
    finally:
        _schedule_enforce_lock.release()


def _schedule_snapshot() -> dict:
    """A per-zone read-only view of what the programs say right now, for the
    dashboard's "which schedule is in effect?" readout. Uses the SAME per-zone
    arbitration as enforcement (resolve_desired) so what the operator sees is
    exactly what enforcement would act on - never a second, divergent opinion.

    Returns:
      {enforce, server_time, timezone,
       zones: {deviceID: {state, program?, summary?, programs?}},
       counts: {on_target, drifted, unscheduled, conflict, rotating, offline},
       conflicts: [{zone, programs}]}

    Per-zone `state` is one of:
      on_target   - the zone already matches its program (following the schedule)
      drifted     - a program covers it but the live values differ (someone
                    changed it at the thermostat / in Resideo); with enforcement
                    on, the next poll corrects it
      unscheduled - no enabled program covers this zone right now
      conflict    - two programs set it differently at the same boundary; left alone
      rotating    - under an active generator rotation (outage control) - schedule
                    writes are deferred until utility power returns
      offline     - a program covers it but Honeywell can't reach it to confirm/apply
    """
    enforce = _load_schedule_enforce()
    base = {"enforce": enforce, "server_time": _local_now_str(),
            "timezone": scheduler.timezone_name() if scheduler else None,
            "zones": {}, "conflicts": [],
            "counts": {k: 0 for k in ("on_target", "drifted", "unscheduled",
                                      "conflict", "rotating", "offline")}}
    if scheduler is None:
        return base
    ids = store.all_device_ids()
    desired, conflicts = scheduler.resolve_desired(ids)
    conflict_programs = {c["zone"]: c["programs"] for c in conflicts}
    active_rot = engine.active_rotation_targets() if engine else set()
    zones = base["zones"]
    counts = base["counts"]
    for zid in ids:
        if zid in conflict_programs:
            zones[zid] = {"state": "conflict", "programs": conflict_programs[zid]}
            counts["conflict"] += 1
            continue
        if zid not in desired:
            zones[zid] = {"state": "unscheduled"}
            counts["unscheduled"] += 1
            continue
        action, prog = desired[zid]
        entry = {"program": prog, "summary": _action_summary(action)}
        z = store.get(zid) or {}
        if zid in active_rot:
            entry["state"] = "rotating"
        elif not z.get("online"):
            entry["state"] = "offline"
        elif _zone_matches_action(z, action):
            entry["state"] = "on_target"
        else:
            entry["state"] = "drifted"
        counts[entry["state"]] += 1
        zones[zid] = entry
    base["conflicts"] = conflicts
    return base


def _is_held(device: dict) -> bool:
    """True if the app already owns this zone (it's under a permanent hold)."""
    return (device.get("setpointStatus") or "") == "PermanentHold"


def take_over_device(device_id: str) -> None:
    """Assert a permanent hold on one device at its current settings, so it stops
    following its onboard/Resideo schedule. Raises HoneywellError on failure."""
    loc = store.location_of(device_id)
    if loc is None:
        raise HoneywellError(f"Unknown device {device_id} (has it been polled yet?)")
    with _device_lock(device_id):
        cached = store.get(device_id) or {}
        current_cv = cached.get("changeableValues") or {}
        # Merge only the hold onto the device's existing values so we freeze whatever
        # it's doing right now (mode + setpoints) under a permanent hold. If the
        # cache has no changeableValues yet, pass None so set_thermostat fetches
        # the live object first - a hold-only body would be rejected by Resideo.
        client.set_thermostat(device_id, loc, {"thermostatSetpointStatus": "PermanentHold"},
                              current_changeable=current_cv or None)
        # Update the cache locally instead of spending an extra GET; the next full
        # poll reconciles reality anyway and the cooldown limits re-tries.
        store.apply_local_override(device_id, {"thermostatSetpointStatus": "PermanentHold"})


def _enforce_sole_control() -> None:
    """Re-assert a permanent hold on every online zone that isn't already held.
    Single-flighted so the poller and a manual toggle can't both sweep at once and
    double the takeover burst. Cheap in steady state: a held zone reports
    PermanentHold and is skipped."""
    if not _load_sole_control() or not client.is_authorized:
        return
    if not _sole_enforce_lock.acquire(blocking=False):
        return  # a sweep is already running
    try:
        now = time.time()
        for d in store.devices():
            did = d.get("deviceID")
            if not did or not d.get("online") or _is_held(d):
                continue
            with _sole_control_lock:
                if now - _takeover_cooldown.get(did, 0.0) < _TAKEOVER_COOLDOWN_SECONDS:
                    continue
                _takeover_cooldown[did] = now
            try:
                take_over_device(did)
                log.info("Sole control: took over %s (permanent hold).", did)
                store.add_alert("info", "sole_control",
                                f"{d.get('name') or did} is now held by the app "
                                f"(onboard schedule suspended).", did)
            except HoneywellError as exc:
                log.warning("Sole control: could not take over %s: %s", did, exc)
    finally:
        _sole_enforce_lock.release()


# ------------------------------------------------------------ command plumbing

def _clamp_overrides(cached: Optional[dict], overrides: dict) -> dict:
    """Clamp heat/cool setpoints to the device's reported limits so a mistyped or
    runaway value can't drive a zone to an extreme. Leaves fields alone when the
    device doesn't report a limit."""
    if not cached or not overrides:
        return overrides
    out = dict(overrides)
    for field, lo_key, hi_key in (("heatSetpoint", "minHeatSetpoint", "maxHeatSetpoint"),
                                  ("coolSetpoint", "minCoolSetpoint", "maxCoolSetpoint")):
        val = out.get(field)
        if val is None:
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        lo, hi = cached.get(lo_key), cached.get(hi_key)
        if isinstance(lo, (int, float)) and v < lo:
            log.warning("Clamping %s %s up to device minimum %s", field, v, lo)
            out[field] = lo
        elif isinstance(hi, (int, float)) and v > hi:
            log.warning("Clamping %s %s down to device maximum %s", field, v, hi)
            out[field] = hi
    return out


# Every field a control write may carry (dashboard, MQTT commands, automations,
# schedule periods). Anything else is dropped/rejected before it can be merged
# into the changeableValues body POSTed to Resideo.
_SET_FIELDS = frozenset({"mode", "heatSetpoint", "coolSetpoint", "thermostatSetpointStatus",
                         "nextPeriodTime", "autoChangeoverActive", "fan"})


def apply_action(targets: Any, action: dict, refresh: bool = True) -> list[str]:
    """Apply a control action to one device, a list, or 'all'. Used by the
    scheduler, automations and MQTT commands. Returns the list of deviceIDs that
    FAILED (empty = all applied) so callers (the automation engine) can tell a
    partial shed/restore from a success. `action` may contain a 'fan' key handled
    separately from setpoint fields.

    Set `refresh=False` for bulk automation/scheduler writes: the cache is updated
    locally so serialized follow-up writes stay coherent, and the next poll
    reconciles for the UI/MQTT - saving one API GET per zone against a tight limit.
    """
    if targets == "all":
        device_ids = store.all_device_ids()
    elif isinstance(targets, str):
        device_ids = [targets]
    else:
        device_ids = list(targets)

    fan_mode = action.get("fan")
    unknown = sorted(k for k in action if k not in _SET_FIELDS)
    if unknown:
        # Junk keys (a typo'd MQTT payload, a hand-edited rule) must not ride
        # into the changeableValues POST body and break the write.
        log.warning("Dropping unknown control field(s) %s (allowed: %s)",
                    unknown, sorted(_SET_FIELDS))
    setpoint_overrides = {k: v for k, v in action.items()
                          if k != "fan" and k in _SET_FIELDS}

    # Make this app the sole source of truth over the thermostats' onboard
    # (Resideo-app) 7-day schedule. A setpoint written with anything other than
    # PermanentHold is surrendered back to the onboard schedule at the next period
    # boundary, so the Resideo schedule would win. Defaulting programmatic changes
    # (scheduler, automations, MQTT) to a permanent hold suspends the onboard
    # schedule. Callers that pass an explicit status keep full control.
    if setpoint_overrides and not setpoint_overrides.get("thermostatSetpointStatus"):
        setpoint_overrides["thermostatSetpointStatus"] = "PermanentHold"

    failed: list[str] = []
    for did in device_ids:
        loc = store.location_of(did)
        if loc is None:
            log.warning("No known location for device %s; skipping.", did)
            failed.append(did)
            continue
        # Serialize writes to this device so concurrent writers don't merge onto the
        # same stale cache and lose each other's changes.
        with _device_lock(did):
            cached = store.get(did)
            current_cv = cached.get("changeableValues") if cached else None
            overrides = _clamp_overrides(cached, setpoint_overrides)
            try:
                if overrides:
                    client.set_thermostat(did, loc, overrides, current_changeable=current_cv)
                    store.apply_local_override(did, overrides)
                if fan_mode:
                    client.set_fan(did, loc, fan_mode)
                if refresh:
                    _refresh_one(did, loc)
            except HoneywellError as exc:
                log.error("Failed to apply action to %s: %s", did, exc)
                store.add_alert("critical", "control_failed",
                                f"Control failed for {did}: {exc}", did)
                failed.append(did)
    return failed


def apply_schedule_action(targets: Any, action: dict) -> list[str]:
    """apply_action for schedule periods, minus any zone under an active
    generator rotation. A period boundary firing mid-outage ("all zones ON at
    6am") must not re-energize shed zones and overload the generator - the same
    hazard the startup schedule-assertion deferral guards against. Skipped
    zones return to normal program control at the next boundary after the
    rotation stops (the post-outage restore puts them back first)."""
    active = engine.active_rotation_targets() if engine else set()
    if active:
        if targets == "all":
            ids = store.all_device_ids()
        elif isinstance(targets, str):
            ids = [targets]
        else:
            ids = list(targets)
        skipped = sorted(d for d in ids if d in active)
        ids = [d for d in ids if d not in active]
        if skipped:
            log.warning("Schedule period skipped zone(s) under an active rotation: %s", skipped)
            notify("warning", "schedule_deferred",
                   f"Schedule period skipped {len(skipped)} zone(s) under an active "
                   f"generator rotation: {', '.join(skipped)}. They stay under outage "
                   "control until utility power returns.")
        if not ids:
            return []
        targets = ids
    return apply_action(targets, action, refresh=False)


def on_zones_restored(device_ids: list) -> None:
    """After an automation restore returns zones to their pre-outage state,
    immediately re-assert every enabled program's currently-active period —
    the same thing startup does. Program boundaries that fired during the
    outage were skipped for rotated zones, so without this a restored zone
    would sit at stale pre-outage setpoints until the NEXT boundary (which can
    be hours away). Zones not covered by any program keep the restored state."""
    if not scheduler:
        return
    log.info("Restore completed for %d zone(s); re-asserting active schedule periods.",
             len(device_ids))
    try:
        scheduler.apply_all_active_now(store.all_device_ids())
    except Exception as exc:
        log.exception("Post-restore schedule assertion failed: %s", exc)
        notify("critical", "schedule",
               f"Zones were restored, but re-asserting the daily programs failed: {exc}")


def handle_mqtt_command(device_id: str, command: dict) -> None:
    apply_action(device_id, command)


# ------------------------------------------------------------------- polling

def _publish_all(devices: list[dict]) -> None:
    if bridge and bridge.connected:
        for d in devices:
            bridge.publish_state(d)


def _refresh_one(device_id: str, location_id: Any) -> None:
    """Targeted refresh of a single device after a control action so the UI and
    MQTT reflect the change without waiting for the next full poll."""
    try:
        raw = client.get_thermostat(device_id, location_id)
        events = store.ingest([raw], location_id)
        d = store.get(device_id)
        if d and bridge and bridge.connected:
            wire = {k: v for k, v in d.items() if k != "changeableValues"}
            bridge.publish_state(wire)
        _emit_events(events)
    except HoneywellError as exc:
        log.error("Targeted refresh of %s failed: %s", device_id, exc)


def _emit_events(events: list[dict]) -> None:
    if not (bridge and bridge.connected):
        return
    for ev in events:
        bridge.publish_event(ev)
    # push any freshly generated alerts to MQTT too
    for alert in store.alerts(limit=len(events) + 2):
        if time.time() - alert["ts"] < 5:
            bridge.publish_alert(alert)


def _is_thermostat(device: Any) -> bool:
    """True for thermostat entries in a /locations `devices` array, which also
    lists leak detectors and other device classes. Falls back to structural
    hints when the class field is absent so we never drop a real thermostat."""
    if not isinstance(device, dict):
        return False
    dc = device.get("deviceClass")
    if isinstance(dc, str):
        return dc.lower() == "thermostat"
    return ("changeableValues" in device or "indoorTemperature" in device
            or "allowedModes" in device or "thermostatSetpointStatus" in device)


def poll_once() -> None:
    """One full poll of every thermostat across the account.

    The /locations response already embeds each location's devices with full
    state, so we read thermostats straight from it - a single API call for the
    whole account instead of one call per location. That keeps us well under
    Resideo's rate limit (the Basic plan is sized for ~20 devices every 5
    minutes)."""
    if not client.is_authorized:
        return
    # Single-flight: if a poll is already running (poller vs manual refresh vs the
    # post-auth kick), don't stack a second one on top of the rate limit.
    if not _poll_lock.acquire(blocking=False):
        log.debug("Poll already in progress; skipping this trigger.")
        return
    try:
        try:
            locations = client.get_locations()
        except NotAuthorized:
            return
        except HoneywellError as exc:
            store.mark_poll(error=str(exc))
            log.error("Poll failed at /locations: %s", exc)
            return

        all_events: list[dict] = []
        errors: list[str] = []
        seen: set[str] = set()
        complete = True
        for loc in locations:
            loc_id = loc.get("locationID")
            inline = loc.get("devices")
            thermostats = [d for d in (inline or []) if _is_thermostat(d)]
            # Only fall back to a per-location fetch if the location truly carried
            # no inline devices - otherwise we'd re-introduce the per-location call
            # volume this whole approach exists to avoid.
            if not thermostats and inline is None:
                try:
                    thermostats = client.get_thermostats(loc_id)
                except HoneywellError as exc:
                    errors.append(f"location {loc_id}: {exc}")
                    complete = False
                    log.error("Poll failed at location %s: %s", loc_id, exc)
                    continue
            elif not thermostats and inline:
                # Devices were present but none parsed as a thermostat - surface it
                # so a summary-shaped payload can't silently drop real zones.
                log.warning("Location %s reported %d device(s) but no recognized thermostats.",
                            loc_id, len(inline))
            if not thermostats and store.device_ids_at(loc_id):
                # This location previously had zones but reported none this cycle
                # (a transient empty `devices` array). Treat the poll as incomplete
                # so reap can't evict the whole location on a blip - mid-outage
                # that would also drop location_of and break rotation writes.
                complete = False
                log.warning("Location %s reported no thermostats but zones are known "
                            "there; skipping reap this cycle.", loc_id)
            for t in thermostats:
                did = t.get("deviceID")
                if did:
                    seen.add(did)
            all_events.extend(store.ingest(thermostats, loc_id))

        # Reap devices that dropped off the account, but only after a *complete*
        # poll that actually saw devices - never on a transient empty result (that
        # would wipe every zone and mask the outage as success).
        if complete and locations and seen:
            all_events.extend(store.reap(seen))

        # Poll-health accounting: a poll that returns nothing must NOT read as a
        # green "ok" once devices have been seen before.
        device_ids = store.all_device_ids()
        if not locations:
            store.mark_poll(error="Poll returned no locations (account/auth issue?)")
        elif not seen and device_ids:
            store.mark_poll(error="Poll returned no thermostats this cycle")
        elif errors:
            store.mark_poll(error="; ".join(errors))
        elif not complete:
            # A location with known zones reported none this cycle: reap was
            # (rightly) skipped, but those zones are showing stale data - the
            # poll must not read as a green "ok".
            store.mark_poll(error="Some locations reported no thermostats this cycle; "
                                  "their zones show last-known values")
        else:
            store.mark_poll()

        _publish_all(store.devices())
        _emit_events(all_events)
        log.info("Poll complete: %d device(s), %d change event(s).",
                 len(device_ids), len(all_events))
    finally:
        _poll_lock.release()


def _poller_loop() -> None:
    global _schedules_asserted, _deferral_notified
    # Small initial delay so the server is up before the first poll.
    _poller_stop.wait(2)
    while not _poller_stop.is_set():
        try:
            poll_once()
        except Exception as exc:  # never let the loop die
            log.exception("Unexpected poller error: %s", exc)
        # Once devices are known, assert each program's active setpoints so the
        # app owns them immediately after a (re)start, not only at the next period.
        if not _schedules_asserted and scheduler and store.all_device_ids():
            active = engine.active_rotation_targets() if engine else set()
            if active:
                # Restart mid-outage: re-asserting schedules would re-energize the
                # shed zones all at once and overload the generator. Defer until the
                # rotation stops (utility restored), then assert on a later poll.
                # Alert ONCE per episode - one per poll would flood the alert
                # buffer over a long outage and evict genuinely critical alerts.
                if not _deferral_notified:
                    _deferral_notified = True
                    log.warning("Active generator rotation(s) at startup; deferring schedule "
                                "assertion for %d zone(s) to avoid re-energizing shed zones.",
                                len(active))
                    notify("warning", "schedule_deferred",
                           "Startup schedule assertion deferred: a generator rotation is active.")
            else:
                _schedules_asserted = True
                _deferral_notified = False
                try:
                    scheduler.apply_all_active_now(store.all_device_ids())
                except Exception as exc:
                    log.exception("Startup schedule assertion failed: %s", exc)
        # Keep every zone under the app's control so the onboard/Resideo schedule
        # never acts. Runs after schedule assertion so program setpoints win.
        try:
            _enforce_sole_control()
        except Exception as exc:
            log.exception("Sole-control enforcement failed: %s", exc)
        # Correct any program-covered zone that drifted from its schedule (a change
        # at the thermostat or in Resideo). Runs after sole-control so the program's
        # setpoint values, not just the hold, are what wins.
        try:
            _enforce_schedules()
        except Exception as exc:
            log.exception("Schedule enforcement failed: %s", exc)
        _poller_stop.wait(Config.POLL_INTERVAL_SECONDS)


# ------------------------------------------------------------------- lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, scheduler, engine, slack

    if Config.HOST not in ("127.0.0.1", "localhost", "::1") and not Config.DASHBOARD_TOKEN:
        log.warning("SECURITY: binding %s with no DASHBOARD_TOKEN set - the control API is "
                    "reachable UNAUTHENTICATED on the network. Set DASHBOARD_TOKEN and/or put "
                    "the app behind a VPN or authenticating reverse proxy.", Config.HOST)

    # The automation engine reacts to inbound MQTT; build it first so the bridge
    # can hand it trigger messages.
    engine = AutomationEngine(
        apply_fn=lambda t, v: apply_action(t, v, refresh=False),
        resolve_fn=store.all_device_ids,
        snapshot_read_fn=snapshot_read,
        notify_fn=notify,
        on_topics_changed=sync_automation_topics,
        hourly_write_budget=Config.RL_HOURLY_CAP,
        on_restored=on_zones_restored,   # resume daily programs after a restore
        is_heating_fn=_zone_is_heating,  # leave gas-heat zones out of load-shed cycling
    )

    if Config.MQTT_ENABLED:
        try:
            from mqtt_bridge import MqttBridge
            bridge = MqttBridge(
                host=Config.MQTT_HOST, port=Config.MQTT_PORT,
                base_topic=Config.MQTT_BASE_TOPIC,
                username=Config.MQTT_USERNAME, password=Config.MQTT_PASSWORD,
                command_handler=handle_mqtt_command,
                trigger_handler=engine.handle_message,
                on_connection_change=_mqtt_connection_change,
            )
            bridge.start()
        except Exception as exc:
            log.error("MQTT bridge failed to start (continuing without it): %s", exc)
            bridge = None

    engine.start()
    sync_automation_topics()  # subscribe to whatever loaded rules watch

    scheduler = FacilityScheduler(
        apply_fn=apply_schedule_action,   # skips zones under an active rotation
        timezone=Config.SCHEDULE_TZ or None,
    )
    scheduler.start()
    if scheduler.timezone_error:
        notify("critical", "config", scheduler.timezone_error)

    # Slack offline/online notifications (optional). Wire the store's alert sink
    # BEFORE the poller starts so the very first poll's transitions are caught.
    if Config.SLACK_ENABLED:
        if not (Config.SLACK_BOT_TOKEN and Config.SLACK_CHANNEL):
            log.warning("SLACK_ENABLED but SLACK_BOT_TOKEN and/or SLACK_CHANNEL is unset - "
                        "Slack alerts stay OFF until both are provided.")
        else:
            if not Config.SLACK_BOT_TOKEN.startswith("xoxb-"):
                log.warning("SLACK_BOT_TOKEN doesn't look like a bot token (expected an "
                            "'xoxb-...' value); Slack may reject the posts.")
            try:
                slack = SlackNotifier(Config.SLACK_BOT_TOKEN, Config.SLACK_CHANNEL)
                slack.start()
                store.set_on_alert(_on_new_alert)
                log.info("Slack alerts enabled (channel %s): notifying on unit offline/online.",
                         Config.SLACK_CHANNEL)
            except Exception as exc:
                log.error("Slack notifier failed to start (continuing without it): %s", exc)
                slack = None

    poller = threading.Thread(target=_poller_loop, name="poller", daemon=True)
    poller.start()

    log.info("Startup complete. Authorized=%s", client.is_authorized)
    try:
        yield
    finally:
        _poller_stop.set()
        # Give an in-flight poll a moment to finish before we tear down its deps.
        poller.join(timeout=5)
        if engine:
            engine.stop()
        if scheduler:
            scheduler.stop()
        if bridge:
            bridge.stop()
        if slack:
            slack.stop()


app = FastAPI(title="Facility Thermostat Dashboard", lifespan=lifespan)


# ------------------------------------------------------------- optional gate

@app.middleware("http")
async def token_gate(request: Request, call_next):
    if Config.DASHBOARD_TOKEN:
        # Allow the OAuth callback through (Honeywell can't send our header). Exact
        # match so a lookalike path like /auth/callbackX isn't also exempted.
        if request.url.path != "/auth/callback":
            supplied = request.headers.get("X-Token") or request.query_params.get("token") or ""
            # Constant-time comparison so the token can't be guessed by timing.
            if not secrets.compare_digest(supplied, Config.DASHBOARD_TOKEN):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------- OAuth

def _new_oauth_state() -> str:
    st = secrets.token_urlsafe(16)
    now = time.time()
    with _oauth_states_lock:
        # prune states older than 10 minutes
        for k in [k for k, t in _oauth_states.items() if now - t > 600]:
            _oauth_states.pop(k, None)
        _oauth_states[st] = now
    return st


def _check_oauth_state(state: Optional[str]) -> bool:
    if not state:
        return False
    with _oauth_states_lock:
        if state in _oauth_states:
            _oauth_states.pop(state, None)
            return True
    return False


@app.get("/auth/login")
def auth_login():
    return RedirectResponse(client.authorize_url(state=_new_oauth_state()))


@app.get("/auth/callback")
def auth_callback(code: Optional[str] = None, state: Optional[str] = None,
                  error: Optional[str] = None):
    if error:
        return HTMLResponse(f"<h3>Authorization failed:</h3><pre>{html.escape(error)}</pre>",
                            status_code=400)
    if not code:
        return HTMLResponse("<h3>Missing authorization code.</h3>", status_code=400)
    if not _check_oauth_state(state):
        # No/expired/forged state: refuse so an attacker can't bind their own
        # Resideo account's tokens to this server (login-CSRF).
        return HTMLResponse("<h3>Invalid or expired authorization state. "
                            "Please start the connect flow again.</h3>", status_code=400)
    try:
        client.exchange_code(code)
    except HoneywellError as exc:
        return HTMLResponse(f"<h3>Token exchange failed:</h3><pre>{html.escape(str(exc))}</pre>",
                            status_code=400)
    # Kick off an immediate poll in the background so data shows up fast.
    threading.Thread(target=poll_once, daemon=True).start()
    # Carry the dashboard token through the redirect; otherwise the gate would
    # 401 the bare "/" the user lands on right after connecting their account.
    dest = "/?token=" + quote(Config.DASHBOARD_TOKEN) if Config.DASHBOARD_TOKEN else "/"
    return RedirectResponse(dest)


# --------------------------------------------------------------------- API

@app.get("/api/status")
def api_status():
    ts, err = store.poll_status()
    return {
        "authorized": client.is_authorized,
        "device_count": len(store.all_device_ids()),
        "last_poll_ts": ts,
        # Last SUCCESSFUL poll — the UI shows "Updated X ago" from this so a failed
        # cycle can't read as fresh (last_poll_ts stamps every attempt).
        "last_ok_poll_ts": store.last_ok_poll_ts,
        "last_poll_error": err,
        "poll_interval_seconds": Config.POLL_INTERVAL_SECONDS,
        "mqtt_enabled": Config.MQTT_ENABLED,
        "mqtt_connected": bool(bridge and bridge.connected),
        # Timezone schedules are interpreted in, plus the server's current local
        # time, so a wrong-timezone misconfig (units firing hours off) is visible.
        "schedule_timezone": scheduler.timezone_name() if scheduler else None,
        "server_time": _local_now_str(),
        # Live control-mode flags so the dashboard's toggles reflect the real state.
        "schedule_enforce": _load_schedule_enforce(),
    }


@app.get("/api/schedule_status")
def api_schedule_status():
    """What each zone's programs say right now, and whether the zone is following
    them - powers the dashboard's per-zone schedule readout."""
    return _schedule_snapshot()


@app.get("/api/devices")
def api_devices():
    return {"devices": store.devices()}


@app.post("/api/devices/{device_id}/set")
def api_set_device(device_id: str, payload: dict = Body(...)):
    """
    Body may include any of:
      mode, heatSetpoint, coolSetpoint, thermostatSetpointStatus,
      nextPeriodTime, autoChangeoverActive, fan
    """
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    loc = store.location_of(device_id)
    if loc is None:
        raise HTTPException(404, f"Unknown device {device_id} (has it been polled yet?)")
    unknown = sorted(k for k in payload if k not in _SET_FIELDS)
    if unknown:
        raise HTTPException(400, f"Unknown field(s): {', '.join(unknown)}. "
                                 f"Allowed: {', '.join(sorted(_SET_FIELDS))}")

    # Validate mode against what the device actually supports.
    cached = store.get(device_id)
    mode = payload.get("mode")
    if mode and cached:
        allowed = cached.get("allowedModes") or []
        if allowed and mode not in allowed:
            raise HTTPException(400, f"mode '{mode}' not in allowedModes {allowed}")

    fan_mode = payload.pop("fan", None)
    try:
        with _device_lock(device_id):
            # Re-read the cache UNDER the lock (like apply_action does): a
            # concurrent writer to the same zone (scheduler, rotation, MQTT)
            # updates the cache when its write lands, and merging onto a
            # snapshot taken before the lock would silently undo that write.
            cached = store.get(device_id)
            current_cv = cached.get("changeableValues") if cached else None
            overrides = _clamp_overrides(cached, payload)
            if overrides:
                client.set_thermostat(device_id, loc, overrides, current_changeable=current_cv)
                store.apply_local_override(device_id, overrides)
            if fan_mode:
                client.set_fan(device_id, loc, fan_mode)
            _refresh_one(device_id, loc)
    except HoneywellError as exc:
        raise HTTPException(502, f"Honeywell API error: {exc}")

    d = store.get(device_id)
    return {"ok": True, "device": d and {k: v for k, v in d.items() if k != "changeableValues"}}


@app.post("/api/devices/set")
def api_bulk_set(payload: dict = Body(...)):
    """Apply one set of control values to many zones at once.

    Body: {"targets": "all" | [deviceID, ...], "values": {mode / heatSetpoint /
    coolSetpoint / thermostatSetpointStatus / fan ...}}

    Zones under an active generator rotation are SKIPPED (the same guard
    schedule periods use): a bulk write - especially a select-all - firing
    mid-outage must not re-energize shed zones and overload the generator.
    They're reported back so the operator sees exactly what was left alone;
    the per-zone controls remain the deliberate single-zone override.

    Returns {ok, applied, failed: [ids], skipped_rotating: [ids]}. Setpoints
    are clamped per device and each zone's write serializes under its device
    lock inside apply_action, exactly like scheduler/automation writes.
    """
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    targets = payload.get("targets")
    values = payload.get("values") or {}
    if targets != "all" and (not isinstance(targets, list) or not targets):
        raise HTTPException(400, "targets must be 'all' or a non-empty list of deviceIDs")
    unknown = sorted(k for k in values if k not in _SET_FIELDS)
    if unknown:
        raise HTTPException(400, f"Unknown field(s): {', '.join(unknown)}. "
                                 f"Allowed: {', '.join(sorted(_SET_FIELDS))}")
    if not values:
        raise HTTPException(400, "values must include at least one control field")

    ids = store.all_device_ids() if targets == "all" else list(dict.fromkeys(targets))
    active = engine.active_rotation_targets() if engine else set()
    skipped = sorted(d for d in ids if d in active)
    ids = [d for d in ids if d not in active]
    if skipped:
        notify("warning", "bulk_skipped",
               f"Bulk control skipped {len(skipped)} zone(s) under an active generator "
               f"rotation: {', '.join(skipped)}. They stay under outage control.")

    # refresh=False: the cache is updated locally per write and the next poll
    # reconciles - a targeted GET per zone would burn the rate budget on bulk.
    failed = apply_action(ids, values, refresh=False)
    applied = len(ids) - len(failed)
    if applied:
        notify("info", "bulk_control",
               f"Bulk control applied to {applied} zone(s)"
               + (f" ({len(failed)} failed)" if failed else "") + ".")
    return {"ok": not failed, "applied": applied,
            "failed": failed, "skipped_rotating": skipped}


@app.post("/api/refresh")
def api_refresh():
    threading.Thread(target=poll_once, daemon=True).start()
    return {"ok": True, "message": "Refresh started"}


# ------------------------------------------------ sole controller endpoints

@app.get("/api/onboard_schedule")
def api_onboard_status():
    """Per-device control state, derived from live device state (no Resideo calls):
    a zone is 'taken over' when it's under the app's permanent hold. Also reports
    whether Sole Controller mode is on."""
    taken = [d["deviceID"] for d in store.devices() if _is_held(d) and d.get("deviceID")]
    return {"taken_over": taken, "sole_control": _load_sole_control()}


@app.get("/api/sole_control")
def api_sole_control_get():
    return {"enabled": _load_sole_control()}


@app.post("/api/sole_control")
def api_sole_control_set(payload: dict = Body(...)):
    """Turn Sole Controller mode on or off (persisted). When turned on, the next
    poll asserts a permanent hold on every zone; turning it off simply stops the
    app re-asserting - zones keep whatever hold they currently have until changed."""
    enabled = bool(payload.get("enabled"))
    with _sole_control_lock:
        _save_sole_control(enabled)
    if enabled:
        # Take control now rather than waiting for the next poll.
        threading.Thread(target=_enforce_sole_control, daemon=True).start()
    notify("info", "sole_control",
           "Sole Controller mode on — the app now holds every zone." if enabled
           else "Sole Controller mode off — zones may follow their onboard schedule.")
    return {"ok": True, "enabled": enabled}


@app.get("/api/schedule_enforce")
def api_schedule_enforce_get():
    return {"enabled": _load_schedule_enforce()}


@app.post("/api/schedule_enforce")
def api_schedule_enforce_set(payload: dict = Body(...)):
    """Turn schedule enforcement on or off (persisted). When on, every poll puts
    any program-covered zone that drifted back to what its schedule says."""
    enabled = bool(payload.get("enabled"))
    _save_schedule_enforce(enabled)
    if enabled:
        # Reconcile now rather than waiting for the next poll.
        threading.Thread(target=_enforce_schedules, daemon=True).start()
    notify("info", "schedule_enforce",
           "Schedule enforcement on — zones a program covers are put back to the "
           "schedule on every update." if enabled else "Schedule enforcement off.")
    return {"ok": True, "enabled": enabled}


@app.get("/api/devices/{device_id}/raw")
def api_device_raw(device_id: str):
    """Read-only diagnostic: the raw Resideo thermostat object, plus the result of
    probing the onboard-schedule endpoint. Use it to inspect the real schedule
    shape/type for a device when wiring up onboard-schedule takeover."""
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    loc = store.location_of(device_id)
    if loc is None:
        raise HTTPException(404, f"Unknown device {device_id}")
    out: dict[str, Any] = {"locationId": loc}
    try:
        out["device"] = client.get_thermostat(device_id, loc)
    except HoneywellError as exc:
        out["device_error"] = str(exc)
    cached = store.get(device_id)
    stype = cached.get("scheduleType") if cached else None
    out["detected_scheduleType"] = stype
    # Probe the schedule endpoint both with and without the type param so we can
    # see which the backend accepts.
    for label, kwargs in (("with_type", {"schedule_type": stype}), ("no_type", {})):
        try:
            out[f"schedule_{label}"] = client.get_schedule(device_id, loc, **kwargs)
        except HoneywellError as exc:
            out[f"schedule_{label}_error"] = str(exc)
    return out


@app.post("/api/devices/{device_id}/onboard_schedule/disable")
def api_disable_onboard(device_id: str):
    """Take control of one zone now: assert a permanent hold so it stops following
    its onboard/Resideo schedule. Uses a normal setpoint write (which works on
    every unit), not the /devices/schedule endpoint (which 404s on LCC devices)."""
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    if store.location_of(device_id) is None:
        raise HTTPException(404, f"Unknown device {device_id} (has it been polled yet?)")
    try:
        take_over_device(device_id)
    except HoneywellError as exc:
        raise HTTPException(502, f"Couldn't take over the thermostat: {exc}")
    # Reset any cooldown so the poller won't fight a manual action.
    with _sole_control_lock:
        _takeover_cooldown.pop(device_id, None)
    notify("info", "sole_control",
           f"App took control of {device_id} (permanent hold; onboard schedule suspended).")
    return {"ok": True, "taken_over": True}


@app.post("/api/devices/{device_id}/onboard_schedule/restore")
def api_restore_onboard(device_id: str):
    """Release one zone back to its onboard schedule (NoHold). Note: while Sole
    Controller mode is on, the next poll will take the zone back - turn that mode
    off first if you want the onboard schedule to run."""
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    loc = store.location_of(device_id)
    if loc is None:
        raise HTTPException(404, f"Unknown device {device_id}")
    try:
        with _device_lock(device_id):
            cached = store.get(device_id) or {}
            current_cv = cached.get("changeableValues") or {}
            # None -> set_thermostat fetches the live object when the cache is
            # empty, so a hold-only body can't be rejected.
            client.set_thermostat(device_id, loc, {"thermostatSetpointStatus": "NoHold"},
                                  current_changeable=current_cv or None)
            store.apply_local_override(device_id, {"thermostatSetpointStatus": "NoHold"})
            _refresh_one(device_id, loc)
    except HoneywellError as exc:
        raise HTTPException(502, f"Couldn't release the thermostat: {exc}")
    # Cool down so the poller doesn't immediately re-grab it within one interval
    # (it still will on a later poll if Sole Controller mode is on - by design).
    with _sole_control_lock:
        _takeover_cooldown[device_id] = time.time()
    notify("info", "sole_control", f"Released {device_id} to its onboard schedule.")
    return {"ok": True, "taken_over": False}


@app.get("/api/alerts")
def api_alerts(limit: int = Query(50, ge=1, le=200)):
    return {"alerts": store.alerts(limit=limit)}


@app.get("/api/schedules")
def api_schedules():
    return {"schedules": scheduler.list_rules() if scheduler else []}


@app.post("/api/schedules")
def api_add_schedule(rule: dict = Body(...)):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    # Saving a program updates the PLAN only - zones change at the scheduled
    # period times, never as a side effect of editing. `apply_now` is the
    # explicit opt-in ("also apply the current period now" in the dashboard);
    # it's popped so it never persists into the rule itself.
    apply_now = bool(rule.pop("apply_now", False))
    try:
        created = scheduler.add_rule(rule)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if apply_now and created.get("enabled", True):
        threading.Thread(target=scheduler.apply_active_now,
                         args=(created["id"],), daemon=True).start()
    return {"ok": True, "rule": created}


@app.put("/api/schedules/{rule_id}")
def api_update_schedule(rule_id: str, rule: dict = Body(...)):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    apply_now = bool(rule.pop("apply_now", False))
    try:
        updated = scheduler.update_rule(rule_id, rule)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if updated is None:
        raise HTTPException(404, "No such rule")
    if apply_now and updated.get("enabled", True):
        threading.Thread(target=scheduler.apply_active_now,
                         args=(rule_id,), daemon=True).start()
    return {"ok": True, "rule": updated}


@app.delete("/api/schedules/{rule_id}")
def api_delete_schedule(rule_id: str):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    ok = scheduler.remove_rule(rule_id)
    if not ok:
        raise HTTPException(404, "No such rule")
    return {"ok": True}


@app.post("/api/schedules/{rule_id}/enabled")
def api_toggle_schedule(rule_id: str, enabled: bool = Body(..., embed=True)):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    ok = scheduler.set_enabled(rule_id, enabled)
    if not ok:
        raise HTTPException(404, "No such rule")
    # Deliberately no immediate write here: program changes (including
    # re-enabling) follow the schedule - the zones change at the program's
    # period times. Startup and post-outage-restore still re-assert active
    # periods, because those catch up boundaries that fired while the app
    # was down (that's what keeps the schedule accurate).
    return {"ok": True}


# ---------------------------------------------------------- zone groups
# Named, reusable sets of zones. Pure picker convenience: the dashboard expands a
# group to concrete deviceIDs before sending them to any control/schedule
# endpoint, so nothing here touches the schedule/rotation/apply path.

@app.get("/api/groups")
def api_groups():
    return {"groups": groups.list_groups()}


@app.post("/api/groups")
def api_add_group(group: dict = Body(...)):
    try:
        created = groups.add(group)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "group": created}


@app.put("/api/groups/{group_id}")
def api_update_group(group_id: str, group: dict = Body(...)):
    try:
        updated = groups.update(group_id, group)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if updated is None:
        raise HTTPException(404, "No such group")
    return {"ok": True, "group": updated}


@app.delete("/api/groups/{group_id}")
def api_delete_group(group_id: str):
    if not groups.remove(group_id):
        raise HTTPException(404, "No such group")
    return {"ok": True}


# ---------------------------------------------------- automations (MQTT rules)

@app.get("/api/automations")
def api_automations():
    return {
        "automations": engine.list_rules() if engine else [],
        "status": engine.status() if engine else {},
        "mqtt_enabled": Config.MQTT_ENABLED,
        "mqtt_connected": bool(bridge and bridge.connected),
    }


@app.post("/api/automations")
def api_add_automation(rule: dict = Body(...)):
    if not engine:
        raise HTTPException(503, "Automation engine not ready")
    try:
        created = engine.add_rule(rule)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "rule": created}


@app.put("/api/automations/{rule_id}")
def api_update_automation(rule_id: str, rule: dict = Body(...)):
    if not engine:
        raise HTTPException(503, "Automation engine not ready")
    try:
        updated = engine.update_rule(rule_id, rule)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if updated is None:
        raise HTTPException(404, "No such automation")
    return {"ok": True, "rule": updated}


@app.delete("/api/automations/{rule_id}")
def api_delete_automation(rule_id: str):
    if not engine or not engine.remove_rule(rule_id):
        raise HTTPException(404, "No such automation")
    return {"ok": True}


@app.post("/api/automations/{rule_id}/enabled")
def api_toggle_automation(rule_id: str, enabled: bool = Body(..., embed=True)):
    if not engine or not engine.set_enabled(rule_id, enabled):
        raise HTTPException(404, "No such automation")
    return {"ok": True}


@app.post("/api/automations/{rule_id}/run")
def api_run_automation(rule_id: str):
    """Run an automation's actions now, ignoring the trigger. For testing."""
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    if not engine or not engine.run_rule_now(rule_id):
        raise HTTPException(404, "No such automation")
    return {"ok": True}


# --------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def index():
    html_doc = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html_doc)


# Serve any other static assets (none required, but handy for extension).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=Config.HOST, port=Config.PORT, reload=False)
