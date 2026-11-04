"""v6.229 (Wilson 3 Nov 2026: "for shook, record his tips and only bet on them when 1 hour from tip
if the line has not moved by more than 1 stat point (instead of betting 10 hours before game starts
when he randomly posts early)").

The pure parts of the Shook NBA 1-hour deferral: the persisted store (logs/shook_deferred.json), the
line rule, the tip-off pick, the board-line pick and the tip (de)serialisation. main.py owns the
scheduling and the placement (it calls the existing place_tip with every guard it already has).

Store format (one JSON object, written atomically via a temp file + os.replace):
  {"version": 1, "entries": {<key>: {
      "key": "shook::<fingerprint>::<event>::<tip-off date>",   # ONE entry per tip AND game
      "fp": "<tipster>::<fingerprint>",  # the resend-guard fingerprint
      "status": "pending" | "placing" | "done",
          # "placing" = marked handled just before the send; "done" = a tombstone (outcome, no tip)
          # kept until tip-off + 30 h, so a re-post of the same tip for the same game is refused
      "recorded_ts", "due_ts", "tipoff_ts": epoch seconds,
      "runs": int,                       # checks started (a crash or an unreadable board retries)
      "event": "A v B", "channel": str, "msg_time": iso,
      "player", "side", "stat", "line": the tipped line, "threshold": bool,
      "tip": {...ParsedTip as JSON...}}}}
A file that is not JSON, or not a store, is renamed aside (once, with an alert). A file that cannot be
READ (PermissionError, a sharing violation) is retried, then raises StoreError: add / update / remove /
finish then refuse to write, so a transient error can never overwrite the store."""
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from models import ParsedLeg, ParsedTip

log = logging.getLogger("tipbot")

if os.getenv("SHOOK_DEFERRED_PATH", "").strip():
    PATH = Path(os.environ["SHOOK_DEFERRED_PATH"].strip())
elif os.getenv("TIPBOT_TESTING", "").strip().lower() in ("1", "true", "yes"):
    PATH = Path(tempfile.gettempdir()) / "tipbot_test_shook_deferred.json"
else:
    PATH = Path(__file__).with_name("logs") / "shook_deferred.json"

_LOCK = threading.RLock()
VERSION = 1
LOOKBACK_SEC = 24 * 3600.0           # how far back a played game still counts (classify, handled)
TOMBSTONE_KEEP_SEC = 30 * 3600.0     # a "done" entry is kept this long past tip-off (> LOOKBACK_SEC:
                                     # the look-back rule never outlives its record)
READ_TRIES = 5
READ_RETRY_SEC = 0.2
ALERT_EVERY_SEC = 30 * 60.0          # a read that keeps failing alerts at most this often
# hook(text) set by main.py: sends one critical Telegram alert. None = log only.
ALERT = None
_last_read_alert: dict = {}          # rate-limit key -> time of its last alert


class StoreError(OSError):
    """The store could not be read or moved aside: nothing may be written over it."""


def _alert(text: str, rate_limited: bool = False, key: str = "read") -> None:
    """One critical alert via ALERT. rate_limited: at most one per ALERT_EVERY_SEC per `key`."""
    if rate_limited:
        if time.time() - _last_read_alert.get(key, 0.0) < ALERT_EVERY_SEC:
            return
        _last_read_alert[key] = time.time()
    try:
        if ALERT is not None:
            ALERT(text)
    except Exception as e:
        log.error(f"Shook deferred store: alert failed: {e}")


def _num(v, default):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


# ── store ───────────────────────────────────────────────────────────

def _read_locked() -> dict:
    """The entries dict ({} when there is no file). A file that is not JSON, or not a store, is
    renamed aside (the bad bytes are kept) and read as empty, with one ERROR line and one alert; if
    that rename fails it raises StoreError (never write over it). A file that cannot be READ is
    retried READ_TRIES times, then raises StoreError (one alert per ALERT_EVERY_SEC)."""
    p = Path(PATH)
    raw = None
    last = None
    for i in range(READ_TRIES):
        try:
            with open(p, "rb") as f:        # bytes: a decode error is corruption, handled below
                raw = f.read()
            last = None
            break
        except FileNotFoundError:
            return {}
        except OSError as e:
            last = e
            if i < READ_TRIES - 1:
                time.sleep(READ_RETRY_SEC)
    if last is not None:
        msg = (f"Shook deferred store {p} cannot be read ({last!r}) after {READ_TRIES} tries; "
               f"nothing is written over it and recorded tips are not checked until it reads")
        log.error(msg)
        _alert(msg, rate_limited=True)
        raise StoreError(msg)
    try:
        data = json.loads(raw.decode("utf-8"))      # UnicodeDecodeError is a ValueError
        ents = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(ents, dict):
            raise ValueError("no entries object")
        out = {}
        bad = []
        for k, v in ents.items():
            if isinstance(v, dict):
                out[str(k)] = v
            else:
                bad.append(str(k))
                # kept as a malformed pending entry: its check removes it with an alert
                out[str(k)] = {"status": "pending", "due_ts": 0, "tipoff_ts": None, "tip": None,
                               "malformed": repr(v)[:200]}
        if bad:
            msg = (f"Shook deferred store has {len(bad)} entry(ies) that are not objects "
                   f"({', '.join(b[:60] for b in bad[:3])}); they are treated as malformed")
            log.error(msg)
            _alert(msg, rate_limited=True, key="malformed")
        return out
    except ValueError as e:
        bad = p.with_name(p.name + f".bad-{int(time.time())}")
        try:
            os.replace(p, bad)
        except OSError as oe:
            msg = (f"Shook deferred store {p} is corrupt ({e}) and could NOT be moved aside "
                   f"({oe!r}); nothing is written over it")
            log.error(msg)
            _alert(msg, rate_limited=True)
            raise StoreError(msg) from oe
        msg = (f"Shook deferred store {p} was corrupt ({e}); moved aside to {bad.name}, starting "
               f"empty: tips recorded in it are NOT bet")
        log.error(msg)
        _alert(msg)
        return {}


def _write_locked(entries: dict) -> None:
    """Atomic: temp file in the same folder, fsync, os.replace (retried on a sharing violation).
    Raises on failure."""
    p = Path(PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "entries": entries}, f)
        f.flush()
        os.fsync(f.fileno())
    for i in range(READ_TRIES):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if i == READ_TRIES - 1:
                raise
            time.sleep(READ_RETRY_SEC)


def entries() -> dict:
    with _LOCK:
        return {k: dict(v) for k, v in _read_locked().items()}


def get(key: str):
    with _LOCK:
        e = _read_locked().get(key)
        return dict(e) if e else None


def add(entry: dict) -> bool:
    """Record a new entry. False (nothing written) when the key is already on file, whatever its
    status (a pending entry, or a tombstone of a tip already handled for this game). Raises when
    the store cannot be read or written (the caller then does not bet and tells Wilson)."""
    with _LOCK:
        ents = _read_locked()
        if entry["key"] in ents:
            return False
        ents[entry["key"]] = entry
        _write_locked(ents)
        return True


def update(key: str, **changes) -> bool:
    """Change fields of an entry; False when it is gone. Raises when the write fails."""
    with _LOCK:
        ents = _read_locked()
        if key not in ents:
            return False
        ents[key].update(changes)
        _write_locked(ents)
        return True


def remove(key: str) -> bool:
    with _LOCK:
        ents = _read_locked()
        if key not in ents:
            return False
        del ents[key]
        _write_locked(ents)
        return True


def finish(key: str, outcome: str, now: float = None) -> bool:
    """Turn an entry into a tombstone: status "done", the outcome, no tip, kept until tip-off + 30 h
    (at least 30 h from now, longer than the 24 h look-back) so add() refuses a re-post of the same tip for the same game. False when
    the entry is gone. Raises when the write fails."""
    now = time.time() if now is None else now
    with _LOCK:
        ents = _read_locked()
        e = ents.get(key)
        if e is None:
            return False
        e.pop("tip", None)
        e.update(status="done", outcome=outcome, finished_ts=now,
                 keep_until=max(_num(e.get("tipoff_ts"), 0.0), now) + TOMBSTONE_KEEP_SEC)
        _write_locked(ents)
        return True


def prune(now: float = None) -> int:
    """Drop tombstones past their keep_until. Returns how many; writes only when there are some."""
    now = time.time() if now is None else now
    with _LOCK:
        ents = _read_locked()
        old = [k for k, e in ents.items()
               if e.get("status") == "done" and _num(e.get("keep_until"), 0.0) < now]
        if not old:
            return 0
        for k in old:
            del ents[k]
        _write_locked(ents)
        return len(old)


# ── the rules ───────────────────────────────────────────────────────

def side_of(selection) -> str:
    """'over' / 'under' from a leg selection ('Over', 'Jalen Johnson Under'), else ''."""
    words = str(selection or "").strip().lower().split()
    last = words[-1] if words else ""
    return last if last in ("over", "under") else ""


def tipped_line(leg, threshold: bool) -> float:
    """The over/under line the tip means: an explicit 'N+' (an integer line on a threshold tip) is
    the over N - 0.5."""
    ln = float(leg.line)
    if threshold and float(ln).is_integer():
        ln -= 0.5
    return ln


def line_verdict(side: str, tipped: float, current: float, max_worse: float) -> tuple:
    """(ok, worse_by). worse_by is how far the current line is WORSE for us than the tipped one
    (an over is worse on a higher line, an under on a lower line); negative = moved our way. ok
    when worse_by <= max_worse (a move in our favour always passes). Fails closed on anything not
    finite or an unknown side."""
    try:
        t, c, m = float(tipped), float(current), float(max_worse)
    except (TypeError, ValueError):
        return False, float("nan")
    if not (math.isfinite(t) and math.isfinite(c) and math.isfinite(m)):
        return False, float("nan")
    if side == "over":
        worse = c - t
    elif side == "under":
        worse = t - c
    else:
        return False, float("nan")
    return worse <= m + 1e-9, worse


def classify_starts(starts, now: float, handled=(), recent_sec: float = 4 * 3600.0,
                    lookback_sec: float = LOOKBACK_SEC) -> tuple:
    """(kind, tip-off epoch or None) for the game a tip is for, from the ESPN starts of its two
    teams. A start within `recent_sec` in the past is a game in play or just finished; one within
    `lookback_sec` is a game already played (a stale re-post of its tip must not be pinned to a
    later rematch). `handled` = tip-off times this very tip (same fingerprint and event) is already
    on file for: a played game within 6 h of one of them is accounted for.
      "future":    upcoming start(s): the nearest one (a played game that is accounted for does not
                   make it ambiguous: a new tip for the next game);
      "started":   only a recent start (the game is on): its start;
      "ambiguous": a played game NOT accounted for AND an upcoming one (a back-to-back or a
                   rematch): which game the tip means is unclear, so none is picked;
      "handled":   only a played game this tip is already on file for, on the very same calendar
                   day as its record (the store key): plainly a re-post, nothing to do;
      "unknown":   no usable start (ESPN down, or no such game, not listed yet, or only a game long
                   over), or only an accounted-for game that is not plainly the same game (the next
                   game may be past ESPN's window): never dropped quietly."""
    hs = [h for h in (_num(x, None) for x in handled or ()) if h is not None]

    def accounted(v):
        return any(abs(v - h) < 6 * 3600.0 for h in hs)

    def same_key(v):
        d = datetime.fromtimestamp(v).date()
        return any(datetime.fromtimestamp(h).date() == d for h in hs)

    recent, past, done, fut = [], [], [], []
    for s in starts or []:
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        if v > now:
            fut.append(v)
        elif v > now - lookback_sec:
            if accounted(v):
                done.append(v)
            else:
                past.append(v)
                if v > now - recent_sec:
                    recent.append(v)
    if past and fut:
        return "ambiguous", None
    if fut:
        return "future", min(fut)
    if recent:
        return "started", max(recent)
    if done and same_key(max(done)):
        return "handled", max(done)
    return "unknown", None


def handled_tipoffs(fp: str, event: str, now: float = None, lookback_sec: float = LOOKBACK_SEC) -> list:
    """Tip-off times of every store entry (any status) for resend fingerprint `fp` and game `event`
    that began within `lookback_sec` or is still ahead: the games this tip is already on file for.
    Raises StoreError when the store cannot be read."""
    now = time.time() if now is None else now
    out = []
    for e in entries().values():
        if e.get("fp") != fp or e.get("event") != event:
            continue
        t = _num(e.get("tipoff_ts"), None)
        if t is not None and t > now - lookback_sec:
            out.append(t)
    return out


_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def norm_name(name) -> str:
    s = unicodedata.normalize("NFD", str(name or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    toks = [t for t in re.sub(r"[^a-z0-9 ]+", " ", s).split() if t not in _SUFFIX]
    return " ".join(toks)


def pick_board_line(ou_lines, threshold_lines, tipped: float, side: str = "over"):
    """The current line for the tipped player / stat / side from the board.
      ou_lines: the lines of the player's plain full-game over/under rows on the tipped side;
      threshold_lines: the equivalent over lines of his 'N+' rows (N - 0.5), overs only.
    The tipped line itself still on the board counts as unmoved (the bet is as tipped). Otherwise
    the over/under row nearest the tipped line (Sportsbet lists one main line per player and
    stat); two rows the same distance away pick the one better for us (an over: the lower line, an
    under: the higher). With no over/under row, only a threshold row AT the tipped line vouches for
    it (a ladder has no main line to compare). (line, note); line None = the board cannot say."""
    ou = sorted({float(x) for x in ou_lines or []})
    if ou:
        for ln in ou:
            if abs(ln - tipped) < 1e-9:
                return ln, "tipped line on the board"
        sign = -1.0 if side == "under" else 1.0
        return min(ou, key=lambda x: (round(abs(x - tipped), 9), sign * x)), "nearest over/under row"
    th = sorted({float(x) for x in threshold_lines or []})
    if th and any(abs(ln - tipped) < 1e-9 for ln in th):
        return tipped, "tipped rung on the ladder"
    if th:
        return None, "only a ladder without the tipped rung (no main line to compare)"
    return None, "no row for the player"


# ── tip (de)serialisation ───────────────────────────────────────────

_SKIP_ATTRS = {"_timing", "_presolved_event"}


def _jsonable(v):
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


def _obj_to_dict(obj) -> dict:
    out = {}
    for k, v in vars(obj).items():
        if k in _SKIP_ATTRS:
            continue
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif _jsonable(v):
            out[k] = v
    return out


def tip_to_dict(tip: ParsedTip) -> dict:
    d = _obj_to_dict(tip)
    d["legs"] = [_obj_to_dict(l) for l in tip.legs]
    return d


def _build(cls, d: dict):
    names = {f.name for f in fields(cls)}
    base = {k: v for k, v in d.items() if k in names}
    return base, {k: v for k, v in d.items() if k not in names}


def tip_from_dict(d: dict) -> ParsedTip:
    """Rebuild the ParsedTip (legs and extra private attributes such as _is_threshold) from
    tip_to_dict."""
    d = dict(d)
    legs = []
    for ld in d.pop("legs", []) or []:
        base, extra = _build(ParsedLeg, ld)
        leg = ParsedLeg(**base)
        for k, v in extra.items():
            setattr(leg, k, v)
        legs.append(leg)
    ts = d.get("timestamp")
    if isinstance(ts, str):
        try:
            d["timestamp"] = datetime.fromisoformat(ts)
        except ValueError:
            d["timestamp"] = None
    base, extra = _build(ParsedTip, d)
    base["legs"] = legs
    tip = ParsedTip(**base)
    for k, v in extra.items():
        setattr(tip, k, v)
    return tip
