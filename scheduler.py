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
    "targets": "all",                           # "all" | [deviceID, ...]
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

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

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
        self._sched = BackgroundScheduler(timezone=timezone) if timezone else BackgroundScheduler()
        self._load()

    def start(self) -> None:
        self._sched.start()
        for rule in self._rules.values():
            if rule.get("enabled", True):
                self._schedule(rule)
        log.info("Scheduler started with %d program(s).", len(self._rules))

    def stop(self) -> None:
        self._sched.shutdown(wait=False)

    # ------------------------------------------------------------- rule CRUD

    def list_rules(self) -> list[dict]:
        return list(self._rules.values())

    def add_rule(self, rule: dict) -> dict:
        rule = _normalize(rule)
        rule.setdefault("id", str(uuid.uuid4())[:8])
        rule.setdefault("enabled", True)
        self._validate(rule)
        self._rules[rule["id"]] = rule
        self._save()
        if rule["enabled"]:
            self._schedule(rule)
        log.info("Added schedule program '%s' (%d period(s))", rule["id"], len(rule["periods"]))
        return rule

    def update_rule(self, rule_id: str, rule: dict) -> dict | None:
        if rule_id not in self._rules:
            return None
        merged = _normalize(rule)
        merged["id"] = rule_id
        merged.setdefault("enabled", self._rules[rule_id].get("enabled", True))
        self._validate(merged)
        self._rules[rule_id] = merged
        self._save()
        self._unschedule(rule_id)
        if merged["enabled"]:
            self._schedule(merged)
        log.info("Updated schedule program '%s'", rule_id)
        return merged

    def remove_rule(self, rule_id: str) -> bool:
        if rule_id not in self._rules:
            return False
        self._rules.pop(rule_id)
        self._unschedule(rule_id)
        self._save()
        log.info("Removed schedule program '%s'", rule_id)
        return True

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
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
        periods = rule.get("periods")
        if not isinstance(periods, list) or not periods:
            raise ValueError("a program needs at least one time period")
        for p in periods:
            _valid_hhmm(p.get("time"))
            if not isinstance(p.get("action"), dict) or not p.get("action"):
                raise ValueError("each period needs an action")
        days = rule.get("days")
        if days:
            bad = [d for d in days if str(d).lower() not in _DAYS]
            if bad:
                raise ValueError(f"invalid days: {bad}")

    def _trigger(self, time_str: str, days) -> CronTrigger:
        hour, minute = _valid_hhmm(time_str)
        kwargs = {"hour": hour, "minute": minute}
        if days:
            kwargs["day_of_week"] = ",".join(str(d).lower() for d in days)
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
        rule = self._rules.get(rule_id)
        if not rule or not rule.get("enabled", True):
            return
        try:
            period = rule["periods"][idx]
        except (IndexError, KeyError):
            return
        log.info("Running schedule '%s' period %s (%s @ %s)",
                 rule_id, idx, rule.get("name"), period.get("time"))
        try:
            self.apply_fn(rule.get("targets", "all"), period["action"])
        except Exception as exc:
            log.error("Schedule '%s' period %s failed: %s", rule_id, idx, exc)

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text())
            for rule in data:
                rule = _normalize(rule)
                self._rules[rule["id"]] = rule
            log.info("Loaded %d schedule program(s).", len(self._rules))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Could not load schedules: %s", exc)

    def _save(self) -> None:
        try:
            self.store_path.write_text(json.dumps(list(self._rules.values()), indent=2))
        except OSError as exc:  # pragma: no cover
            log.error("Could not save schedules: %s", exc)
