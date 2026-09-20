# Adapted from Fullbrain 1013ff18, server/accounts/usage_rollouts.py.
"""Codex usage traces: the newest token_count event across an account's homes.

Rollouts are append-only JSONL files under `<home>/sessions/YYYY/MM/DD/`. The
read stays cheap: only the tail of the few newest files per home is scanned.
The winner is the newest event by its own timestamp (file mtime only orders
the candidates), and an event carrying rate-limit windows beats a bare token
count: a turn cut by the limit logs its tokens without windows. Planted links
inside worker-owned homes are never followed.
"""

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

TAIL_BYTES = 512 * 1024
PER_HOME = 5
WINDOW_KEYS = ("primary", "secondary")


def _subdirs(root: Path):
    descriptor = None
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        with os.scandir(descriptor) as entries:
            return [root / e.name for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    finally:
        if descriptor is not None:
            os.close(descriptor)


def newest_rollouts(home: Path, limit: int = PER_HOME) -> list:
    """The newest rollout files of one home, by mtime."""
    files = []
    for year in _subdirs(home / "sessions"):
        for month in _subdirs(year):
            for day in _subdirs(month):
                try:
                    with os.scandir(day) as entries:
                        for entry in entries:
                            if (entry.name.startswith("rollout-") and entry.name.endswith(".jsonl")
                                    and entry.is_file(follow_symlinks=False)):
                                files.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
                except OSError:
                    continue
    files.sort(reverse=True)
    return [path for _mtime, path in files[:limit]]


def _tail(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            if info.st_size > TAIL_BYTES:
                fh.seek(info.st_size - TAIL_BYTES)
                fh.readline()  # drop the partial line at the cut
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def has_windows(tc: dict) -> bool:
    limits = tc.get("rate_limits")
    if not isinstance(limits, dict):
        return False
    return any(isinstance(limits.get(key), dict) and limits[key].get("used_percent") is not None
               for key in WINDOW_KEYS)


def last_token_count(path: Path) -> dict | None:
    """Newest token_count of one rollout; one with windows wins over a bare count."""
    tail = _tail(path)
    if tail is None:
        return None
    bare = None
    for line in reversed(tail.splitlines()):
        if '"token_count"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        found = {"at": event.get("timestamp"), **payload}
        if has_windows(found):
            return found
        bare = bare or found
    return bare


def observed_at(tc: dict, path: Path) -> datetime:
    """When the event happened: its own stamp, else the file's mtime."""
    raw = tc.get("at")
    if isinstance(raw, str):
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo:
                return stamp
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def newest_token_count(homes: list) -> dict | None:
    """Newest event across homes: windows-bearing events first, then by time."""
    best, best_key = None, None
    for home in homes:
        for path in newest_rollouts(home):
            tc = last_token_count(path)
            if not tc:
                continue
            key = (has_windows(tc), observed_at(tc, path))
            if best_key is None or key > best_key:
                best, best_key = tc, key
    return best
