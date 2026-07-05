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

Safety-critical design choices (this path runs on generator power):

* The trigger's rising edge is latched only AFTER the actions succeed. If a shed
  partially fails (a zone unreachable, a 429), the rule is NOT marked "fired", so a
  re-announced "on" retries it instead of leaving non-critical zones running on the
  generator with the log falsely reporting success.

* Each rotation tick drives the FULL desired state (window -> on, everyone else ->
  off) rather than only flipping the members it believes changed. That guarantees
  no more than run_count zones are ever energized even if a zone drifted on (wall,
  Resideo app, a failed earlier write) or the window re-entered from a new outage.

* snapshot is non-clobbering and restore clears the snapshot on success, so a
  retained "on" replayed after a restart can't overwrite the good pre-outage
  snapshot with the already-shed state (which would drive every zone permanently
  Off on restore).

* All state is persisted atomically (storage.atomic_write_json) with a .bak
  fallback, so a crash/power-loss mid-write can't corrupt it.

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

How many units run at once (keep the group under the generator's limit) can be set
three composable ways on a `rotate` action:
  run_count    a fixed number of units on at a time.
  on_fraction  a fraction of the group, e.g. 0.5 = half on / half off (auto-adjusts
               if you add/remove zones).
  max_power    a power budget; with a per-unit `power` map (deviceID -> kW/amps/any
               consistent unit, default 1 each) the on-set is trimmed so its total
               draw stays under the budget. Units are added around the rotating
               window in order until the next one wouldn't fit.
Swaps are break-before-make (outgoing units off before incoming on) so a swap never
transiently exceeds the cap, and incoming units start one at a time to stagger
compressor inrush.
}

Legacy rules using a single {"topic", "match": {...}} trigger are migrated to the
one-condition shape automatically.

Injected dependencies (kept generic so this module doesn't import the API client):
  apply_fn(targets, values)      -> apply a control action; returns a list of
                                    deviceIDs that FAILED (empty = all applied).
  resolve_fn()                   -> list every known deviceID
  snapshot_read_fn(device_id)    -> current changeableValues dict for a device (or None)
  notify_fn(severity, kind, msg) -> raise an operator-facing alert
  on_restored(device_ids)        -> optional; called after a restore action fully
                                    succeeds. The app uses it to immediately
                                    re-assert daily programs' active periods, so
                                    zones resume the regular schedule the moment
                                    utility power returns (program boundaries that
                                    fired during the outage were skipped for
                                    rotated zones - without this, a restored zone
                                    would sit at its pre-outage setpoints until
                                    the NEXT boundary, potentially hours away).
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

from storage import atomic_write_json, load_json

log = logging.getLogger("honeywell.automation")

ApplyFn = Callable[[Any, dict], Any]
ResolveFn = Callable[[], list]
SnapshotReadFn = Callable[[str], Optional[dict]]
NotifyFn = Callable[[str, str, str], None]

# Compressors hate short-cycling. Refuse rotation intervals below this.
MIN_ROTATION_MINUTES = 5
# Fields we carry through a snapshot/restore. thermostatSetpointStatus is always
# captured so restore hands a zone back with exactly the hold it had.
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


def _dedupe(seq) -> list:
    """Order-preserving de-dup (rotation windows must not double-count a zone)."""
    return list(dict.fromkeys(seq))


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
        hourly_write_budget: Optional[int] = None,
        on_restored: Optional[Callable[[list], None]] = None,
    ):
        self.apply_fn = apply_fn
        self.resolve_fn = resolve_fn
        self.snapshot_read_fn = snapshot_read_fn
        self.notify_fn = notify_fn
        self.on_topics_changed = on_topics_changed
        self.on_restored = on_restored
        # The API-call budget shared with polling (Config.RL_HOURLY_CAP). Used
        # only to WARN when a rotation's implied write rate gets close to it -
        # limiter sleeps inside actions delay everything else the engine does.
        self._hourly_write_budget = hourly_write_budget

        self.rules_path = Path(rules_path)
        self.snapshots_path = Path(snapshots_path)
        self.trigger_state_path = Path(trigger_state_path)
        self.rotations_path = Path(rotations_path)

        self._lock = threading.RLock()
        self._rules: dict[str, dict] = {}
        self._snapshots: dict[str, dict[str, dict]] = {}   # name -> {deviceID: changeableValues}
        self._rotations: dict[str, dict] = {}              # rotation_id -> runtime state
        self._cond_state: dict[str, list] = {}             # rule_id -> [[matched, value] | None, ...]
        self._last_overall: dict[str, bool] = {}           # rule_id -> last combined result
        self._last_fired: dict[str, float] = {}            # rule_id -> ts
        # Serializes action execution (MQTT handler, "Run now", rotation ticks) so
        # a manual test can't clobber a real outage's snapshot/rotation midway.
        self._action_lock = threading.Lock()

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

    def active_rotation_targets(self) -> set[str]:
        """Every zone currently under an active duty-cycle rotation. The poller
        uses this to avoid re-energizing shed zones with a schedule assertion when
        it (re)starts mid-outage."""
        with self._lock:
            out: set[str] = set()
            for st in self._rotations.values():
                out.update(st.get("targets", []))
            return out

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
                targets = _dedupe(self._static_targets(a.get("targets")))
                if not a.get("targets"):
                    raise ValueError("rotate needs targets")
                # How many run at once can be given three (composable) ways:
                #   run_count   - a fixed number of units
                #   on_fraction - a fraction of the group (0.5 = half on/half off)
                #   max_power   - a power budget; with per-unit `power` weights the
                #                 on-set is trimmed so its total draw stays under it
                # At least one must be present; max_power/on_fraction default the
                # others sensibly. This keeps the group under the generator's limit.
                if "run_count" in a and int(a.get("run_count", 1)) < 1:
                    raise ValueError("rotate run_count must be >= 1")
                if "on_fraction" in a:
                    try:
                        f = float(a["on_fraction"])
                    except (TypeError, ValueError):
                        raise ValueError("rotate on_fraction must be a number in (0, 1]")
                    if not (0 < f <= 1):
                        raise ValueError("rotate on_fraction must be in (0, 1]")
                if "max_power" in a:
                    try:
                        if float(a["max_power"]) <= 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        raise ValueError("rotate max_power must be a positive number")
                power = a.get("power")
                if power is not None:
                    if not isinstance(power, dict):
                        raise ValueError("rotate power must be a map of deviceID -> power")
                    for k, v in power.items():
                        try:
                            if float(v) < 0:
                                raise ValueError
                        except (TypeError, ValueError):
                            raise ValueError(f"rotate power[{k}] must be a non-negative number")
                if int(a.get("interval_minutes", 0)) < MIN_ROTATION_MINUTES:
                    raise ValueError(f"rotate interval_minutes must be >= {MIN_ROTATION_MINUTES} "
                                     f"(protects compressors from short-cycling)")
                # Advisory: a big group on a short interval can consume most of the
                # hourly API budget (shared with polling), and limiter sleeps then
                # stall every other action. Warn, don't block.
                if self._hourly_write_budget:
                    n_est = len(targets) or len(self.resolve_fn() or [])
                    interval = max(int(a.get("interval_minutes", MIN_ROTATION_MINUTES) or
                                       MIN_ROTATION_MINUTES), 1)
                    per_hour = n_est * (60.0 / interval)
                    if n_est and per_hour > 0.8 * self._hourly_write_budget:
                        msg = (f"Rotation '{a.get('rotation_id')}' implies ~{int(per_hour)} control "
                               f"writes/hour across {n_est} zone(s) - close to the API budget "
                               f"({self._hourly_write_budget}/h, shared with polling). "
                               "Consider a longer swap interval.")
                        log.warning("%s", msg)
                        self.notify_fn("warning", "rotation_rate", msg)
                # A fixed run_count >= number of zones means nothing is ever shed -
                # warn (only meaningful for the plain count mode).
                if targets and "run_count" in a and "max_power" not in a \
                        and "on_fraction" not in a and int(a.get("run_count", 1)) >= len(targets):
                    log.warning("rotate '%s': run_count %s >= %d zones; no zones will be shed.",
                                a.get("rotation_id"), a.get("run_count"), len(targets))
            if at in ("snapshot", "restore") and not a.get("name"):
                raise ValueError(f"{at} needs a snapshot name")
            if at == "stop_rotation" and not a.get("rotation_id"):
                raise ValueError("stop_rotation needs a rotation_id")

    @staticmethod
    def _static_targets(targets: Any) -> list:
        if targets == "all" or targets is None:
            return []
        if isinstance(targets, str):
            return [targets]
        return list(targets)

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
            changed = False        # did anything persistence-relevant change?
            if not state or len(state) != len(conditions):
                state = [None] * len(conditions)
                changed = True
            # Update every condition that watches the topic this message arrived on.
            for i, cond in enumerate(conditions):
                if cond.get("topic") == topic:
                    matched, value = self._match(cond, payload)
                    # Only the MATCHED flag drives edge detection across restarts;
                    # the stored value is informational. Comparing just the flag
                    # keeps a chatty topic (a load % published every second) from
                    # forcing an fsync'd write per message - it persists only on
                    # threshold crossings.
                    if state[i] is None or bool(state[i][0]) != bool(matched):
                        changed = True
                    state[i] = [matched, value]
            mode = trig.get("mode", "all")
            seen = [s for s in state if s is not None]
            if mode == "any":
                overall = any(s[0] for s in seen)
            else:  # all
                overall = len(seen) == len(conditions) and all(s[0] for s in state)
            prev = self._last_overall.get(rid, False)
            before_latch = self._last_overall.get(rid)
            self._cond_state[rid] = state
            on_change = trig.get("retrigger", "on_change") == "on_change"
            # Decide whether to fire now.
            fire = overall and not (on_change and prev)
            # Commit the combined state EXCEPT the rising-edge latch: if we're about
            # to fire, leave _last_overall at prev until the actions actually succeed
            # so a failed shed retries on the next matching message.
            if not overall:
                self._last_overall[rid] = False
            elif not fire:
                self._last_overall[rid] = True
            if self._last_overall.get(rid) != before_latch:
                changed = True
        if changed:
            self._save_trigger_state()

        if not fire:
            return

        self._last_fired[rid] = time.time()
        log.info("Automation '%s' triggered (topic=%s payload=%r).", rid, topic, payload[:80])
        ok = self._run_actions(rule)
        with self._lock:
            # Latch the edge only if the actions succeeded; otherwise a re-announced
            # "on" will fire again (retry) rather than silently give up.
            self._last_overall[rid] = bool(ok)
        self._save_trigger_state()

    def _match(self, cond: dict, payload: str) -> tuple[bool, Any]:
        mtype = cond.get("type", "equals")
        raw = payload.strip()
        subject: Any = raw

        field = cond.get("field")
        if field:
            try:
                obj = json.loads(raw)
                for part in str(field).split("."):
                    if isinstance(obj, list):
                        obj = obj[int(part)]      # allow array indices in the dot-path
                    else:
                        obj = obj[part]
                subject = obj
            except (ValueError, KeyError, TypeError, IndexError):
                return (False, None)

        if mtype == "any":
            return (True, subject)

        target = cond.get("value")
        if mtype in ("gt", "lt", "between"):
            s = self._as_number(subject)
            if s is None:
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

        # For equality/contains, compare numerically when both sides are numbers so
        # a JSON field of 85 matches "85" / 85.0 (a common BMS payload mismatch).
        s_num, t_num = self._as_number(subject), self._as_number(target)
        s_str = self._stringify(subject)
        t_str = self._stringify(target)
        if cond.get("ignore_case", True):
            s_cmp, t_cmp = s_str.lower(), t_str.lower()
        else:
            s_cmp, t_cmp = s_str, t_str

        if mtype == "equals":
            if s_num is not None and t_num is not None:
                return (s_num == t_num, subject)
            return (s_cmp == t_cmp, subject)
        if mtype == "not_equals":
            if s_num is not None and t_num is not None:
                return (s_num != t_num, subject)
            return (s_cmp != t_cmp, subject)
        if mtype == "contains":
            return (t_cmp in s_cmp, subject)
        if mtype == "regex":
            flags = re.IGNORECASE if cond.get("ignore_case", True) else 0
            return (re.search(t_str, s_str, flags) is not None, subject)
        return (False, subject)

    @staticmethod
    def _as_number(v: Any) -> Optional[float]:
        if isinstance(v, bool):
            return None  # don't treat True/False as 1/0 for equality
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stringify(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

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

    def _run_actions(self, rule: dict) -> bool:
        """Run a rule's actions in order. Returns True only if every action fully
        succeeded (used to decide whether to latch the trigger's rising edge)."""
        summary = []
        all_ok = True
        with self._action_lock:
            for action in rule.get("actions", []):
                try:
                    text, ok = self._run_action(action)
                    summary.append(text)
                    all_ok = all_ok and ok
                except Exception as exc:
                    log.error("Action %s in '%s' failed: %s", action.get("type"), rule["id"], exc)
                    summary.append(f"{action.get('type')} FAILED")
                    all_ok = False
        severity = "info" if all_ok else "critical"
        self.notify_fn(severity, "automation",
                       f"{rule.get('name', rule['id'])}: " + "; ".join(summary))
        return all_ok

    def _resolve(self, targets: Any) -> list[str]:
        if targets == "all":
            return list(self.resolve_fn())
        if isinstance(targets, str):
            return [targets]
        return list(targets)

    def _apply(self, device_id: str, values: dict) -> list[str]:
        """Apply to one device; normalize the injected apply_fn's return into a
        list of failed deviceIDs (it may return None, a list, or raise)."""
        failed = self.apply_fn(device_id, dict(values))
        if failed:
            return list(failed) if not isinstance(failed, str) else [failed]
        return []

    def _run_action(self, action: dict) -> tuple[str, bool]:
        atype = action["type"]

        if atype == "set":
            ids = self._resolve(action.get("targets", "all"))
            values = action.get("values", {})
            failed: list[str] = []
            for did in ids:
                failed += self._apply(did, values)
            ok = not failed
            note = f"set {len(ids)} zone(s) -> {self._short(values)}"
            if failed:
                note += f" ({len(failed)} FAILED: {','.join(sorted(set(failed)))})"
            return note, ok

        if atype == "snapshot":
            name = action["name"]
            # Non-clobbering: if a snapshot under this name is already saved (an
            # outage in progress), keep the original pre-outage capture so a
            # retained "on" replay after a restart can't overwrite it with the
            # already-shed state.
            with self._lock:
                existing = self._snapshots.get(name)
            if existing:
                return f"snapshot '{name}' kept ({len(existing)} zone(s) already captured)", True
            ids = self._resolve(action.get("targets", "all"))
            snap = {}
            missing = []
            for did in ids:
                cv = self.snapshot_read_fn(did)
                if cv:
                    snap[did] = {k: cv.get(k) for k in _RESTORE_FIELDS if k in cv}
                else:
                    missing.append(did)
            with self._lock:
                self._snapshots[name] = snap
            self._save_snapshots()
            if missing:
                # Un-captured zones can't be restored later - make that visible now.
                self.notify_fn("warning", "snapshot_incomplete",
                               f"snapshot '{name}' could not capture {len(missing)} zone(s): "
                               f"{','.join(sorted(missing))}")
            ok = not missing
            return f"snapshot '{name}' ({len(snap)} zone(s))" + (f", {len(missing)} MISSING" if missing else ""), ok

        if atype == "restore":
            name = action["name"]
            with self._lock:
                snap = dict(self._snapshots.get(name, {}))
            if not snap:
                return f"restore '{name}' (nothing saved)", True
            failed = []
            for did, values in snap.items():
                failed += self._apply(did, values)
            if failed:
                # Keep the snapshot so a re-fire can retry the zones that didn't restore.
                self.notify_fn("critical", "restore_incomplete",
                               f"restore '{name}': {len(failed)} zone(s) did not restore: "
                               f"{','.join(sorted(set(failed)))}")
                return f"restore '{name}' ({len(snap)} zone(s), {len(failed)} FAILED)", False
            # Full success: clear the snapshot so the next outage captures fresh.
            with self._lock:
                self._snapshots.pop(name, None)
            self._save_snapshots()
            # Hand programmed zones back to their schedules NOW, not at the next
            # period boundary - boundaries that fired during the outage were
            # deliberately skipped for rotated zones. Fires only on FULL success
            # (a partial restore retries on the next matching message instead).
            if self.on_restored:
                try:
                    self.on_restored(list(snap.keys()))
                except Exception as exc:
                    log.error("on_restored hook failed after '%s': %s", name, exc)
                    self.notify_fn("critical", "restore_followup",
                                   f"Zones restored, but the post-restore schedule "
                                   f"re-assert failed: {exc}")
            return f"restore '{name}' ({len(snap)} zone(s))", True

        if atype == "rotate":
            return self._start_rotation(action), True

        if atype == "stop_rotation":
            rid = action["rotation_id"]
            self._stop_rotation(rid)
            return f"stopped rotation '{rid}'", True

        return f"noop({atype})", True

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
        targets = _dedupe(self._resolve(action["targets"]))
        n = len(targets)
        interval = max(MIN_ROTATION_MINUTES, int(action.get("interval_minutes", MIN_ROTATION_MINUTES)))
        on_values = action.get("on_values", {"mode": "Heat"})
        off_values = action.get("off_values", {"mode": "Off"})
        on_fraction = action.get("on_fraction")
        max_power = action.get("max_power")
        power = action.get("power") or {}

        # Resolve the count cap (how many units may run at once):
        #   explicit run_count wins; else on_fraction of the group; else if only a
        #   power budget is set let power be the sole limiter (cap = all units);
        #   else the legacy default of 1.
        if "run_count" in action and action["run_count"] is not None:
            run_count = int(action["run_count"])
        elif on_fraction is not None:
            run_count = round(n * float(on_fraction))
        elif max_power is not None:
            run_count = n
        else:
            run_count = 1
        run_count = max(1, min(run_count, n)) if n else 1
        max_power = float(max_power) if max_power is not None else None

        self._cancel_rotation_job(rid)
        with self._lock:
            self._rotations[rid] = {
                "targets": targets, "run_count": run_count, "interval": interval,
                "on_values": on_values, "off_values": off_values,
                "on_fraction": float(on_fraction) if on_fraction is not None else None,
                "max_power": max_power, "power": {k: float(v) for k, v in power.items()},
                "index": 0, "current_on": set(), "job_id": f"rotation:{rid}",
            }
        # First tick now (drives the full desired state), then every interval.
        # _run_actions already holds _action_lock, so go straight to the body -
        # _rotation_tick would deadlock re-acquiring the (non-reentrant) lock.
        self._advance_and_drive(rid)
        self._sched.add_job(
            self._rotation_tick, IntervalTrigger(minutes=interval),
            args=[rid], id=f"rotation:{rid}", replace_existing=True,
        )
        cap = f"{run_count}/{n}" + (f", <= {max_power} power" if max_power is not None else "")
        return f"rotate '{rid}': {cap} on, every {interval}m"

    def _window_at(self, state: dict, index: int) -> list:
        """The set of units that should be ON starting at `index`: a contiguous
        slice of the (rotating) target order, grown until either the count cap
        (run_count) or the power budget (max_power, summing per-unit `power`) would
        be exceeded. At least one unit is always included so we never run nothing.
        With no max_power this is exactly the run_count sliding window."""
        targets = list(state.get("targets", []))
        n = len(targets)
        if n == 0:
            return []
        cap = min(max(1, int(state.get("run_count", 1) or 1)), n)
        max_power = state.get("max_power")
        power = state.get("power") or {}
        window: list = []
        total = 0.0
        for k in range(n):
            if len(window) >= cap:
                break
            did = targets[(index + k) % n]
            w = float(power.get(did, 1))
            if window and max_power is not None and total + w > max_power:
                break  # adding this unit would blow the power budget
            window.append(did)
            total += w
            if max_power is not None and total >= max_power:
                break
        if max_power is not None and total > max_power and len(window) == 1:
            log.warning("Rotation '%s': unit %s alone draws %s > budget %s; running it anyway.",
                        state.get("job_id"), window[0], total, max_power)
        return window

    def _rotation_tick(self, rid: str) -> None:
        """Scheduled tick entry point. Serialized with rule actions via
        _action_lock: without it an in-flight tick could interleave with a
        concurrent stop_rotation + restore and land an Off write on a zone
        AFTER the restore already wrote it (leaving that zone off while the
        log reports a full restore)."""
        with self._action_lock:
            self._advance_and_drive(rid)

    def _advance_and_drive(self, rid: str) -> None:
        """Advance the window one step and drive the full desired state.
        Caller must hold _action_lock (the action runner already does; the
        scheduled tick acquires it in _rotation_tick)."""
        with self._lock:
            state = self._rotations.get(rid)
            if not state:
                return
            targets = list(state["targets"])
            if not targets:
                return
            idx = state["index"]
            window = self._window_at(state, idx)
            state["index"] = (idx + 1) % len(targets)
        self._drive_rotation(rid, window)

    def _drive_rotation(self, rid: str, window: list) -> None:
        """Command every rotation zone to its desired state: window -> on_values,
        all other targets -> off_values. Driving the FULL state (not just believed
        changes) guarantees the group never exceeds its count/power cap even if a
        unit drifted on or an earlier write failed.

        BREAK-BEFORE-MAKE: outgoing units are turned OFF *before* incoming units are
        turned ON, so a swap never transiently exceeds the cap (which on a maxed
        generator could trip it). Incoming units are then started one at a time
        (serialized by the client's rate limiter), staggering compressor inrush.

        If an OFF (break) write FAILS, that unit may still be running - energizing
        the full window on top of it could exceed the cap. Incoming units are held
        back (least-priority end of the window first) until the held-back draw
        covers the unconfirmed-off draw. Cap safety wins over immediacy: a
        held-back zone starts at the next swap, when the off is retried."""
        with self._lock:
            state = self._rotations.get(rid)
            if not state:
                return
            targets = list(state["targets"])
            on_values = dict(state["on_values"])
            off_values = dict(state["off_values"])
            power = dict(state.get("power") or {})
        window_set = set(window)
        off_list = [d for d in targets if d not in window_set]
        # Off first (break), then on (make).
        off_failed: list[str] = []
        for did in off_list:
            if not self._is_rotating(rid):   # a concurrent stop_rotation won -> bail
                return
            off_failed += self._apply(did, off_values)
        window_on = list(window)
        if off_failed:
            failed_draw = sum(float(power.get(d, 1)) for d in set(off_failed))
            held_back: list[str] = []
            held_draw = 0.0
            while window_on and held_draw < failed_draw:
                held = window_on.pop()
                held_back.append(held)
                held_draw += float(power.get(held, 1))
            self.notify_fn("warning", "rotation_degraded",
                           f"Rotation '{rid}': {len(set(off_failed))} zone(s) failed to switch "
                           f"off ({','.join(sorted(set(off_failed)))}); holding back "
                           f"{len(held_back)} incoming zone(s) "
                           f"({','.join(held_back) or 'none'}) to stay under the cap. "
                           "Retrying at the next swap.")
        on_failed: list[str] = []
        for did in window_on:
            if not self._is_rotating(rid):
                return
            on_failed += self._apply(did, on_values)
        actually_on = [d for d in window_on if d not in set(on_failed)]
        with self._lock:
            state = self._rotations.get(rid)
            if state is not None:
                # Record what we actually turned on (not what we intended), so the
                # status display and a restart's reconcile reflect reality.
                state["current_on"] = set(actually_on)
        if window or off_list:
            log.info("Rotation '%s': on=%s off=%s%s", rid, sorted(actually_on), sorted(off_list),
                     f" (off FAILED: {sorted(set(off_failed))})" if off_failed else "")
        self._save_rotations()

    def _is_rotating(self, rid: str) -> bool:
        with self._lock:
            return rid in self._rotations

    def _resume_rotations(self) -> None:
        """Re-arm interval jobs for rotations that were active before a restart and
        reconcile the physical state to the last-applied window (without advancing),
        so a restart mid-outage can't leave more than run_count zones energized."""
        with self._lock:
            items = list(self._rotations.items())
        for rid, st in items:
            interval = int(st.get("interval", MIN_ROTATION_MINUTES) or MIN_ROTATION_MINUTES)
            self._sched.add_job(
                self._rotation_tick, IntervalTrigger(minutes=interval),
                args=[rid], id=f"rotation:{rid}", replace_existing=True,
            )
            window = list(st.get("current_on") or self._window_at(st, int(st.get("index", 0) or 0)))
            log.info("Resumed rotation '%s' (every %dm, %d/%d running); reconciling.",
                     rid, interval, len(window), len(st.get("targets", [])))
            try:
                # Same serialization as a scheduled tick: don't interleave the
                # reconcile with rule actions that may already be running.
                with self._action_lock:
                    self._drive_rotation(rid, window)
            except Exception as exc:
                log.error("Reconciling rotation '%s' on resume failed: %s", rid, exc)

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
                 "interval_minutes": st["interval"],
                 "max_power": st.get("max_power"), "on_fraction": st.get("on_fraction"),
                 "on_power": round(sum(float((st.get("power") or {}).get(d, 1))
                                       for d in st["current_on"]), 3) if st.get("max_power") is not None else None}
                for rid, st in self._rotations.items()
            ]
            snapshots = {name: list(snap.keys()) for name, snap in self._snapshots.items()}
            last = dict(self._last_fired)
        return {"rotations": rotations, "snapshots": snapshots, "last_fired": last}

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        rules = load_json(self.rules_path)
        if isinstance(rules, list):
            loaded = 0
            for r in rules:
                try:
                    r = _normalize_rule(r)
                    self._validate(r)
                    self._rules[r["id"]] = r
                    loaded += 1
                except (ValueError, TypeError, KeyError) as exc:
                    log.warning("Skipping invalid automation %r: %s",
                                (r or {}).get("id") if isinstance(r, dict) else r, exc)
            log.info("Loaded %d automation(s).", loaded)

        snaps = load_json(self.snapshots_path)
        if isinstance(snaps, dict):
            self._snapshots = snaps

        raw = load_json(self.trigger_state_path)
        if isinstance(raw, dict):
            # A retained message replayed after a restart then reads as
            # "already seen", so an on_change rule won't spuriously re-fire.
            for rid, st in raw.items():
                if isinstance(st, dict) and "conds" in st:
                    self._cond_state[rid] = [list(c) if c is not None else None
                                             for c in st.get("conds", [])]
                    self._last_overall[rid] = bool(st.get("overall", False))

        rots = load_json(self.rotations_path)
        if isinstance(rots, dict):
            for rid, st in rots.items():
                if not isinstance(st, dict) or "targets" not in st:
                    log.warning("Skipping malformed rotation %r on load.", rid)
                    continue
                st["current_on"] = set(st.get("current_on", []))
                st.setdefault("index", 0)
                st.setdefault("run_count", 1)
                st.setdefault("interval", MIN_ROTATION_MINUTES)
                st.setdefault("on_values", {"mode": "Heat"})
                st.setdefault("off_values", {"mode": "Off"})
                st.setdefault("on_fraction", None)
                st.setdefault("max_power", None)
                st.setdefault("power", {})
                self._rotations[rid] = st
            if self._rotations:
                log.info("Loaded %d active rotation(s) to resume.", len(self._rotations))

    def _save_trigger_state(self) -> None:
        try:
            with self._lock:
                data = {rid: {"conds": [list(c) if c is not None else None for c in state],
                              "overall": self._last_overall.get(rid, False)}
                        for rid, state in self._cond_state.items()}
            atomic_write_json(self.trigger_state_path, data, default=str)
        except OSError as exc:  # pragma: no cover
            log.error("Could not save trigger state: %s", exc)

    def _save(self) -> None:
        try:
            with self._lock:
                data = list(self._rules.values())
            atomic_write_json(self.rules_path, data, indent=2)
        except OSError as exc:  # pragma: no cover
            log.error("Could not save automations: %s", exc)

    def _save_snapshots(self) -> None:
        try:
            with self._lock:
                data = {name: dict(snap) for name, snap in self._snapshots.items()}
            atomic_write_json(self.snapshots_path, data, indent=2)
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
            atomic_write_json(self.rotations_path, data, indent=2)
        except OSError as exc:  # pragma: no cover
            log.error("Could not save rotations: %s", exc)
