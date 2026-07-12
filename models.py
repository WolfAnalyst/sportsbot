from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class ParsedLeg:
    """A single leg of a tip (player prop, H2H, total, spread, etc.)."""
    market: str              # "player_prop", "h2h", "total", "line"
    team_abbr: str = ""
    team_full: str = ""
    player: str = ""
    stat: str = ""           # HyperBot stat code e.g. "disposals", "points"
    line: float = 0.0
    selection: str = ""      # "over"/"under" for props, team name for H2H
    raw_text: str = ""


@dataclass
class ParsedTip:
    """A fully parsed tip ready for event resolution and bet placement."""
    tipster: str
    sport: str               # "afl", "nba", "nbl"
    is_sgm: bool
    legs: list
    units: float
    unit_size: float
    raw_message: str = ""
    timestamp: Optional[datetime] = None
    event: Optional[str] = None
    telegram_msg_id: Optional[int] = None
    is_live: bool = False    # LIVE bets -> alert only
    alert_only: bool = False # True if tip can't be automated
    alert_reason: str = ""   # Why this tip is alert-only
    suggested_bookie: str = ""  # Bookie from the tip text
    suggested_odds: float = 0.0  # Odds from the tip text
    is_pyo_sgm: bool = False  # Pick-your-own-line SGM
    alt_line: Optional[dict] = None  # Fallback alt line if primary fails (legacy single-alt)
    alt_lines: Optional[list] = None  # Ordered list of alt props for spillover fill
                                       # Each dict: {stat, line, selection, market, is_threshold}
    units_explicit: bool = True  # False when the tipster gave NO unit/stake and
                                  # we defaulted it. Some tipsters (aus/kev) must
                                  # carry an explicit unit to count as a bet —
                                  # see UNITS_REQUIRED_TIPSTERS gate. Default True
                                  # so every other parser/path is unaffected.

    @property
    def stake_dollars(self) -> float:
        return round(self.units * self.unit_size, 2)

    @property
    def primary_team(self) -> str:
        if self.legs:
            return self.legs[0].team_full
        return ""

    @property
    def all_teams(self) -> set:
        return {leg.team_full for leg in self.legs if leg.team_full}


@dataclass
class BetResult:
    """Result of a bet placement attempt."""
    success: bool
    tip: ParsedTip
    session_id: Optional[str] = None
    bookie: Optional[str] = None
    bet_id: Optional[str] = None
    odds: Optional[float] = None
    stake: Optional[float] = None
    error: Optional[str] = None
    timestamp: Optional[datetime] = None
    # Snapshot of the prop(s) placed at the moment this result was recorded.
    # Needed because when alt_lines spillover modifies the tip's leg in place,
    # later inspection of tip.legs doesn't tell us what each placement was.
    # Format: human-readable string like "Jaylen Brown OVER 25.5 points" or
    # for SGMs "Tatum OVER 23.5 pts / Brown OVER 25.5 pts".
    placed_leg_summary: Optional[str] = None
    used_boost: bool = False  # True if this placement used a boost token
    # Exact values sent to HyperBot for the primary leg. Notifier renders these
    # in preference to tip.legs[0] so Telegram shows the line that ACTUALLY
    # placed (e.g. after auto-line-tolerance adjustment 21.5 -> 22.5, or after
    # handicap sign-flip retry 11.0 -> -11.0). Populated by _execute_bet on
    # both success and failure. Singles only; SGMs use placed_leg_summary.
    placed_market: Optional[str] = None
    placed_player: Optional[str] = None
    placed_stat: Optional[str] = None
    placed_line: Optional[float] = None
    placed_selection: Optional[str] = None
    # Internal flag set on intermediate ladder-step failures so the outer
    # warn-on-failure loop can suppress noise. Set to True for failed ladder
    # steps that were followed by another step (success or final fail) on
    # the same session. Sicily AFL 2026-04-30 produced 11 spurious warnings
    # before this filter; with it, only the final outcome surfaces.
    is_intermediate: bool = False
    # Set True when a placement was flagged AMBIGUOUS_OUTCOME (slow rejection
    # that may have actually landed at the bookie). The stake is debited from
    # remaining "as placed" and the bookie blocklisted, but success stays False
    # because we don't KNOW it landed. Excluded from both placed_results (not a
    # confirmed placement) and failed_results (must NOT prompt manual re-placement
    # of a bet that may already exist). Accounted for separately in unfilled via
    # the ambiguous_outcomes list. 2026-05-30.
    is_ambiguous: bool = False
    # v5.9x (2026-07-12): bookie-stated allowable max stake from a stake-too-high
    # (538) reject — Sportsbet surfaces it (HB v1.7.85, e.g. "max=$86.20" +
    # top-level max_stake). Read by the max-stake rebet (place at exactly this
    # instead of laddering down). None when the bookie didn't provide it.
    max_stake: Optional[float] = None
    # HyperBot v3 correlation_id for the placement attempt. Populated on
    # ambiguous/timeout outcomes so the critical alert and any later
    # /v3/transactions reconciliation can tie back to the server-side request.
    # 2026-05-30.
    correlation_id: Optional[str] = None
    # Bookie-side elapsed time for this single placement, in seconds.
    # Captured around the hb.place_single_sports_bet / hb.place_sgm_bet
    # call inside _execute_bet (singles) and the SGM placement loop. Lets
    # notify_tip_placed_summary show "(N.Ns)" per account so slow bookies
    # are visible at a glance — same pattern as the racing tip alerts.
    # 2026-05-03 added: AFL/NBA alerts previously had no timing info.
    elapsed_sec: Optional[float] = None

    def summary(self) -> str:
        if self.success:
            parts = []
            for l in self.tip.legs:
                if l.market in ("h2h", "head_to_head"):
                    parts.append(f"{l.selection} Win")
                elif l.market in ("total", "line"):
                    parts.append(f"{l.selection} {l.line}")
                else:
                    parts.append(f"{l.player} {l.selection} {l.line} {l.stat}")
            return (
                f"BET PLACED on {self.bookie}\n"
                f"{' / '.join(parts)}\n"
                f"${self.stake:.2f} @ {self.odds}\n"
                f"Bet ID: {self.bet_id}"
            )
        return f"BET FAILED: {self.error}"
