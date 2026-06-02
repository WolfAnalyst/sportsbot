"""
Stat-level fallback for tipsters that don't supply explicit alt props.

Background: when Kev or AusBets posts e.g. "Cain o13.5pra" and the bookie
doesn't offer the PRA market for that player on the day, TipBot's only
prior recovery path was the ±1 line search on the SAME stat. If the
bookie doesn't carry the stat at all (PRA on Cain, Points on Walter),
±1 doesn't help.

This module loads a per-sport YAML config of stat fallback chains and
exposes a helper that the v4 placement loop calls when the primary stat
has been exhausted. The helper bulk-price-checks the player across all
priority sessions, walks the fallback chain in order, and returns the
first acceptable (stat, line, bookie) triple within the odds tolerance.

Tipster filter: only tipsters listed in `enabled_tipsters` in the config
are eligible. Shook is excluded because its tips already include an
explicit alt_line; double-falling-back would override that.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("tipbot.stat_fallback")

# Match the auto-alt odds tolerance used elsewhere in main.py. Kept loose
# so the fallback fires on real moves (e.g. PRA 1.89 -> Points 1.85) but
# still rejects garbage odds.
_DEFAULT_ODDS_TOL = 0.10

# How many lines either side of the tipped line to consider when picking
# the best candidate inside the fallback stat. The bookie may carry the
# fallback stat at a different line; we want to be reasonably permissive
# but not blindly accept any number.
_DEFAULT_LINE_RANGE = 5.0


@dataclass(frozen=True)
class StatFallbackConfig:
    """Loaded YAML, normalised to lookup-friendly dicts."""
    # sport -> list of tipster ids
    enabled_tipsters: dict[str, list[str]]
    # sport -> stat -> ordered list of fallback stats
    chains: dict[str, dict[str, list[str]]]

    def is_enabled(self, sport: str, tipster: str) -> bool:
        sport_l = (sport or "").lower()
        tipster_l = (tipster or "").lower()
        listed = [t.lower() for t in self.enabled_tipsters.get(sport_l, [])]
        return tipster_l in listed

    def chain_for(self, sport: str, stat: str) -> list[str]:
        sport_l = (sport or "").lower()
        stat_l = (stat or "").lower()
        return list(self.chains.get(sport_l, {}).get(stat_l, []))


def load_stat_fallback_config(
    path: str | Path = "stat_fallbacks.yaml",
) -> StatFallbackConfig:
    """
    Load the YAML config. Missing file is non-fatal: returns an empty
    config so the rest of TipBot continues running with no fallback.
    """
    p = Path(path)
    if not p.exists():
        log.info(f"stat_fallbacks.yaml not found at {p}, fallback disabled")
        return StatFallbackConfig(enabled_tipsters={}, chains={})

    try:
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning(f"Failed to load {p}: {e}. Fallback disabled.")
        return StatFallbackConfig(enabled_tipsters={}, chains={})

    enabled: dict[str, list[str]] = {}
    chains: dict[str, dict[str, list[str]]] = {}
    for sport, sport_cfg in (raw or {}).items():
        if not isinstance(sport_cfg, dict):
            continue
        sport_l = sport.lower()
        enabled[sport_l] = list(sport_cfg.get("enabled_tipsters") or [])
        chain_map = sport_cfg.get("chains") or {}
        normalised_chain: dict[str, list[str]] = {}
        if isinstance(chain_map, dict):
            for stat, fallbacks in chain_map.items():
                if isinstance(fallbacks, list):
                    normalised_chain[stat.lower()] = [
                        f.lower() for f in fallbacks if isinstance(f, str)
                    ]
        chains[sport_l] = normalised_chain

    log.info(
        f"Loaded stat_fallbacks.yaml: "
        f"{sum(len(v) for v in enabled.values())} tipster mapping(s), "
        f"{sum(len(v) for v in chains.values())} chain(s)"
    )
    return StatFallbackConfig(enabled_tipsters=enabled, chains=chains)


@dataclass(frozen=True)
class FallbackCandidate:
    """A viable fallback (stat, line, direction, odds) for placement."""
    stat: str
    line: float
    selection: str  # "over" or "under"
    odds: float
    bookie: str
    session_id: str


def find_fallback_candidates(
    bulk_price_response: dict,
    sport: str,
    sport_to_market_map: dict[str, str],
    chain: list[str],
    player: str,
    direction: str,
    tipped_line: float,
    tipped_odds: float,
    odds_tol: float = _DEFAULT_ODDS_TOL,
    line_range: float = _DEFAULT_LINE_RANGE,
) -> list[FallbackCandidate]:
    """
    Scan a bulk /api/price_check response for fallback selections matching
    the player and direction. For each fallback stat in `chain`, find the
    closest available line within `line_range` of `tipped_line` whose odds
    fall inside the tolerance band around `tipped_odds`.

    Returns candidates ordered by chain priority (earlier stats first),
    then by closeness to the tipped line. Caller picks the head and
    places. Returns [] when nothing in the chain is acceptable.

    `sport_to_market_map` maps stat -> HyperBot market name (e.g.
    {"points_rebounds": "player_pts_rebs"}). Provided by caller so we
    don't import main.py.
    """
    if not bulk_price_response or not chain or not player:
        return []
    if not tipped_odds or tipped_odds <= 1.0:
        # Without a tipped odds reference we can't enforce the band.
        # Skip rather than place blind.
        return []

    direction_l = direction.lower()
    player_l = player.lower()
    odds_lo = tipped_odds * (1.0 - odds_tol)
    odds_hi = tipped_odds * (1.0 + odds_tol)

    selections = bulk_price_response.get("selections") or []

    out: list[FallbackCandidate] = []
    for chain_idx, fallback_stat in enumerate(chain):
        target_market = sport_to_market_map.get(fallback_stat)
        if not target_market:
            log.debug(
                f"stat_fallback: no market mapping for stat '{fallback_stat}' "
                f"in {sport}, skipping"
            )
            continue

        # Filter to this player + this market + this direction
        matches = []
        for s in selections:
            sel_market = (s.get("market") or "").lower()
            sel_player = (s.get("player") or "").lower()
            sel_text = (s.get("selection") or "").lower()
            if sel_market != target_market:
                continue
            if sel_player != player_l:
                continue
            if direction_l not in sel_text:
                continue
            try:
                ln = float(s.get("line", 0))
                od = float(s.get("odds", 0))
            except (TypeError, ValueError):
                continue
            if abs(ln - tipped_line) > line_range:
                continue
            if not (odds_lo <= od <= odds_hi):
                continue
            matches.append({
                "stat": fallback_stat,
                "line": ln,
                "odds": od,
                "bookie": (s.get("bookie") or "").lower(),
                "session_id": str(s.get("session_id") or ""),
                "_dist": abs(ln - tipped_line),
                "_chain_idx": chain_idx,
            })

        # Within a single fallback stat, prefer closer line. Caller already
        # handles bookie ordering by priority.
        matches.sort(key=lambda m: (m["_dist"], -m["odds"]))
        for m in matches:
            out.append(FallbackCandidate(
                stat=m["stat"],
                line=m["line"],
                selection=direction_l,
                odds=m["odds"],
                bookie=m["bookie"],
                session_id=m["session_id"],
            ))

    if out:
        log.info(
            f"stat_fallback: {len(out)} candidate(s) found across chain "
            f"{chain} for {player} (tipped={tipped_line} @ {tipped_odds})"
        )
    else:
        log.info(
            f"stat_fallback: no candidates in chain {chain} for {player} "
            f"(tipped={tipped_line} @ {tipped_odds}, tol={odds_tol})"
        )
    return out
