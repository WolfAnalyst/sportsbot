"""bets_placed.csv ledger — ONE row per landed bet, for Excel (Power Query).

Written from notifier.py at every placement-success across ALL paths
(singles / fan-out / SGM / MLB via the sports notifiers, racing + Tip Titans
via the racing notifier). v5.21 (Wilson 2026-06-06).

Design guarantees:
  * Every write is wrapped — a ledger failure can NEVER break a placement.
  * bet_id dedup: a path that notifies twice (per-bet AND in a summary) logs
    the bet only once.
  * CSV (not xlsx) so Excel links to it via Power Query without locking the bot
    out and without a corruption risk. The 19 columns are a superset; map/hide
    in Excel as needed. Closes the audit's coverage gap (racing + MLB previously
    wrote no machine-readable placement record).
"""
import csv
import os
import re
import logging
from datetime import datetime
from threading import Lock

log = logging.getLogger("tipbot")

def _default_ledger_path() -> str:
    """Resolve the ledger file path. v5.41 (2026-06-08): the unit suite was
    POLLUTING the production logs/bets_placed.csv with fixture rows (bet_id
    X1/X2) on every run — `notify_*` tests incidentally trigger a ledger write,
    and the path was hard-coded. Now: BET_LEDGER_PATH env override wins; else
    TIPBOT_TESTING (set by the test harness) forces a temp file so a test can
    NEVER touch the real ledger; else the production path."""
    env = os.getenv("BET_LEDGER_PATH")
    if env:
        return env
    if os.getenv("TIPBOT_TESTING"):
        import tempfile
        return os.path.join(tempfile.gettempdir(), "tipbot_test_bets_placed.csv")
    return os.path.join("logs", "bets_placed.csv")


_LEDGER_PATH = _default_ledger_path()
COLUMNS = [
    "placed_at", "date", "tipster", "sport", "event", "market", "selection",
    "line", "side", "bookie", "account", "stake", "odds", "potential_return",
    "potential_profit", "bet_id", "correlation_id", "units", "unit_size",
    # v5.92: free-text note — e.g. "sportsbet:65463 403 proxy error — fixed on
    # re-bet". Appended column (the ledger is a documented superset; existing rows
    # leave it blank, a fresh CSV headers it). Blank for the vast majority of bets.
    "note",
]
_lock = Lock()
_logged_bet_ids: set = set()
_seeded = False


def _seed_logged_ids() -> None:
    """v5.69 (i5): seed the dedup set from the existing CSV ONCE so dedup
    survives a process restart. Without this the in-memory set started empty
    every process, so a re-notification / replay of a placement made BEFORE the
    restart appended a DUPLICATE row. Best-effort; never raises. Caller holds
    _lock."""
    global _seeded
    if _seeded:
        return
    _seeded = True
    try:
        if os.path.exists(_LEDGER_PATH) and os.path.getsize(_LEDGER_PATH) > 0:
            with open(_LEDGER_PATH, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    bid = str((r.get("bet_id") or "")).strip()
                    if bid:
                        _logged_bet_ids.add(bid)
    except Exception as e:
        log.error(f"bet_ledger: failed to seed dedup ids from CSV: {e}")


def _num(v):
    """Coerce to float or return None (blank cell) — never raises."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _direction_of(*candidates) -> str:
    """First candidate yielding a whitelisted over/under direction, else "".

    v6.07 (sweep #31). TOKEN-based on purpose: the old `"over" in selection.lower()`
    substring test would label a horse named "Overlord" or a player "Overton" as an
    OVER bet. Only a standalone over/under token counts, and anything else (a horse
    name on a racing win row, a team on a handicap) correctly yields "" rather than a
    made-up direction."""
    for c in candidates:
        toks = re.split(r"[^a-z]+", str(c or "").strip().lower())
        if "over" in toks:
            return "over"
        if "under" in toks:
            return "under"
    return ""


def _round2(v):
    n = _num(v)
    return round(n, 2) if n is not None else ""


def _write_row(row: dict) -> None:
    """Append one row to bets_placed.csv (dedup on bet_id). Fully guarded."""
    try:
        bet_id = str(row.get("bet_id") or "").strip()
        if not bet_id:
            return  # a landed bet always has a bet_id; skip malformed/blank rows
        with _lock:
            _seed_logged_ids()  # v5.69 (i5): make dedup survive a restart
            if bet_id in _logged_bet_ids:
                return
            _logged_bet_ids.add(bet_id)
            os.makedirs(os.path.dirname(_LEDGER_PATH) or ".", exist_ok=True)
            fresh = (not os.path.exists(_LEDGER_PATH)
                     or os.path.getsize(_LEDGER_PATH) == 0)
            with open(_LEDGER_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
                if fresh:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in COLUMNS})
    except Exception as e:  # logging must never break placement
        log.error(f"bet_ledger: failed to write row (bet_id={row.get('bet_id')}): {e}")


def _now():
    n = datetime.now()
    return n.isoformat(timespec="seconds"), n.date().isoformat()


def log_sports_bet(tip, result, account: str = "") -> None:
    """Log a placed sports bet (singles / fan-out / SGM / MLB). tip is a
    ParsedTip, result a BetResult."""
    try:
        placed_at, today = _now()
        legs = list(getattr(tip, "legs", None) or [])
        is_sgm = bool(getattr(tip, "is_sgm", False))
        # Prefer the actually-placed snapshot (post line-tolerance/handicap flip).
        market = (getattr(result, "placed_market", None)
                  or (legs[0].market if legs else "") or "")
        if is_sgm and len(legs) > 1:
            market = "SGM"
            selection = " / ".join(
                f"{(l.player or l.selection or '').strip()} "
                f"{(l.selection if l.player else '')} {l.line or ''} {l.stat or ''}".strip()
                for l in legs
            )
            line = ""
        else:
            selection = (getattr(result, "placed_selection", None)
                         or (legs[0].selection if legs else "")
                         or (legs[0].player if legs else "") or "")
            line = (getattr(result, "placed_line", None)
                    if getattr(result, "placed_line", None) is not None
                    else (legs[0].line if legs else ""))
            line = "" if line is None else line
        # v6.07 (sweep #31): side used to be sniffed ONLY from the display selection,
        # so it was filled when placed_selection happened to embed the word ("Colby
        # McKercher Under") and BLANK when it was just the player name ("Lachie
        # Neale") - 521 of 1,095 player_disposals rows blank, inconsistent within the
        # SAME market. Prefer the STRUCTURED leg direction, which is set either way.
        # Token-based, not substring: "over" in "Overlord"/"Overton" used to read as
        # an OVER bet on a racing win row.
        side = _direction_of(
            (legs[0].selection if legs and not (is_sgm and len(legs) > 1) else ""),
            getattr(result, "placed_selection", None),
            selection,
        )
        stake = _num(getattr(result, "stake", None))
        odds = _num(getattr(result, "odds", None))
        _write_row({
            "placed_at": placed_at,
            "date": today,
            "tipster": getattr(tip, "tipster", "") or "",
            "sport": getattr(tip, "sport", "") or "",
            "event": getattr(tip, "event", "") or "",
            "market": market,
            "selection": selection,
            "line": line,
            "side": side,
            "bookie": getattr(result, "bookie", "") or "",
            "account": account or str(getattr(result, "session_id", "") or ""),
            "stake": _round2(stake),
            "odds": odds if odds is not None else "",
            "potential_return": _round2(stake * odds) if (stake and odds) else "",
            "potential_profit": _round2(stake * (odds - 1)) if (stake and odds) else "",
            "bet_id": getattr(result, "bet_id", "") or "",
            "correlation_id": getattr(result, "correlation_id", "") or "",
            "units": _num(getattr(tip, "units", None)) if getattr(tip, "units", None) is not None else "",
            "unit_size": _num(getattr(tip, "unit_size", None)) if getattr(tip, "unit_size", None) is not None else "",
            "note": getattr(result, "_recovered_note", "") or "",
        })
    except Exception as e:
        log.error(f"bet_ledger.log_sports_bet failed: {e}")


def log_racing_bet(parsed: dict, placement: dict, account: str = "") -> None:
    """Log a placed racing bet (Tip Titans / Zak / Trial). parsed is the racing
    tip dict, placement is one {session_id,bookie,stake,odds,bet_id} entry."""
    try:
        placed_at, today = _now()
        track = parsed.get("track") or ""
        race_num = parsed.get("race_num")
        saddle = parsed.get("saddle")
        runner = parsed.get("runner") or ""
        selection = f"{saddle}. {runner}".strip(". ").strip() if saddle else runner
        stake = _num(placement.get("stake"))
        odds = _num(placement.get("odds"))
        _write_row({
            "placed_at": placed_at,
            "date": parsed.get("date") or today,
            "tipster": parsed.get("titan") or parsed.get("tipster") or "",
            "sport": "racing",
            "event": f"{track} R{race_num}".strip() if race_num is not None else track,
            "market": parsed.get("market") or "win",
            "selection": selection,
            "line": "",
            "side": "",
            "bookie": placement.get("bookie") or "",
            "account": account or str(placement.get("session_id") or ""),
            "stake": _round2(stake),
            "odds": odds if odds is not None else "",
            "potential_return": _round2(stake * odds) if (stake and odds) else "",
            "potential_profit": _round2(stake * (odds - 1)) if (stake and odds) else "",
            "bet_id": placement.get("bet_id") or "",
            "correlation_id": placement.get("correlation_id") or "",
            "units": _num(parsed.get("units")) if parsed.get("units") is not None else "",
            "unit_size": "",
        })
    except Exception as e:
        log.error(f"bet_ledger.log_racing_bet failed: {e}")
