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

A rule has a trigger (one or more conditions combined with AND/OR) and an ordered
list of actions:

{
  "id": "gen-on",
  "name": "Generator start: shed and rotate",
  "enabled": true,
  "trigger": {
    "mode": "all",                               # all = AND, any = OR
    "conditions": [
      { "topic": "facility/generator/status",    # MQTT topic to watch
        "type": "equals",                        # equals|not_equals|contains|regex|gt|lt|between|any
        "value": "on",
        "value2": null,                          # upper bound for "between"
        "field": null,                           # optional JSON dot-path (e.g. "power.source")
        "ignore_case": true }
    ],
    "retrigger": "on_change"                      # on_change (edge) | every_message
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

A rule fires when its condition combination becomes true: with mode "all" every
condition must currently match (each condition remembers the last message seen on
its topic); with mode "any" a single matching condition is enough. "on_change"
fires on the rising edge into true; "every_message" fires on each matching message.

Legacy rules using a single {"topic", "match": {...}} trigger are migrated to the
one-condition shape automatically.

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
import time
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

_MATCH_TYPES = ("equals", "not_equals", "contains", "regex", "gt", "lt", "between", "any")
_ACTION_TYPES = ("set", "snapshot", "restore", "rotate", "stop_rotation")


def _normalize_rule(rule: dict) -> dict:
    """Return a copy of `rule` with a conditions[] trigger, migrating a legacy
    single {topic, match} trigger into a one-condition rule."""
    rule = dict(rule)
    trig = dict(rule.get("trigger") or {})
    if "conditions" not in trig:
        match = dict(trig.get("match") or {})
        cond = {"topic": trig.get("topic")}
        cond.update(match)
        trig = {"mode": "all", "conditions": [cond],
                "retrigger": trig.get("retrigger", "on_change")}
    trig.setdefault("mode", "all")
    trig.setdefault("retrigger", "on_change")
    trig.pop("topic", None)
    trig.pop("match", None)
    rule["trigger"] = trig
    return rule


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
        rotations_path: str = "rotations.json",
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
        self.rotations_path = Path(rotations_path)

        self._lock = threading.Lock()
        self._rules: dict[str, dict] = {}
        self._snapshots: dict[str, dict[str, dict]] = {}   # name -> {deviceID: changeableValues}
        self._rotations: dict[str, dict] = {}              # rotation_id -> runtime state
        self._cond_state: dict[str, list] = {}             # rule_id -> [[matched, value] | None, ...]
        self._last_overall: dict[str, bool] = {}           # rule_id -> last combined result
        self._last_fired: dict[str, float] = {}            # rule_id -> ts

        self._sched = BackgroundScheduler()
        self._load()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._sched.start()
        self._resume_rotations()
        log.info("Automation engine started with %d rule(s).", len(self._rules))

    def stop(self) -> None:
        for rid in list(self._rotations):
            self._cancel_rotation_job(rid)
        self._sched.shutdown(wait=False)

    def subscribed_topics(self) -> set[str]:
        with self._lock:
            topics: set[str] = set()
            for r in self._rules.values():
                if not r.get("enabled", True):
                    continue
                for cond in r.get("trigger", {}).get("conditions", []):
                    if cond.get("topic"):
                        topics.add(cond["topic"])
            return topics

    # ------------------------------------------------------------- rule CRUD

    def list_rules(self) -> list[dict]:
        with self._lock:
            return list(self._rules.values())

    def add_rule(self, rule: dict) -> dict:
        rule = _normalize_rule(rule)
        rule.setdefault("id", "auto-" + str(uuid.uuid4())[:6])
        rule.setdefault("enabled", True)
        self._validate(rule)
        with self._lock:
            self._rules[rule["id"]] = rule
            self._cond_state.pop(rule["id"], None)
            self._last_overall.pop(rule["id"], None)
        self._save()
        if self.on_topics_changed:
            self.on_topics_changed()
        log.info("Added automation '%s' (%d condition(s))",
                 rule["id"], len(rule["trigger"]["conditions"]))
        return rule

    def update_rule(self, rule_id: str, rule: dict) -> dict | None:
        with self._lock:
            exists = rule_id in self._rules
            prev_enabled = self._rules.get(rule_id, {}).get("enabled", True)
        if not exists:
            return None
        merged = _normalize_rule(rule)
        merged["id"] = rule_id
        merged.setdefault("enabled", prev_enabled)
        self._validate(merged)
        with self._lock:
            self._rules[rule_id] = merged
            # A changed trigger should re-evaluate from a clean slate.
            self._cond_state.pop(rule_id, None)
            self._last_overall.pop(rule_id, None)
        self._save()
        if self.on_topics_changed:
            self.on_topics_changed()
        log.info("Updated automation '%s'", rule_id)
        return merged

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            if rule_id not in self._rules:
                return False
            self._rules.pop(rule_id)
            self._cond_state.pop(rule_id, None)
            self._last_overall.pop(rule_id, None)
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
        if trig.get("mode", "all") not in ("all", "any"):
            raise ValueError("trigger.mode must be 'all' or 'any'")
        conditions = trig.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("at least one trigger condition is required")
        for c in conditions:
            if not c.get("topic"):
                raise ValueError("each condition needs a topic")
            mtype = c.get("type", "equals")
            if mtype not in _MATCH_TYPES:
                raise ValueError(f"unknown match type '{mtype}'")
            if mtype in ("equals", "not_equals", "contains", "regex", "gt", "lt", "between") \
                    and "value" not in c:
                raise ValueError(f"match type '{mtype}' needs a value")
            if mtype == "between":
                try:
                    float(c["value"]); float(c.get("value2"))
                except (TypeError, ValueError):
                    raise ValueError("'between' needs numeric value and value2")
            if mtype == "regex":
                try:
                    re.compile(str(c["value"]))
                except re.error as exc:
                    raise ValueError(f"invalid regex: {exc}")
        actions = rule.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError("at least one action is required")
        for a in actions:
            at = a.get("type")
            if at not in _ACTION_TYPES:
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
                          if r.get("enabled", True)
                          and any(c.get("topic") == topic for c in r["trigger"]["conditions"])]
        for rule in candidates:
            try:
                self._evaluate(rule, topic, payload)
            except Exception as exc:
                log.error("Automation '%s' failed: %s", rule.get("id"), exc)
                self.notify_fn("critical", "automation_error",
                               f"Automation '{rule.get('name', rule.get('id'))}' failed: {exc}")

    def _evaluate(self, rule: dict, topic: str, payload: str) -> None:
        rid = rule["id"]
        trig = rule["trigger"]
        conditions = trig["conditions"]
        with self._lock:
            state = self._cond_state.get(rid)
            if not state or len(state) != len(conditions):
                state = [None] * len(conditions)
            # Update every condition that watches the topic this message arrived on.
            for i, cond in enumerate(conditions):
                if cond.get("topic") == topic:
                    matched, value = self._match(cond, payload)
                    state[i] = [matched, value]
            mode = trig.get("mode", "all")
            seen = [s for s in state if s is not None]
            if mode == "any":
                overall = any(s[0] for s in seen)
            else:  # all
                overall = len(seen) == len(conditions) and all(s[0] for s in state)
            prev = self._last_overall.get(rid, False)
            self._cond_state[rid] = state
            self._last_overall[rid] = overall
        self._save_trigger_state()

        if not overall:
            return
        if trig.get("retrigger", "on_change") == "on_change" and prev:
            return  # already true; wait for a falling edge before firing again

        self._last_fired[rid] = time.time()
        log.info("Automation '%s' triggered (topic=%s payload=%r).", rid, topic, payload[:80])
        self._run_actions(rule)

    def _match(self, cond: dict, payload: str) -> tuple[bool, Any]:
        mtype = cond.get("type", "equals")
        raw = payload.strip()
        subject: Any = raw

        field = cond.get("field")
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

        target = cond.get("value")
        if mtype in ("gt", "lt", "between"):
            try:
                s = float(subject)
            except (TypeError, ValueError):
                return (False, subject)
            if mtype == "between":
                try:
                    lo, hi = float(cond.get("value")), float(cond.get("value2"))
                except (TypeError, ValueError):
                    return (False, subject)
                if lo > hi:
                    lo, hi = hi, lo
                return (lo <= s <= hi, subject)
            try:
                t = float(target)
            except (TypeError, ValueError):
                return (False, subject)
            return ((s > t) if mtype == "gt" else (s < t), subject)

        s_str = str(subject)
        t_str = str(target)
        if cond.get("ignore_case", True):
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
            flags = re.IGNORECASE if cond.get("ignore_case", True) else 0
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
        # First tick now (also persists state), then every interval.
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
        # Persist the advanced window/index so a restart resumes where we left off.
        self._save_rotations()

    def _resume_rotations(self) -> None:
        """Re-arm interval jobs for rotations that were active before a restart.
        Zones are already in their last-applied state, so we do NOT tick
        immediately - we just keep advancing the window on schedule. The paired
        'restore on utility' rule still stops the rotation when power returns."""
        with self._lock:
            items = list(self._rotations.items())
        for rid, st in items:
            self._sched.add_job(
                self._rotation_tick, IntervalTrigger(minutes=st["interval"]),
                args=[rid], id=f"rotation:{rid}", replace_existing=True,
            )
            log.info("Resumed rotation '%s' (every %dm, %d/%d running).",
                     rid, st["interval"], len(st["current_on"]), len(st["targets"]))

    def _stop_rotation(self, rid: str) -> None:
        self._cancel_rotation_job(rid)
        with self._lock:
            self._rotations.pop(rid, None)
        self._save_rotations()

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
                    r = _normalize_rule(r)
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
                # A retained message replayed after a restart then reads as
                # "already seen", so an on_change rule won't spuriously re-fire.
                for rid, st in raw.items():
                    if isinstance(st, dict) and "conds" in st:
                        self._cond_state[rid] = [tuple(c) if c is not None else None
                                                 for c in st.get("conds", [])]
                        self._last_overall[rid] = bool(st.get("overall", False))
            except (OSError, ValueError, TypeError, IndexError) as exc:
                log.warning("Could not load trigger state: %s", exc)
        if self.rotations_path.exists():
            try:
                raw = json.loads(self.rotations_path.read_text())
                for rid, st in raw.items():
                    st["current_on"] = set(st.get("current_on", []))
                    self._rotations[rid] = st
                if self._rotations:
                    log.info("Loaded %d active rotation(s) to resume.", len(self._rotations))
            except (OSError, ValueError) as exc:
                log.warning("Could not load rotations: %s", exc)

    def _save_trigger_state(self) -> None:
        try:
            with self._lock:
                data = {rid: {"conds": [list(c) if c is not None else None for c in state],
                              "overall": self._last_overall.get(rid, False)}
                        for rid, state in self._cond_state.items()}
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

    def _save_rotations(self) -> None:
        """Persist active rotations so they resume across a restart. Sets are
        serialized as sorted lists and rehydrated in _load()."""
        try:
            with self._lock:
                data = {}
                for rid, st in self._rotations.items():
                    d = dict(st)
                    d["current_on"] = sorted(st["current_on"])
                    data[rid] = d
            self.rotations_path.write_text(json.dumps(data, indent=2))
        except OSError as exc:  # pragma: no cover
            log.error("Could not save rotations: %s", exc)
