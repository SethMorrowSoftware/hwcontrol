"""
storage.py
----------
Durable JSON persistence primitives shared by every module that keeps state on
disk (tokens, schedules, automations, snapshots, rotations, trigger state, the
sole-controller flag).

Why this exists: the app is deployed on generator power and *expects* power
events, yet the original code persisted with a plain ``Path.write_text(...)``.
That truncates the file first and then writes, so a crash or power-loss in the
middle - or two threads writing the same file at once - leaves a half-written,
unparseable file. Losing ``tokens.json`` bricks OAuth; losing ``rotations.json``
drops an in-progress generator load-shed; losing ``schedules.json`` wipes every
program. All of it was silently tolerated by loaders that started empty on a
parse error.

``atomic_write_json`` writes to a temp file in the same directory, fsyncs it,
keeps the previous good copy as ``<name>.bak``, then atomically ``os.replace``s
it into place. ``load_json`` reads the primary and transparently falls back to
the ``.bak`` if the primary is missing or corrupt, so a torn write at worst
costs the single most-recent update, never the whole file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("honeywell.storage")


def _bak_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def atomic_write_json(
    path: str | os.PathLike,
    data: Any,
    *,
    mode: Optional[int] = None,
    indent: Optional[int] = None,
    default: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Durably persist ``data`` as JSON at ``path``.

    Temp file (same dir) -> flush + fsync -> keep previous as ``.bak`` ->
    ``os.replace`` into place -> fsync the directory. If ``mode`` is given
    (e.g. ``0o600`` for secrets) the file is created and left with exactly
    those permissions. Raises ``OSError`` on failure so callers can decide how
    loudly to react (a failed token save, for instance, is operator-visible).
    """
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")

    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        if mode is not None:
            os.chmod(tmp_name, mode)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=indent, default=default)
            fh.flush()
            os.fsync(fh.fileno())
        # Preserve the last good copy so a crash between the two replaces below
        # still leaves a loadable file (load_json falls back to it).
        if path.exists():
            try:
                os.replace(path, _bak_path(path))
            except OSError:
                pass
        os.replace(tmp_path, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        # fsync the directory so the rename itself is durable across power loss.
        try:
            dir_fd = os.open(str(directory), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def load_json(path: str | os.PathLike, default: Any = None) -> Any:
    """Load JSON from ``path``, transparently falling back to its ``.bak`` sibling
    if the primary is absent or corrupt. Returns ``default`` if neither is usable.
    Never raises for a missing/corrupt file - the caller gets ``default`` and a
    warning is logged."""
    path = Path(path)
    candidates = [path, _bak_path(path)]
    for i, candidate in enumerate(candidates):
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text())
        except (OSError, ValueError) as exc:
            # Primary corrupt/unreadable: warn and try the backup next.
            log.warning("Could not load %s (%s); %s", candidate, exc,
                        "trying backup" if i == 0 else "no backup usable")
    return default
