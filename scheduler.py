"""
scheduler.py
------------
Application-level scheduling for the whole facility.

Why application-level instead of the thermostats' onboard 7-day schedule:
  * It works for every device, including older round (TCC-) units that have no
    onboard schedule.
  * It lets you write facility-wide programs ("all zones to 62 at 7pm weekdays",
    "warehouse ON at 6am / OFF at 10pm") in one place instead of programming each
    thermostat.
  * You keep the rules; they're versioned in schedules.json.

A schedule rule is a *daily program*: one target + set of days, plus one or more
time **periods**, each with its own action. A period that sets mode "Off" is a
"daily OFF"; a period that sets mode + setpoints is a "daily ON" or a timed
temperature change. A simple one-time rule is just a program with a single period.

  {
    "id": "weekday-program",
    "name": "Weekday program",
    "enabled": true,
    "days": ["mon","tue","wed","thu","fri"],   # omit/empty = every day
    "targets": "all",              # "all" | "deviceID" | [deviceID, ...] (any group)
    "periods": [
      {"time": "06:00", "action": {"mode": "Heat", "heatSetpoint": 70,
                                    "coolSetpoint": 76, "thermostatSetpointStatus": "PermanentHold"}},
      {"time": "22:00", "action": {"mode": "Off"}}
    ]
  }

An action may include: mode, heatSetpoint, coolSetpoint, thermostatSetpointStatus,
and fan. The action-applying function is injected: apply(targets, action) -> None.

Legacy rules using a single top-level {"time", "action"} are migrated to a
one-period program automatically on load and on create.
"""

from __future__ import annotations

import datetime
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from storage import atomic_write_json, load_json

log = logging.getLogger("honeywell.scheduler")

ApplyFn = Callable[[Any, dict], None]

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _valid_hhmm(value: Any) -> tuple[int, int]:
    parts = str(value).split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError("time must be 'HH:MM' (24-hour)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time hour must be 0-23 and minute 0-59")
    return hour, minute


def _normalize(rule: dict) -> dict:
    """Return a copy of `rule` in the periods[] shape, migrating a legacy
    single {time, action} rule into a one-period program."""
    rule = dict(rule)
    if "periods" not in rule and ("time" in rule or "action" in rule):
        rule["periods"] = [{"time": rule.get("time"), "action": rule.get("action", {})}]
    rule.pop("time", None)
    rule.pop("action", None)
    # Group targets: de-dup a list while keeping order, so a zone accidentally
    # picked twice isn't written twice per period.
    if isinstance(rule.get("targets"), list):
        rule["targets"] = list(dict.fromkeys(rule["targets"]))
    # Sort periods by time of day for a tidy, predictable display.
    periods = rule.get("periods") or []
    try:
        periods = sorted(periods, key=lambda p: _valid_hhmm(p.get("time")))
    except ValueError:
        pass  # validation will surface the bad value with a clear message
    rule["periods"] = periods
    return rule


class FacilityScheduler:
    def __init__(self, apply_fn: ApplyFn, store_path: str = "schedules.json",
                 timezone: str | None = None):
        self.apply_fn = apply_fn
        self.store_path = Path(store_path)
        self._rules: dict[str, dict] = {}
        self._lock = threading.RLock()
        # A typo in SCHEDULE_TZ must degrade to server-local time (loudly), not
        # crash the whole app into a systemd restart loop - one bad env var would
        # otherwise take down all climate control, not just the schedules.
        self.timezone_error: Optional[str] = None
        if timezone:
            try:
                self._sched = BackgroundScheduler(timezone=timezone)
            except Exception as exc:
                log.error("Invalid SCHEDULE_TZ %r (%s); falling back to the server's "
                          "local timezone.", timezone, exc)
                self.timezone_error = (f"SCHEDULE_TZ {timezone!r} is invalid - schedule times "
                                       "are running in the SERVER'S local timezone until it is "
                                       "fixed (use an IANA name like America/New_York)")
                self._sched = BackgroundScheduler()
        else:
            self._sched = BackgroundScheduler()
        self._load()

    def start(self) -> None:
        self._sched.start()
        with self._lock:
            rules = list(self._rules.values())
        for rule in rules:
            if rule.get("enabled", True):
                # One bad rule must never stop the scheduler (and thus the whole
                # app under systemd Restart=always) from starting.
                try:
                    self._schedule(rule)
                except Exception as exc:
                    log.error("Could not schedule program '%s': %s", rule.get("id"), exc)
        # Log the effective timezone + local time so a wrong-timezone misconfig
        # (schedules firing hours off because SCHEDULE_TZ is unset and the server
        # is UTC) is obvious in the logs instead of a mystery.
        log.info("Scheduler started with %d program(s). Timezone=%s, local time now=%s",
                 len(rules), self.timezone_name(), self._now().strftime("%Y-%m-%d %H:%M %Z"))

    def timezone_name(self) -> str:
        """The timezone program times are interpreted in (from SCHEDULE_TZ, or the
        server's local zone if unset). Surfaced so an operator can confirm at a
        glance that '22:00 Off' means 22:00 in the RIGHT timezone."""
        tz = getattr(self._sched, "timezone", None)
        return str(tz) if tz else "system-local"

    def stop(self) -> None:
        self._sched.shutdown(wait=False)

    # ------------------------------------------------------------- rule CRUD

    def list_rules(self) -> list[dict]:
        with self._lock:
            return list(self._rules.values())

    def add_rule(self, rule: dict) -> dict:
        rule = _normalize(rule)
        rule.setdefault("id", str(uuid.uuid4())[:8])
        rule.setdefault("enabled", True)
        self._validate(rule)
        with self._lock:
            self._rules[rule["id"]] = rule
        self._save()
        if rule["enabled"]:
            self._schedule(rule)
        log.info("Added schedule program '%s' (%d period(s))", rule["id"], len(rule["periods"]))
        return rule

    def update_rule(self, rule_id: str, rule: dict) -> dict | None:
        with self._lock:
            if rule_id not in self._rules:
                return None
            prev_enabled = self._rules[rule_id].get("enabled", True)
        merged = _normalize(rule)
        merged["id"] = rule_id
        merged.setdefault("enabled", prev_enabled)
        self._validate(merged)
        with self._lock:
            self._rules[rule_id] = merged
        self._save()
        self._unschedule(rule_id)
        if merged["enabled"]:
            self._schedule(merged)
        log.info("Updated schedule program '%s'", rule_id)
        return merged

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            if rule_id not in self._rules:
                return False
            self._rules.pop(rule_id)
        self._unschedule(rule_id)
        self._save()
        log.info("Removed schedule program '%s'", rule_id)
        return True

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule:
                return False
            rule["enabled"] = enabled
        self._save()
        self._unschedule(rule_id)
        if enabled:
            self._schedule(rule)
        return True

    # ------------------------------------------------------------- internals

    def _validate(self, rule: dict) -> None:
        rid = rule.get("id")
        if rid is not None and "#" in str(rid):
            # '#' is our job-id separator; an id containing it corrupts _unschedule.
            raise ValueError("rule id must not contain '#'")
        periods = rule.get("periods")
        if not isinstance(periods, list) or not periods:
            raise ValueError("a program needs at least one time period")
        seen_times = set()
        for p in periods:
            hhmm = _valid_hhmm(p.get("time"))
            if hhmm in seen_times:
                raise ValueError(f"duplicate period time {p.get('time')} in one program")
            seen_times.add(hhmm)
            if not isinstance(p.get("action"), dict) or not p.get("action"):
                raise ValueError("each period needs an action")
        days = rule.get("days")
        if days:
            bad = [d for d in days if str(d).lower() not in _DAYS]
            if bad:
                raise ValueError(f"invalid days: {bad}")
        # Targets: "all", one deviceID, or a group (non-empty list of deviceIDs).
        # An empty list would make every period a silent no-op - reject it.
        targets = rule.get("targets", "all")
        if targets != "all":
            if isinstance(targets, str):
                if not targets.strip():
                    raise ValueError("targets must be 'all', a deviceID, or a list of deviceIDs")
            elif isinstance(targets, list):
                if not targets or not all(isinstance(t, str) and t.strip() for t in targets):
                    raise ValueError("targets must be a non-empty list of deviceIDs")
            else:
                raise ValueError("targets must be 'all', a deviceID, or a list of deviceIDs")

    def _trigger(self, time_str: str, days) -> CronTrigger:
        hour, minute = _valid_hhmm(time_str)
        kwargs: dict[str, Any] = {"hour": hour, "minute": minute}
        if days:
            kwargs["day_of_week"] = ",".join(str(d).lower() for d in days)
        # Pin the trigger to the scheduler's timezone. A bare CronTrigger defaults
        # to the *host* zone, which silently diverges from _active_period (which
        # uses the scheduler tz) whenever SCHEDULE_TZ differs from the host clock.
        tz = getattr(self._sched, "timezone", None)
        if tz is not None:
            kwargs["timezone"] = tz
        return CronTrigger(**kwargs)

    def _job_id(self, rule_id: str, idx: int) -> str:
        return f"{rule_id}#{idx}"

    def _schedule(self, rule: dict) -> None:
        self._unschedule(rule["id"])
        days = rule.get("days")
        for idx, period in enumerate(rule["periods"]):
            self._sched.add_job(
                self._run_period, self._trigger(period["time"], days),
                args=[rule["id"], idx], id=self._job_id(rule["id"], idx),
                replace_existing=True,
            )

    def _unschedule(self, rule_id: str) -> None:
        prefix = rule_id + "#"
        for job in self._sched.get_jobs():
            if job.id == rule_id or job.id.startswith(prefix):
                try:
                    self._sched.remove_job(job.id)
                except Exception:
                    pass

    def _run_period(self, rule_id: str, idx: int) -> None:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule or not rule.get("enabled", True):
                return
            try:
                period = rule["periods"][idx]
            except (IndexError, KeyError):
                return
            targets = rule.get("targets", "all")
            action = dict(period.get("action") or {})
            name = rule.get("name")
            time_str = period.get("time")
        log.info("Running schedule '%s' period %s (%s @ %s)", rule_id, idx, name, time_str)
        try:
            self.apply_fn(targets, action)
        except Exception as exc:
            log.error("Schedule '%s' period %s failed: %s", rule_id, idx, exc)

    # ---------------------------------------------- enforce (source of truth)

    def _now(self) -> datetime.datetime:
        tz = getattr(self._sched, "timezone", None)
        try:
            return datetime.datetime.now(tz) if tz else datetime.datetime.now()
        except Exception:
            return datetime.datetime.now()

    def _active_period_at(self, rule: dict,
                          now: datetime.datetime) -> tuple[dict | None, datetime.datetime | None]:
        """The period a program says should be in effect *now*, plus the datetime
        that period's boundary last occurred — the most recent boundary that has
        already passed on an active day, looking back up to a week. The boundary
        time lets the enforcement pass decide, when two programs cover one zone,
        which one most recently took effect (so a weekday program's this-morning
        boundary wins over a weekend program's carried-over Sunday-night one).
        Returns (None, None) if the program has no periods."""
        periods = rule.get("periods") or []
        if not periods:
            return (None, None)
        days = [str(d).lower() for d in (rule.get("days") or [])]
        dow = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        def pmin(p):
            h, m = _valid_hhmm(p.get("time"))
            return h * 60 + m
        ordered = sorted(periods, key=pmin)
        now_min = now.hour * 60 + now.minute
        for back in range(0, 8):
            day_idx = (now.weekday() - back) % 7
            if days and dow[day_idx] not in days:
                continue
            period = None
            if back == 0:
                passed = [p for p in ordered if pmin(p) <= now_min]
                if passed:
                    period = passed[-1]
                # else nothing yet today; keep walking back to a prior active day
            else:
                period = ordered[-1]
            if period is not None:
                h, m = _valid_hhmm(period.get("time"))
                boundary = (now - datetime.timedelta(days=back)).replace(
                    hour=h, minute=m, second=0, microsecond=0)
                return (period, boundary)
        return (None, None)

    def _active_period(self, rule: dict, now: datetime.datetime) -> dict | None:
        """The period whose setpoints a program says should be in effect *now*."""
        return self._active_period_at(rule, now)[0]

    def resolve_desired(self, all_device_ids):
        """Resolve, per zone, the single action that should be in effect right now
        across ALL enabled programs — for the enforcement pass. When two programs
        cover the same zone, the one whose boundary occurred MOST RECENTLY wins
        (so today's program beats an off-day program that merely carried over).
        A zone whose two most-recent programs land on the SAME boundary time but
        disagree is reported as a conflict and left OUT of the desired map, rather
        than guessed.

        Returns (desired, conflicts):
          desired   = {deviceID: (action, program_name)}
          conflicts = [{"zone": deviceID, "programs": [name, ...]}]
        """
        with self._lock:
            rules = [dict(r) for r in self._rules.values() if r.get("enabled", True)]
        now = self._now()
        per_zone: dict[str, list] = {}
        for rule in rules:
            try:
                period, boundary = self._active_period_at(rule, now)
            except ValueError:
                continue
            if not period or boundary is None:
                continue
            action = dict(period.get("action") or {})
            if not action:
                continue
            targets = rule.get("targets", "all")
            if targets == "all":
                zone_ids = list(all_device_ids)
            elif isinstance(targets, str):
                zone_ids = [targets]
            else:
                zone_ids = list(targets)
            name = rule.get("name") or rule.get("id")
            for zid in zone_ids:
                per_zone.setdefault(zid, []).append((boundary, action, name))

        desired: dict[str, tuple] = {}
        conflicts: list[dict] = []
        for zid, entries in per_zone.items():
            entries.sort(key=lambda e: e[0], reverse=True)   # most-recent boundary first
            top_time = entries[0][0]
            top = [e for e in entries if e[0] == top_time]
            distinct_actions = []
            for _, action, _name in top:
                if action not in distinct_actions:
                    distinct_actions.append(action)
            if len(distinct_actions) == 1:
                desired[zid] = (top[0][1], top[0][2])
            else:
                conflicts.append({"zone": zid,
                                  "programs": sorted({e[2] for e in top})})
        return desired, conflicts

    def apply_active_now(self, rule_id: str) -> bool:
        """Apply the program's currently-active period right now, so it takes
        control immediately on create/edit (not only at the next boundary)."""
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule or not rule.get("enabled", True):
                return False
            rule = dict(rule)
        try:
            period = self._active_period(rule, self._now())
        except ValueError as exc:
            log.error("Program '%s' has a bad period time: %s", rule_id, exc)
            return False
        if not period:
            return False
        log.info("Asserting program '%s' active period (%s).", rule_id, period.get("time"))
        try:
            self.apply_fn(rule.get("targets", "all"), dict(period.get("action") or {}))
            return True
        except Exception as exc:
            log.error("Asserting program '%s' failed: %s", rule_id, exc)
            return False

    def apply_all_active_now(self, all_device_ids=None) -> None:
        """Re-assert the active schedule (used once at startup and after an outage
        restore, so the app owns the setpoints as soon as devices are known).

        With `all_device_ids`, it resolves the winning program PER ZONE (same
        arbitration as enforcement) so an off-day program's carried-over period
        can't clobber the program that's actually in effect today, and genuine
        same-time conflicts are skipped. Without it, falls back to the legacy
        per-program assertion (kept for callers that don't have the device list)."""
        if all_device_ids is None:
            with self._lock:
                ids = list(self._rules)
            for rid in ids:
                self.apply_active_now(rid)
            return
        desired, _conflicts = self.resolve_desired(all_device_ids)
        groups: list[tuple[dict, list]] = []   # (action, [zones]) — group identical writes
        for zid, (action, _prog) in desired.items():
            for g in groups:
                if g[0] == action:
                    g[1].append(zid)
                    break
            else:
                groups.append((action, [zid]))
        for action, zids in groups:
            try:
                self.apply_fn(zids, action)
            except Exception as exc:
                log.error("Asserting schedule for %s failed: %s", zids, exc)

    def active_assertions(self) -> list[tuple[Any, dict]]:
        """For every enabled program that has a currently-active period, return
        (targets, action) — what the schedule says those zones should be right
        now. The poller's optional 'enforce schedules' pass uses this to detect
        and correct zones that drifted (a change at the thermostat or in the
        Resideo app). Read-only: it applies nothing itself."""
        with self._lock:
            rules = [dict(r) for r in self._rules.values() if r.get("enabled", True)]
        now = self._now()
        out: list[tuple[Any, dict]] = []
        for rule in rules:
            try:
                period = self._active_period(rule, now)
            except ValueError:
                continue
            if not period:
                continue
            action = dict(period.get("action") or {})
            if action:
                out.append((rule.get("targets", "all"), action))
        return out

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        data = load_json(self.store_path)
        if data is None:
            return
        if not isinstance(data, list):
            log.warning("schedules.json is not a list; ignoring.")
            return
        loaded = 0
        for raw in data:
            try:
                rule = _normalize(raw)
                rule.setdefault("id", str(uuid.uuid4())[:8])
                rule.setdefault("enabled", True)
                self._validate(rule)   # skip malformed rules instead of crashing at start()
            except (ValueError, TypeError, KeyError) as exc:
                log.warning("Skipping invalid schedule program %r: %s",
                            (raw or {}).get("id") if isinstance(raw, dict) else raw, exc)
                continue
            self._rules[rule["id"]] = rule
            loaded += 1
        log.info("Loaded %d schedule program(s).", loaded)

    def _save(self) -> None:
        try:
            with self._lock:
                data = list(self._rules.values())
            atomic_write_json(self.store_path, data, indent=2)
        except OSError as exc:  # pragma: no cover
            log.error("Could not save schedules: %s", exc)
