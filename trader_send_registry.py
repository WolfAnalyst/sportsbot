"""v6.203: a process-wide register of every bet POST the bot sends on a trader session (Betdeluxe 99999, BetRight
99996, the two TAB accounts), kept for about 30 minutes after the reply.

Why (trader_balance_watch): a Betdeluxe balance that falls by a trader-referred bet's stake is the bot's only early
sign that the trader accepted it, but the bot's OWN bets on the same account debit the balance too, and their
ledger rows (stamped when they are WRITTEN, at the end of the tip) say nothing about WHEN the debit happened. So
every POST is noted here from before it goes out until its reply: session, stake, start, end (None while in
flight), outcome ("placed" with the stake placed, "refused" when nothing was sent, "unknown" otherwise). The
balance watch uses the window to subtract the bot's own debits from a balance step, or to withhold its message
when an own send could be the whole step.

Plain data and one lock; nothing here sends, places or decides anything. Entries are dicts so tests can build them."""
import itertools
import math
import threading
import time

KEEP_SEC = 30 * 60
OPEN_MAX_SEC = 10 * 60      # an entry with no reply after this long counts as "unknown" (then pruned like any other)
_LOCK = threading.Lock()
_SENDS: list = []
_IDS = itertools.count(1)


def _prune(now: float) -> None:
    for e in _SENDS:
        if e["end"] is None and now - e["start"] > OPEN_MAX_SEC:
            e["end"], e["outcome"] = e["start"] + OPEN_MAX_SEC, "unknown"      # never answered: in doubt, then pruned
    cutoff = now - KEEP_SEC
    _SENDS[:] = [e for e in _SENDS if e["end"] is None or e["end"] >= cutoff]


def begin(session_id, stake, tip_id="", now=None):
    """Note a POST about to go out. Returns the entry (give it back to end()), or None for a stake that is not a
    positive finite number."""
    t = time.time() if now is None else float(now)
    if isinstance(stake, bool):
        return None
    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(stake) or stake <= 0:
        return None                 # not a stake: nothing to register (the POST still goes)
    e = {"id": next(_IDS), "sid": str(session_id), "stake": stake, "tip": str(tip_id or ""),
         "start": t, "end": None, "outcome": None, "actual": None}
    with _LOCK:
        _prune(t)
        _SENDS.append(e)
    return e


def end(entry, outcome: str, actual=None, now=None) -> None:
    """The reply came (or the POST raised): outcome "placed" (actual = the stake placed), "refused" (nothing was
    sent) or "unknown"."""
    if not entry:
        return
    t = time.time() if now is None else float(now)
    with _LOCK:
        entry["end"] = t
        entry["outcome"] = outcome if outcome in ("placed", "refused", "unknown") else "unknown"
        if actual is not None:
            try:
                entry["actual"] = float(actual)
            except (TypeError, ValueError):
                entry["actual"] = None


def window(session_id, t0: float, t1: float, exclude_tip=None) -> list:
    """The sends on this session whose window overlaps (t0, t1]: started by t1 and not finished before t0. Copies.
    `exclude_tip`: a tip id whose sends are left out (the referral being watched is one of them)."""
    out = []
    with _LOCK:
        _prune(time.time())
        for e in _SENDS:
            if e["sid"] != str(session_id):
                continue
            if exclude_tip is not None and e["tip"] == str(exclude_tip):
                continue
            if e["start"] <= t1 and (e["end"] is None or e["end"] >= t0):
                out.append(dict(e))
    return out


def clear() -> None:
    with _LOCK:
        _SENDS.clear()
