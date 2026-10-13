"""NBA player-props model (tipbot contract v3): the money-safety core.

Everything here is PURE of main.py: the exposure ledger (permanent key dedup + per-selection
cap + the retry guard), the fills file the model reads back, the rebounds liability ladder
and its "confirmed" gate, player identity (roster + pid cross-check) and the start/quiet
gates. main.py is glue around it (`_process_nba_model_tip`, `_place_one_nba_row`).

It is the NBA twin of the `_disposals_*` block in main.py (the AFL v2 feed) and follows it
line for line where the rule is the same. It is kept out of main.py on purpose, so that a
mistake here cannot reach the AFL code and so that the AFL code stays byte-for-byte what is
live. Every setting is read from `config` AT CALL TIME (`cfg()`), never cached, so a test
can monkeypatch `config` and be certain it is not touching the live ledger.

ALL of this is OFF unless config.NBA_MODEL_ENABLED is true (it ships false). Nothing in this
module is imported on the AFL path.

The contract is docs/PIPELINE-DESIGN.md section 3.4 of the NBA model repo, derived from
DISPOSALS_MODEL_CONTRACT_v2.md. See NBA_MODEL_CONTRACT_v3.md in this repo for the tipbot side.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config as _config

log = logging.getLogger("tipbot.nba_model")

TIPSTER = "nba_model"
SPORT = "nba"

# The v3 vocabulary (parsers/nba_model.NBA_TEAMS) differs from tipbot's roster and ESPN
# schedule by ONE spelling: those say "LA Clippers" where the contract says
# "Los Angeles Clippers". Everything that talks to the roster, the ESPN schedule or the
# HyperBot event name goes through this map, so the contract spelling never leaks into a
# lookup that would miss it.
ROSTER_TEAM_NAMES = {"Los Angeles Clippers": "LA Clippers"}

# market -> (config name of its liability ladder, config name of its confirmed flag).
# A market with no entry here has NO ladder, and is refused at placement: adding a prop to
# NBA_MODEL_MARKETS is not enough to bet it, Wilson must also set its ladder.
LADDER_CONFIG_NAMES = {
    "player_rebounds": ("NBA_REB_LIABILITY_LADDER", "NBA_REB_LADDER_CONFIRMED"),
}

# The exact fields of one fills-file bet record (contract section 4; the same shape as
# logs/disposals_model_fills.jsonl). The model joins on `key`.
FILL_FIELDS = (
    "key", "evt", "pid", "player", "team", "event", "market", "side", "line", "kind",
    "attempt", "revision", "target", "requested", "placed", "ambiguous", "unfilled",
    "selection_placed", "selection_ambiguous", "selection_committed", "accounts",
    "outcome", "retryable", "final", "manual", "processed_at",
)
FILL_OUTCOMES = ("filled", "partial", "none", "manual", "refused", "capped")

_STATE_LOCK = threading.RLock()

# HyperBot is off 23:00-07:00 SYDNEY time, whatever the machine's own clock says.
SYDNEY = ZoneInfo("Australia/Sydney")

#: How long a ledger operation waits for the lock before it fails closed. SHORT on purpose:
#: the ledger is touched from the telethon event loop, which every other feed shares.
LOCK_TIMEOUT_SEC = 0.5

#: The commit that follows placed money has its OWN, much longer budget and runs off the event
#: loop (main.py): losing it loses per-account liability and the ambiguous flag.
COMMIT_LOCK_TIMEOUT_SEC = 10.0

#: Identifies THIS process run. Every reservation is stamped with it; a reservation carrying
#: another boot id belongs to a process that died mid-placement (see recover_pending).
BOOT_ID = uuid.uuid4().hex

SEEN, UNSEEN, UNKNOWN = "seen", "unseen", "unknown"


class LedgerLockError(RuntimeError):
    """The cross-process ledger lock could not be taken: fail closed."""


def cfg(name: str):
    """A config value, read fresh on every call."""
    return getattr(_config, name)


def roster_team(team_v3: str) -> str:
    """The roster / ESPN spelling of a v3 team name."""
    return ROSTER_TEAM_NAMES.get(team_v3, team_v3)


# ── Identity: names, the roster, the pid cross-check ──────────────────────────────────

def fold_name(s: str) -> str:
    """Accent-, case-, punctuation- and whitespace-insensitive form of a player name.

    'Nikola Jokić' == 'Nikola Jokic', 'Jaren Jackson Jr.' == 'Jaren Jackson Jr',
    "De'Aaron Fox" == 'DeAaron Fox'. The generational suffix is KEPT: 'Gary Payton II' and
    'Gary Payton' are different players."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower().replace("-", " ")
    s = re.sub(r"[.'`’]", "", s)
    return " ".join(s.split())


def resolve_player(player: str, team_v3: str):
    """Deterministic roster lookup for a model-supplied player. NO fuzzy matching.

    Returns (roster_name, roster_team, "") or ("", "", reason). The roster is nba_api-fresh
    (roster_nba.json). The match is exact on the case/accent/punctuation-folded full name
    and must be unique; the roster's team for him must equal the message's `team` (mapped
    to the roster spelling). Anything else is a REFUSAL: tipbot picks the player from
    `player` + `team`, so a wrong pick is a wrong bet, and a missed bet is cheap.

    The roster also carries surname-only alias keys ('Adebayo'); a match must be on a
    full name of two or more words so a bare surname can never resolve."""
    import roster
    roster._load_rosters()
    table = getattr(roster, "_nba_roster", None) or {}
    want_team = roster_team(team_v3)
    key = " ".join(str(player or "").split()).lower()
    if not key or len(key.split()) < 2:
        return "", "", f"player {player!r} is not a full name"
    info = table.get(key)
    if info and len(str(info.get("name", "")).split()) >= 2:
        hits = [info]
    else:
        folded = fold_name(player)
        seen, hits = set(), []
        for v in table.values():
            nm = str(v.get("name", ""))
            if len(nm.split()) < 2 or fold_name(nm) != folded:
                continue
            if (nm, v.get("team")) in seen:
                continue
            seen.add((nm, v.get("team")))
            hits.append(v)
    if not hits:
        return "", "", f"player {player!r} is not in the NBA roster (nothing placed)"
    if len(hits) > 1:
        return "", "", (f"player {player!r} matches {len(hits)} roster entries "
                        f"({[h['name'] + ' / ' + h['team'] for h in hits][:4]}), ambiguous")
    hit = hits[0]
    if str(hit.get("team", "")).strip().lower() != want_team.strip().lower():
        return "", "", (f"roster has {hit['name']!r} on {hit.get('team')!r} but the message "
                        f"says team={team_v3!r}: roster or message is stale, nothing placed")
    return str(hit["name"]), str(hit["team"]), ""


def canonical_catalog_player(name: str, catalog_players) -> str:
    """Sportsbet's own spelling of `name`, from the players its live catalog carries for the
    market. Folded-exact only: returns the catalog spelling when EXACTLY ONE distinct catalog
    player folds to the same name, else "" (the caller routes to manual). Never a fuzzy pick."""
    want = fold_name(name)
    if not want:
        return ""
    spellings = {str(p) for p in catalog_players if p and fold_name(p) == want}
    return spellings.pop() if len(spellings) == 1 else ""


def check_identity(state: dict, row: dict) -> str:
    """The pid cross-check. Returns a refusal reason, or "" when the identity is consistent.

    tipbot never picks the player from `pid` (it uses `player` + `team`), but the pid is half
    of the selection identity every cap keys on, and a wrong pid silently disables those
    caps (DISPOSALS_MODEL_ID_VALIDATION_ASK.md). The shape is checked in the parser; this
    checks CONSISTENCY against everything this ledger has ever admitted:
      * one pid is one player: a pid first seen as 'A' and now sent with the name 'B' refuses;
      * one (evt, player) is one pid: the same player in the same game under a second pid
        would mint a second selection with a fresh full allowance and walk past the cap."""
    ident = state.get("identity") or {}
    pid = str(row.get("pid") or "").strip()
    name = fold_name(row.get("player"))
    prev_name = (ident.get("pid_names") or {}).get(pid)
    if prev_name and prev_name != name:
        return (f"pid {pid} was admitted as {prev_name!r} and now arrives with player "
                f"{row.get('player')!r}: one of them is wrong")
    evt = str(row.get("evt") or "").strip().casefold()
    prev_pid = (ident.get("evt_players") or {}).get(f"{evt}~{name}")
    if prev_pid and prev_pid != pid:
        return (f"{row.get('player')!r} in event {row.get('evt')} was admitted under pid "
                f"{prev_pid} and now arrives as pid {pid}: refusing a second identity")
    return ""


# ── The rebounds liability ladder and its gate ────────────────────────────────────────

def ladder_for(market: str):
    """(ladder tuple or None, confirmed bool) for a market, from config."""
    names = LADDER_CONFIG_NAMES.get(market)
    if not names:
        return None, False
    ladder = getattr(_config, names[0], None)
    return (tuple(ladder) if ladder else None), bool(getattr(_config, names[1], False))


def ladder_refusal(market: str) -> str:
    """"" when this market may be placed; otherwise why it must not be.

    THE GATE: while the ladder is not confirmed (NBA_REB_LADDER_CONFIRMED false, which is the
    default), the model refuses to place anything. The shipped NBA_REB_LIABILITY_LADDER is a
    deliberately conservative PLACEHOLDER; Wilson has not set the real one yet."""
    if market not in LADDER_CONFIG_NAMES:
        return f"market {market!r} has no liability ladder configured (nothing placed)"
    ladder, confirmed = ladder_for(market)
    if not ladder or any((not isinstance(v, (int, float))) or isinstance(v, bool)
                         or not math.isfinite(v) or v <= 0 for v in ladder):
        return f"the {market} liability ladder is empty or invalid (nothing placed)"
    if not confirmed:
        name = LADDER_CONFIG_NAMES[market][1]
        return (f"the {market} liability ladder is an unconfirmed PLACEHOLDER ({name} is "
                f"false): Wilson has not set it, so nothing is placed")
    return ""


def test_stake_usable() -> bool:
    """True when NBA_MODEL_TEST_STAKE is a finite positive number."""
    try:
        v = float(cfg("NBA_MODEL_TEST_STAKE"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def placement_refusal(market: str) -> str:
    """"" when the model may place this market at all; otherwise why not. The one gate every
    placement path asks (the row handler, the place_tip dispatch, the fan-out sizing):

      * a config error recorded at startup while the feed is ENABLED (a malformed NBA_* value
        fell back to its default; a half-understood configuration must not place real money);
      * TEST MODE on with an unusable test stake: REFUSE. (It used to fall through to the
        production cap, so a stake of 0 turned "small test bets" into "$1,500 bets".);
      * the rebounds ladder gate (ladder_refusal)."""
    errors = list(getattr(_config, "NBA_MODEL_CONFIG_ERRORS", []) or [])
    if errors and cfg("NBA_MODEL_ENABLED"):
        return ("the NBA configuration has errors, so nothing is placed: "
                + "; ".join(errors[:3]))
    if cfg("NBA_MODEL_TEST_MODE") and not test_stake_usable():
        return ("NBA test mode is ON but NBA_MODEL_TEST_STAKE is not a positive number: "
                "nothing is placed (it would otherwise fall through to the production cap)")
    return ladder_refusal(market)


def liability_to_stake(liability: float, odds: float) -> float:
    """floor(liability / (odds - 1)), as session_priority.liability_to_max_stake."""
    if not odds or liability <= 0 or odds <= 1.0:
        return 0.0
    return float(math.floor(liability / (odds - 1)))


def ladder_cap(market: str):
    """The per-account liability cap for ONE selection: the ladder's top rung (None if the
    market has no usable ladder)."""
    ladder, _ = ladder_for(market)
    return float(ladder[0]) if ladder else None


def is_capped(market: str, odds, max_odds, used_liability: float,
              min_stake: float = 0.0) -> bool:
    """True when this account can place NOTHING more on the selection purely because of its
    liability cap: the config is valid and the price can size a liability, yet even an unlimited
    request leaves no whole dollar (floor(remaining / (max_odds - 1)) < 1) or less than
    `min_stake` (the fan-out's per-account floor, which must never raise a stake above the cap)."""
    if placement_refusal(market):
        return False
    try:
        if not (math.isfinite(float(odds)) and float(odds) > 1.0):
            return False
    except (TypeError, ValueError):
        return False
    steps = ladder_stake_steps(market, odds, 1e9, max_odds=max_odds,
                               used_liability=used_liability)
    return (not steps) or steps[0] < float(min_stake or 0.0)


def ladder_stake_steps(market: str, odds, intended: float, max_odds=None,
                       used_liability: float = 0.0) -> list:
    """The descending per-account stake rungs, capped at `intended`. EMPTY (fail closed) when
    the market is not placeable (ladder unconfirmed or missing), the odds cannot size a
    liability, or the account has no liability left on this selection.

    The ladder is a cap on one ACCOUNT's liability on one SELECTION over the life of the bet:
      * `max_odds` is the highest price HyperBot may fill at (the max_odds actually sent). A
        stake is sized so that its liability survives a fill at that price, so the divisor is
        `max(odds, max_odds) - 1`, not the quoted price;
      * `used_liability` is what this account already carries on the selection (confirmed
        stake at its fill odds plus maybe-landed stake priced at max_odds). The first rung is
        the account's REMAINING cap, `top rung - used`; the lower rungs are the fallback on a
        stake-too-high reject, and are never above the remaining cap either."""
    if placement_refusal(market):
        return []
    try:
        odds = float(odds or 0)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(odds) or odds <= 1.0:
        return []
    basis = odds
    try:
        mo = float(max_odds) if max_odds is not None else 0.0
        if math.isfinite(mo) and mo > basis:
            basis = mo
    except (TypeError, ValueError):
        pass
    ladder, _ = ladder_for(market)
    remaining = float(ladder[0]) - max(0.0, float(used_liability or 0.0))
    if remaining <= 0:
        return []
    steps = []
    for liability in ladder:
        s = min(liability_to_stake(min(float(liability), remaining), basis), float(intended))
        if s > 0:
            s = round(s, 2)
            if not steps or steps[-1] != s:
                steps.append(s)
    return steps


def price_gate_refusal(live, posted, any_resolved: bool) -> str:
    """The two price gates, applied to the LIVE Sportsbet price of the exact line. "" = pass.

      (1) absolute floor NBA_MODEL_MIN_ODDS (1.70): the model itself refuses to tip below it
          (PIPELINE-DESIGN 3.3), so tipbot refuses to place below it;
      (2) WORSE gate: live more than NBA_MODEL_WORSE_GATE (10%) below the posted price means
          the line moved against us since the tip fired.

    The existing 1.25x MAX_ODDS_MULT ceiling still applies on top, in the resolver. No live
    price while the line WAS resolved is a refusal too: never a blind placement."""
    if live is None:
        return "no live Sportsbet price for this exact line" if any_resolved else ""
    try:
        live = float(live)
    except (TypeError, ValueError):
        return f"live Sportsbet price {live!r} is not a number"
    if not math.isfinite(live):
        return f"live Sportsbet price {live!r} is not a finite number"
    floor = float(cfg("NBA_MODEL_MIN_ODDS"))
    if live < floor:
        return (f"live SB {live} is below the {floor} floor (the model would not have "
                f"tipped it at this price)")
    try:
        posted = float(posted or 0) or None
    except (TypeError, ValueError):
        posted = None
    if posted is not None and not math.isfinite(posted):
        posted = None
    worse = float(cfg("NBA_MODEL_WORSE_GATE"))
    if posted is not None and live < posted * (1.0 - worse):
        return (f"live SB {live} is >{int(worse * 100)}% below the posted {posted}: "
                f"the line moved against us")
    return ""


_THRESHOLD_RE = re.compile(r"(?<![\d.])(\d+)\s*\+")
# The wording of a threshold market that carries no "N+" (a "To Record 10 Or More Rebounds" row):
# still not the over/under market. The real one is "<player> - Rebounds".
_TO_RECORD_RE = re.compile(r"\bto record\b|\bor more\b")
# the same exclusion main._nbl_full_game uses on the raw market name (quarters, halves, minutes)
_PARTIAL_GAME_RE = re.compile(
    r"\b(qtr|quarter|half|1st|2nd|3rd|4th|q[1-4]|[1-4]q|[12]h|first|second|minutes?)\b")
_FULL_GAME = ("", "full_game", "full game", "fullgame", "ft", "full_time", "full time", "game",
              "match", "regulation")


def exact_catalog_match(selections, player: str, line: float, market=None):
    """(match, reason): the ONE full-game under row for `player` (already Sportsbet's spelling)
    at exactly `line`, or (None, why).

    Mirrors the proven NBL checks (main._nbl_full_game / _nbl_threshold_of): a row is full-game
    only if its `period` is a full-game spelling (missing counts as full game) AND its
    `raw_market_name` carries no quarter / half / minutes marker, so a half row is excluded
    even when `period` is missing; an "N+" threshold (in `raw_market_name` or the selection
    text, or a `threshold` flag) is a different product and excluded. Rows are compared on the
    lower-cased player, the `direction` field (or a trailing "under"), and the exact line.
    EXACTLY ONE distinct row must remain, identified by `proposition_id` ALONE (the same row
    listed twice with odds 1.85 and "1.850" is one row); two different ids are ambiguous and the
    caller routes to manual, never picks one."""
    want = str(player or "").strip().lower()
    try:
        want_line = float(line)
    except (TypeError, ValueError):
        return None, f"line {line!r} is not a number"
    found = []
    for sel in selections or []:
        if not isinstance(sel, dict) or str(sel.get("player") or "").strip().lower() != want:
            continue
        direction = str(sel.get("direction") or "").strip().lower()
        text = str(sel.get("selection") or "").strip().lower()
        if direction:
            if direction != "under":
                continue
        elif not (text == "under" or text.endswith(" under")):
            continue
        period = str(sel.get("period") or "").strip().lower()
        raw = str(sel.get("raw_market_name") or "").strip().lower()
        if period not in _FULL_GAME or _PARTIAL_GAME_RE.search(raw):
            continue
        if (sel.get("threshold") or sel.get("is_threshold") or _THRESHOLD_RE.search(raw)
                or _THRESHOLD_RE.search(text) or _TO_RECORD_RE.search(raw)):
            continue
        try:
            if abs(float(sel.get("line")) - want_line) > 0.01:
                continue
        except (TypeError, ValueError):
            continue
        if sel.get("proposition_id") is None:
            continue
        found.append(sel)
    distinct = {}
    for sel in found:
        distinct.setdefault(str(sel.get("proposition_id")), sel)
    if not distinct:
        return None, f"no full-game under {want_line} for {player!r}"
    if len(distinct) > 1:
        return None, (f"{len(distinct)} different full-game under {want_line} rows for "
                      f"{player!r}: ambiguous, not picking one")
    sel = next(iter(distinct.values()))
    return {"market": market or sel.get("market"), "line": float(sel.get("line")),
            "selection": sel.get("selection"), "player": sel.get("player"),
            "proposition_id": sel.get("proposition_id"),
            "odds": sel.get("odds", sel.get("price"))}, ""


def earliest_start(*epochs):
    """The earliest of the given tip-off times (epoch seconds), ignoring missing / non-finite
    ones; None if none is usable. The in-play guard gates on THIS: the message, the ESPN
    schedule and HyperBot's catalog can disagree, and the earliest one wins."""
    vals = []
    for e in epochs:
        try:
            v = float(e)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            vals.append(v)
    return min(vals) if vals else None


def fixture_start_checkable(pc_start) -> bool:
    """True when the price-check envelope carries a usable (finite, numeric) start time."""
    try:
        return math.isfinite(float(pc_start))
    except (TypeError, ValueError):
        return False


def fixture_start_refusal(pc_start, pinned_epoch) -> str:
    """"" when the HyperBot catalog we priced against is the pinned game; else why not.

    resolve_nba_game pins the game on the ESPN side, but HyperBot re-matches the EVENT NAME,
    and the same two clubs can appear on two nights in its catalog (a playoff series, a
    home-and-home), so the priced board could be the other night's. HyperBot's price-check
    envelope carries `start_time_iso` (epoch seconds): it must be within
    NBA_MODEL_EVENT_START_TOLERANCE_SEC of the pinned tip-off. Rejects ONLY on positive
    evidence (as the AFL post-check does): with no start time on the envelope it cannot
    verify, returns "" and the caller logs a warning."""
    if pinned_epoch is None:
        return ""
    try:
        st = float(pc_start)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(st):
        return ""
    drift = abs(st - float(pinned_epoch))
    tol = float(cfg("NBA_MODEL_EVENT_START_TOLERANCE_SEC"))
    if drift > tol:
        return (f"the Sportsbet catalog event starts {drift / 3600.0:.1f}h from the pinned "
                f"tip-off (tolerance {tol / 3600.0:.1f}h): a different night's game")
    return ""


_NICKNAME_OVERRIDES = {"Portland Trail Blazers": "Trail Blazers"}
_EVENT_ALTERNATES = {"LA Clippers": ["Los Angeles Clippers"],
                     "Los Angeles Clippers": ["LA Clippers"]}
EVENT_MAX_CANDIDATES = 8


def _nickname(team: str) -> str:
    team = (team or "").strip()
    return _NICKNAME_OVERRIDES.get(team) or (team.split()[-1] if team else "")


def event_name_candidates(event: str) -> list:
    """Ordered event strings to ask HyperBot for the same fixture. HyperBot matches events by
    TEAM NAMES only (measured 2026-10: "Charlotte Hornets v Brooklyn Nets" asked on the night of
    a preseason game returned the regular-season game of 2026-10-21), so when the first form
    matches the wrong night the other renderings are tried: as given, away v home, the Los
    Angeles / LA spelling of the Clippers, and nicknames only. Every candidate is verified
    against the pinned tip-off before it can be priced, which is what makes a wide list safe."""
    out, seen = [], set()

    def _add(c):
        if c and c not in seen and len(out) < EVENT_MAX_CANDIDATES:
            seen.add(c)
            out.append(c)

    _add(event)
    if not event or " v " not in event:
        return out
    h, a = [x.strip() for x in event.split(" v ", 1)]
    _add(f"{a} v {h}")
    ha = [h] + list(_EVENT_ALTERNATES.get(h, []))
    aa = [a] + list(_EVENT_ALTERNATES.get(a, []))
    for alt in ha[1:]:
        _add(f"{alt} v {a}")
        _add(f"{a} v {alt}")
    for alt in aa[1:]:
        _add(f"{h} v {alt}")
        _add(f"{alt} v {h}")
    _add(f"{_nickname(h)} v {_nickname(a)}")
    _add(f"{_nickname(a)} v {_nickname(h)}")
    return out


# ── Gates: start time, HyperBot blackout ──────────────────────────────────────────────

def parse_start_utc(raw: str):
    """The BET| `start` field as an AWARE UTC datetime, or None.

    A naive value is REFUSED, not guessed: reading local time as UTC puts the tip off 10-11
    hours LATE, the permissive direction, and would wave a started game through the gate."""
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def start_slack_sec() -> float:
    """Slack past `start` before a message is refused. Clamped: it is additive on the
    permissive side, and a bet past tip-off is an in-play bet. It is also kept strictly BELOW
    the minimum lead, so the slack can never reopen the window the lead closes."""
    return min(float(cfg("NBA_MODEL_START_SLACK_SEC")), 300.0, min_lead_sec() - 1.0)


def min_lead_sec() -> float:
    """Seconds before tip-off inside which no message is placed (never negative)."""
    try:
        v = float(cfg("NBA_MODEL_MIN_LEAD_SEC"))
    except (TypeError, ValueError):
        return 90.0
    return v if math.isfinite(v) and v >= 0 else 90.0


def too_late(now_ts: float, start_ts: float) -> bool:
    """True when a message must be refused on timing: now is later than tip-off minus the
    minimum lead, plus the (clamped) slack. Applies to every kind, retries included."""
    return now_ts > start_ts - min_lead_sec() + start_slack_sec()


def in_quiet_window(now=None) -> bool:
    """True inside the scheduled HyperBot blackout (default 23:00-07:00 SYDNEY time, whatever
    the machine clock says; may wrap midnight). Equal start and end disables it."""
    if now is None:
        now = datetime.now(SYDNEY)
    elif now.tzinfo is not None:
        now = now.astimezone(SYDNEY)           # an aware time is converted, a naive one is
    start = int(cfg("NBA_MODEL_QUIET_START_HOUR"))      # taken to BE Sydney time
    end = int(cfg("NBA_MODEL_QUIET_END_HOUR"))
    if start == end:
        return False
    h = now.hour
    return (h >= start or h < end) if start > end else (start <= h < end)


# ── The exposure ledger ───────────────────────────────────────────────────────────────

def selection_id(row: dict) -> str:
    """Selection identity: the bet itself, independent of which message carried it.

    NORMALISED, because every cap keys off it: an emitter that changes case, pads whitespace
    or writes the line as 5.50 instead of 5.5 would otherwise mint a brand new selection
    with a fresh full allowance. The MARKET is part of it (last, so the pid stays the second
    part the per-fixture cap reads): once NBA_MODEL_MARKETS grows, rebounds U5.5 and assists
    U5.5 for one player must not share a ceiling."""
    def _t(v) -> str:
        return str(v or "").strip().casefold()

    try:
        line = f"{float(row.get('line')):g}"
    except (TypeError, ValueError):
        line = _t(row.get("line"))
    return (f"{_t(row.get('evt'))}~{_t(row.get('pid'))}~{line}~{_t(row.get('side'))}"
            f"~{_t(row.get('market'))}")


def _absolute(path: str) -> str:
    """A configured path made ABSOLUTE the same way for the bot and for the operator CLI: a
    relative path is anchored at the directory holding config.py, never at the caller's cwd
    (`python nba_model_core.py clear-quarantine ...` from another directory must find the same
    files the running bot uses)."""
    path = str(path)
    if os.path.isabs(path):
        return path
    base = os.path.dirname(os.path.abspath(getattr(_config, "__file__", __file__)))
    return os.path.normpath(os.path.join(base, path))


def state_path() -> str:
    return _absolute(cfg("NBA_MODEL_STATE_PATH"))


def fills_path() -> str:
    return _absolute(cfg("NBA_MODEL_FILLS_PATH"))


def load_state() -> dict:
    """Load the exposure ledger. A MISSING file is a clean first run only if nothing was ever
    placed; a CORRUPT one is NOT (returning {} would silently reset every cap and re-admit
    every key), so it is surfaced as corrupt and the caller refuses to place."""
    path = state_path()
    if not os.path.exists(path):
        # The fills file is an independent, append-only record of what actually went on. If
        # it already carries MONEY (a bet that landed, may have landed, or went to manual)
        # while the ledger is gone, the two disagree about reality and the safe reading is
        # "the ledger was lost": fail closed. A record that placed nothing (a refusal before
        # any claim: the ladder gate, the blackout, a started game) is NOT evidence of a lost
        # ledger, and treating it as one would brick the feed the first time it is switched
        # on after a run of refusals, which is exactly what the ladder gate produces.
        try:
            fp = fills_path()
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            rec = json.loads(ln)
                        except Exception:
                            continue        # a torn / non-JSON line is not evidence of money
                        if not isinstance(rec, dict) or not rec.get("key"):
                            continue        # reconciliation-style records carry no key
                        try:
                            # A keyed record whose money fields do not parse is treated AS
                            # money (fail closed), never skipped.
                            is_money = (float(rec.get("placed") or 0) > 0
                                        or float(rec.get("ambiguous") or 0) > 0
                                        or bool(rec.get("manual")))
                        except (TypeError, ValueError):
                            is_money = True
                        if is_money:
                            log.error(
                                "[nba_model] state file is MISSING but %s already holds "
                                "placement records: refusing to treat this as a clean "
                                "first run (a lost ledger would re-admit every key and "
                                "re-stake every open selection at FULL size).", fp)
                            return {"selections": {}, "state_corrupt": True}
        except Exception as e:
            log.warning("[nba_model] first-run fills check skipped (%s)", e)
        return {"selections": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("selections"), dict):
            raise ValueError("unexpected shape")
        return data
    except Exception as e:
        log.error("[nba_model] state file unreadable (%s): FAILING CLOSED", e)
        return {"selections": {}, "state_corrupt": True}


def _os_try_lock(fd: int) -> None:
    """Take an exclusive, NON-blocking OS lock on byte 0 of the open lock file. The OS releases
    it the moment the holding process dies, so there is no stale-lock heuristic to get wrong
    (a reused PID can never keep a dead process's lock alive). Raises OSError if held."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


_lock_state = threading.local()


@contextlib.contextmanager
def _ledger_lock(timeout=None):
    """The in-process lock plus an OS lock on an OPEN HANDLE of `<state>.lock`, held for the
    whole read-modify-write. Waits at most LOCK_TIMEOUT_SEC (the caller may be on the event
    loop; `timeout` overrides it for the one caller that must not give up early, the commit
    after money landed), then raises LedgerLockError, and callers fail closed. ANY OSError while creating or
    locking the file (access denied, a busy handle, a vanished directory) is treated as "held,
    retry until the deadline", never allowed to escape raw. Re-entrant within a thread."""
    timeout = float(LOCK_TIMEOUT_SEC if timeout is None else timeout)
    # ONE deadline for the in-process lock and the OS lock together: the wait is bounded by
    # `timeout`, not by two separate budgets.
    deadline = time.monotonic() + timeout
    if not _STATE_LOCK.acquire(timeout=max(0.0, timeout)):
        raise LedgerLockError("the in-process ledger lock is busy")
    try:
        depth = getattr(_lock_state, "depth", 0)
        if depth > 0:
            _lock_state.depth = depth + 1
            try:
                yield
            finally:
                _lock_state.depth -= 1
            return
        path = state_path() + ".lock"
        fd = None
        last = None
        while True:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    _os_try_lock(fd)
                except OSError:
                    os.close(fd)
                    fd = None
                    raise
                break
            except OSError as e:
                last = e
                if time.monotonic() >= deadline:
                    raise LedgerLockError(
                        f"the ledger lock {path} is not available ({last})") from None
                time.sleep(0.02)
        try:
            # diagnostics only (who holds it); byte 0 is the locked byte, so write past it
            os.lseek(fd, 8, os.SEEK_SET)
            os.write(fd, json.dumps({"pid": os.getpid(), "boot": BOOT_ID,
                                     "ts": time.time()}).encode())
        except OSError:
            pass
        _lock_state.depth = 1
        try:
            yield
        finally:
            _lock_state.depth = 0
            _os_unlock(fd)
            os.close(fd)
    finally:
        _STATE_LOCK.release()


def save_state(state: dict) -> bool:
    """Crash-safe save: a UNIQUE temp name, flush + fsync before the replace, os.replace retried
    on the Windows PermissionError another handle can cause, and the directory fsynced where
    the OS allows it. Returns True on success; the CALLER MUST CHECK, because an unpersisted
    claim lets the very next offer double-stake."""
    path = state_path()
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        # compact AND one-shot: the ledger is rewritten several times per message and grows with
        # every key. json.dumps uses the C encoder; json.dump streams through the pure-Python
        # one (about 5x slower), and indent=1 triples the bytes on top of that.
        payload = json.dumps(state, separators=(",", ":"), allow_nan=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
        if os.name != "nt":
            dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        return True
    except Exception as e:
        log.error("[nba_model] failed to save state: %s", e)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


_Q_LOCK = threading.Lock()
# Quarantines whose side file could NOT be written: held in memory only (a restart loses them,
# which is logged CRITICAL). Entries that reached the side file are NOT cached here, so the file
# is the single truth and an operator's clear takes effect in a running process.
_QUARANTINE: dict = {}


def _quarantine_path() -> str:
    return state_path() + ".quarantine.json"


def _load_quarantine():
    """(data, unreadable). A missing file is ({}, False). A file that does not parse is
    ({}, True): the caller fails closed and must NOT overwrite it. A plain OSError reading it is
    transient and raises LedgerLockError (the claim then writes `none`, never a terminal
    `refused`)."""
    try:
        with open(_quarantine_path(), "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, False
    except OSError as e:
        raise LedgerLockError(f"the quarantine file could not be read ({e})") from None
    try:
        data = json.loads(raw)
    except ValueError:
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def _write_quarantine(data: dict) -> None:
    """Crash-safe write of the side file: unique temp, fsync, os.replace retried on the Windows
    PermissionError another handle can cause (as the ledger save does)."""
    path = _quarantine_path()
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _set_aside_bad_quarantine() -> str:
    """The side file does not parse: move it to `<file>.bad.<ts>` (kept for the operator, never
    overwritten) and return the new name."""
    bad = f"{_quarantine_path()}.bad.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    os.replace(_quarantine_path(), bad)
    return bad


def quarantine_selection(sel_id: str, why: str) -> None:
    """Refuse every further claim on this selection until an operator clears it. Used when the
    ledger commit AFTER money was placed could not be taken or persisted: the ledger is then
    behind the book (per-account liability and the ambiguous flag are missing), so sizing a
    further placement against it could exceed the ladder cap. Held in a small durable side file
    (`<state>.quarantine.json`) that survives a restart and is re-read on every claim; only if
    the file cannot be written does it fall back to memory. A side file that does not parse is
    NEVER overwritten: it is moved to `.bad.<ts>` and replaced by a file that keeps failing
    closed for every selection (a `*` entry) until an operator reconciles and clears it."""
    log.critical("[nba_model] QUARANTINED %s: %s. No further offer will be placed on it until "
                 "an operator clears it (python nba_model_core.py clear-quarantine <selection>).",
                 sel_id, why)
    try:
        with _Q_LOCK:
            try:
                data, unreadable = _load_quarantine()
            except LedgerLockError:
                raise
            if unreadable:
                bad = _set_aside_bad_quarantine()
                data = {"*": {"why": f"the quarantine file was unreadable and was moved to "
                                     f"{os.path.basename(bad)}: reconcile every quarantined "
                                     f"selection by hand, then clear '*'",
                              "ts": datetime.now().isoformat(timespec="seconds")}}
                msg = (f"the quarantine side file was UNREADABLE and was moved to {bad}; every "
                       f"selection is refused until an operator reconciles and clears '*'")
                log.critical("[nba_model] %s", msg)
                _ALERTS.setdefault(state_path(), []).append(msg)
            data[sel_id] = {"why": why, "ts": datetime.now().isoformat(timespec="seconds")}
            _write_quarantine(data)
        _QUARANTINE.pop((state_path(), sel_id), None)
    except Exception as e:
        _QUARANTINE[(state_path(), sel_id)] = why
        log.critical("[nba_model] the quarantine of %s could not be written to its side file "
                     "(%s): it holds only until this process restarts", sel_id, e)


def quarantine_reason(sel_id: str) -> str:
    """Why this selection is quarantined, or ''. Reads the side file EVERY time (it is tiny), so
    a clear by an operator's CLI takes effect in the running bot with no restart. Raises
    LedgerLockError on a transient read error (fail closed, retryable)."""
    why = _QUARANTINE.get((state_path(), sel_id))
    if why:
        return why
    with _Q_LOCK:
        data, unreadable = _load_quarantine()
    if unreadable:
        return ("the quarantine side file is unreadable (it will be moved aside and kept at "
                "the next quarantine); an operator must inspect it")
    if "*" in data:
        return str((data["*"] or {}).get("why") or "every selection is quarantined")
    e = data.get(sel_id)
    return str((e or {}).get("why") or "quarantined") if e else ""


def list_quarantine() -> dict:
    """{selection: reason} for the side file plus any memory-only entries."""
    with _Q_LOCK:
        data, unreadable = _load_quarantine()
    out = {k: str((v or {}).get("why") or "") for k, v in data.items()}
    for (sp, sel), why in _QUARANTINE.items():
        if sp == state_path():
            out[sel] = why
    if unreadable:
        out["(unreadable)"] = f"{_quarantine_path()} does not parse"
    return out


def clear_quarantine(sel_id: str) -> bool:
    """OPERATOR-ONLY: lift a quarantine once a human has reconciled the selection by hand.
    Atomic and fsynced like every other write of this file. True if the selection is no longer
    quarantined in the side file afterwards."""
    _QUARANTINE.pop((state_path(), sel_id), None)
    try:
        with _Q_LOCK:
            data, unreadable = _load_quarantine()
            if unreadable:
                log.error("[nba_model] the quarantine side file does not parse: inspect %s "
                          "by hand", _quarantine_path())
                return False
            if sel_id not in data:
                return True
            del data[sel_id]
            _write_quarantine(data)
        return True
    except Exception as e:
        log.error("[nba_model] could not clear the quarantine of %s: %s", sel_id, e)
        return False


def main(argv=None) -> int:
    """Operator CLI: `python nba_model_core.py list-quarantine` and
    `python nba_model_core.py clear-quarantine <selection>`. The paths come from config exactly
    as the bot resolves them (absolute, anchored at config.py), whatever the cwd."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("list-quarantine", "clear-quarantine"):
        print("usage: python nba_model_core.py list-quarantine\n"
              "       python nba_model_core.py clear-quarantine <selection id | *>")
        return 2
    print(f"quarantine file: {_quarantine_path()}")
    if args[0] == "list-quarantine":
        held = list_quarantine()
        for sel, why in held.items():
            print(f"  {sel}: {why}")
        if not held:
            print("  (none)")
        return 0
    if len(args) != 2:
        print("clear-quarantine needs exactly one selection id (or * for the global entry)")
        return 2
    ok = clear_quarantine(args[1])
    print(("cleared " if ok else "COULD NOT CLEAR ") + args[1])
    return 0 if ok else 1


# Messages the ledger raised on its own (a dead process's reservation converted at a claim)
# that the caller should alert on; drained by the row handler. Keyed by ledger path.
_ALERTS: dict = {}


def pop_alerts() -> list:
    return _ALERTS.pop(state_path(), [])


# Process liveness: one OS lock file per boot id, held for the life of the process. A
# reservation is only treated as orphaned when the lock of the boot that made it is FREE, so a
# second process (a script, a second instance) can never convert a LIVE process's reservation.
_BOOT_LOCK = threading.Lock()
_BOOT_FDS: dict = {}


def _boot_lock_path(boot: str) -> str:
    return f"{state_path()}.boot.{boot}"


def _ensure_boot_lock() -> bool:
    """Take (once per process and ledger) the liveness lock for this boot id. False if it
    cannot be taken: the caller must then not stamp a reservation another process could
    mistake for an orphan."""
    key = (state_path(), BOOT_ID)
    with _BOOT_LOCK:
        if key in _BOOT_FDS:
            return True
        fd = None
        try:
            path = _boot_lock_path(BOOT_ID)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            _os_try_lock(fd)
            try:
                os.lseek(fd, 8, os.SEEK_SET)
                os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode())
            except OSError:
                pass
            _BOOT_FDS[key] = fd
            return True
        except OSError as e:
            log.error("[nba_model] could not take the process liveness lock: %s", e)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False


def release_boot_lock(boot: str = None) -> None:
    """Release a boot id's liveness lock (a clean shutdown; also how a test simulates the
    death of a process within one interpreter)."""
    key = (state_path(), boot or BOOT_ID)
    with _BOOT_LOCK:
        fd = _BOOT_FDS.pop(key, None)
    if fd is not None:
        _os_unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(_boot_lock_path(boot or BOOT_ID))
        except OSError:
            pass


def _release_all_boot_locks() -> None:
    """atexit: release AND delete every liveness lock file this process holds, so a clean
    shutdown leaves no `<state>.boot.<id>` file behind (a crash leaves one; `_boot_alive`
    removes it once it finds the lock free)."""
    for (sp, boot) in list(_BOOT_FDS):
        fd = _BOOT_FDS.pop((sp, boot), None)
        if fd is None:
            continue
        try:
            _os_unlock(fd)
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(f"{sp}.boot.{boot}")
        except OSError:
            pass


atexit.register(_release_all_boot_locks)


def _boot_alive(boot) -> bool:
    """Is the process that stamped this boot id still running? Ours: yes. Another's: only if its
    liveness lock is still held. No lock file at all (an older ledger, or one already cleaned up)
    means its owner is gone. An unreadable one counts as ALIVE (never convert on a doubt)."""
    if not boot:
        return False
    if boot == BOOT_ID:
        return True
    path = _boot_lock_path(str(boot))
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True
    try:
        try:
            _os_try_lock(fd)
        except OSError:
            return True
        _os_unlock(fd)
    finally:
        os.close(fd)
    try:
        os.remove(path)
    except OSError:
        pass
    return False


def note_price(row: dict) -> None:
    """Record the FIRST price this selection was ever seen at, on EVERY processed message,
    including ones refused before any claim (a failed fixture lookup, the ladder gate, the
    blackout). Otherwise a base that never reached the claim leaves the first ADMITTED message,
    typically a retry re-posted at a lower quote, as the reference for the 10% price gate
    (signal fired at 2.10, retry at 1.76, live 1.71 would pass). Never overwrites; a corrupt
    ledger is left alone (the claim will refuse anyway)."""
    try:
        price = float(row.get("price") or 0)
        if not math.isfinite(price) or price <= 1.0:
            return
        sid = selection_id(row)
        with _ledger_lock():
            state = load_state()
            if state.get("state_corrupt"):
                return
            prices = state.setdefault("first_prices", {})
            if sid not in prices:
                prices[sid] = price
                save_state(state)
    except Exception as e:
        log.warning("[nba_model] could not note the first price: %s", e)


def _fills_for_key(key: str):
    """(any_record, answered): whether the fills file holds ANY record for this key, and whether
    it holds one that is not a plain refusal (money, a send, a manual route, a cap)."""
    any_rec = answered = False
    try:
        with open(fills_path(), "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("key") != key:
                    continue
                any_rec = True
                if r.get("outcome") != "refused":
                    answered = True
    except OSError:
        pass
    return any_rec, answered


def _fills_money_for_key(key: str) -> bool:
    try:
        with open(fills_path(), "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("key") != key:
                    continue
                if (float(r.get("placed") or 0) > 0 or float(r.get("ambiguous") or 0) > 0
                        or r.get("outcome") in ("filled", "partial", "manual", "capped")):
                    return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def key_state(key: str) -> str:
    """SEEN, UNSEEN or UNKNOWN for a message key. THREE states, because "I could not tell" is
    not "it is a repeat":

      * SEEN: the ledger admitted this key, or the fills file shows it was answered with money or
        a send (so a ledger that was lost or is corrupt still recognises a placed key). The
        caller drops it with NO record;
      * UNKNOWN: the ledger cannot be read or locked right now. The caller writes nothing, says
        so (it is NOT a repeat), and fails fast;
      * UNSEEN: a fresh key (a key that only has a pre-claim "none" or "refused" record is
        fresh: it never reached the ledger and may be processed again as a new revision).

    A CORRUPT ledger cannot vouch that a key is fresh: if the fills file already holds ANY
    record for it, it is SEEN, otherwise it is UNSEEN and the claim will refuse it as corrupt
    (and answer it) as usual."""
    try:
        with _ledger_lock():
            state = load_state()
    except Exception:
        return UNKNOWN
    if state.get("state_corrupt"):
        return SEEN if _fills_for_key(key)[0] else UNSEEN
    sels = state.get("selections") or {}
    if any(key in (sel.get("keys") or []) for sel in sels.values()):
        return SEEN
    return SEEN if _fills_money_for_key(key) else UNSEEN


def key_seen(key: str) -> bool:
    """True unless the key is positively UNSEEN (an unreadable ledger counts as seen)."""
    return key_state(key) != UNSEEN


def _numbers_from_fills(row: dict) -> dict:
    """The highest selection numbers the FILLS FILE already holds for this selection. Used when
    the ledger is corrupt or locked: writing 0.0 there would tell the model the whole tranche
    is still owed. Matches on evt / pid / line."""
    best = {"placed": 0.0, "ambiguous": 0.0, "committed": 0.0, "manual": False}
    try:
        evt = str(row.get("evt") or "").strip().casefold()
        pid = str(row.get("pid") or "").strip()
        line = float(row.get("line"))
        with open(fills_path(), "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                    if (str(r.get("evt") or "").strip().casefold() != evt
                            or str(r.get("pid") or "") != pid
                            or abs(float(r.get("line")) - line) > 1e-6):
                        continue
                    best["committed"] = max(best["committed"],
                                            float(r.get("selection_committed") or 0))
                    best["ambiguous"] = max(best["ambiguous"],
                                            float(r.get("selection_ambiguous") or 0))
                    best["placed"] = max(best["placed"], float(r.get("selection_placed") or 0))
                    best["manual"] = best["manual"] or bool(r.get("manual"))
                except (ValueError, TypeError, AttributeError):
                    continue
    except (OSError, ValueError, TypeError):
        pass
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in best.items()}


def _numbers_from_state(state: dict, row: dict) -> dict:
    sel = (state.get("selections") or {}).get(selection_id(row)) or {}
    committed_ = round(float(sel.get("committed") or 0), 2)
    amb = round(float(sel.get("ambiguous") or 0), 2)
    return {"placed": round(max(0.0, committed_ - amb), 2), "ambiguous": amb,
            "committed": committed_, "manual": bool(sel.get("manual"))}


def selection_numbers(row: dict) -> dict:
    """{placed, ambiguous, committed, manual} for the row's selection, with committed == placed
    + ambiguous. From the ledger; when it is corrupt or cannot be read, the highest figures
    already in the fills file (never a misleading 0.0)."""
    try:
        with _ledger_lock():
            state = load_state()
    except Exception:
        return _numbers_from_fills(row)
    if state.get("state_corrupt"):
        return _numbers_from_fills(row)
    return _numbers_from_state(state, row)


def _convert_orphans(state: dict) -> list:
    """Turn every reservation left by a DEAD process into exposure, in place.

    "Dead" is PROVEN, not assumed from a different boot id: the reservation's boot lock
    (`<state>.boot.<id>`, held by its process for its whole life) must be free. A process killed
    between the claim and the commit leaves `inflight` that nothing will ever release and, if it
    had already fired, money that may be on the book. If the fan-out had recorded its per-account
    asks (note_intent) that money is booked as AMBIGUOUS, charged to each account that was asked
    at the highest allowed fill, and the key gets an outcome entry (`outcome` none, `requested`
    the asks) so a missing fills row is rebuilt. With no intent nothing was ever sent: the
    reservation is just released (and the key still gets an outcome so it is answered).
    Returns messages."""
    msgs = []
    alive = {}
    for sel_id, sel in (state.get("selections") or {}).items():
        res_map = sel.get("reservations") or {}
        for key, res in list(res_map.items()):
            boot = res.get("boot")
            if boot not in alive:
                alive[boot] = _boot_alive(boot)
            if alive[boot]:
                continue
            asks = [a for a in (res.get("accounts") or []) if float(a.get("stake") or 0) > 0]
            amb = round(sum(float(a["stake"]) for a in asks), 2)
            sel["inflight"] = round(max(0.0, float(sel.get("inflight") or 0)
                                        - float(res.get("stake") or 0)), 2)
            mo = float(res.get("max_odds") or 0)
            if amb > 0:
                sel["committed"] = round(float(sel.get("committed") or 0) + amb, 2)
                sel["ambiguous"] = round(float(sel.get("ambiguous") or 0) + amb, 2)
                accs = state.setdefault("accounts", {}).setdefault(sel_id, {})
                for a in asks:
                    e = accs.setdefault(str(a["session_id"]),
                                        {"placed": 0.0, "ambiguous": 0.0, "liability": 0.0})
                    e["ambiguous"] = round(e["ambiguous"] + float(a["stake"]), 2)
                    e["liability"] = round(e["liability"] + float(a["stake"]) * max(0.0, mo - 1.0), 2)
            state.setdefault("outcomes", {})[key] = {
                "row": res.get("row") or {}, "placed": 0.0, "ambiguous": amb, "manual": False,
                "stake": float(res.get("stake") or 0), "event": "", "accounts": [],
                "outcome": "none", "requested": amb, "rev0": res.get("rev0"),
                "written": False, "orphan": True}
            del res_map[key]
            msgs.append(
                f"a process died mid-placement on {sel_id} key {key}: "
                + (f"${amb:.2f} that may have landed is now booked AMBIGUOUS across "
                   f"{len(asks)} account(s) at max_odds {mo:.2f}; check the book by hand"
                   if amb > 0 else "nothing had been sent; the reservation was released"))
    return msgs


def note_intent(row: dict, accounts, max_odds: float) -> bool:
    """Persist the fan-out's per-account asks BEFORE any POST: [(session_id, stake), ...] and the
    highest allowed fill. A crash after this point is recoverable (the asks become ambiguous
    exposure); a crash before it is provably nothing sent. Returns False if it could not be
    saved, and the caller must then NOT send anything."""
    try:
        with _ledger_lock():
            state = load_state()
            if state.get("state_corrupt"):
                return False
            sel = (state.get("selections") or {}).get(selection_id(row)) or {}
            res = (sel.get("reservations") or {}).get(row["key"])
            if res is None:
                return False
            res["accounts"] = [{"session_id": str(sid), "stake": round(float(st), 2)}
                               for sid, st in accounts]
            res["max_odds"] = float(max_odds)
            return save_state(state)
    except Exception as e:
        log.error("[nba_model] could not record the fan-out intent: %s", e)
        return False


def _prune_outcome(key: str) -> None:
    """The fills record for this key is on disk: its ledger outcome entry (kept only so a crash
    between the commit and the append can be rebuilt) is no longer needed. Dropping it keeps the
    ledger from growing with every key ever processed."""
    try:
        with _ledger_lock():
            state = load_state()
            if (state.get("outcomes") or {}).pop(key, None) is not None:
                save_state(state)
    except Exception as e:
        log.warning("[nba_model] could not prune the answered outcome of %s: %s", key, e)


def recover_pending() -> list:
    """Run at STARTUP (and safe to run at any time, because only a reservation whose process is
    PROVEN dead, its boot lock free, is converted): convert reservations left by a dead process
    into ambiguous exposure, then REBUILD every outcome the ledger holds that has no NEWER record
    in the fills file as a NEW REVISION of its key (a crash between the ledger commit and the
    fills append otherwise leaves the producer waiting forever and its loss stop booking the
    full target). Returns human-readable messages for the caller to alert on."""
    msgs = []
    try:
        with _ledger_lock():
            state = load_state()
            if state.get("state_corrupt"):
                return ["the NBA exposure ledger is CORRUPT: nothing will be placed until repaired"]
            changed = False
            outs = state.get("outcomes") or {}
            for k in [k for k, o in outs.items() if o.get("written")]:
                del outs[k]                     # legacy "written" entries: nothing left to do
                changed = True
            conv = _convert_orphans(state)
            msgs += conv
            if conv or changed:
                save_state(state)
            todo = list((state.get("outcomes") or {}).items())
        for key, o in todo:
            rev0 = o.get("rev0")
            have = _next_revision(key) - 1
            if (have > 0) if rev0 is None else (have > int(rev0)):
                _prune_outcome(key)            # a record newer than the claim exists
                continue
            row = dict(o.get("row") or {})
            row["key"] = key
            placed, amb = float(o.get("placed") or 0), float(o.get("ambiguous") or 0)
            stake = float(o.get("stake") or 0)
            outcome = o.get("outcome")
            if outcome not in FILL_OUTCOMES:
                if o.get("manual"):
                    outcome = "manual"
                elif placed >= stake - 0.01 and placed > 0:
                    outcome = "filled"
                elif placed > 0:
                    outcome = "partial"
                else:
                    outcome = "none"
            req = o.get("requested")
            req = round(placed + amb, 2) if req is None else float(req)
            write_fill(row, requested=req, placed=placed, ambiguous=amb,
                       manual=bool(o.get("manual")), accounts=list(o.get("accounts") or []),
                       outcome=outcome, event=o.get("event") or "")
            msgs.append(f"rebuilt the missing fills record for {key} ({outcome}, placed "
                        f"${placed:.2f}, ambiguous ${amb:.2f})")
    except Exception as e:
        msgs.append(f"startup recovery failed: {e}")
        log.error("[nba_model] startup recovery failed: %s", e)
    return msgs


def committed(row: dict) -> float:
    """Dollars already committed for this selection (placed + ambiguous), so the fills file
    reports a REAL `selection_committed` on the paths that place nothing."""
    return selection_numbers(row)["committed"]


def _new_selection(row: dict) -> dict:
    return {"committed": 0.0, "ambiguous": 0.0, "target": 0.0, "keys": [],
            "manual": False, "player": row.get("player", "")}


def claim(row: dict):
    """Admit or refuse this message (fails closed if the ledger lock cannot be taken, or if the
    selection is quarantined after a commit that could not be recorded)."""
    try:
        sel_id = selection_id(row)
        why = quarantine_reason(sel_id)
        if why:
            return 0.0, (f"selection {sel_id} is QUARANTINED ({why}): the ledger is behind the "
                         f"book on it, so no further offer is placed until an operator "
                         f"reconciles it by hand and clears the quarantine")
        return _claim(row)
    except LedgerLockError as e:
        return 0.0, f"could not lock the exposure ledger ({e}): refusing to place"


def _claim(row: dict):
    """Admit or refuse this message, and return how much of it may be placed.

    Returns (allowed_stake, reason). allowed_stake == 0.0 means DO NOT PLACE and `reason`
    explains why. The claim is written BEFORE placement so a crash mid-flight cannot re-admit
    the same key on restart. Reserves the stake as `inflight` so two concurrent messages for
    one selection cannot both claim the full headroom."""
    sel_id = selection_id(row)
    key = row["key"]
    requested = float(row["stake"])
    target = float(row["target"])
    # In TEST MODE the per-selection ceiling is the test stake, not the model's target;
    # otherwise the hourly retry drips another test stake onto the selection every tick.
    if cfg("NBA_MODEL_TEST_MODE"):
        if not test_stake_usable():
            return 0.0, ("NBA test mode is ON but NBA_MODEL_TEST_STAKE is not a positive "
                         "number: refusing to place")
        target = min(target, round(float(cfg("NBA_MODEL_TEST_STAKE")), 2))
    if not _ensure_boot_lock():
        return 0.0, ("could not lock the process liveness file: refusing to place (another "
                     "process could mistake this one's reservation for an orphan)")
    with _ledger_lock():
        state = load_state()
        if state.get("state_corrupt"):
            return 0.0, ("exposure ledger is CORRUPT: refusing to place until it is "
                         "repaired (placing blind risks double-staking every open selection)")
        orphans = _convert_orphans(state)
        if orphans:
            for m in orphans:
                log.critical("[nba_model] %s", m)
            _ALERTS.setdefault(state_path(), []).extend(orphans)
            save_state(state)
        sel = state["selections"].setdefault(sel_id, _new_selection(row))

        if key in sel["keys"]:
            return 0.0, (f"repeat key {key!r}: the feed guarantees keys are unique, so this "
                         f"is an UPSTREAM BUG. Dropped, not placed.")

        # The key is ALSO checked across the whole ledger, not only this selection: a key
        # that reappears under a re-spelled selection identity is still a repeat.
        for other_id, other in state["selections"].items():
            if other_id != sel_id and key in (other.get("keys") or []):
                return 0.0, (f"repeat key {key!r} already admitted under selection "
                             f"{other_id}: UPSTREAM BUG. Dropped, not placed.")

        why = check_identity(state, row)
        if why:
            return 0.0, f"identity cross-check failed: {why}"

        if sel.get("manual"):
            return 0.0, (f"selection {sel_id} previously routed to MANUAL: refusing to "
                         f"auto-place a further offer against a bet that may have been "
                         f"placed by hand")
        if float(sel.get("ambiguous") or 0) > 0:
            return 0.0, (f"selection {sel_id} has ${sel['ambiguous']:.2f} in an AMBIGUOUS "
                         f"state (may have landed at the bookie after we stopped waiting): "
                         f"refusing a further offer (double-stake)")

        if row["kind"] == "retry" and not cfg("NBA_MODEL_RETRY_ENABLED"):
            return 0.0, "kind=retry and NBA_MODEL_RETRY_ENABLED=false"

        # Per-fixture player cap (correlation bound). 0 disables it, which is the shipped
        # default: the NBA design has no per-game cap, so none is invented here. Counts
        # DISTINCT pids with money in the fixture; a further tranche on a backed player is
        # free (the same correlated position).
        cap = int(cfg("NBA_MODEL_MAX_PLAYERS_PER_EVENT") or 0)
        this_live = float(sel.get("committed") or 0) + float(sel.get("inflight") or 0)
        if cap > 0 and this_live <= 0:
            evt_prefix = f"{str(row.get('evt') or '').strip().casefold()}~"
            this_pid = str(row.get("pid") or "").strip().casefold()
            backed = {
                sid.split("~")[1]
                for sid, s in state["selections"].items()
                if sid.startswith(evt_prefix) and len(sid.split("~")) > 1
                and (float(s.get("committed") or 0) + float(s.get("inflight") or 0)) > 0
            }
            if this_pid not in backed and len(backed) >= cap:
                return 0.0, (f"fixture {row.get('evt', '')} already has {len(backed)} "
                             f"distinct players backed (cap {cap}): refusing a "
                             f"{len(backed) + 1}th")

        # The selection's TARGET is fixed by the first admitted message and never moves. A later
        # key (retry or otherwise) whose `target` differs is REFUSED: taking the max let a
        # retry with a bigger target raise the ceiling (base $500 filled, retry stake $1,000 /
        # target $1,500 admitted, committed reached $1,500). The ceiling is then
        # min(that fixed target, NBA_MODEL_MAX_SELECTION_STAKE), tipbot's own backstop.
        raw_target = float(row["target"])
        if sel.get("keys") and sel.get("row_target") is not None:
            if abs(float(sel["row_target"]) - raw_target) > 0.005:
                return 0.0, (f"target mismatch on {sel_id}: this selection was admitted with "
                             f"target ${float(sel['row_target']):.2f} and key {key!r} says "
                             f"${raw_target:.2f}; a later key cannot move the ceiling")
        elif not sel.get("keys"):
            sel["row_target"] = raw_target
            sel["target"] = target           # the clamped target (TEST MODE) the cap uses
        ceiling = min(sel["target"], float(cfg("NBA_MODEL_MAX_SELECTION_STAKE")))
        inflight = float(sel.get("inflight") or 0)
        headroom = round(ceiling - float(sel.get("committed") or 0) - inflight, 2)
        if headroom <= 0:
            return 0.0, (f"selection {sel_id} already has ${sel['committed']:.2f} committed"
                         + (f" + ${inflight:.2f} in flight" if inflight > 0 else "")
                         + f" of a ${ceiling:.2f} ceiling: nothing left to place")
        allowed = round(min(requested, headroom), 2)
        # Claim + reserve NOW, before placement. Released in commit().
        sel["keys"].append(key)
        sel["inflight"] = round(inflight + allowed, 2)
        # Stamp the reservation with this process run: a reservation whose boot id is not ours
        # belongs to a process that died between the claim and the commit (recover_pending).
        sel.setdefault("reservations", {})[key] = {
            "stake": allowed, "boot": BOOT_ID, "ts": time.time(), "accounts": [],
            "max_odds": 0.0,
            # how many fills records this key already had at the claim, so a rebuild can tell
            # a NEWER record from an older pre-claim refusal or none
            "rev0": _next_revision(key) - 1,
            "row": {k: row.get(k) for k in ("evt", "pid", "player", "team", "opp", "market",
                                              "side", "line", "kind", "attempt", "target",
                                              "key")}}
        # The price the signal FIRST fired at, kept for the life of the selection. A retry
        # arrives re-posted at the model's latest quote; gating it against its own `price`
        # would reset the "no more than 10% below the signal price" gate on every retry.
        prices = state.setdefault("first_prices", {})
        prices.setdefault(sel_id, float(row["price"]))
        sel.setdefault("price", float(prices[sel_id]))
        # Handed back on the row itself so the caller need not re-read the ledger (a second,
        # separate read that could fail would gate the message against its own price).
        row["first_price"] = float(prices[sel_id])
        ident = state.setdefault("identity", {"pid_names": {}, "evt_players": {}})
        pid = str(row.get("pid") or "").strip()
        name = fold_name(row.get("player"))
        ident.setdefault("pid_names", {})[pid] = name
        ident.setdefault("evt_players", {})[
            f"{str(row.get('evt') or '').strip().casefold()}~{name}"] = pid
        if not save_state(state):
            return 0.0, ("could not persist the exposure ledger: refusing to place (an "
                         "unpersisted claim would let the next offer double-stake)")
        if allowed < requested:
            log.info("[nba_model] %s: requested $%.2f but only $%.2f of headroom "
                     "(committed $%.2f of $%.2f): placing the remainder only",
                     sel_id, requested, allowed, sel["committed"], ceiling)
        return allowed, ""


def commit(row: dict, placed: float, ambiguous: float, manual: bool,
           reserved: float = 0.0, accounts=None, amb_accounts=None, event: str = "",
           outcome=None, requested=None, numbers_out=None, lock_timeout=None):
    """Record the outcome and RELEASE the claim's reservation. Returns the selection's new
    cumulative committed total, or None if the commit could NOT be taken or persisted (the
    caller then still writes its fills record, with the best-known state).

    MUST be called even when placement raised, or the reservation leaks and permanently blocks
    the selection. `ambiguous` dollars count toward committed: a maybe-landed bet is treated
    as ON for exposure purposes and never re-offered against.

    `accounts` ([{session_id, stake, odds}]) are the CONFIRMED fills and `amb_accounts`
    ([{session_id, stake, max_odds}]) the maybe-landed ones. Each account's liability on this
    selection grows by stake * (fill odds - 1) for a confirmed fill and by stake * (max_odds -
    1) for a maybe-landed one (priced at the highest fill HyperBot was allowed), and the
    ladder then caps what that account may be asked for next time.

    `outcome` and `requested` (the fills record's) are saved with the key's outcome entry so a
    crash before the fills append rebuilds it exactly (a `capped` stays `capped`). `numbers_out`,
    a dict, is filled with the selection's post-commit {placed, ambiguous, committed, manual}
    so the fills record needs no second ledger read. The lock wait is COMMIT_LOCK_TIMEOUT_SEC,
    not the short event-loop budget: if it still cannot be taken (or persisted) after money was
    placed, the selection is QUARANTINED so no later claim sizes against a stale ledger."""
    sel_id = selection_id(row)
    money = (float(placed or 0) > 0 or float(ambiguous or 0) > 0 or bool(manual))
    timeout = COMMIT_LOCK_TIMEOUT_SEC if lock_timeout is None else float(lock_timeout)
    result = None
    lock = None
    try:
        lock = _ledger_lock(timeout)
        lock.__enter__()
    except LedgerLockError as e:
        lock = None
        log.critical("[nba_model] COMMIT NOT TAKEN for %s: %s. Placed $%.2f, ambiguous $%.2f "
                     "is NOT in the ledger; check the selection by hand.", sel_id, e,
                     float(placed or 0), float(ambiguous or 0))
    if lock is not None:
        try:
            result = _commit_locked(row, sel_id, placed, ambiguous, manual, reserved, accounts,
                                    amb_accounts, event, outcome, requested, numbers_out)
        except Exception as e:
            log.critical("[nba_model] COMMIT RAISED for %s: %s", sel_id, e)
            result = None
        finally:
            lock.__exit__(None, None, None)
    if result is None and money:
        quarantine_selection(
            sel_id, f"the commit of {row.get('key')} failed after money was placed (placed "
                    f"${float(placed or 0):.2f}, ambiguous ${float(ambiguous or 0):.2f}, "
                    f"manual={bool(manual)})")
    return result


def _commit_locked(row, sel_id, placed, ambiguous, manual, reserved, accounts, amb_accounts,
                   event="", outcome=None, requested=None, numbers_out=None):
    state = load_state()
    if state.get("state_corrupt"):
        log.error("[nba_model] cannot commit %s: state corrupt", sel_id)
        return None
    sel = state["selections"].setdefault(sel_id, _new_selection(row))
    # Pop THIS key's reservation and subtract ONLY its own stake. If it is already gone (a dead
    # process's reservation converted at a claim), nothing is subtracted: taking `reserved` off
    # again would release another key's in-flight money.
    res = (sel.get("reservations") or {}).pop(row.get("key"), None)
    if res is not None:
        sel["inflight"] = round(
            max(0.0, float(sel.get("inflight") or 0) - float(res.get("stake") or 0)), 2)
    if res is not None or reserved:
        # the key's outcome, kept so a missing fills row can be rebuilt after a crash
        state.setdefault("outcomes", {})[row.get("key")] = {
            "row": {k: row.get(k) for k in ("evt", "pid", "player", "team", "opp", "market",
                                              "side", "line", "kind", "attempt", "target")},
            "placed": round(float(placed or 0), 2), "ambiguous": round(float(ambiguous or 0), 2),
            "manual": bool(manual),
            "stake": float((res or {}).get("stake") or reserved or 0), "event": event or "",
            "accounts": [{"session_id": str(a["session_id"]), "stake": float(a["stake"]),
                          "odds": float(a["odds"]), "bet_id": str(a.get("bet_id", "") or "")}
                         for a in (accounts or [])],
            "outcome": outcome, "requested": requested,
            "rev0": (res or {}).get("rev0"), "written": False}
    sel["committed"] = round(float(sel.get("committed") or 0)
                             + float(placed or 0) + float(ambiguous or 0), 2)
    sel["ambiguous"] = round(float(sel.get("ambiguous") or 0)
                             + float(ambiguous or 0), 2)
    if manual:
        sel["manual"] = True
    acc = state.setdefault("accounts", {}).setdefault(sel_id, {})
    for a in accounts or []:
        e = acc.setdefault(str(a["session_id"]),
                           {"placed": 0.0, "ambiguous": 0.0, "liability": 0.0})
        e["placed"] = round(e["placed"] + float(a["stake"]), 2)
        e["liability"] = round(e["liability"] + float(a["stake"]) * (float(a["odds"]) - 1.0), 2)
    for a in amb_accounts or []:
        e = acc.setdefault(str(a["session_id"]),
                           {"placed": 0.0, "ambiguous": 0.0, "liability": 0.0})
        e["ambiguous"] = round(e["ambiguous"] + float(a["stake"]), 2)
        e["liability"] = round(
            e["liability"] + float(a["stake"]) * (float(a["max_odds"]) - 1.0), 2)
    if not save_state(state):
        # A lost commit drops the ambiguous / manual flags (the reservation stays, so
        # headroom stays conservative). Windows can fail os.replace while another handle
        # has the file open: try once more before shouting.
        time.sleep(0.05)
        if not save_state(state):
            log.critical(
                "[nba_model] COMMIT NOT PERSISTED for %s (placed $%.2f, ambiguous $%.2f, "
                "manual=%s): the ledger is now behind the book. Check the selection by "
                "hand before the next offer.", sel_id, float(placed or 0),
                float(ambiguous or 0), manual)
            return None
    if numbers_out is not None:
        numbers_out.update(_numbers_from_state(state, row))
    return sel["committed"]


def account_liability(sel_id: str) -> dict:
    """{session_id: liability already carried on this selection, plus the asks of its open
    reservations at max_odds}. An unreadable or corrupt ledger raises: the caller must NOT size
    a placement without knowing."""
    with _ledger_lock():
        state = load_state()
    if state.get("state_corrupt"):
        raise LedgerLockError("the exposure ledger is corrupt")
    out = {sid: float(e.get("liability") or 0.0)
           for sid, e in ((state.get("accounts") or {}).get(sel_id) or {}).items()}
    # An OPEN reservation that has recorded its asks (note_intent) is money about to be on the
    # book: count each ask at the highest allowed fill, so a concurrent sizing for the same
    # selection cannot treat those accounts as empty.
    sel = (state.get("selections") or {}).get(sel_id) or {}
    for res in (sel.get("reservations") or {}).values():
        mo = max(0.0, float(res.get("max_odds") or 0.0) - 1.0)
        for a in res.get("accounts") or []:
            sid = str(a.get("session_id"))
            out[sid] = round(out.get(sid, 0.0) + float(a.get("stake") or 0.0) * mo, 2)
    return out


def selection_snapshot(sel_id: str) -> dict:
    """A copy of one selection's ledger entry, read under the lock ({} if unknown)."""
    try:
        with _ledger_lock():
            return dict((load_state().get("selections") or {}).get(sel_id) or {})
    except Exception:
        return {}


def reference_price(row: dict) -> float:
    """The price to gate this message against: the price the selection's FIRST admitted
    message was posted at, or this message's own `price` if it is higher or the selection is
    new. See claim(): a retry re-posts the latest quote, and a signal whose edge was computed
    at 2.00 must not be placed at full size at 1.75 just because the retry says 1.75."""
    own = float(row.get("price") or 0)
    first = float(row.get("first_price") or 0)        # set by claim() on this very row
    if first <= 0:
        try:
            with _ledger_lock():
                st = load_state()
            first = float((st.get("first_prices") or {}).get(selection_id(row))
                          or (st.get("selections", {}).get(selection_id(row)) or {})
                          .get("price") or 0)
        except Exception:
            first = 0.0
    return max(own, first)


def release_ambiguous(sel_id: str, amount: float, why: str) -> float:
    """OPERATOR-ONLY: give back ambiguous dollars a human has PROVEN absent from the book.
    Never called automatically: releasing on "we could not tell" re-arms the double-stake.
    Returns the amount released."""
    if amount <= 0 or not sel_id:
        return 0.0
    try:
        lock = _ledger_lock()
        lock.__enter__()
    except LedgerLockError:
        return 0.0
    try:
        return _release_locked(sel_id, amount, why)
    finally:
        lock.__exit__(None, None, None)


def _release_locked(sel_id: str, amount: float, why: str) -> float:
    if True:
        state = load_state()
        if state.get("state_corrupt"):
            return 0.0
        sel = (state.get("selections") or {}).get(sel_id)
        if not sel:
            return 0.0
        amb = float(sel.get("ambiguous") or 0.0)
        give = round(min(amount, amb), 2)
        if give <= 0:
            return 0.0
        sel["ambiguous"] = round(amb - give, 2)
        sel["committed"] = round(max(0.0, float(sel.get("committed") or 0.0) - give), 2)
        if not save_state(state):
            return 0.0
        log.warning("[nba_model] RELEASED $%.2f of ambiguous on %s (%s)", give, sel_id, why)
        return give


def effective_ceiling(target: float) -> float:
    """What a selection can actually reach: min(target, NBA_MODEL_MAX_SELECTION_STAKE, and the
    test stake while test mode is on). `retryable` is measured against THIS, not the raw target,
    so a selection sitting at its ceiling stops being reported retryable."""
    ceiling = float(target or 0.0)
    try:
        ceiling = min(ceiling, float(cfg("NBA_MODEL_MAX_SELECTION_STAKE")))
        if cfg("NBA_MODEL_TEST_MODE") and test_stake_usable():
            ceiling = min(ceiling, float(cfg("NBA_MODEL_TEST_STAKE")))
    except (TypeError, ValueError):
        pass
    return ceiling


def fill_flags(outcome: str, ambiguous: float, manual: bool, selection_committed: float,
               target: float, selection_ambiguous: float = 0.0):
    """(retryable, final) for a record.

    retryable: the producer may send a retry for this selection: outcome partial or none, no
    ambiguous money, not manual, and a real shortfall left. final: this key's record will not
    be revised, i.e. it carries no ambiguous amount that could still resolve."""
    key_amb = float(ambiguous or 0.0) > 0.005
    any_amb = max(float(ambiguous or 0.0), float(selection_ambiguous or 0.0)) > 0.005
    short = effective_ceiling(target) - float(selection_committed or 0.0)
    retryable = (outcome in ("partial", "none") and not any_amb and not manual and short > 0.005)
    return bool(retryable), (not key_amb)


def _fills_scan(key: str):
    """(records_for_key, any_record, answered) in ONE pass over the fills file."""
    n = 0
    answered = False
    try:
        with open(fills_path(), "r", encoding="utf-8") as f:
            for ln in f:
                if key not in ln:
                    continue                       # cheap pre-filter before the JSON parse
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("key") != key:
                    continue
                n += 1
                if r.get("outcome") != "refused":
                    answered = True
    except OSError:
        pass
    return n, n > 0, answered


def _next_revision(key: str) -> int:
    return _fills_scan(key)[0] + 1


def _attempt_of(row: dict) -> int:
    try:
        return int(row.get("attempt"))
    except (TypeError, ValueError):
        pass
    try:
        return int(str(row.get("key", "")).split("~")[5])
    except (IndexError, ValueError):
        return 0


def write_fill(row: dict, *, requested: float, placed: float, ambiguous: float,
               manual: bool, accounts: list, outcome: str, event: str = "",
               committed=None, add_selection=None, selection=None) -> None:
    """Append ONE record for this key to the fills file both producers read (contract section 4).

    One record per key per revision, never for a repeated key (the caller checks key_state).
    Per-key amounts (`requested` = what Sportsbet was actually ASKED for on THIS key, 0.0 if
    nothing was sent; `placed`; `ambiguous`; `unfilled` = requested - placed - ambiguous) and
    whole-selection amounts (`selection_placed`, `selection_ambiguous`, `selection_committed`
    == placed + ambiguous) come from `selection` (the post-commit numbers `commit` returned: no
    ledger read at all), else from the ledger, else from the fills file when the ledger cannot
    be read. The ledger lock is taken ONCE for the whole write (a failure to take it only means
    the numbers come from the fills file and the answered outcome is pruned later). Consumers
    keep the highest `revision` per key. Guarded: a logging failure must never break placement."""
    try:
        lock = None
        try:
            lock = _ledger_lock()
            lock.__enter__()
        except LedgerLockError:
            lock = None
        try:
            _write_fill_locked(row, requested, placed, ambiguous, manual, accounts, outcome,
                               event, committed, add_selection, selection, lock is not None)
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)
    except Exception as e:
        log.error("[nba_model] failed to append fill record: %s", e)


def _write_fill_locked(row, requested, placed, ambiguous, manual, accounts, outcome, event,
                       committed, add_selection, selection, have_lock):
    key = row.get("key", "")
    n_prev, _any, answered = _fills_scan(key)
    if outcome == "refused" and answered:
        # A REFUSED record must never be written for a key that already has a non-refused one:
        # consumers keep the highest revision, so it would replace the real record (a replay of
        # a placed key) with "refused, placed 0".
        log.warning("[nba_model] not writing a refused record over the existing answer "
                    "for %s", key)
        return
    state = None
    if have_lock:
        state = load_state()
        if state.get("state_corrupt"):
            state = None
    if selection is not None:
        snap = dict(selection)
    elif state is not None:
        snap = _numbers_from_state(state, row)
    else:
        snap = _numbers_from_fills(row)
    if add_selection and selection is None:
        # The ledger commit FAILED after money was placed: report the best-known state,
        # the ledger's figures plus this key's own placed / ambiguous dollars.
        snap = {"placed": round(snap["placed"] + float(add_selection[0]), 2),
                "ambiguous": round(snap["ambiguous"] + float(add_selection[1]), 2),
                "committed": round(snap["committed"] + float(add_selection[0])
                                   + float(add_selection[1]), 2),
                "manual": snap.get("manual", False)}
    sel_amb = snap["ambiguous"]
    sel_committed = float(committed) if committed is not None else snap["committed"]
    sel_placed = round(max(0.0, sel_committed - sel_amb), 2)
    clean = []
    for a in accounts or []:
        try:
            odds = float(a.get("odds"))
            stake = float(a.get("stake"))
        except (TypeError, ValueError):
            log.error("[nba_model] dropping an account with no real odds/stake: %r", a)
            continue
        if not (math.isfinite(odds) and odds > 1.0 and math.isfinite(stake) and stake > 0):
            log.error("[nba_model] dropping an account with unusable odds/stake: %r", a)
            continue
        clean.append({"session_id": str(a.get("session_id", "")), "stake": round(stake, 2),
                      "odds": odds, "bet_id": str(a.get("bet_id", "") or "")})
    target = round(float(row.get("target") or 0), 2)
    is_manual = bool(manual) or bool(snap.get("manual"))
    retryable, final = fill_flags(outcome, ambiguous, is_manual, sel_committed, target, sel_amb)
    req = round(float(requested or 0), 2)
    pl = round(float(placed or 0), 2)
    am = round(float(ambiguous or 0), 2)
    rec = {
        "key": key,
        "evt": row.get("evt", ""),
        "pid": str(row.get("pid", "")),
        "player": row.get("player", ""),
        "team": row.get("team", ""),
        "event": event,
        "market": row.get("market", ""),
        "side": row.get("side", ""),
        "line": row.get("line"),
        "kind": row.get("kind", ""),
        "attempt": _attempt_of(row),
        "revision": n_prev + 1,
        "target": target,
        "requested": req,
        "placed": pl,
        "ambiguous": am,
        # never negative: a loader may reject a negative amount
        "unfilled": round(max(0.0, req - pl - am), 2),
        "selection_placed": sel_placed,
        "selection_ambiguous": round(sel_amb, 2),
        "selection_committed": round(sel_committed, 2),
        "accounts": clean,
        "outcome": outcome,
        "retryable": retryable,
        "final": final,
        "manual": is_manual,
        "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = fills_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    # answered: drop the ledger's outcome entry for this key in the SAME load/save
    if state is not None and (state.get("outcomes") or {}).pop(key, None) is not None:
        save_state(state)


def validate_fill_record(rec: dict, *, writer: bool = True, check_key: bool = True) -> list:
    """Problems with a fills record (empty list = valid). The set tipbot's writer asserts, which
    covers what the model's `fills.validate_record` checks plus a little more:

    HARD on BOTH sides (the model rejects the record): the field set and order, types, the
    outcome vocabulary, `key` against evt / pid / line / kind / attempt (a v3 key), retry <=>
    attempt >= 1, line > 0, `processed_at` an ISO time WITH a UTC offset, unfilled ==
    max(0, requested - placed - ambiguous), selection_committed == selection_placed +
    selection_ambiguous, placed + ambiguous <= requested, account stakes summing to placed and
    real odds above 1, final false exactly when ambiguous > 0, manual / retryable / refused /
    capped implications.

    WRITER-SIDE ONLY (`writer=True`, the default; the model treats these as a SOFT flag
    `placed_over_selection` because tipbot writes selection_placed 0 when its ledger lock timed
    out after a commit): placed <= selection_placed and ambiguous <= selection_ambiguous. tipbot
    must NEVER write such a record, so its own tests keep them hard.

    `check_key=False` skips the key checks for fixtures with synthetic keys."""
    bad = []
    if tuple(rec) != FILL_FIELDS:
        bad.append(f"fields/order {tuple(rec)}")
        return bad
    if rec["outcome"] not in FILL_OUTCOMES:
        bad.append(f"outcome {rec['outcome']!r}")
    for k in ("retryable", "final", "manual"):
        if not isinstance(rec[k], bool):
            bad.append(f"{k} is not a bool")
    if not isinstance(rec["pid"], str):
        bad.append("pid is not a string")
    for k in ("attempt", "revision"):
        if (not isinstance(rec[k], int) or isinstance(rec[k], bool)
                or rec[k] < (1 if k == "revision" else 0)):
            bad.append(f"{k} is not an int >= {1 if k == 'revision' else 0}")
    line = rec["line"]
    if isinstance(line, bool) or not isinstance(line, (int, float)) or not math.isfinite(line) \
            or line <= 0:
        bad.append("line is not a positive number")
    stamp = rec["processed_at"]
    try:
        when = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None or when.utcoffset() is None:
        bad.append("processed_at is not an ISO time with a UTC offset")
    nums = ("target", "requested", "placed", "ambiguous", "unfilled", "selection_placed",
            "selection_ambiguous", "selection_committed")
    for k in nums:
        v = rec[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            bad.append(f"{k} is not a finite non-negative number")
    if bad:
        return bad
    if check_key:
        key = rec["key"]
        parts = str(key).split("~")
        if any(c in str(key) for c in "| \t\n") or len(parts) != 6 \
                or parts[4] not in ("base", "addon", "retry") or not parts[5].isdigit():
            bad.append(f"key {key!r} is not a v3 key")
        else:
            try:
                line_ok = abs(float(parts[2]) - float(line)) <= 1e-9
            except ValueError:
                line_ok = False
            if (parts[0] != rec["evt"] or parts[1] != rec["pid"] or parts[4] != rec["kind"]
                    or int(parts[5]) != rec["attempt"] or not line_ok):
                bad.append("the key does not match evt, pid, line, kind and attempt")
    if (rec["kind"] == "retry") != (rec["attempt"] >= 1):
        bad.append("a retry has attempt >= 1 and nothing else does")
    if abs(rec["selection_committed"] - rec["selection_placed"] - rec["selection_ambiguous"]) > 0.011:
        bad.append("selection_committed != selection_placed + selection_ambiguous")
    if abs(rec["unfilled"] - max(0.0, rec["requested"] - rec["placed"] - rec["ambiguous"])) > 0.011:
        bad.append("unfilled != requested - placed - ambiguous")
    if rec["outcome"] in ("refused", "capped") and (rec["requested"] != 0.0 or rec["retryable"]):
        bad.append("a refused or capped record must have requested 0.0 and not be retryable")
    if writer:
        if rec["placed"] > rec["selection_placed"] + 0.011:
            bad.append("placed exceeds selection_placed")
        if rec["ambiguous"] > rec["selection_ambiguous"] + 0.011:
            bad.append("ambiguous exceeds selection_ambiguous")
    if rec["placed"] + rec["ambiguous"] > rec["requested"] + 0.011:
        bad.append("placed + ambiguous exceeds requested")
    if rec["final"] != (rec["ambiguous"] <= 0):
        bad.append("final must be false exactly when ambiguous > 0")
    if rec["outcome"] == "manual" and not rec["manual"]:
        bad.append("outcome manual needs manual true")
    if rec["retryable"] and (
            rec["outcome"] not in ("partial", "none") or rec["ambiguous"] > 0 or rec["manual"]
            or rec["selection_committed"] >= rec["target"] - 0.005):
        bad.append("retryable only for partial/none, nothing ambiguous, not manual, with a "
                   "shortfall left")
    if not isinstance(rec["accounts"], list):
        bad.append("accounts is not a list")
    else:
        for a in rec["accounts"]:
            if (not isinstance(a, dict) or tuple(a) != ("session_id", "stake", "odds", "bet_id")
                    or isinstance(a["odds"], bool) or not isinstance(a["odds"], (int, float))
                    or not math.isfinite(a["odds"]) or a["odds"] <= 1.0):
                bad.append(f"account {a!r}")
        if abs(sum(a["stake"] for a in rec["accounts"] if isinstance(a, dict)
                   and isinstance(a.get("stake"), (int, float))) - rec["placed"]) > 0.011:
            bad.append("sum(accounts.stake) != placed")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
