"""
scheduler.py
------------
Application-level scheduling for the whole facility.

Why application-level instead of the thermostats' onboard 7-day schedule:
  * It works for every device, including older round (TCC-) units that have no
    onboard schedule.
  * It lets you write facility-wide rules ("all zones to 62 at 7pm weekdays",
    "holiday setback") in one place instead of programming each thermostat.
  * You keep the rules; they're versioned in schedules.json.

(If you specifically want a rule stored ON a T-series device so it runs even when
this server is down, that's the /schedule resource on LCC- devices - a good
future addition. This scheduler drives setpoints via the normal control path.)

Each rule looks like:
{
  "id": "weekday-night-setback",
  "name": "Weekday night setback",
  "enabled": true,
  "days": ["mon","tue","wed","thu","fri"],   # or omit for every day
  "time": "19:00",                            # 24h local time
  "targets": "all",                           # "all" | [deviceID, ...]
  "action": {
    "mode": "Heat",
    "heatSetpoint": 62,
    "coolSetpoint": 80,
    "thermostatSetpointStatus": "PermanentHold"
  }
}

The action-applying function is injected:  apply(targets, action) -> None
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

_DAY_MAP = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
            "fri": "fri", "sat": "sat", "sun": "sun"}


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
        log.info("Scheduler started with %d rule(s).", len(self._rules))

    def stop(self) -> None:
        self._sched.shutdown(wait=False)

    # ------------------------------------------------------------- rule CRUD

    def list_rules(self) -> list[dict]:
        return list(self._rules.values())

    def add_rule(self, rule: dict) -> dict:
        rule = dict(rule)
        rule.setdefault("id", str(uuid.uuid4())[:8])
        rule.setdefault("enabled", True)
        self._validate(rule)
        self._rules[rule["id"]] = rule
        self._save()
        if rule["enabled"]:
            self._schedule(rule)
        log.info("Added schedule rule '%s'", rule["id"])
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        if rule_id not in self._rules:
            return False
        self._rules.pop(rule_id)
        self._unschedule(rule_id)
        self._save()
        log.info("Removed schedule rule '%s'", rule_id)
        return True

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        rule = self._rules.get(rule_id)
        if not rule:
            return False
        rule["enabled"] = enabled
        self._save()
        if enabled:
            self._schedule(rule)
        else:
            self._unschedule(rule_id)
        return True

    # ------------------------------------------------------------- internals

    def _validate(self, rule: dict) -> None:
        parts = str(rule.get("time", "")).split(":")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            raise ValueError("rule.time must be 'HH:MM' (24-hour)")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("rule.time hour must be 0-23 and minute 0-59")
        if "action" not in rule or not isinstance(rule["action"], dict):
            raise ValueError("rule.action must be an object")
        days = rule.get("days")
        if days:
            bad = [d for d in days if str(d).lower() not in _DAY_MAP]
            if bad:
                raise ValueError(f"invalid days: {bad}")

    def _trigger(self, rule: dict) -> CronTrigger:
        hour, minute = str(rule["time"]).split(":")
        kwargs = {"hour": int(hour), "minute": int(minute)}
        days = rule.get("days")
        if days:
            kwargs["day_of_week"] = ",".join(_DAY_MAP[d.lower()] for d in days)
        return CronTrigger(**kwargs)

    def _schedule(self, rule: dict) -> None:
        self._unschedule(rule["id"])
        self._sched.add_job(
            self._run_rule, self._trigger(rule),
            args=[rule["id"]], id=rule["id"], replace_existing=True,
        )

    def _unschedule(self, rule_id: str) -> None:
        try:
            self._sched.remove_job(rule_id)
        except Exception:
            pass

    def _run_rule(self, rule_id: str) -> None:
        rule = self._rules.get(rule_id)
        if not rule or not rule.get("enabled", True):
            return
        log.info("Running schedule rule '%s' (%s)", rule_id, rule.get("name"))
        try:
            self.apply_fn(rule.get("targets", "all"), rule["action"])
        except Exception as exc:
            log.error("Schedule rule '%s' failed: %s", rule_id, exc)

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text())
            for rule in data:
                self._rules[rule["id"]] = rule
            log.info("Loaded %d schedule rule(s).", len(self._rules))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Could not load schedules: %s", exc)

    def _save(self) -> None:
        try:
            self.store_path.write_text(json.dumps(list(self._rules.values()), indent=2))
        except OSError as exc:  # pragma: no cover
            log.error("Could not save schedules: %s", exc)
