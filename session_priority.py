"""
TipBot v4.0 — session priority + liability module.

Owns per-session metadata (sessions.yaml) and per-sport priority lists
(.env). Provides helpers for:

  - Loading and validating sessions.yaml at startup
  - Grouping active sessions by bookmaker
  - Computing max stake from a liability cap given live odds
  - Resolving the priority order for a sport + bet kind (single vs SGM)
  - Looking up the liability cap for a (session, sport, market) combo
    with sensible fallbacks (shared `player_threshold` key, `sgm` key,
    racing per-track + MBL fallback)

This module is the single source of truth for v4.0 placement routing.
Singles, SGMs, and racing all consume the helpers here so behaviour is
consistent across bet kinds.

Wilson maintains sessions.yaml manually; this module never writes to it.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("tipbot.session_priority")


# ── Constants ───────────────────────────────────────────────────────

# Sport keys allowed inside sessions.yaml liability blocks.
# MLB added 2026-06-01 (v5.0): only MLB SGMs auto-place (the Shook HRRBI 2+
# -> 2-leg same-player SGM edge). MLB singles have NO priority list, so
# for_sport("mlb", is_sgm=False) returns [] and they route to manual.
ALLOWED_SPORTS = {"nba", "nbl", "afl", "mlb", "racing"}

# Markets that may appear in a per-sport liability block (excluding racing,
# which is structured per-track). Any market not listed here is allowed
# (yaml is forward-compatible) but a warning is logged at load time so
# typos surface early.
KNOWN_NBA_MARKETS = {
    # main lines
    "h2h", "line", "total_points", "sgm",
    # O/U player props
    "player_points", "player_rebounds", "player_assists", "player_threes",
    "player_blocks", "player_steals",
    "player_pra", "player_pts_rebs", "player_pts_asts", "player_asts_rebs",
    # shared cap for ALL NBA *_threshold markets
    "player_threshold",
}

KNOWN_AFL_MARKETS = {
    # main lines
    "h2h", "line", "total_points", "sgm",
    # O/U + threshold player markets. By default a stat's cap covers BOTH its
    # O/U and threshold variant (sibling fallback: player_disposals covers
    # player_disposals_threshold). A stat MAY also declare its threshold cap
    # EXPLICITLY to give overs a different ladder — e.g. player_disposals_threshold
    # = [300,250,200,150] (overs) vs player_disposals = [124,99,74,50] (unders),
    # added 2026-06-06; the explicit key then wins over the sibling fallback.
    "player_disposals", "player_disposals_threshold",
    "player_marks", "player_tackles", "player_kicks",
    "player_handballs", "player_clearances", "player_hitouts",
    "player_fantasy", "player_goals",
    # Over-ladder sizing keys (2026-06-07, Task B). For 8 of 9 stats the OVER
    # actually PLACES on the base player_* O/U market (dir=over, half-line ladder
    # per the live catalog probe) — these *_threshold names are INTERNAL sizing
    # keys only (Task A swaps to them at sizing time so overs get the higher
    # ladder; the suffix never goes on the wire). GOALS is the exception:
    # goalscorer_threshold_afl is a REAL Sportsbet market AND a DIRECT yaml key
    # (the _threshold_afl normaliser would strip it to goalscorer_threshold ->
    # sibling goalscorer -> None = UNCAPPED, so it MUST resolve via a direct hit).
    "goalscorer_threshold_afl",
    "player_fantasy_threshold", "player_marks_threshold",
    "player_tackles_threshold", "player_kicks_threshold",
    "player_handballs_threshold", "player_clearances_threshold",
    "player_hitouts_threshold",
}

# MLB markets (2026-06-01). For the gated rollout only `sgm` is reachable
# (the HRRBI 2-leg SGM); the rest are listed so a forward-looking yaml cap
# doesn't warn. MLB player props all live in ONE `player_stats` market on
# Sportsbet (keyed by a per-selection `stat` field).
KNOWN_MLB_MARKETS = {
    "h2h", "money_line", "line", "total_points", "team_total", "sgm",
    "player_stats",
}

# Racing bet types we expect under per-track caps. AGS covered separately
# under sports above (player_goals).
KNOWN_RACING_BET_TYPES = {"win", "place"}


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class SessionMeta:
    """Per-session metadata loaded from sessions.yaml."""
    session_id: str
    name: str
    bookmaker: str
    boost_eligible: bool = False
    # Raw liability block as nested dict for flexible lookups.
    # Shape: {sport: {market: cap}} where cap is float | "unlimited" | "mbl"
    # For racing: {sport: {track_name: {bet_type: cap}, "default": cap}}
    liability: dict = field(default_factory=dict)


@dataclass
class PriorityConfig:
    """Per-sport priority lists loaded from .env."""
    nba_singles: list[str] = field(default_factory=list)
    nba_sgm: list[str] = field(default_factory=list)
    afl_singles: list[str] = field(default_factory=list)
    afl_sgm: list[str] = field(default_factory=list)
    mlb_singles: list[str] = field(default_factory=list)
    mlb_sgm: list[str] = field(default_factory=list)
    racing: list[str] = field(default_factory=list)

    def for_sport(self, sport: str, is_sgm: bool = False) -> list[str]:
        """
        Return priority list for a (sport, is_sgm) combo.

        Sport keys: 'nba', 'nbl', 'afl', 'mlb', 'racing'. NBL falls back to NBA
        priority (no separate list — same bookmakers, same accounts).
        Anything else returns [] (no auto-placement).

        MLB (2026-06-01): mlb_singles is intentionally left EMPTY in .env so
        MLB singles return [] -> manual. Only mlb_sgm is populated, so the
        HRRBI 2-leg SGM auto-places while every other MLB tip goes to manual.
        """
        s = (sport or "").lower()
        if s in ("nba", "nbl"):
            return self.nba_sgm if is_sgm else self.nba_singles
        if s == "afl":
            return self.afl_sgm if is_sgm else self.afl_singles
        if s == "mlb":
            return self.mlb_sgm if is_sgm else self.mlb_singles
        if s == "racing":
            return self.racing
        return []


# ── Module state ────────────────────────────────────────────────────

# Loaded by load_sessions_yaml(). Empty until startup.
_session_meta: dict[str, SessionMeta] = {}
_priority_config: PriorityConfig = PriorityConfig()
_yaml_path: Optional[Path] = None


# ── Loading + validation ────────────────────────────────────────────

def load_sessions_yaml(path: str | Path) -> dict[str, SessionMeta]:
    """
    Load sessions.yaml from disk and populate module state.

    Returns the loaded session_meta dict (also stored in module state for
    helper-function access). Raises FileNotFoundError if the file is
    missing — Wilson must create it before v4.0 can run.

    Validation: warns on unknown sport keys, unknown market names,
    missing required fields. Doesn't refuse to load — best-effort to
    keep startup non-fatal if Wilson typos a market name.
    """
    global _session_meta, _yaml_path
    p = Path(path)
    _yaml_path = p

    if not p.exists():
        raise FileNotFoundError(
            f"sessions.yaml not found at {p}. "
            f"v4.0 requires sessions.yaml to be populated before startup."
        )

    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"sessions.yaml must be a top-level mapping, got {type(raw).__name__}")

    parsed: dict[str, SessionMeta] = {}
    for key, body in raw.items():
        sid = str(key).strip()
        if not sid:
            log.warning("sessions.yaml: skipping empty session id key")
            continue
        if not isinstance(body, dict):
            log.warning(f"sessions.yaml: session {sid} body is not a mapping, skipping")
            continue

        name = str(body.get("name", "")).strip() or f"session-{sid}"
        bookmaker = str(body.get("bookmaker", "")).strip().lower()
        if not bookmaker:
            log.warning(f"sessions.yaml: session {sid} has no bookmaker, skipping")
            continue

        boost = bool(body.get("boost_eligible", False))
        liability = body.get("liability") or {}
        if not isinstance(liability, dict):
            log.warning(f"sessions.yaml: session {sid} liability is not a mapping, treating as empty")
            liability = {}

        # Validate sports + markets (warn-only)
        for sport_key, sport_body in liability.items():
            if sport_key not in ALLOWED_SPORTS:
                log.warning(
                    f"sessions.yaml: session {sid} has unknown sport '{sport_key}' "
                    f"(allowed: {sorted(ALLOWED_SPORTS)})"
                )
                continue
            if not isinstance(sport_body, dict):
                log.warning(f"sessions.yaml: session {sid} {sport_key} block is not a mapping")
                continue

            if sport_key in ("nba", "nbl"):
                known = KNOWN_NBA_MARKETS
            elif sport_key == "afl":
                known = KNOWN_AFL_MARKETS
            elif sport_key == "mlb":
                known = KNOWN_MLB_MARKETS
            else:
                known = None  # racing validated separately

            if known is not None:
                for market_key in sport_body.keys():
                    if market_key not in known:
                        log.warning(
                            f"sessions.yaml: session {sid} {sport_key}.{market_key} "
                            f"is not a recognised market name (treating as custom)"
                        )

            if sport_key == "racing":
                _validate_racing_block(sid, sport_body)

        parsed[sid] = SessionMeta(
            session_id=sid,
            name=name,
            bookmaker=bookmaker,
            boost_eligible=boost,
            liability=liability,
        )

    _session_meta = parsed
    log.info(f"Loaded sessions.yaml: {len(parsed)} session(s) configured")
    return parsed


_VALID_DAY_KEYS = {
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
}


def _validate_racing_block(sid: str, racing_body: dict) -> None:
    """Validate a racing liability block. Warn-only.

    Layout: `racing -> {thoroughbreds: {win,place}, harness: {<track>: caps,
    default: mbl}}`. The legacy flat layout (tracks directly under `racing`) is
    still accepted for back-compat.
    """
    # Discipline cap: flat {win, place} (thoroughbreds — Zak/Trial day-before).
    tb = racing_body.get("thoroughbreds")
    if tb is not None:
        if not isinstance(tb, dict):
            log.warning(
                f"sessions.yaml: session {sid} racing.thoroughbreds should be a "
                f"mapping of bet_type -> cap"
            )
        else:
            for bt, cap in tb.items():
                if bt not in KNOWN_RACING_BET_TYPES:
                    log.warning(
                        f"sessions.yaml: session {sid} racing.thoroughbreds.{bt} "
                        f"is not a known racing bet type (expected: win/place)"
                    )
                if not _is_valid_cap(cap):
                    log.warning(
                        f"sessions.yaml: session {sid} racing.thoroughbreds.{bt} = "
                        f"{cap!r} is not a valid cap"
                    )

    # Per-track HARNESS caps live under racing.harness; fall back to the legacy
    # flat layout (tracks directly under racing, minus the thoroughbreds key).
    harness = racing_body.get("harness")
    if isinstance(harness, dict):
        _validate_track_caps(sid, "racing.harness", harness)
    elif harness is not None:
        log.warning(
            f"sessions.yaml: session {sid} racing.harness should be a mapping of "
            f"<track> -> caps"
        )
    else:
        legacy = {k: v for k, v in racing_body.items() if k != "thoroughbreds"}
        _validate_track_caps(sid, "racing", legacy)


def _validate_track_caps(sid: str, prefix: str, tracks_body: dict) -> None:
    """Validate a mapping of <track> -> {win/place (+ day overrides)} plus an
    optional `default`. Warn-only. Shared by the harness section + the legacy
    flat layout."""
    for track_key, track_body in tracks_body.items():
        if track_key == "default":
            # Default may be a number, "unlimited", or "mbl"
            if not _is_valid_cap(track_body):
                log.warning(
                    f"sessions.yaml: session {sid} {prefix}.default has invalid value "
                    f"{track_body!r} (expected number, 'unlimited', or 'mbl')"
                )
            continue
        if not isinstance(track_body, dict):
            log.warning(
                f"sessions.yaml: session {sid} {prefix}.{track_key} should be a "
                f"mapping of bet_type -> cap"
            )
            continue
        for bt, cap in track_body.items():
            # Day-of-week override sub-block — recursively validate the
            # contained bet_type caps.
            if bt in _VALID_DAY_KEYS:
                if not isinstance(cap, dict):
                    log.warning(
                        f"sessions.yaml: session {sid} {prefix}.{track_key}.{bt} "
                        f"day override should be a mapping of bet_type -> cap"
                    )
                    continue
                for inner_bt, inner_cap in cap.items():
                    if inner_bt not in KNOWN_RACING_BET_TYPES:
                        log.warning(
                            f"sessions.yaml: session {sid} {prefix}.{track_key}.{bt}."
                            f"{inner_bt} is not a known racing bet type "
                            f"(expected: win/place)"
                        )
                    if not _is_valid_cap(inner_cap):
                        log.warning(
                            f"sessions.yaml: session {sid} {prefix}.{track_key}.{bt}."
                            f"{inner_bt} = {inner_cap!r} is not a valid cap"
                        )
                continue
            # Plain bet_type cap at track level
            if bt not in KNOWN_RACING_BET_TYPES:
                log.warning(
                    f"sessions.yaml: session {sid} {prefix}.{track_key}.{bt} "
                    f"is not a known racing bet type (expected: win/place)"
                )
            if not _is_valid_cap(cap):
                log.warning(
                    f"sessions.yaml: session {sid} {prefix}.{track_key}.{bt} = "
                    f"{cap!r} is not a valid cap"
                )


def _is_valid_cap(value) -> bool:
    """
    A cap must be a non-negative number, 'unlimited', or 'mbl' (racing only).
    Zero is intentionally valid — used for "do not bet" sentinel on WA places.
    """
    if isinstance(value, (int, float)) and value >= 0:
        return True
    if isinstance(value, str) and value.lower() in ("unlimited", "mbl"):
        return True
    return False


def load_priority_from_env() -> PriorityConfig:
    """
    Read per-sport priority lists from .env and store in module state.

    Env vars (comma-separated session IDs, in priority order):
      NBA_SESSION_PRIORITY
      NBA_SGM_SESSION_PRIORITY
      AFL_SESSION_PRIORITY
      AFL_SGM_SESSION_PRIORITY
      MLB_SESSION_PRIORITY        (leave EMPTY -> MLB singles route to manual)
      MLB_SGM_SESSION_PRIORITY    (the HRRBI 2-leg SGM session(s))
      RACING_SESSION_PRIORITY

    Sessions not in the relevant list are excluded from auto-placement
    for that (sport, kind) — manual alert only.
    """
    global _priority_config
    cfg = PriorityConfig(
        nba_singles=_parse_priority_env("NBA_SESSION_PRIORITY"),
        nba_sgm=_parse_priority_env("NBA_SGM_SESSION_PRIORITY"),
        afl_singles=_parse_priority_env("AFL_SESSION_PRIORITY"),
        afl_sgm=_parse_priority_env("AFL_SGM_SESSION_PRIORITY"),
        mlb_singles=_parse_priority_env("MLB_SESSION_PRIORITY"),
        mlb_sgm=_parse_priority_env("MLB_SGM_SESSION_PRIORITY"),
        racing=_parse_priority_env("RACING_SESSION_PRIORITY"),
    )
    _priority_config = cfg
    return cfg


def _parse_priority_env(var_name: str) -> list[str]:
    """Parse a comma-separated env var into a list of cleaned session IDs."""
    raw = os.getenv(var_name, "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


# ── Public lookups ──────────────────────────────────────────────────

def get_session_meta(session_id: str) -> Optional[SessionMeta]:
    """Look up a session's yaml metadata. Returns None if unknown."""
    return _session_meta.get(str(session_id))


def get_priority_config() -> PriorityConfig:
    """Return the current priority config (read-only access)."""
    return _priority_config


def get_priority_for(sport: str, is_sgm: bool = False) -> list[str]:
    """Convenience: priority list for a (sport, is_sgm) combo."""
    return _priority_config.for_sport(sport, is_sgm)


def filter_and_order_sessions(
    sessions: list[dict], sport: str, is_sgm: bool = False,
) -> list[dict]:
    """
    Filter active sessions to only those in the relevant priority list,
    in priority order. Drops anything not listed.

    `sessions` is the raw HyperBot session list (list of dicts with
    'session_id' key). Returns a filtered + reordered list.

    If no priority list is configured for the sport, returns sessions
    unchanged (no regression — caller can fall back to legacy behaviour).
    """
    priority = get_priority_for(sport, is_sgm)
    if not priority:
        return sessions

    by_id = {str(s.get("session_id", "")): s for s in sessions}
    ordered = [by_id[pid] for pid in priority if pid in by_id]

    listed = set(priority)
    dropped = [
        str(s.get("session_id", ""))
        for s in sessions
        if str(s.get("session_id", "")) not in listed
    ]
    if dropped:
        kind = "SGM" if is_sgm else "singles"
        # M29: promoted from INFO to WARNING — a dropped session means a
        # priority list misconfiguration (typo in .env) silently misses bets.
        log.warning(
            f"Priority filter ({sport} {kind}): kept {len(ordered)}, "
            f"dropped {len(dropped)} unlisted: {dropped}"
        )
    return ordered


def group_by_bookmaker(sessions: list[dict]) -> dict[str, list[dict]]:
    """
    Group session dicts by bookmaker. Preserves input order within each
    group, so callers that pass a priority-ordered list get
    priority-ordered groups.

    Bookmaker is read from session_meta first (yaml), falling back to
    the 'bookie' key on the session dict (HyperBot raw response).
    """
    out: dict[str, list[dict]] = {}
    for s in sessions:
        sid = str(s.get("session_id", ""))
        meta = _session_meta.get(sid)
        bookie = (meta.bookmaker if meta else "") or s.get("bookie", "")
        bookie = (bookie or "unknown").lower()
        out.setdefault(bookie, []).append(s)
    return out


# ── Liability + stake math ──────────────────────────────────────────

def lookup_liability_cap(
    session_id: str, sport: str, market: str,
) -> Optional[float | str]:
    """
    Resolve the liability cap for a (session, sport, market) combo.

    Returns:
      float  — numeric cap in AUD
      "unlimited" — no cap (full intended stake)
      None — no entry found (caller decides: treat as unlimited or skip)

    Lookup order:
      1. Direct: liability[sport][market]
      2. If market endswith '_threshold' and not found: liability[sport]['player_threshold']
         (Spec: all NBA threshold markets share a single cap key)
      3. None
    """
    meta = _session_meta.get(str(session_id))
    if not meta:
        return None

    sport_key = (sport or "").lower()
    if sport_key == "nbl":
        sport_key = "nba"  # NBL caps fall under NBA in the yaml
    sport_block = meta.liability.get(sport_key)
    if not isinstance(sport_block, dict):
        return None

    # Direct hit
    if market in sport_block:
        return _normalise_cap(sport_block[market])

    # Normalise an AFL "_threshold_afl" suffix to "_threshold" so BOTH the
    # HyperBot market name (e.g. player_disposals_threshold_afl) and the
    # internal one (player_disposals_threshold) resolve to the same cap. Without
    # this, the _afl form fell through every branch below and returned None
    # (uncapped) — a real risk once thresholds got their own high ladder.
    norm = market[: -len("_afl")] if market.endswith("_threshold_afl") else market
    if norm != market and norm in sport_block:
        return _normalise_cap(sport_block[norm])

    # Threshold fallbacks. NBA: all *_threshold markets share player_threshold.
    # AFL: an EXPLICIT <stat>_threshold key (e.g. player_disposals_threshold,
    # 2026-06-06: its own 300/250/200/150 ladder) wins via the direct hit above.
    # Otherwise fall back to the O/U sibling (drop "_threshold") so a stat
    # WITHOUT its own threshold cap still inherits the base O/U cap. Goals
    # special case: HyperBot only has goalscorer_threshold_afl (no O/U variant)
    # so callers look it up via player_goals directly.
    if norm.endswith("_threshold"):
        if "player_threshold" in sport_block:
            return _normalise_cap(sport_block["player_threshold"])
        # AFL-style sibling fallback: drop "_threshold" and retry
        sibling = norm[: -len("_threshold")]
        if sibling in sport_block:
            return _normalise_cap(sport_block[sibling])

    return None


def lookup_racing_liability(
    session_id: str, track: str, bet_type: str,
    date: Optional[str] = None,
) -> Optional[float | str]:
    """
    Resolve racing liability cap for a (session, track, bet_type, date).

    Returns float, 'unlimited', 'mbl', or None.
    A returned numeric 0.0 means "do not bet" — caller must respect this
    rather than treating zero as missing-cap. Used for WA places where
    bookies have no MBL obligation and Wilson doesn't trust the market.

    Lookup order:
      1. liability.racing[exact_track][<day_of_week>][bet_type]   if date given
      2. liability.racing[case-insensitive track match][<day_of_week>][bet_type]
      3. liability.racing[exact_track][bet_type]
      4. liability.racing[case-insensitive track match][bet_type]
      5. liability.racing[default]
      6. None

    Day override format inside a track block (lowercase weekday key):
      "Albion Park":
        win: 500
        place: 200
        saturday:
          win: 1000
          place: 400

    Days other than the looked-up day are ignored. If the day key exists
    but doesn't have the requested bet_type, fall through to the
    track-level default (NOT to the global default — keeps explicit
    track overrides authoritative).
    """
    meta = _session_meta.get(str(session_id))
    if not meta:
        return None
    racing_block = meta.liability.get("racing")
    if not isinstance(racing_block, dict):
        return None

    # Per-track HARNESS caps live under racing.harness (Tip Titans etc.). Fall
    # back to the legacy flat layout (tracks directly under racing) for
    # robustness if a session predates the harness wrapper. The thoroughbreds
    # discipline cap is resolved separately (lookup_thoroughbreds_liability).
    tracks_block = racing_block.get("harness")
    if not isinstance(tracks_block, dict):
        tracks_block = racing_block

    # Direct track match first
    track_block = tracks_block.get(track)

    # Case-insensitive track lookup if no direct hit
    if track_block is None and track:
        t_lower = track.lower().strip()
        for k, v in tracks_block.items():
            # reserved keys, NOT track names — never match a track lookup.
            if k in ("default", "thoroughbreds", "harness"):
                continue
            if isinstance(k, str) and k.lower().strip() == t_lower:
                track_block = v
                break

    if isinstance(track_block, dict):
        # Try day override first if date supplied
        day_name = _resolve_day_name(date)
        if day_name and day_name in track_block:
            day_block = track_block.get(day_name)
            if isinstance(day_block, dict):
                day_cap = day_block.get(bet_type)
                if day_cap is not None:
                    return _normalise_cap(day_cap)
            # Day key exists but isn't a dict — yaml typo. Fall through
            # to track-level default rather than crashing.

        # Track-level (non-day-specific) cap
        cap = track_block.get(bet_type)
        if cap is not None:
            return _normalise_cap(cap)

    # Fall back to default
    default = tracks_block.get("default")
    if default is not None:
        return _normalise_cap(default)
    return None


def lookup_thoroughbreds_liability(
    session_id: str, bet_type: str,
) -> Optional[float | str]:
    """Resolve the THOROUGHBRED discipline liability cap for a session.

    Thoroughbred bets (Zak / Trial Sniper image-racing tips) are day-before
    tips with no MBL, so they use a flat per-account cap kept under
    `liability.racing.thoroughbreds.{win,place}` — INDEPENDENT of the per-track
    HARNESS caps. This is a separate lookup on purpose so a thoroughbred bet can
    NEVER pick up a same-named harness track's cap (e.g. Bunbury has both a
    thoroughbred and a harness track).

    Returns float, 'unlimited', 0.0 (do-not-bet), or None when not configured
    (caller decides the fallback).
    """
    meta = _session_meta.get(str(session_id))
    if not meta:
        return None
    racing_block = meta.liability.get("racing")
    if not isinstance(racing_block, dict):
        return None
    tb_block = racing_block.get("thoroughbreds")
    if not isinstance(tb_block, dict):
        return None
    cap = tb_block.get(bet_type)
    if cap is None:
        return None
    return _normalise_cap(cap)


def _resolve_day_name(date: Optional[str]) -> Optional[str]:
    """Convert ISO date string (YYYY-MM-DD) to lowercase weekday name."""
    if not date:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.strptime(date, "%Y-%m-%d").strftime("%A").lower()
    except (ValueError, TypeError):
        return None


def _normalise_cap(value) -> float | str | tuple[float, ...] | None:
    """
    Coerce a yaml cap value into the canonical types we use downstream.

    0 is a valid value meaning "do not bet" (used for WA places where
    bookies have no MBL obligation). Caller must check for 0 explicitly
    rather than treating it as falsy.

    List support (added 2026-05-17 for AFL liability ladder): a yaml
    sequence like `[300, 250, 200]` is returned as a tuple of floats in
    descending intent. Each value is a separate liability target; the
    caller iterates highest-to-lowest, computing max_stake at each step.
    Used for AFL player_disposals where Wilson wants graceful degradation
    instead of the percentage-based stake ladder used for NBA. Tuples
    are immutable so downstream code can't accidentally mutate the
    canonical cap. Strings inside the list are rejected (no 'unlimited'
    mid-list — use a single 'unlimited' value if no cap is wanted).
    """
    if isinstance(value, (int, float)):
        if value < 0:
            return None  # negative makes no sense
        return float(value)  # 0.0 valid as "do not bet"
    if isinstance(value, str):
        v = value.lower().strip()
        if v in ("unlimited", "mbl"):
            return v
    if isinstance(value, list):
        # List cap: each entry must be a non-negative number. Reject
        # mixed content (strings, nested lists) rather than silently
        # dropping entries — better to surface a yaml typo than place
        # bets on a half-parsed ladder.
        out: list[float] = []
        for entry in value:
            if isinstance(entry, (int, float)) and entry >= 0:
                out.append(float(entry))
            else:
                log.warning(
                    f"_normalise_cap: rejecting list with non-numeric entry "
                    f"{entry!r} (full value: {value!r})"
                )
                return None
        if not out:
            return None
        return tuple(out)
    if isinstance(value, tuple):
        # Idempotent pass-through for already-normalised list caps. Real
        # flow calls _normalise_cap on raw yaml values every lookup, so
        # an upstream change that stores a tuple shouldn't break here.
        # Validate contents same as list path.
        for entry in value:
            if not isinstance(entry, (int, float)) or entry < 0:
                return None
        return value if value else None
    return None


def pick_best_bookie_for_tip(
    odds_by_bookie: dict[str, float],
    priority_sessions: list[dict],
    tipped_odds: Optional[float],
    used_session_ids: set[str],
    odds_floor_pct: float = 0.9,
) -> Optional[str]:
    """
    From a {bookie: best_odds} map, pick the bookmaker that:
      1. Has at least one priority session not yet used
      2. Has odds at or above the floor (tipped_odds * odds_floor_pct)
         — no upper bound, higher than tipped is fine
      3. Among eligible bookies, returns the one with the HIGHEST odds
      4. On odds tie, returns the bookie holding the highest-priority
         unused session across all tied bookies

    `priority_sessions` is the full priority-ordered session list (already
    filtered to relevant priority list for this tip). Order matters — earlier
    in the list = higher priority.

    Returns the chosen bookie key, or None if no bookmaker is eligible.

    `tipped_odds` may be None if the tipster didn't supply odds — in that
    case the floor is skipped (we accept any bookie with priced odds).
    """
    if not odds_by_bookie:
        return None

    # Build per-bookie priority info: lowest priority index (= best priority)
    # of any unused session on that bookie. Bookies with no unused priority
    # session are ineligible.
    bookie_best_priority: dict[str, int] = {}
    for idx, sess in enumerate(priority_sessions):
        sid = str(sess.get("session_id", ""))
        if sid in used_session_ids:
            continue
        meta = _session_meta.get(sid)
        bookie = (meta.bookmaker if meta else "") or sess.get("bookie", "")
        bookie = (bookie or "").lower()
        if not bookie:
            continue
        if bookie not in bookie_best_priority or idx < bookie_best_priority[bookie]:
            bookie_best_priority[bookie] = idx

    # Apply odds floor + must-have-unused-session filter
    floor = (tipped_odds * odds_floor_pct) if tipped_odds and tipped_odds > 0 else 0.0
    eligible: list[tuple[str, float, int]] = []  # (bookie, odds, priority_idx)
    for bookie, odds in odds_by_bookie.items():
        if bookie not in bookie_best_priority:
            continue  # no unused priority session on this bookie
        if odds < floor:
            continue
        eligible.append((bookie, odds, bookie_best_priority[bookie]))

    if not eligible:
        return None

    # Sort: highest odds first, then lowest priority index (= best priority)
    # as tiebreak. Returns the winner.
    eligible.sort(key=lambda t: (-t[1], t[2]))
    return eligible[0][0]


def first_unused_session_on_bookie(
    bookie: str,
    priority_sessions: list[dict],
    used_session_ids: set[str],
) -> Optional[dict]:
    """
    Walk the priority-ordered session list, return the first unused session
    whose bookmaker matches. Used after pick_best_bookie_for_tip() picks a
    bookie — we then take the highest-priority session on it.
    """
    bookie_lower = (bookie or "").lower()
    for sess in priority_sessions:
        sid = str(sess.get("session_id", ""))
        if sid in used_session_ids:
            continue
        meta = _session_meta.get(sid)
        sess_bookie = (meta.bookmaker if meta else "") or sess.get("bookie", "")
        if (sess_bookie or "").lower() == bookie_lower:
            return sess
    return None


def liability_to_max_stake(liability_cap: float, odds: float) -> float:
    """
    Convert liability cap to max stake at given odds, rounded down to
    the nearest whole dollar.

    Formula: max_stake = floor(liability_cap / (odds - 1))

    Example: cap $500 at odds 1.9 -> floor(500 / 0.9) = $555.
    Resulting liability = 555 * 0.9 = $499.50, just under the cap.

    Returns 0 for non-positive odds or cap (caller should skip the bet).
    """
    if not odds or liability_cap <= 0 or odds <= 1.0:
        return 0.0
    return float(math.floor(liability_cap / (odds - 1)))


def resolve_max_stake(
    session_id: str, sport: str, market: str, live_odds: float,
    intended_stake: float,
) -> tuple[float, str]:
    """
    High-level helper: given a session, sport, market, live odds, and
    the intended stake, return (max_allowed_stake, reason).

    `reason` is a short label describing how the cap was derived — useful
    for logging:
      'unlimited'     — yaml says unlimited or no cap configured
      'cap=$X'        — numeric cap applied
      'cap-exceeds'   — cap is high enough that intended stake passes through
      'no-cap'        — no entry in yaml (treated as unlimited per spec
                        intention; safer than dropping the bet)

    The returned max_allowed_stake is min(intended_stake, cap-derived cap).
    """
    cap = lookup_liability_cap(session_id, sport, market)
    if cap is None:
        # No entry — treat as unlimited but log at WARNING so Wilson can spot
        # a missing market in his yaml (avoids silent over-staking). M16:
        # was log.debug which was invisible at production log levels.
        log.warning(
            f"No liability cap for session {session_id} {sport}.{market} "
            f"— treating as unlimited (add to sessions.yaml to cap)"
        )
        return (intended_stake, "no-cap")

    if cap == "unlimited":
        return (intended_stake, "unlimited")

    if isinstance(cap, str):
        # 'mbl' shouldn't reach here for sports markets — racing has its own
        # path. Log and treat as unlimited to fail open rather than blocking.
        log.warning(
            f"Unexpected string cap {cap!r} for session {session_id} "
            f"{sport}.{market} — treating as unlimited"
        )
        return (intended_stake, "unlimited")

    # H16: list-cap (tuple) — use first value for single-stake sizing.
    # Full list-mode is handled by resolve_stake_steps. This path is for callers
    # that just need one max_stake (e.g. SGM path, stat-fallback). Using first
    # value (highest liability) is the conservative choice. Record list-ness
    # BEFORE unwrapping so the no-odds branch below can honour H48's refusal.
    _was_list_cap = isinstance(cap, tuple)
    if isinstance(cap, tuple):
        cap = cap[0] if cap else 0.0

    # M17: guard None/zero live_odds to prevent TypeError in liability_to_max_stake.
    if not live_odds or live_odds <= 1.0:
        # M17/NEW fix (2026-05-30): for a LIST-cap market with no odds we must
        # REFUSE, mirroring H48 in resolve_stake_steps — returning the full
        # intended_stake here would bypass the liability ladder entirely and
        # over-stake. For scalar caps, no-odds remains "can't size, pass through"
        # (unchanged behaviour — those callers price-shop first anyway).
        if _was_list_cap:
            log.warning(
                f"resolve_max_stake: list-cap market {sport}.{market} session "
                f"{session_id} has no usable odds (live_odds={live_odds!r}) — "
                f"refusing to place rather than bypass the liability ladder"
            )
            return (0.0, "no-odds-refused")
        log.warning(
            f"resolve_max_stake: no usable odds (live_odds={live_odds!r}) for "
            f"session {session_id} {sport}.{market} — cannot size from liability cap"
        )
        return (intended_stake, "no-odds")

    max_stake = liability_to_max_stake(cap, live_odds)
    if max_stake >= intended_stake:
        return (intended_stake, f"cap-exceeds (cap=${cap:.0f})")
    return (max_stake, f"cap=${cap:.0f}")


def resolve_stake_steps(
    session_id: str, sport: str, market: str, live_odds: float,
    intended_stake: float, default_ladder_fn,
) -> tuple[list[float], str, bool]:
    """
    Return the ordered stake steps to try on this session for this market.

    Returns (steps, reason, is_list_mode):
      - steps: list of stake values to try, in order. Empty if cap is 0
        ("do not bet") or no usable stake can be derived.
      - reason: short label for logging, same conventions as resolve_max_stake
      - is_list_mode: True if the yaml cap was a list (liability ladder
        per Wilson's 2026-05-17 AFL design). Caller uses this to suppress
        MBL violation alerts that would otherwise fire on every expected
        rejection in a graceful-degradation ladder.

    Behaviour:
      - Scalar/unlimited/no-cap: returns default_ladder_fn(max_stake) i.e.
        the existing percentage-based stake ladder. Preserves NBA + AFL
        non-disposals + racing behaviour exactly.
      - List cap [L1, L2, L3]: computes max_stake at each liability value
        against the live odds, capped by intended_stake, filtered by
        STAKE_FLOOR. Returns the list of derived stakes in order.
        Designed for AFL player_disposals (Adam 300,250,200 / others
        100,80,75) where Wilson wants three explicit liability tries
        before skipping the session rather than the 8-rung NBA ladder.

    default_ladder_fn is a callable (max_stake: float) -> list[float],
    passed in to avoid importing main from this module. Caller passes
    _v4_ladder_steps from main.py.
    """
    cap = lookup_liability_cap(session_id, sport, market)

    # List mode: AFL liability ladder. Use each liability value as a
    # separate target — no percentage steps between them.
    if isinstance(cap, tuple):
        steps: list[float] = []
        if not live_odds or live_odds <= 1.0:
            # No odds -> can't convert liability to stake. The original code
            # used cap[0] directly as a raw single stake (H48: over-stakes at
            # long odds). The H48 fix then REFUSED entirely — but that blocked
            # legitimate threshold bets whose market isn't priced pre-place
            # (AFL player_disposals_threshold returns no price-check odds;
            # Clayton Oliver 23+ disposals went $0/$1 unfilled 2026-05-30).
            # Correct middle ground: fall back to the normal percentage ladder,
            # but SEEDED at the top liability value as a stake CEILING so a
            # blind placement can't exceed the configured cap magnitude. The
            # bookie's MBL + the stake ladder + AUTO-CAP detection are the
            # backstops; precise liability sizing resumes the moment we have
            # odds. NOTE: liability is NOT precisely enforced on this no-odds
            # path (would need odds) — see _readme / Wilson decision.
            ceiling = min(intended_stake, float(cap[0]))
            steps = default_ladder_fn(ceiling)
            log.warning(
                f"resolve_stake_steps: list cap for {session_id} {sport}.{market} "
                f"but no usable odds — sizing blind via default ladder seeded at "
                f"${ceiling:.2f} (min of intended ${intended_stake:.2f}, top "
                f"liability ${cap[0]:.0f}); liability not enforced without odds"
            )
            return (steps, f"list cap (no-odds, ceiling=${ceiling:.0f})", True)

        for liability in cap:
            if liability <= 0:
                continue
            max_stake_at_liab = liability_to_max_stake(liability, live_odds)
            # Cap against remaining intended stake. If remaining is small
            # we still try this step (the bookie just sees a smaller bet);
            # the caller's outer loop handles "all placed" termination.
            step = min(max_stake_at_liab, intended_stake)
            if step <= 0:
                continue
            steps.append(round(step, 2))
        # De-dupe consecutive identical steps (e.g. when intended_stake
        # caps two consecutive liability values to the same stake).
        deduped: list[float] = []
        for s in steps:
            if not deduped or deduped[-1] != s:
                deduped.append(s)
        reason = f"list cap {list(cap)} (capped by remaining=${intended_stake:.2f})"
        return (deduped, reason, True)

    # Scalar / unlimited / no-cap: fall through to existing
    # resolve_max_stake + default ladder. Preserves all current
    # NBA/AFL-non-disposals/racing behaviour byte-identically.
    max_stake, reason = resolve_max_stake(
        session_id, sport, market, live_odds, intended_stake,
    )
    if max_stake <= 0:
        return ([], reason, False)
    steps = default_ladder_fn(max_stake)
    return (steps, reason, False)




def log_startup_summary() -> None:
    """
    Emit a one-shot INFO summary of loaded session metadata + priority
    config. Called from main.py at startup right after both loaders.
    """
    if not _session_meta:
        log.warning("session_priority: no sessions loaded from yaml")
    else:
        log.info(
            f"session_priority: {len(_session_meta)} session(s) in yaml"
        )
        for sid, meta in _session_meta.items():
            sports = sorted(meta.liability.keys())
            boost_flag = " [boost]" if meta.boost_eligible else ""
            log.info(
                f"  {sid} {meta.name} ({meta.bookmaker}){boost_flag} "
                f"sports={sports}"
            )

    cfg = _priority_config
    log.info("Priority lists from .env:")
    log.info(f"  NBA singles : {cfg.nba_singles or '(empty)'}")
    log.info(f"  NBA SGM     : {cfg.nba_sgm or '(empty)'}")
    log.info(f"  AFL singles : {cfg.afl_singles or '(empty)'}")
    log.info(f"  AFL SGM     : {cfg.afl_sgm or '(empty)'}")
    log.info(f"  MLB singles : {cfg.mlb_singles or '(empty -> manual)'}")
    log.info(f"  MLB SGM     : {cfg.mlb_sgm or '(empty)'}")
    log.info(f"  Racing      : {cfg.racing or '(empty)'}")

    # MLB design (2026-06-01): only the HRRBI 2-leg SGM auto-places; MLB
    # singles route to manual. MLB_SESSION_PRIORITY is meant to stay EMPTY.
    # If it's populated, MLB singles would auto-place (still flat-staked) —
    # warn loudly rather than crash (Wilson may enable it deliberately later).
    if cfg.mlb_singles:
        log.warning(
            f"MLB_SESSION_PRIORITY is NON-EMPTY ({cfg.mlb_singles}) — MLB "
            f"SINGLES will AUTO-PLACE. By design only MLB SGMs (the HRRBI "
            f"play) auto-place; leave MLB_SESSION_PRIORITY empty unless you "
            f"intend MLB singles to auto-place."
        )

    # Cross-validate: any session referenced in a priority list that's
    # missing from sessions.yaml is a likely typo. Warn loudly so Wilson
    # spots it before live tips arrive.
    all_priority_ids = set(
        cfg.nba_singles + cfg.nba_sgm + cfg.afl_singles
        + cfg.afl_sgm + cfg.mlb_singles + cfg.mlb_sgm + cfg.racing
    )
    missing = sorted(all_priority_ids - set(_session_meta.keys()))
    if missing:
        log.warning(
            f"Priority lists reference session IDs not in sessions.yaml: "
            f"{missing}. These sessions will be skipped (no liability info)."
        )

    # Also: any yaml session NOT in any priority list is unreachable.
    # Useful diagnostic but not necessarily a bug — Wilson may have
    # disabled a session by removing it from priority lists.
    unreached = sorted(set(_session_meta.keys()) - all_priority_ids)
    if unreached:
        log.info(
            f"sessions.yaml entries with no priority assignment "
            f"(unreachable for auto-placement): {unreached}"
        )
