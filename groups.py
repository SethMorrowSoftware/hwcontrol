"""
groups.py
---------
Named, reusable zone groups. A group is just a name plus a list of deviceIDs
("Arcade" -> ["TCC-1","TCC-2"]) that the dashboard uses as a quick-select
shortcut wherever zones are picked: bulk control, program targets, and the
outage-plan pickers.

Deliberately decoupled and simple:

* Groups store raw deviceIDs and never validate them against the live device
  list — devices come and go (offline, reaped, renamed) exactly like they do
  for schedules, and a group should survive a zone being briefly absent. The
  UI resolves/filters members against the current devices at use time.

* Selections are expanded to concrete deviceIDs by the caller BEFORE they reach
  any control path, so nothing here touches the safety-critical schedule /
  rotation / apply code — a group is only ever a picker convenience.

* Persistence uses the same durable atomic writer as every other store, so a
  crash or power-loss mid-write can't corrupt groups.json.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from storage import atomic_write_json, load_json

log = logging.getLogger("honeywell.groups")


def _dedupe(seq) -> list:
    """Order-preserving de-dup so a zone picked twice isn't stored twice."""
    return list(dict.fromkeys(seq))


def _normalize(raw: dict) -> dict:
    """Return a clean {id, name, members} group, raising ValueError on bad input."""
    if not isinstance(raw, dict):
        raise ValueError("a group must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("a group needs a name")
    members = raw.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("a group needs at least one zone")
    clean_members = []
    for m in members:
        if not isinstance(m, str) or not m.strip():
            raise ValueError("group members must be non-empty deviceIDs")
        clean_members.append(m.strip())
    out = {
        "id": str(raw.get("id") or "")[:64] or ("grp-" + str(uuid.uuid4())[:8]),
        "name": name,
        "members": _dedupe(clean_members),
    }
    return out


class GroupStore:
    def __init__(self, store_path: str = "groups.json"):
        self.store_path = Path(store_path)
        self._lock = threading.RLock()
        self._groups: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ reads

    def list_groups(self) -> list[dict]:
        with self._lock:
            # Sorted by name for a stable, tidy display.
            return sorted((dict(g) for g in self._groups.values()),
                          key=lambda g: (g.get("name") or "").lower())

    def get(self, group_id: str) -> dict | None:
        with self._lock:
            g = self._groups.get(group_id)
            return dict(g) if g else None

    # ------------------------------------------------------------------ writes

    def add(self, raw: dict) -> dict:
        group = _normalize(raw)
        with self._lock:
            self._reject_duplicate_name(group["name"], ignore_id=None)
            self._groups[group["id"]] = group
        self._save()
        log.info("Added zone group '%s' (%s, %d zone(s))",
                 group["name"], group["id"], len(group["members"]))
        return group

    def update(self, group_id: str, raw: dict) -> dict | None:
        with self._lock:
            if group_id not in self._groups:
                return None
            group = _normalize(dict(raw, id=group_id))
            self._reject_duplicate_name(group["name"], ignore_id=group_id)
            self._groups[group_id] = group
        self._save()
        log.info("Updated zone group '%s' (%s)", group["name"], group_id)
        return group

    def remove(self, group_id: str) -> bool:
        with self._lock:
            if group_id not in self._groups:
                return False
            self._groups.pop(group_id)
        self._save()
        log.info("Removed zone group %s", group_id)
        return True

    def _reject_duplicate_name(self, name: str, ignore_id: str | None) -> None:
        low = name.lower()
        for gid, g in self._groups.items():
            if gid != ignore_id and (g.get("name") or "").lower() == low:
                raise ValueError(f"a group named '{name}' already exists")

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        data = load_json(self.store_path)
        if not isinstance(data, list):
            return
        loaded = 0
        for raw in data:
            try:
                g = _normalize(raw)
            except (ValueError, TypeError) as exc:
                log.warning("Skipping invalid zone group %r: %s",
                            (raw or {}).get("id") if isinstance(raw, dict) else raw, exc)
                continue
            self._groups[g["id"]] = g
            loaded += 1
        if loaded:
            log.info("Loaded %d zone group(s).", loaded)

    def _save(self) -> None:
        try:
            with self._lock:
                data = list(self._groups.values())
            atomic_write_json(self.store_path, data, indent=2)
        except OSError as exc:  # pragma: no cover
            log.error("Could not save zone groups: %s", exc)
