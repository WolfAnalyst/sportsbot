"""Persistent "already sent" registry for tips (v6.44).

Why: 2026-09-27 16:38 4th&EV re-sent his whole day as one 21-bet summary and the bot
re-placed six of them ($2,610): three he had placed by hand after they went to manual,
and three the bot had auto-placed that morning. Every duplicate guard in the bot was an
in-memory dict, and the 16:13 deploy restart had wiped the NFL one; the manual ones were
never registered at all. Wilson: "we now need a guard that checks to see if the bet for
ANY BET from any tipster was already sent and if it was, to ignore it".

A tip is registered here the first time it is SEEN (placed, failed or sent to manual),
under a key built from the tipster's own parsed fields, and a later tip with the same key
inside the TTL is ignored. The file survives restarts. Every public function NEVER raises;
on any storage error it answers "not seen" so a broken file can't block live betting
(the old in-memory guards still run behind it).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("tipbot.seen_bets")

SEEN_PATH = Path(__file__).with_name("logs") / "seen_bets.json"
_MAX_AGE_SEC = 8 * 24 * 3600  # pruned on write; no TTL in use is longer than 7 days
_lock = threading.Lock()
_state: dict = {"loaded": False, "path": None, "data": {}}

# Off under the test suite unless a test switches it on (tests re-send the same tip
# across cases on purpose).
ENABLED = os.getenv("SEEN_BETS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
_IN_TESTS = bool(os.getenv("TIPBOT_TESTING"))
_ENABLED_IN_TESTS = False


def _active() -> bool:
    return ENABLED and (not _IN_TESTS or _ENABLED_IN_TESTS)


def _load(path: Path):
    """The registry dict, or None if the file exists but could not be read. A failed
    read is NOT cached and callers must not write (review v6.44: caching {} and saving
    it would erase the whole history on one transient lock/AV glitch)."""
    if _state["loaded"] and _state["path"] == str(path):
        return _state["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        log.error(f"seen_bets: could not read {path.name} ({e}); guard skipped this time")
        return None
    _state.update(loaded=True, path=str(path), data=data)
    return data


def _save(path: Path, data: dict) -> None:
    now = time.time()
    for k in [k for k, v in data.items() if now - float(v.get("ts", 0)) > _MAX_AGE_SEC]:
        del data[k]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0, sort_keys=True)
    os.replace(tmp, path)


def check_and_mark(key: str, ttl_sec: float, label: str = "", path: Path | None = None):
    """Atomically: if `key` was seen within `ttl_sec`, return that entry (a dict with
    'ts' and 'label') and leave it; otherwise record it now and return None."""
    if not key or not _active():
        return None
    path = path or SEEN_PATH
    try:
        with _lock:
            data = _load(path)
            if data is None:
                return None
            now = time.time()
            prev = data.get(key)
            if prev and now - float(prev.get("ts", 0)) <= ttl_sec:
                return dict(prev)
            data[key] = {"ts": now, "label": (label or "")[:160]}
            _save(path, data)
            return None
    except Exception as e:
        log.error(f"seen_bets: check_and_mark failed for {key!r} ({e}) -> treated as new")
        return None


def check(key: str, ttl_sec: float, path: Path | None = None):
    """The entry if `key` was seen within `ttl_sec`, else None. Does not register."""
    if not key or not _active():
        return None
    try:
        with _lock:
            data = _load(path or SEEN_PATH)
            prev = data.get(key) if data is not None else None
            if prev and time.time() - float(prev.get("ts", 0)) <= ttl_sec:
                return dict(prev)
            return None
    except Exception as e:
        log.error(f"seen_bets: check failed for {key!r} ({e}) -> treated as new")
        return None


def mark(key: str, label: str = "", path: Path | None = None) -> None:
    """Register `key` as seen now."""
    if not key or not _active():
        return
    path = path or SEEN_PATH
    try:
        with _lock:
            data = _load(path)
            if data is None:
                return
            data[key] = {"ts": time.time(), "label": (label or "")[:160]}
            _save(path, data)
    except Exception as e:
        log.error(f"seen_bets: mark failed for {key!r} ({e})")


def scan(prefix: str, ttl_sec: float, path: Path | None = None) -> list:
    """[(key, entry)] for every key starting with `prefix` seen within `ttl_sec`."""
    if not prefix or not _active():
        return []
    try:
        with _lock:
            data = _load(path or SEEN_PATH)
            if data is None:
                return []
            now = time.time()
            return [(k, dict(v)) for k, v in data.items()
                    if k.startswith(prefix) and now - float(v.get("ts", 0)) <= ttl_sec]
    except Exception as e:
        log.error(f"seen_bets: scan failed for {prefix!r} ({e})")
        return []


def age_text(entry) -> str:
    try:
        mins = (time.time() - float(entry.get("ts", 0))) / 60
        return f"{mins:.0f} min" if mins < 120 else f"{mins / 60:.1f} h"
    except Exception:
        return "?"


def _reset_for_tests(path: Path | None = None) -> None:
    _state.update(loaded=False, path=None, data={})
