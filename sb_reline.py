"""v6.232: the Sportsbet re-line guard (state, judge, alert).

Sportsbet re-lines a prop on the SAME outcome id (Cade Cunningham Over 22.5 -> 23.5) while HyperBot's read
of the event lags, so HyperBot "matches" the old line at the old price, sends, and Sportsbet fills its LIVE
line. HyperBot's place_bet answer carries no line, but HyperBot logs Sportsbet's receipt, which does
(sb_receipt.py reads it). After every Sportsbet single fill with a bet id, judge() looks the receipt up:

  * the receipt's line is the line sent (or the record carries no line to compare): NOTHING, whatever the
    price did (a price jump on the same line is a market move, not a re-line);
  * the receipt's line differs (a CONFIRMED move): the true line goes on the ledger row, the tip's further
    Sportsbet sends are stopped, ONE alert names the real line, the end-of-tip message says stopped;
  * the receipt cannot be read within SB_RECEIPT_WAIT_SEC: when the fill odds are more than
    SB_RELINE_ODDS_PCT above the odds checked for that send, ONE alert and a LINE UNVERIFIED note, NO stop;
    otherwise only an info log.

The stop lives on the tip (tip._sb_reline) and in a small JSON file so a restart, a model re-offer or a
queued top-up of the same selection is stopped too. It is kept until the event's start + 6 h when the start
is known, else 24 h (SB_RELINE_STOP_TTL_SEC): a re-offer an hour later must not bet the moved line. This module is
imported by main, notifier and tennis_books alike, so there is ONE copy of the table (importing main
from notifier loads a second copy when main runs as __main__).
"""
import json
import logging
import math
import os
import re
import sys
import threading
import time

import config
import sb_receipt

log = logging.getLogger("tipbot.sb_reline")

_LOCK = threading.Lock()
_STOPS: dict = {}            # selection key -> {"at": epoch, "info": {...}}
_LOADED = [False]


def _cfg(name, default):
    return getattr(config, name, default)


# ── persistence ──────────────────────────────────────────────────────────────────────────────────

def _path() -> str:
    if os.getenv("TIPBOT_TESTING") or "pytest" in sys.modules:
        import tempfile
        return os.path.join(tempfile.gettempdir(), f"sb_reline_stops_{os.getpid()}.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sb_reline_stops.json")


def _ttl() -> float:
    """How long a stop is kept when the event's start is not known (default 24 h)."""
    try:
        v = float(_cfg("SB_RELINE_STOP_TTL_SEC", 86400.0))
        return v if math.isfinite(v) and v >= 0 else 86400.0
    except (TypeError, ValueError):
        return 86400.0


def _after_start() -> float:
    try:
        v = float(_cfg("SB_RELINE_STOP_AFTER_START_SEC", 21600.0))
        return v if math.isfinite(v) and v >= 0 else 21600.0
    except (TypeError, ValueError):
        return 21600.0


def _expiry(start, now=None) -> float:
    """When a stop made now ends: the event's start + 6 h when the start (epoch) is known, but never
    sooner than now + 24 h: a start recorded hours early (or one already in the past) must not end the
    stop before the real game. Unknown start: now + 24 h."""
    now = time.time() if now is None else now
    st = _fl(start)
    if st is not None and st > 1e9:
        return max(st + _after_start(), now + _ttl())
    return now + _ttl()


def _load_locked() -> None:
    if _LOADED[0]:
        return
    _LOADED[0] = True
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(v.get("info"), dict):
                    try:
                        exp = float(v["exp"]) if v.get("exp") is not None else float(v.get("at")) + _ttl()
                        if now <= exp:
                            _STOPS[str(k)] = {"at": float(v.get("at") or now), "exp": exp, "info": v["info"]}
                    except (TypeError, ValueError):
                        continue
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error(f"sb_reline: could not read {_path()} ({e}); starting with no stops")


def _save_locked() -> None:
    """Atomic write. A failure is logged, never raised: the in-memory stop still holds."""
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STOPS, f)
        os.replace(tmp, path)
    except Exception as e:
        log.error(f"sb_reline: could not save the stops ({e}); they hold in memory until a restart")


def reset(clear_file: bool = True) -> None:
    """Forget the in-memory table (tests; a restart does the same). Optionally delete the file."""
    with _LOCK:
        _STOPS.clear()
        _LOADED[0] = False
    if clear_file:
        try:
            os.remove(_path())
        except OSError:
            pass


# ── the stop table ───────────────────────────────────────────────────────────────────────────────

def _fl(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if (math.isfinite(v) and v > 0) else None


def key_for(tip):
    """tipster + event + leg 0's player / team, stat, side, line, market (the TIP's selection). None when
    the tip has no leg."""
    try:
        leg = tip.legs[0]
        ln = _fl(getattr(leg, "line", None))
        return "|".join([
            str(getattr(tip, "tipster", "") or "").strip().lower(),
            str(getattr(tip, "event", "") or "").strip().lower(),
            str(getattr(leg, "player", "") or getattr(leg, "team_full", "") or "").strip().lower(),
            str(getattr(leg, "stat", "") or "").strip().lower(),
            str(getattr(leg, "selection", "") or "").strip().lower(),
            f"{ln:g}" if ln is not None else str(getattr(leg, "line", "") or ""),
            str(getattr(leg, "market", "") or "").strip().lower()])
    except Exception:
        return None


def info(tip):
    """The stop on this tip (its info dict), else None. Reads the tip, then the table (which was reloaded
    from the file after a restart); a table hit is copied onto the tip."""
    if not _cfg("SB_RELINE_GUARD", True):
        return None
    try:
        got = getattr(tip, "_sb_reline", None)
    except Exception:
        got = None
    if got:
        return got
    key = key_for(tip)
    if key is None:
        return None
    with _LOCK:
        _load_locked()
        hit = _STOPS.get(key)
        if hit and time.time() <= hit["exp"]:
            try:
                tip._sb_reline = hit["info"]
            except Exception:
                pass
            return hit["info"]
        if hit:
            _STOPS.pop(key, None)
            _save_locked()
    return None


def stopped(tip) -> bool:
    try:
        return info(tip) is not None
    except Exception:
        return False


def _register(tip, data: dict, start=None) -> bool:
    """Record the stop; True when this call is the FIRST for the selection (it owns the alert). `start` is
    the event's start epoch when known (the stop then lasts until start + 6 h, else 24 h)."""
    key = key_for(tip)
    with _LOCK:
        _load_locked()
        if getattr(tip, "_sb_reline", None) is not None:
            return False
        hit = _STOPS.get(key) if key is not None else None
        if hit and time.time() <= hit["exp"]:
            tip._sb_reline = hit["info"]
            return False
        tip._sb_reline = data
        if key is not None:
            _STOPS[key] = {"at": time.time(), "exp": _expiry(start), "info": data}
            _save_locked()
        return True


# ── the judge ────────────────────────────────────────────────────────────────────────────────────

class Verdict:
    """kind: "moved" (receipt line differs: stop), "unverified" (no receipt, price jump: alert only) or
    None. note goes on the ledger row; line is the TRUE line (moved only). v6.237: `data` is what the
    stop and the alert need (assess() fills it; nothing is touched until _commit or alert_late)."""
    __slots__ = ("kind", "note", "line", "price", "first", "data")

    def __init__(self, kind=None, note="", line=None, price=None, first=False, data=None):
        self.kind, self.note, self.line, self.price, self.first = kind, note, line, price, first
        self.data = data

    def __bool__(self):
        return self.kind is not None


def _lt(x) -> str:
    v = _fl(x)
    return f"{v:g}" if v is not None else str(x)


def tip_line(tip):
    try:
        return tip.legs[0].line
    except Exception:
        return None


def next_line_text(tip, sent_line):
    """The LIKELY moved line when the receipt could not be read: the next line in the tip's direction
    (over: +1, under: -1), or None."""
    ln = _fl(sent_line)
    if ln is None:
        return None
    try:
        toks = re.split(r"[^a-z]+", str(getattr(tip.legs[0], "selection", "") or "").lower())
    except Exception:
        return None
    if "over" in toks:
        return f"{ln + 1:g}"
    if "under" in toks and ln - 1 > 0:
        return f"{ln - 1:g}"
    return None


def assess(tip, *, bookie, sid, checked_odds, fill_odds, stake, bet_id, sent_line, player=None, stat=None,
           label=None, since_ts=None):
    """v6.237 (Wilson 4 Nov): the verdict on ONE Sportsbet fill WITHOUT side effects (no stop, no alert).
    judge() acts on it as v6.232 did; the deferred look (a bet found late) writes its row with it and sends
    alert_late(). `since_ts` = the send time of a bet found late: the receipt is looked for in the logs
    written since then (sb_receipt.window_since), not only the last 300 s. Never raises."""
    try:
        if not _cfg("SB_RELINE_GUARD", True) or "sportsbet" not in str(bookie or "").lower():
            return Verdict()
        bid = str(bet_id or "").strip()
        if not bid:
            return Verdict()
        acct = label or str(sid)
        if label is None:
            try:
                import session_priority
                acct = session_priority.session_label(sid, "sportsbet")
            except Exception:
                acct = str(sid)
        checked, filled = _fl(checked_odds), _fl(fill_odds)
        _wkw = {}
        if since_ts is not None:
            _rs, _tb = sb_receipt.window_since(since_ts)
            _wkw = {"recent_sec": _rs, **({"tail_bytes": _tb} if _tb else {})}
        rec = sb_receipt.find(bid, str(_cfg("SB_RECEIPT_LOG_ROOT", "") or ""),
                              wait_sec=float(_cfg("SB_RECEIPT_WAIT_SEC", 6.0) or 0.0), **_wkw)
        if rec is not None:
            moved, true = sb_receipt.true_line(rec, sent_line)
            if not moved:
                if _fl(sent_line) is None:
                    log.info(f"SB re-line guard: bet {bid} on {acct}: this bet has no line to compare "
                             f"(moneyline / no line sent): nothing to do")
                else:
                    log.info(f"SB re-line guard: bet {bid} on {acct}: receipt line matches the line sent "
                             f"({_lt(sent_line)}): nothing to do")
                return Verdict()
            price = rec.price or filled
            note = (f"LINE MOVED: Sportsbet filled {_lt(true)} @ {price} (receipt), "
                    f"tipped {_lt(tip_line(tip))}")
            data = {"kind": "moved", "line": true, "price": price, "checked": checked, "sid": str(sid),
                    "account": acct, "bet_id": bid, "stake": stake, "sent_line": sent_line,
                    "tip_line": tip_line(tip), "player": player, "stat": stat, "at": time.time()}
            return Verdict("moved", note, true, price, False, data)
        # no receipt
        if checked is None and not getattr(tip, "_sb_nocheck_warned", False):
            try:
                tip._sb_nocheck_warned = True
            except Exception:
                pass
            log.warning(f"SB re-line guard: bet {bid} on {acct}: the receipt could not be read and there is "
                        f"no checked price to compare the fill with: this tip's fills cannot be judged")
        pct = _fl(_cfg("SB_RELINE_ODDS_PCT", 0.05)) or 0.05
        if checked is not None and filled is not None and checked > 1.0 and filled > 1.0 \
                and round(filled, 4) > round(checked * (1.0 + pct), 4):
            note = (f"LINE UNVERIFIED (receipt not read): filled {filled:.2f} vs checked {checked:.2f} "
                    f"(line may have moved)")
            log.warning(f"SB re-line guard: bet {bid} on {acct}: receipt not read, fill {filled:.2f} is "
                        f"{(filled / checked - 1) * 100:.1f}% over the checked {checked:.2f}: alert, no stop")
            return Verdict("unverified", note, None, filled, False,
                           dict(checked=checked, filled=filled, account=acct, bet_id=bid, stake=stake,
                                sent_line=sent_line, player=player, stat=stat))
        log.info(f"SB re-line guard: bet {bid} on {acct}: receipt not read within "
                 f"{_cfg('SB_RECEIPT_WAIT_SEC', 6.0)}s and no price jump: nothing to do")
        return Verdict()
    except Exception as e:
        log.error(f"SB re-line guard: judge crashed ({type(e).__name__}: {e}); no verdict")
        return Verdict()


def _alert_unverified_once(tip, v, notify) -> None:
    """v6.232: the LINE UNVERIFIED alert, one per tip."""
    if not getattr(tip, "_sb_unverified_alerted", False):      # one alert per tip
        try:
            tip._sb_unverified_alerted = True
        except Exception:
            pass
        _alert_unverified(tip, v.data or {}, notify)


def judge(tip, *, bookie, sid, checked_odds, fill_odds, stake, bet_id, sent_line, player=None, stat=None,
          label=None, notify=None, register=True, event_start=None):
    """Judge ONE Sportsbet fill. Never raises (a failure reads as "no verdict" and is logged).
    `notify` is the critical-alert callable (default notifier.notify_critical). register=False judges
    without touching the tip / table (tennis keeps its own plan flag). v6.237: assess() then the v6.232
    stop and alert, unchanged."""
    try:
        v = assess(tip, bookie=bookie, sid=sid, checked_odds=checked_odds, fill_odds=fill_odds, stake=stake,
                   bet_id=bet_id, sent_line=sent_line, player=player, stat=stat, label=label)
        if v.kind == "moved":
            data = v.data
            first = _register(tip, data, event_start) if register else False
            v.first = first
            log.warning(f"SB RE-LINE CONFIRMED: {data['account']} bet {data['bet_id']} was booked on "
                        f"{_lt(v.line)} @ {v.price} (receipt), line sent {_lt(sent_line)}, tipped "
                        f"{_lt(tip_line(tip))}; {'this tip is STOPPED' if first else 'tip already stopped'}")
            if first or not register:
                _alert_moved(tip, data, notify)
        elif v.kind == "unverified":
            _alert_unverified_once(tip, v, notify)
        return v
    except Exception as e:
        log.error(f"SB re-line guard: judge crashed ({type(e).__name__}: {e}); no verdict")
        return Verdict()


def alert_late(tip, v, notify=None) -> None:
    """v6.237 (Wilson 4 Nov: a late-found re-lined bet is recorded at its real line): the alert for a bet
    the deferred look found late, after its row is written. It sets NO stop: the look runs minutes after
    the send and can match another tip's bet, so a stop there protects little and stopped the wrong tip.
    A move = the LINE MOVED (late) critical; the caller sends it once per bet id (main's
    _DEFERRED_LANDED_DONE). Unverified = the v6.232 alert, one per tip. Never raises."""
    try:
        if not v:
            return
        if v.kind == "moved":
            _alert_moved_late(tip, v.data or {}, notify)
        elif v.kind == "unverified":
            _alert_unverified_once(tip, v, notify)
    except Exception as e:
        log.error(f"SB re-line guard: late alert failed ({type(e).__name__}: {e})")


# ── alerts ───────────────────────────────────────────────────────────────────────────────────────

def _what(tip, player, sent_line) -> str:
    try:
        leg = tip.legs[0]
        bits = [player or getattr(leg, "player", "") or getattr(leg, "team_full", "") or "",
                getattr(leg, "selection", "") or "", _lt(tip_line(tip)) if tip_line(tip) is not None else "",
                getattr(leg, "stat", "") or ""]
        return " ".join(str(b) for b in bits if b not in (None, "")).strip()
    except Exception:
        return str(player or "?")


def _send(msg: str, notify) -> None:
    try:
        if notify is None:
            import notifier
            notify = notifier.notify_critical
        notify(msg)
    except Exception as e:
        log.error(f"SB re-line guard: the alert could not be sent ({e})")


def _alert_moved(tip, d: dict, notify=None) -> None:
    sent = d.get("sent_line")
    tl = d.get("tip_line")
    row = (f" (this send was the {_lt(sent)} row)" if _fl(sent) is not None and _fl(tl) is not None
           and abs(_fl(sent) - _fl(tl)) > 1e-6 else "")
    stk = _fl(d.get("stake"))
    chk = _fl(d.get("checked"))
    _send(
        f"SPORTSBET MOVED THE LINE (confirmed by the bet receipt). Tipster {getattr(tip, 'tipster', '?')}: "
        f"{_what(tip, d.get('player'), sent)}. Tipped line {_lt(tl)}{row}. Sportsbet booked "
        f"{_lt(d.get('line'))} @ {d.get('price')}" + (f" (the odds checked were {chk:.2f})" if chk else "")
        + f". Account {d.get('account')}, stake {('$%.2f' % stk) if stk else '?'}, bet id {d.get('bet_id')}. "
        f"Check the bet slip. This tip is STOPPED: no more accounts, rebets, spills or top-ups will be "
        f"sent, and the rest is NOT to be placed by hand until the slip is checked.", notify)


def _alert_moved_late(tip, d: dict, notify=None) -> None:
    """v6.237 (Wilson 4 Nov): a moved bet the deferred look found late. Its row has the real line; no stop."""
    stk = _fl(d.get("stake"))
    _send(
        f"SPORTSBET MOVED THE LINE on a bet found late (confirmed by the bet receipt). Tipster "
        f"{getattr(tip, 'tipster', '?')}: {_what(tip, d.get('player'), d.get('sent_line'))}. Sportsbet booked "
        f"{_lt(d.get('line'))} @ {d.get('price')}, the tip posted {_lt(d.get('sent_line'))}. Account "
        f"{d.get('account')}, stake {('$%.2f' % stk) if stk else '?'}, bet id {d.get('bet_id')}. "
        f"The ledger row has the real line ({_lt(d.get('line'))}). No stop was set. If this looks like another "
        f"tip's bet, this tip may not have landed: CHECK THE ACCOUNT before placing by hand.", notify)


def _alert_unverified(tip, d: dict, notify=None) -> None:
    sent = d.get("sent_line")
    likely = next_line_text(tip, sent)
    tl = tip_line(tip)
    row = (f" (this send was the {_lt(sent)} row)" if _fl(sent) is not None and _fl(tl) is not None
           and abs(_fl(sent) - _fl(tl)) > 1e-6 else "")
    stk = _fl(d.get("stake"))
    guess = (f" If it moved, the likely line is {likely} (the next line in the tip's direction; a guess)."
             if likely else "")
    _send(
        f"SPORTSBET LINE MAY HAVE MOVED (the bet receipt could not be read). Tipster "
        f"{getattr(tip, 'tipster', '?')}: {_what(tip, d.get('player'), sent)}. Tipped line {_lt(tl)}{row}. "
        f"Checked odds {d['checked']:.2f}, filled odds {d['filled']:.2f} "
        f"(+{(d['filled'] / d['checked'] - 1) * 100:.1f}%). Sportsbet may have filled a moved line.{guess} "
        f"Account {d.get('account')}, stake {('$%.2f' % stk) if stk else '?'}, bet id {d.get('bet_id')}. "
        f"Check the bet slip. The tip is NOT stopped.", notify)
