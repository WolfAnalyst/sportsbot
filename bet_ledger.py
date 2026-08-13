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
    # v6.08g: the specific Tip Titans titan (OC), or the standalone racing tipster's
    # display code. Blank for sports rows. See the `tipster` note below — this exists so
    # making `tipster` uniform does not throw away which titan a Tip Titans bet came from.
    "titan",
    # v6.08r: WHICH runner the bookie actually bound, and HOW it was matched
    # (exact_name / substring_name / saddle_#N). Racing rows only; blank for sports.
    #
    # The ledger previously recorded ONLY the tipster's own string, so a saddle-only
    # match - which is a POSITIONAL guess, not a name match - was invisible downstream.
    # 2026-08-11 tip 62701 read "RUBY RHAYNE BOW" and all four priced bookies bound
    # saddle #5 to "Rebecca Rhayne Bow"; $700.00 went on and every review of the ledger
    # would read it back as the tipster's runner. Recording the matched name and the
    # method makes a wrong-runner placement greppable after the fact, which is the whole
    # point of keeping a ledger.
    "runner_match",
    "match_method",
]

# v6.08g — `tipster` IS NOW UNIFORM. It previously carried two conventions depending on
# which writer produced the row: racing wrote the uppercase titan code from
# parsed["titan"] (OC / ZAK / TRIAL / LEROY) while sports wrote the lowercase internal id
# from tip.tipster (eddie_afl / saiyan_afl / shook / ...). Joining that column against
# config.TIPSTER_CHANNELS therefore worked for sports and SILENTLY FAILED for racing —
# the sort of mismatch that halves a P/L report without erroring. Every row now carries
# the lowercase internal id, and the titan code moved to `titan` so nothing is lost.

# Titan display code -> stable source id. Tip Titans is one FEED with several titans, so
# every titan maps to `tiptitans` and the specific titan stays visible in `tipster`.
# UNKNOWN CODES MAP TO tiptitans on purpose, the same fail-safe direction as
# notifier._RACING_SOURCE_LABELS: an unrecognised code is far more likely to be a new
# titan than a new standalone tipster. The two maps must keep the same key set —
# test_bets_ledger_tipster_id.py pins that so they cannot drift apart.
_TITAN_TO_TIPSTER_ID = {
    # standalone racing tipsters — their own feeds, NOT Tip Titans
    "ZAK": "zak_racing",
    "TRIAL": "trial_sniper",
    "LEROY": "leroy",
    # known Tip Titans codes, listed explicitly so the mapping is case-insensitive for
    # them too rather than relying on the all-caps heuristic below. OC is the only titan
    # seen in 1,081 rows of history; a new one falls to _TITAN_DEFAULT_ID.
    "OC": "tiptitans",
}
_TITAN_DEFAULT_ID = "tiptitans"


def titan_code_for(tipster: str) -> str:
    """The titan/racing display code for a `tipster` cell, or "" if it is a sports id.

    Used to populate the `titan` column and to preserve it when normalising history.
    A titan code is ALL-CAPS by convention at both writers (parsed["titan"]).
    """
    t = str(tipster or "").strip()
    if not t:
        return ""
    if t.upper() in _TITAN_TO_TIPSTER_ID:
        return t.upper()
    return t.upper() if t.isupper() else ""


def tipster_id_for(tipster: str) -> str:
    """Stable source id for a `tipster` cell, from EITHER writer's convention.

    Sports ids already are the internal id and pass through unchanged. A racing titan
    code maps via _TITAN_TO_TIPSTER_ID. Deterministic and pure, so it can also backfill
    history from the existing column.
    """
    t = str(tipster or "").strip()
    if not t:
        return ""
    if t.upper() in _TITAN_TO_TIPSTER_ID:
        return _TITAN_TO_TIPSTER_ID[t.upper()]
    # Sports rows are already lowercase internal ids (eddie_afl, saiyan_afl, shook, ...).
    # Anything else that is ALL-CAPS is a titan code we have not seen before.
    if t.isupper():
        return _TITAN_DEFAULT_ID
    return t


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
    _migrate_ledger_columns()
    try:
        if os.path.exists(_LEDGER_PATH) and os.path.getsize(_LEDGER_PATH) > 0:
            with open(_LEDGER_PATH, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    bid = str((r.get("bet_id") or "")).strip()
                    if bid:
                        _logged_bet_ids.add(bid)
    except Exception as e:
        log.error(f"bet_ledger: failed to seed dedup ids from CSV: {e}")


def _migrate_ledger_columns() -> bool:
    """Bring an existing bets_placed.csv up to the current COLUMNS, once.

    THE ROOT CAUSE THIS FIXES. `_write_row` only writes a header when the file is FRESH,
    so appending a column to COLUMNS silently desynced the file: `note` was added in v5.92
    and every row written since carried 20 fields under a 19-column header. Measured on the
    live file 2026-08-03: 1,640 rows at 19 fields, 1,927 at 20, and **10 rows with real
    stranded `note` text** (the 403-proxy re-bet annotations) that no consumer could read
    by name, because DictReader files a surplus value under the None key. Every reader here
    uses DictReader, which is why it stayed invisible rather than crashing.

    Repairs in ONE atomic pass: writes the full header, pads short rows, and backfills
    `tipster_id` from the existing `tipster` cell (pure and deterministic, so history gets
    the same value it would have been written with). Keeps a one-off `.bak_precolmigrate`
    copy — this is a money ledger and the rewrite touches every row.

    Returns True if it migrated. Idempotent: a file whose header already matches is left
    untouched, so this is safe to call on every process start. Never raises; on any failure
    the original file is left exactly as it was and appends continue as before.
    """
    try:
        if not os.path.exists(_LEDGER_PATH) or os.path.getsize(_LEDGER_PATH) == 0:
            return False
        with open(_LEDGER_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if not rows:
            return False
        header = rows[0]
        if header == COLUMNS:
            return False
        missing = [c for c in COLUMNS if c not in header]
        # `tipster_id` was a short-lived intermediate column (added and superseded on
        # 2026-08-03): its job is now done by the normalised `tipster`, so it is dropped
        # rather than carried. Anything ELSE unknown is real data and stops the migration.
        _RETIRED = {"tipster_id"}
        extra = [c for c in header if c not in COLUMNS and c not in _RETIRED]
        if extra:
            # A column we no longer know about. Do NOT drop data on a money ledger:
            # leave the file alone and say so loudly.
            log.error(
                f"bet_ledger: {_LEDGER_PATH} has unknown column(s) {extra} — NOT "
                f"migrating (refusing to drop ledger data). Reconcile COLUMNS by hand."
            )
            return False
        bak = _LEDGER_PATH + ".bak_precolmigrate"
        if not os.path.exists(bak):
            with open(_LEDGER_PATH, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
        idx = {name: i for i, name in enumerate(header)}
        tmp = _LEDGER_PATH + ".tmp_colmigrate"
        n = 0
        fixed_side = 0
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for raw in rows[1:]:
                if not raw:
                    continue
                rec = {}
                for name, i in idx.items():
                    rec[name] = raw[i] if i < len(raw) else ""
                # The surplus value a 20-field row carried under the old 19-col header
                # was `note`, in COLUMNS order. Recover it rather than discard it.
                if len(raw) > len(header):
                    for j, name in enumerate(
                            [c for c in COLUMNS if c not in header], start=len(header)):
                        if j < len(raw):
                            rec.setdefault(name, raw[j])
                            rec[name] = raw[j]
                # NORMALISE `tipster` to the internal id and preserve the titan code.
                # Order matters: read the ORIGINAL tipster value before overwriting it.
                _orig_tipster = rec.get("tipster")
                if not str(rec.get("titan") or "").strip():
                    rec["titan"] = titan_code_for(_orig_tipster)
                rec["tipster"] = tipster_id_for(_orig_tipster)
                # BACKFILL `side` on player props. v6.07 fixed the writer (side now comes
                # from the structured direction) but never backfilled history: 531 of 1,312
                # player-prop rows carried a blank side, all June/July, so a third of the
                # history could not be told over from under. The direction IS recoverable:
                # an UNDER's selection ends " Under" while an OVER's is the bare player
                # name, because an AFL over places on the base O/U market with a bare-name
                # selection (main.py's _match_afl_player_prop). Verified against the 781
                # rows that DO carry a side: ends_under -> under 689/689, bare name -> over
                # 92/92, zero counterexamples — and the same convention appears in live
                # /api/pending_bets text. This only materialises a derivable value.
                if ("player_" in str(rec.get("market") or "")
                        and not str(rec.get("side") or "").strip()):
                    _sel = str(rec.get("selection") or "").strip().lower()
                    if _sel.endswith(" under"):
                        rec["side"] = "under"
                    elif _sel.endswith(" over"):
                        rec["side"] = "over"
                    elif _sel:
                        rec["side"] = "over"      # bare player name = the over ladder
                    fixed_side += 1
                w.writerow({c: rec.get(c, "") for c in COLUMNS})
                n += 1
        os.replace(tmp, _LEDGER_PATH)
        log.warning(
            f"bet_ledger: migrated {_LEDGER_PATH} to {len(COLUMNS)} columns "
            f"(added {missing}), {n} row(s) rewritten, tipster normalised to the "
            f"internal id, titan preserved, {fixed_side} blank player-prop side(s) "
            f"backfilled. "
            f"Backup at {bak}"
        )
        return True
    except Exception as e:
        log.error(f"bet_ledger: column migration failed ({e}) — file left unchanged")
        try:
            os.unlink(_LEDGER_PATH + ".tmp_colmigrate")
        except Exception:
            pass
        return False


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
            # Uniform: the internal id. A sports tipster already IS one, but go through
            # the same helper both writers use so the column can never diverge again.
            "tipster": tipster_id_for(getattr(tip, "tipster", "")),
            "titan": "",          # sports bets have no titan

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
        # v6.08r: the bookie's own matched name + how it got there. `selection` keeps the
        # tipster's wording (every downstream consumer and the Bet Record feed depend on
        # it), so these go in ALONGSIDE it rather than replacing it.
        _rmatch = (placement.get("runner_match") or "").strip()
        _rmethod = (placement.get("match_method") or "").strip()
        stake = _num(placement.get("stake"))
        odds = _num(placement.get("odds"))
        _write_row({
            "placed_at": placed_at,
            "date": parsed.get("date") or today,
            # Uniform: the internal id, the SAME convention the sports writer uses, so
            # one join works across every row. The titan/display code moves to `titan`.
            "tipster": tipster_id_for(
                parsed.get("titan") or parsed.get("tipster") or ""),
            "titan": titan_code_for(
                parsed.get("titan") or parsed.get("tipster") or ""),
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
            "runner_match": _rmatch,
            "match_method": _rmethod,
        })
    except Exception as e:
        log.error(f"bet_ledger.log_racing_bet failed: {e}")
