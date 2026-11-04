"""v6.232: read the REAL line of a Sportsbet fill from HyperBot's own receipt log.

HyperBot's place_bet answer carries only bet_id / odds / stake, but the HyperBot process logs
Sportsbet's full placement receipt (with the line Sportsbet actually booked) in its local logs:

    <root>\\<account email>\\<date>\\<stamp>.log
      ... Matched: Cade Cunningham Over @ 1.8 (outcome=1279523803, player=Cade Cunningham, line=22.5)
      ... Bet response: {'firstBet': False, ..., 'betPlacements': [{'betNo': 0, 'betId': 7200907227, ...
          'parts': [{'partNo': 1, 'outcome': ..., 'priceDen': 1000, 'handicap': 23.5, ...
          'description': 'Cade Cunningham |Over|', ..., 'eventMarketDesc': '|Player I Total Points|',
          ..., 'priceDecimal': 1.93, ...}]}]}]}]}
      ... Bet placed! ID: 7200907227, Stake: $8.6, Odds: 1.93

A record with no 'handicap' carries its number in the part description: an alt row
("|Jalen Williams |Over| 27.5|", "|Green Bay Packers| (-13.5)") or a milestone ("Kyler Murray |250+ Yards|",
the 249.5 line in the ledger's convention). A repr containing an apostrophe is double-quoted
("D'Andre Swift |Over|").

This module is READ-ONLY and opens the files with the sharing mode Python uses by default on Windows
(HyperBot is writing them). It only looks at files modified in the last few minutes, across every account
folder (bet ids are unique), reading each file's tail. The reader is valid at LOOKUP TIME (a log older
than the recent window is not searched), not for backfilling old bets. Pure functions plus find().
"""
import logging
import math
import os
import re
import time
from dataclasses import dataclass

log = logging.getLogger("tipbot.sb_receipt")

_TAIL_BYTES = 786432                  # the last ~768 KB of a log: minutes of a busy account
_MATCHED_LOOKBACK_LINES = 80          # the "Matched:" line sits a few lines before the response
_LINE_BACKUP_MAX = 65536              # how far back the window start looks for the start of its line
_WARN_EVERY_SEC = 600.0               # one WARNING per this long per kind of silent degradation

_NUM = r"-?\d+(?:\.\d+)?"
_STR = r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")"""      # a Python repr: ' or " quoted
_RE_HANDICAP = re.compile(r"'handicap':\s*(" + _NUM + r")")
_RE_PRICE = re.compile(r"'priceDecimal':\s*(" + _NUM + r")")
_RE_DESC = re.compile(r"'description':\s*" + _STR)
_RE_MKT = re.compile(r"'eventMarketDesc':\s*" + _STR)
_RE_PLUS = re.compile(r"(\d+(?:\.\d+)?)\s*\+")
_RE_ALT_OU = re.compile(r"\|\s*(?:Over|Under)\s*\|\s*(\d+(?:\.\d+)?)", re.I)    # |Jalen Williams |Over| 27.5|
_RE_ALT_SPREAD = re.compile(r"\(\s*([+-]\d+(?:\.\d+)?)\s*\)")                  # |Green Bay Packers| (-13.5)
_RE_MATCHED_LINE = re.compile(r"line=(" + _NUM + r")\)")
_RE_MATCHED_ODDS = re.compile(r"@\s*(" + _NUM + r")")

_last_warn: dict = {}


def _warn_limited(kind: str, msg: str) -> None:
    """One WARNING per kind per _WARN_EVERY_SEC: the reader degrades silently otherwise (a moved root, a
    log nobody can read) and every judged fill would read as 'receipt not found'."""
    now = time.time()
    if now - _last_warn.get(kind, 0.0) >= _WARN_EVERY_SEC:
        _last_warn[kind] = now
        log.warning(msg)


@dataclass
class Receipt:
    bet_id: str
    handicap: object = None        # float or None (the record carries no handicap)
    price: object = None           # float or None (priceDecimal)
    description: str = ""
    market_desc: str = ""
    parts: int = 1                 # how many selections the bet has (a single has 1)
    matched_line: object = None    # the line HyperBot logged as matched, float or None
    matched_odds: object = None
    path: str = ""

    def milestone(self):
        """N from an 'N+' in the part description / market text, else None."""
        for text in (self.description, self.market_desc):
            m = _RE_PLUS.search(text or "")
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    return None
        return None

    def desc_line(self):
        """The line an alt row names in its description ('|Over| 27.5', '(-13.5)'), else None."""
        d = self.description or ""
        for rx in (_RE_ALT_OU, _RE_ALT_SPREAD):
            m = rx.search(d)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    return None
        return None


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _grp(m):
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else (m.group(2) or "")


def parse_response_line(line: str, bet_id: str):
    """The Receipt for `bet_id` from one 'Bet response: {...}' log line, or None when the line is not a
    placement receipt for that bet. A placement has 'betPlacements' and a 'partNo': a betFailures line
    (whose betId is an outcome id) never parses as a receipt."""
    if "Bet response: {" not in line or "'betPlacements'" not in line or "'partNo'" not in line:
        return None
    if not re.search(r"'betId':\s*" + re.escape(str(bet_id)) + r"\b", line):
        return None
    i = line.find("'parts':")
    seg = line[i:] if i >= 0 else line
    n_parts = seg.count("'partNo'") or 1
    j = seg.find("'partNo'")
    first = seg[j:] if j >= 0 else seg
    if n_parts > 1:                                       # keep only the first part's text
        k = first.find("'partNo'", 1)
        first = first[:k] if k > 0 else first
    h = _RE_HANDICAP.search(first)
    p = _RE_PRICE.search(first)
    return Receipt(
        bet_id=str(bet_id),
        handicap=_f(h.group(1)) if h else None,
        price=_f(p.group(1)) if p else None,
        description=_grp(_RE_DESC.search(first)),
        market_desc=_grp(_RE_MKT.search(first)),
        parts=n_parts)


def parse_tail(text: str, bet_id: str):
    """Scan decoded log text for `bet_id`'s receipt (last occurrence wins) and attach the 'Matched:' line
    that precedes it. None when the bet is not in the text."""
    bid = str(bet_id)
    if not bid.isdigit():
        return None
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if bid not in lines[idx]:
            continue
        rec = parse_response_line(lines[idx], bid)
        if rec is None:
            continue
        for back in range(idx - 1, max(-1, idx - _MATCHED_LOOKBACK_LINES), -1):
            if " Matched: " in lines[back]:
                ml = _RE_MATCHED_LINE.search(lines[back])
                mo = _RE_MATCHED_ODDS.search(lines[back])
                rec.matched_line = _f(ml.group(1)) if ml else None
                rec.matched_odds = _f(mo.group(1)) if mo else None
                break
        return rec
    return None


def _recent_logs(root: str, recent_sec: float, now: float):
    """(path, size) of the *.log files two levels under root (account\\date\\file) modified within
    recent_sec, newest first."""
    out = []
    try:
        accts = [e for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return out
    for a in accts:
        try:
            dates = [e for e in os.scandir(a.path) if e.is_dir()]
        except OSError:
            continue
        for d in dates:
            try:
                for f in os.scandir(d.path):
                    if f.name.lower().endswith(".log"):
                        try:
                            st = f.stat()
                        except OSError:
                            continue
                        if now - st.st_mtime <= recent_sec:
                            out.append((st.st_mtime, f.path, st.st_size))
            except OSError:
                continue
    out.sort(reverse=True)
    return [(p, sz) for _mt, p, sz in out]


def _read_tail(path: str, tail_bytes: int = None) -> str:
    """The last ~_TAIL_BYTES of a log, starting at the START OF A LINE: when the window begins mid-line
    the reader backs up to that line's start (up to _LINE_BACKUP_MAX), so a receipt straddling the window
    edge is read whole; if no line start is found that close, the partial first line is dropped.
    v6.237 fix round: `tail_bytes` reads a longer tail (a bet found late, minutes after its send)."""
    with open(path, "rb") as fh:                 # read-only; the default sharing lets HyperBot keep writing
        try:
            size = os.fstat(fh.fileno()).st_size
        except OSError:
            size = 0
        start = max(0, size - (int(tail_bytes) if tail_bytes else _TAIL_BYTES))
        drop_first = False
        if start > 0:
            back, found = start, False
            while back > 0 and start - back < _LINE_BACKUP_MAX:
                step = min(4096, back)
                back -= step
                fh.seek(back)
                chunk = fh.read(step)
                i = chunk.rfind(b"\n")
                if i >= 0:
                    back += i + 1
                    found = True
                    break
            if found:
                start = back
            else:
                drop_first = True                # a line longer than the back-up: skip its partial start
        fh.seek(start)
        data = fh.read()
    if drop_first:
        k = data.find(b"\n")
        data = data[k + 1:] if k >= 0 else b""
    return data.decode("utf-8", errors="replace")


def scan_once(bet_id, root: str, recent_sec: float = 300.0, now: float = None, sizes: dict = None,
              stats: dict = None, tail_bytes: int = None):
    """One pass over the recent logs for `bet_id`. `sizes` (path -> size at the last read) makes a repeat
    pass of the same lookup read only files that GREW; `stats` collects {"ok": set, "bad": set} of the
    paths read / failed. `tail_bytes` (v6.237 fix round) = a longer tail than _TAIL_BYTES. Never raises."""
    bid = str(bet_id or "").strip()
    if not bid.isdigit() or not root or not os.path.isdir(root):
        return None
    now = time.time() if now is None else now
    for path, size in _recent_logs(root, recent_sec, now):
        if sizes is not None and sizes.get(path) == size:
            continue                                  # nothing new in it since the last poll
        try:
            text = _read_tail(path, tail_bytes) if tail_bytes else _read_tail(path)
            if stats is not None:
                stats["ok"].add(path)
            if sizes is not None:
                sizes[path] = size
            if bid not in text:
                continue
            rec = parse_tail(text, bid)
        except Exception as e:                      # a locked / half-written file: try the next one
            log.debug(f"sb_receipt: {path}: {e}")
            if stats is not None:
                stats["bad"].add(path)
            continue
        if rec is not None:
            rec.path = path
            return rec
    return None


def find(bet_id, root: str, wait_sec: float = 6.0, recent_sec: float = 300.0, poll_sec: float = 0.5,
         clock=time.time, sleep=time.sleep, tail_bytes: int = None):
    """The Receipt for `bet_id`, polling up to wait_sec (HyperBot may still be writing it), else None.
    v6.237 fix round: a bet found late passes a wider `recent_sec` and a longer `tail_bytes`
    (window_since); the defaults are unchanged for every other caller.
    A non-numeric id returns None at once. Never raises. A missing log root, or every candidate file
    unreadable, logs one WARNING per 10 minutes (the guard would otherwise read as 'not found' forever)."""
    try:
        bid = str(bet_id or "").strip()
        if not bid.isdigit() or not root:
            return None
        if not os.path.isdir(root):
            _warn_limited("root", f"sb_receipt: the HyperBot log root {root!r} does not exist: no Sportsbet "
                                  f"receipt can be read (every judged fill reads as 'receipt not found')")
            return None
        deadline = clock() + max(0.0, float(wait_sec or 0.0))
        sizes: dict = {}
        stats = {"ok": set(), "bad": set()}
        while True:
            rec = scan_once(bid, root, recent_sec=recent_sec, sizes=sizes, stats=stats,
                            **({"tail_bytes": tail_bytes} if tail_bytes else {}))
            if rec is not None:
                return rec
            if clock() >= deadline:
                break
            sleep(min(poll_sec, max(0.0, deadline - clock())))
        if stats["bad"] and not stats["ok"]:
            _warn_limited("unreadable", f"sb_receipt: all {len(stats['bad'])} recent HyperBot log file(s) "
                                        f"under {root!r} failed to read: no Sportsbet receipt can be read")
        return None
    except Exception as e:
        log.warning(f"sb_receipt: lookup of {bet_id} failed ({type(e).__name__}: {e})")
        return None


_RECENT_SEC = 300.0                  # find()'s default window: a log modified within this long is read


def window_since(sent_ts, now: float = None):
    """v6.237 fix round (review 4 Nov): (recent_sec, tail_bytes) for a bet whose receipt was written at
    `sent_ts`, so a bet found late (the deferred look runs minutes after the send) still reaches its log.
    recent_sec = max(the default 300 s, now - sent_ts + 300 s); the tail grows in step with it (the
    default ~768 KB per 300 s). No usable sent_ts (missing, not finite, in the future) = the defaults
    (300.0, None). Never raises."""
    try:
        now = time.time() if now is None else float(now)
        st = float(sent_ts)
        if not (st == st and abs(st) != float("inf")) or st > now:
            return _RECENT_SEC, None
        recent = max(_RECENT_SEC, now - st + _RECENT_SEC)
        if recent <= _RECENT_SEC:
            return _RECENT_SEC, None
        return recent, int(math.ceil(recent / _RECENT_SEC)) * _TAIL_BYTES
    except (TypeError, ValueError):
        return _RECENT_SEC, None


def true_line(rec: Receipt, sent_line):
    """(moved, line): was the bet booked on a different line than `sent_line`? `line` is the TRUE line in
    the ledger's convention.

    * a record with a handicap: moved when its absolute value differs from the line sent (the sign is the
      team's side on a spread, not a move);
    * no handicap, the description names an alt line ('|Over| 27.5', '(-13.5)'): compared like a handicap;
    * no handicap, an 'N+' milestone: the same line when N or N - 0.5 is the line sent; a different N is a
      move and the true line is N - 0.5 (Kyler Murray 250+ is 249.5 in the ledger);
    * a multi (parts > 1), no receipt, or no line sent: never a move."""
    sent = _f(sent_line)
    if rec is None or sent is None or rec.parts != 1:
        return False, sent
    if rec.handicap is not None:
        if abs(abs(rec.handicap) - abs(sent)) > 1e-6:
            return True, rec.handicap
        return False, sent
    dl = rec.desc_line()
    if dl is not None:
        if abs(abs(dl) - abs(sent)) > 1e-6:
            return True, dl
        return False, sent
    n = rec.milestone()
    if n is not None and abs(n - sent) > 1e-6 and abs(n - 0.5 - sent) > 1e-6:
        return True, n - 0.5
    return False, sent
