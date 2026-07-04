"""
automation.py
-------------
An event-driven rules engine. Inbound MQTT messages from YOUR broker (a generator
transfer switch, a power monitor, a BMS, anything) trigger coordinated actions
across the thermostats.

The motivating case: when the generator starts it publishes to a topic; we then
shed non-critical zones (turn them Off) and duty-cycle the critical ones so they
share the generator's limited capacity instead of all running at once. When utility
power returns, we restore every zone to exactly how it was before.

------------------------------------------------------------------ rule shape

{
  "id": "gen-on",
  "name": "Generator start: shed and rotate",
  "enabled": true,
  "trigger": {
    "topic": "facility/generator/status",       # exact MQTT topic to watch
    "match": {                                   # how to decide a message matches
      "type": "equals",                          # equals|not_equals|contains|regex|gt|lt|any
      "value": "on",
      "field": null,                             # optional JSON dot-path (e.g. "power.source")
      "ignore_case": true
    },
    "retrigger": "on_change"                     # on_change (edge) | every_message
  },
  "actions": [
    { "type": "snapshot", "name": "pre_gen", "targets": "all" },
    { "type": "set", "targets": ["TCC-A","TCC-B"], "values": {"mode": "Off"} },
    { "type": "rotate", "rotation_id": "critical",
      "targets": ["LCC-1","LCC-2","LCC-3","LCC-4"],
      "run_count": 2, "interval_minutes": 15,
      "on_values":  {"mode":"Heat","heatSetpoint":66,"thermostatSetpointStatus":"PermanentHold"},
      "off_values": {"mode":"Off"} }
  ]
}

Paired "power restored" rule:
  trigger match value "off"; actions:
    { "type": "stop_rotation", "rotation_id": "critical" },
    { "type": "restore", "name": "pre_gen" }

------------------------------------------------------------------ action types

  set          apply `values` (mode/heatSetpoint/coolSetpoint/thermostatSetpointStatus/fan)
               to `targets` ("all" | [deviceID,...] | "deviceID").
  snapshot     save current settings of `targets` under `name` so they can be restored.
  restore      restore devices saved in snapshot `name`.
  rotate       duty-cycle a group: keep `run_count` units running at a time, sliding the
               window every `interval_minutes`; on/off units get `on_values`/`off_values`.
  stop_rotation  stop the rotation with `rotation_id`.

Injected dependencies (kept generic so this module doesn't import the API client):
  apply_fn(targets, values)      -> apply a control action (app.apply_action)
  resolve_fn()                   -> list every known deviceID
  snapshot_read_fn(device_id)    -> current changeableValues dict for a device (or None)
  notify_fn(severity, kind, msg) -> raise an operator-facing alert
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger("honeywell.automation")

ApplyFn = Callable[[Any, dict], None]
ResolveFn = Callable[[], list]
SnapshotReadFn = Callable[[str], Optional[dict]]
NotifyFn = Callable[[str, str, str], None]

# Compressors hate short-cycling. Refuse rotation intervals below this.
MIN_ROTATION_MINUTES = 5
# Fields we carry through a snapshot/restore.
_RESTORE_FIELDS = ("mode", "heatSetpoint", "coolSetpoint",
                   "thermostatSetpointStatus", "autoChangeoverActive")


class AutomationEngine:
    def __init__(
        self,
        apply_fn: ApplyFn,
        resolve_fn: ResolveFn,
        snapshot_read_fn: SnapshotReadFn,
        notify_fn: NotifyFn,
        rules_path: str = "automations.json",
        snapshots_path: str = "snapshots.json",
        trigger_state_path: str = "trigger_state.json",
        on_topics_changed: Optional[Callable[[], None]] = None,
    ):
        self.apply_fn = apply_fn
        self.resolve_fn = resolve_fn
        self.snapshot_read_fn = snapshot_read_fn
        self.notify_fn = notify_fn
        self.on_topics_changed = on_topics_changed

        self.rules_path = Path(rules_path)
        self.snapshots_path = Path(snapshots_path)
        self.trigger_state_path = Path(trigger_state_path)

        self._lock = threading.Lock()
        self._rules: dict[str, dict] = {}
        self._snapshots: dict[str, dict[str, dict]] = {}   # name -> {deviceID: changeableValues}
        self._rotations: dict[str, dict] = {}              # rotation_id -> runtime state
        self._last_match: dict[str, tuple] = {}            # rule_id -> (matched, value)
        self._last_fired: dict[str, float] = {}            # rule_id -> ts

        self._sched = BackgroundScheduler()
        self._load()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._sched.start()
        log.info("Automation engine started with %d rule(s).", len(self._rules))

    def stop(self) -> None:
        for rid in list(self._rotations):
            self._cancel_rotation_job(rid)
        self._sched.shutdown(wait=False)

    def subscribed_topics(self) -> set[str]:
        with self._lock:
            return {r["trigger"]["topic"] for r in self._rules.values()
                    if r.get("enabled", True) and r.get("trigger", {}).get("topic")}

    # ------------------------------------------------------------- rule CRUD

    def list_rules(self) -> list[dict]:
        with self._lock:
            return list(self._rules.values())

    def add_rule(self, rule: dict) -> dict:
        rule = dict(rule)
        rule.setdefault("id", "auto-" + str(uuid.uuid4())[:6])
        rule.setdefault("enabled", True)
        self._validate(rule)
        with self._lock:
            self._rules[rule["id"]] = rule
            self._last_match.pop(rule["id"], None)
        self._save()
        if self.on_topics_changed:
            self.on_topics_changed()
        log.info("Added automation '%s' watching %s", rule["id"], rule["trigger"]["topic"])
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            if rule_id not in self._rules:
                return False
            self._rules.pop(rule_id)
            self._last_match.pop(rule_id, None)
        self._save()
        if self.on_topics_changed:
            self.on_topics_changed()
        return True

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule:
                return False
            rule["enabled"] = enabled
        self._save()
        if self.on_topics_changed:
            self.on_topics_changed()
        return True

    def _validate(self, rule: dict) -> None:
        trig = rule.get("trigger") or {}
        if not trig.get("topic"):
            raise ValueError("trigger.topic is required")
        match = trig.get("match") or {}
        mtype = match.get("type", "equals")
        if mtype not in ("equals", "not_equals", "contains", "regex", "gt", "lt", "any"):
            raise ValueError(f"unknown match type '{mtype}'")
        if mtype in ("equals", "not_equals", "contains", "regex", "gt", "lt") and "value" not in match:
            raise ValueError(f"match type '{mtype}' needs a value")
        if mtype == "regex":
            try:
                re.compile(str(match["value"]))
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}")
        actions = rule.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError("at least one action is required")
        for a in actions:
            at = a.get("type")
            if at not in ("set", "snapshot", "restore", "rotate", "stop_rotation"):
                raise ValueError(f"unknown action type '{at}'")
            if at == "rotate":
                if not a.get("targets"):
                    raise ValueError("rotate needs targets")
                if int(a.get("run_count", 1)) < 1:
                    raise ValueError("rotate run_count must be >= 1")
                if int(a.get("interval_minutes", 0)) < MIN_ROTATION_MINUTES:
                    raise ValueError(f"rotate interval_minutes must be >= {MIN_ROTATION_MINUTES} "
                                     f"(protects compressors from short-cycling)")
            if at in ("snapshot", "restore") and not a.get("name"):
                raise ValueError(f"{at} needs a snapshot name")
            if at == "stop_rotation" and not a.get("rotation_id"):
                raise ValueError("stop_rotation needs a rotation_id")

    # ------------------------------------------------------- message handling

    def handle_message(self, topic: str, payload: str) -> None:
        """Called by the MQTT bridge for every message on a subscribed trigger topic."""
        with self._lock:
            candidates = [r for r in self._rules.values()
                          if r.get("enabled", True) and r["trigger"]["topic"] == topic]
        for rule in candidates:
            try:
                self._evaluate(rule, payload)
            except Exception as exc:
                log.error("Automation '%s' failed: %s", rule.get("id"), exc)
                self.notify_fn("critical", "automation_error",
                               f"Automation '{rule.get('name', rule.get('id'))}' failed: {exc}")

    def _evaluate(self, rule: dict, payload: str) -> None:
        matched, value = self._match(rule["trigger"].get("match", {}), payload)
        rid = rule["id"]
        prev = self._last_match.get(rid)
        self._last_match[rid] = (matched, value)
        self._save_trigger_state()

        retrigger = rule["trigger"].get("retrigger", "on_change")
        if not matched:
            return
        if retrigger == "on_change":
            # Fire only on a rising edge into this matching value (avoids thrashing
            # when the source republishes the same status repeatedly).
            if prev is not None and prev[0] and prev[1] == value:
                return

        import time
        self._last_fired[rid] = time.time()
        log.info("Automation '%s' triggered (payload=%r).", rid, payload[:80])
        self._run_actions(rule)

    def _match(self, match: dict, payload: str) -> tuple[bool, Any]:
        mtype = match.get("type", "equals")
        raw = payload.strip()
        subject: Any = raw

        field = match.get("field")
        if field:
            try:
                obj = json.loads(raw)
                for part in str(field).split("."):
                    obj = obj[part]
                subject = obj
            except (ValueError, KeyError, TypeError):
                return (False, None)

        if mtype == "any":
            return (True, subject)

        target = match.get("value")
        if mtype in ("gt", "lt"):
            try:
                s = float(subject)
                t = float(target)
            except (TypeError, ValueError):
                return (False, subject)
            return ((s > t) if mtype == "gt" else (s < t), subject)

        s_str = str(subject)
        t_str = str(target)
        if match.get("ignore_case", True):
            s_cmp, t_cmp = s_str.lower(), t_str.lower()
        else:
            s_cmp, t_cmp = s_str, t_str

        if mtype == "equals":
            return (s_cmp == t_cmp, subject)
        if mtype == "not_equals":
            return (s_cmp != t_cmp, subject)
        if mtype == "contains":
            return (t_cmp in s_cmp, subject)
        if mtype == "regex":
            flags = re.IGNORECASE if match.get("ignore_case", True) else 0
            return (re.search(t_str, s_str, flags) is not None, subject)
        return (False, subject)

    # --------------------------------------------------------- action runner

    def run_rule_now(self, rule_id: str) -> bool:
        """Run a rule's actions immediately, ignoring the trigger. For UI testing."""
        with self._lock:
            rule = self._rules.get(rule_id)
        if not rule:
            return False
        log.info("Manually running automation '%s'.", rule_id)
        self._run_actions(rule)
        return True

    def _run_actions(self, rule: dict) -> None:
        summary = []
        for action in rule.get("actions", []):
            try:
                summary.append(self._run_action(action))
            except Exception as exc:
                log.error("Action %s in '%s' failed: %s", action.get("type"), rule["id"], exc)
                summary.append(f"{action.get('type')} FAILED")
        self.notify_fn("info", "automation",
                       f"{rule.get('name', rule['id'])}: " + "; ".join(summary))

    def _resolve(self, targets: Any) -> list[str]:
        if targets == "all":
            return list(self.resolve_fn())
        if isinstance(targets, str):
            return [targets]
        return list(targets)

    def _run_action(self, action: dict) -> str:
        atype = action["type"]

        if atype == "set":
            ids = self._resolve(action.get("targets", "all"))
            values = action.get("values", {})
            for did in ids:
                self.apply_fn(did, dict(values))
            return f"set {len(ids)} zone(s) -> {self._short(values)}"

        if atype == "snapshot":
            name = action["name"]
            ids = self._resolve(action.get("targets", "all"))
            snap = {}
            for did in ids:
                cv = self.snapshot_read_fn(did)
                if cv:
                    snap[did] = {k: cv.get(k) for k in _RESTORE_FIELDS if k in cv}
            with self._lock:
                self._snapshots[name] = snap
            self._save_snapshots()
            return f"snapshot '{name}' ({len(snap)} zone(s))"

        if atype == "restore":
            name = action["name"]
            with self._lock:
                snap = dict(self._snapshots.get(name, {}))
            if not snap:
                return f"restore '{name}' (nothing saved)"
            for did, values in snap.items():
                self.apply_fn(did, dict(values))
            return f"restore '{name}' ({len(snap)} zone(s))"

        if atype == "rotate":
            return self._start_rotation(action)

        if atype == "stop_rotation":
            rid = action["rotation_id"]
            self._stop_rotation(rid)
            return f"stopped rotation '{rid}'"

        return f"noop({atype})"

    @staticmethod
    def _short(values: dict) -> str:
        bits = []
        if values.get("mode"):
            bits.append(values["mode"])
        if values.get("heatSetpoint") is not None:
            bits.append(f"H{values['heatSetpoint']}")
        if values.get("coolSetpoint") is not None:
            bits.append(f"C{values['coolSetpoint']}")
        return " ".join(bits) or "(unchanged)"

    # -------------------------------------------------------------- rotation

    def _start_rotation(self, action: dict) -> str:
        rid = action.get("rotation_id") or ("rot-" + str(uuid.uuid4())[:6])
        targets = self._resolve(action["targets"])
        run_count = max(1, int(action.get("run_count", 1)))
        interval = max(MIN_ROTATION_MINUTES, int(action.get("interval_minutes", MIN_ROTATION_MINUTES)))
        on_values = action.get("on_values", {"mode": "Heat"})
        off_values = action.get("off_values", {"mode": "Off"})

        self._cancel_rotation_job(rid)
        with self._lock:
            self._rotations[rid] = {
                "targets": targets, "run_count": run_count, "interval": interval,
                "on_values": on_values, "off_values": off_values,
                "index": 0, "current_on": set(), "job_id": f"rotation:{rid}",
            }
        # First tick now, then every interval.
        self._rotation_tick(rid)
        self._sched.add_job(
            self._rotation_tick, IntervalTrigger(minutes=interval),
            args=[rid], id=f"rotation:{rid}", replace_existing=True,
        )
        return f"rotate '{rid}': {run_count}/{len(targets)} on, every {interval}m"

    def _rotation_tick(self, rid: str) -> None:
        with self._lock:
            state = self._rotations.get(rid)
            if not state:
                return
            targets = state["targets"]
            n = len(targets)
            if n == 0:
                return
            run_count = min(state["run_count"], n)
            idx = state["index"]
            window = {targets[(idx + k) % n] for k in range(run_count)}
            prev_on = state["current_on"]
            state["current_on"] = window
            state["index"] = (idx + 1) % n
            on_values = dict(state["on_values"])
            off_values = dict(state["off_values"])

        # Only flip devices whose membership changed (kind to the rate limit).
        turn_on = window - prev_on
        turn_off = prev_on - window
        for did in turn_on:
            self.apply_fn(did, dict(on_values))
        for did in turn_off:
            self.apply_fn(did, dict(off_values))
        if turn_on or turn_off:
            log.info("Rotation '%s': on=%s off=%s", rid, sorted(turn_on), sorted(turn_off))

    def _stop_rotation(self, rid: str) -> None:
        self._cancel_rotation_job(rid)
        with self._lock:
            self._rotations.pop(rid, None)

    def _cancel_rotation_job(self, rid: str) -> None:
        try:
            self._sched.remove_job(f"rotation:{rid}")
        except Exception:
            pass

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        with self._lock:
            rotations = [
                {"rotation_id": rid, "running": sorted(st["current_on"]),
                 "run_count": st["run_count"], "total": len(st["targets"]),
                 "interval_minutes": st["interval"]}
                for rid, st in self._rotations.items()
            ]
            snapshots = {name: list(snap.keys()) for name, snap in self._snapshots.items()}
            last = dict(self._last_fired)
        return {"rotations": rotations, "snapshots": snapshots, "last_fired": last}

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if self.rules_path.exists():
            try:
                for r in json.loads(self.rules_path.read_text()):
                    self._rules[r["id"]] = r
                log.info("Loaded %d automation(s).", len(self._rules))
            except (OSError, ValueError, KeyError) as exc:
                log.warning("Could not load automations: %s", exc)
        if self.snapshots_path.exists():
            try:
                self._snapshots = json.loads(self.snapshots_path.read_text())
            except (OSError, ValueError) as exc:
                log.warning("Could not load snapshots: %s", exc)
        if self.trigger_state_path.exists():
            try:
                raw = json.loads(self.trigger_state_path.read_text())
                # A retained "on" message replayed after a restart then reads as
                # "already seen", so an on_change rule won't spuriously re-fire.
                self._last_match = {k: (v[0], v[1]) for k, v in raw.items()}
            except (OSError, ValueError, IndexError) as exc:
                log.warning("Could not load trigger state: %s", exc)

    def _save_trigger_state(self) -> None:
        try:
            data = {k: [m, v] for k, (m, v) in self._last_match.items()}
            self.trigger_state_path.write_text(json.dumps(data, default=str))
        except OSError as exc:  # pragma: no cover
            log.error("Could not save trigger state: %s", exc)

    def _save(self) -> None:
        try:
            self.rules_path.write_text(json.dumps(list(self._rules.values()), indent=2))
        except OSError as exc:  # pragma: no cover
            log.error("Could not save automations: %s", exc)

    def _save_snapshots(self) -> None:
        try:
            self.snapshots_path.write_text(json.dumps(self._snapshots, indent=2))
        except OSError as exc:  # pragma: no cover
            log.error("Could not save snapshots: %s", exc)
