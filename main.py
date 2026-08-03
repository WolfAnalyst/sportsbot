"""
TipBot - Automated tip-to-bet pipeline.

Listens to Telegram tipster channels, parses tips via Groq LLM
(with regex fallback), resolves events, and places bets via
HyperBot API with multi-account spillover.

Usage:
    python main.py
"""

import asyncio
import copy
import html as _html_mod
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, date as _date, timedelta as _timedelta, timezone as _dt_timezone
from pathlib import Path

from telethon import TelegramClient, events

from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE,
    TIPSTER_CHANNELS, MAX_UNITS, SESSIONS_YAML_PATH,
    USE_LEGACY_PLACEMENT, BOOKIE_AFL_ALIASES, TIPBOT_VERSION,
    AFL_CONCURRENT_FANOUT, AFL_FANOUT_MIN_STAKE, AFL_FANOUT_WEIGHTED,
    AFL_FANOUT_RATIO_CAP, EDDIE_FANOUT_BIG_UNITS, EDDIE_BIG_LIMITED_STAKE,
    EDDIE_FANOUT_DECAY, AFL_FANOUT_PREPLACEMENT_RETRY, AFL_FANOUT_RETRY_DELAY_SEC,
    AFL_DISPOSALS_REDISTRIBUTE, AFL_REDISTRIBUTE_MAX_PASSES,
    SGM_CONCURRENT_FANOUT,
    ETR_NBA_CONCURRENT_FANOUT, ETR_NBA_SESSION_IDS, ETR_NBA_FIXED_LADDER,
    ETR_NBA_TEST_MODE, ETR_NBA_UNIT_SIZE_TEST,
    RECONCILE_AMBIGUOUS, RECONCILE_SPILL,
    X_TIPSTER, X_FORCE_BOOKIE, X_MAX_ODDS_MULT, MAX_ODDS_MULT,
    AUTO_MANUAL_HANDICAP_SGM, MLB_STAT_MAP, MLB_FLAT_STAKE, MLB_HRRBI_LADDER_PCT,
    IMAGE_TIPS_TEST_MODE, IMAGE_TIPS_TEST_UNIT_SIZE, IMAGE_TIP_PARSERS,
    IMAGE_RACING_MAX_UNITS, IMAGE_RACING_TEST_MODE,
    SAIYAN_SGM_UNIT_SIZE,
    DELLO_UNIT_SIZE, DELLO_TEST_MODE, DELLO_TEST_UNIT_SIZE,
    DELLO_BAND_LO, DELLO_BAND_HI, DELLO_SB_WORSE_GATE,
    IMAGE_TIP_CONCURRENT, IMAGE_TIP_MAX_CONCURRENCY,
    IMAGE_RACING_CONCURRENT, IMAGE_RACING_MAX_CONCURRENCY,
    REGEX_FIRST_TIPSTERS,
    TELETHON_WATCHDOG_ENABLED, TELETHON_WATCHDOG_INTERVAL_SEC,
    TELETHON_WATCHDOG_PROBE_TIMEOUT_SEC, TELETHON_WATCHDOG_FAIL_THRESHOLD,
    LOOP_FREEZE_WATCHDOG_ENABLED, LOOP_FREEZE_TICK_SEC,
    LOOP_FREEZE_CHECK_SEC, LOOP_FREEZE_MAX_SILENCE_SEC,
    WORK_STALL_MAX_SILENCE_SEC,
    PLACEMENT_OFFLOAD_ENABLED, PLACEMENT_EXECUTOR_WORKERS,
    STARTUP_PENDING_RECONCILE_ENABLED, STARTUP_PENDING_RECONCILE_LOOKBACK_SEC,
    STARTUP_DEAD_SESSION_ALERT, STARTUP_DEAD_SESSION_GRACE_SEC, STARTUP_DEAD_SESSION_MAX,
    LEROY_UNIT_SIZE, LEROY_MAX_UNITS, LEROY_BETFAIR_SESSION, LEROY_ENABLED,
    SPORTSBET_MAX_STAKE_REBET,
    HB_SEND_MAX_ODDS, HB_SEND_DIRECTION,
    SELF_BET_MAX_STAKE,
    EDDIE_CAPTION_FALLBACK_ENABLED,
    EDDIE_TEXT_PLACE_ENABLED,
    SA_THOROUGHBRED_TRACKS,
    AFL_PERIOD_MARKETS_ENABLED,
    SAIYAN_HC_SGM_ENABLED,
    SESSION_BLACKOUT_ALERTS_QUIET,
    SESSION_BLACKOUT_START_HOUR,
    SESSION_BLACKOUT_END_HOUR,
    # v6.08 DisposalsModel (AFL player-disposals UNDER machine feed)
    DISPOSALS_MODEL_ENABLED,
    DISPOSALS_MODEL_MAX_STAKE,
    DISPOSALS_MODEL_MAX_SELECTION_STAKE,
    DISPOSALS_MODEL_TEST_MODE,
    DISPOSALS_MODEL_TEST_STAKE,
    DISPOSALS_MODEL_MIN_ODDS,
    DISPOSALS_MODEL_WORSE_GATE,
    DISPOSALS_MODEL_START_SLACK_SEC,
    DISPOSALS_MODEL_MAX_PLAYERS_PER_EVENT,
    DISPOSALS_MODEL_RETRY_ENABLED,
    DISPOSALS_MODEL_QUIET_START_HOUR,
    DISPOSALS_MODEL_QUIET_END_HOUR,
    DISPOSALS_MODEL_STATE_PATH,
    DISPOSALS_MODEL_FILLS_PATH,
    DISPOSALS_MODEL_RECONCILE_ENABLED,
    DISPOSALS_MODEL_RECONCILE_MODE,
    # v6.08e NOOP liveness detector
    DISPOSALS_MODEL_LIVENESS_ENABLED,
    DISPOSALS_MODEL_LIVENESS_PATH,
    DISPOSALS_MODEL_SILENCE_HOURS,
    DISPOSALS_MODEL_LIVENESS_POLL_SEC,
    DISPOSALS_MODEL_ACTIVE_START_DAY,
    DISPOSALS_MODEL_ACTIVE_START_HOUR,
    DISPOSALS_MODEL_ACTIVE_END_DAY,
    DISPOSALS_MODEL_ACTIVE_END_HOUR,
    DISPOSALS_MODEL_SEQ_GAP_ALERT,
    DISPOSALS_MODEL_SEQ_RESET_TOLERANCE,
    DISPOSALS_MODEL_LIVENESS_CRITICAL,
)
from groq_parser import parse_tip_image
from models import ParsedTip, ParsedLeg, BetResult
from parsers.saiyan_afl import parse_saiyan_message
try:
    # NBA tipster regex parsers. The sports-only sportsbot fork (Saiyan +
    # Shook only) ships NEITHER module, so tolerate their absence: the names
    # stay None and their REGEX_PARSERS entries are skipped below. No effect on
    # tipbot, where both modules are always present. (Shook is Groq-only.)
    from parsers.ausbets_nba import parse_ausbets_message
    from parsers.kev_nba import parse_kev_message
except ModuleNotFoundError:
    parse_ausbets_message = None
    parse_kev_message = None
try:
    # v5.93 Leroy Betfair-BSP racing tipster — tipbot-only (the fork lacks it).
    from parsers.leroy import parse_leroy_message
except ModuleNotFoundError:
    parse_leroy_message = None
try:
    # v6.08 DisposalsModel machine feed — tipbot-only (the fork lacks it, and its
    # SPORTSBOT_MODE filter drops the channel anyway). Tolerated absence keeps the
    # fork booting; the name staying None makes the handler branch unreachable.
    # NOTE the except clause: `from parsers import <name>` raises a PLAIN ImportError
    # ("cannot import name ..."), NOT ModuleNotFoundError, when the submodule is
    # absent — ModuleNotFoundError is a SUBCLASS, so catching only it would let the
    # error through and crash the fork at import. Verified, not assumed.
    from parsers import disposals_model as disposals_model_parser
except ImportError:
    disposals_model_parser = None
from groq_parser import parse_with_groq, _preprocess_saiyan_emojis
from hyperbot_client import HyperBotClient
from resolver import resolve_afl_event, afl_games_in_play, afl_games_on_date, team_key
from nba_resolver import resolve_nba_event, resolve_mlb_event
from roster import resolve_player_name, get_player_team, afl_surname_candidates, afl_fuzzy_surname_candidates
from roster import _team_matches as _roster_team_matches
import notifier
import session_priority
import stat_fallback
import tip_parser  # v5.80: per-call Claude recovery layer (fallback + resolvers)

# ── Logging ─────────────────────────────────────────────────────────

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# v6.07 (2026-07-29) ROOT CAUSE of the recurring "alive but SILENT" outages
# (07-06 ~9.5h, 07-08 ~6h, 07-29 14.5h): this StreamHandler wrote every log line to
# the launcher's CONSOLE. On Windows a console with QuickEdit Mode (default ON) BLOCKS
# ALL WRITES while text is selected, so one stray click in that window blocks every
# thread that logs - forever. Symptoms match exactly: process alive with near-zero
# CPU; the asyncio liveness ticker keeps stamping (it does not log) so the freeze
# watchdog correctly saw a healthy loop and never fired; telethon + the Tip Titans
# poller both stopped because both log; and NOTHING was recorded about it (no
# exception, no traceback) because logging itself was blocked.
# Fix: only attach a console handler when stdout is a REAL TTY. Under the scheduled
# task / tipbot.bat (which now redirects to logs\stdout.log) there is no TTY, so the
# bot writes to FILES only - a file write cannot be paused by a mouse selection.
# Set TIPBOT_FORCE_CONSOLE_LOG=1 to force it back on for interactive debugging.
# v6.07 (2026-07-31): the unit suite must NOT write into the PRODUCTION log. Importing
# main.py under pytest attached a FileHandler to logs/tipbot.log, so test runs injected
# their own lines (`chat=-100`, `net down (test)`, `403 Cloudflare`, `401 Unauthorized
# (simulated persistent outage)`) into the file used to diagnose real incidents. Today
# that put 1,299 fake "persistent outage" lines and thousands of fake auth errors in the
# middle of a genuine Tip Titans outage and actively slowed the diagnosis - the same class
# of contamination `_audit_log_path()` was written to stop for audit.jsonl. Mirror it here.
_LOG_FILE = LOG_DIR / "tipbot.log"
if os.getenv("TIPBOT_TESTING"):
    import tempfile as _tempfile
    _LOG_FILE = Path(_tempfile.gettempdir()) / "tipbot_test_run.log"
_log_handlers = [logging.FileHandler(_LOG_FILE, encoding="utf-8")]
try:
    _want_console = bool(os.getenv("TIPBOT_FORCE_CONSOLE_LOG")) or (
        sys.stdout is not None and sys.stdout.isatty()
    )
except Exception:
    _want_console = False
if _want_console:
    try:
        _log_handlers.insert(0, logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ))
    except Exception:
        pass  # no usable stdout (pythonw / detached) -> file logging only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("tipbot")

AUDIT_LOG = LOG_DIR / "audit.jsonl"
ERROR_LOG = LOG_DIR / "errors.jsonl"

# Dedicated AFL log file - verbose step-by-step for debugging the AFL
# pipeline (parser output, market translation, HyperBot payload, response).
# Mirrors the racing log pattern so AFL-specific issues (AGS market name,
# threshold mapping, team fuzzy match) can be diagnosed without sifting
# through the main tipbot.log. Writes only when sport=='afl'.
_afl_log = logging.getLogger("apiafl")
_afl_log.setLevel(logging.INFO)
_afl_log.propagate = False
if not _afl_log.handlers:
    _afl_handler = logging.FileHandler(LOG_DIR / "apiafl.log", encoding="utf-8")
    _afl_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    _afl_log.addHandler(_afl_handler)


def _afl_log_event(tip, msg: str, level: str = "info") -> None:
    """No-op unless tip.sport == 'afl'. Used to keep the AFL log focused."""
    try:
        if not tip or (getattr(tip, "sport", "") or "").lower() != "afl":
            return
    except Exception:
        return
    line = f"[{getattr(tip, 'tipster', '?')}] {msg}"
    if level == "warning":
        _afl_log.warning(line)
    elif level == "error":
        _afl_log.error(line)
    else:
        _afl_log.info(line)


# v5.91: serialise audit-file appends. The AFL image fan-out now places multiple
# tips CONCURRENTLY, and each place_tip writes tip_outcome rows via _log_jsonl;
# without a lock two worker threads' appends could interleave bytes in audit.jsonl.
# (bet_ledger.py already serialises its own CSV append with its own Lock.)
_AUDIT_LOCK = threading.Lock()

# v6.03 (incident 2026-07-17): DEDICATED bounded pool for offloaded placement
# (place_tip) off the asyncio loop. A dedicated pool (not the default None
# executor) gives a predictable thread ceiling AND isolates placement from the
# default pool that racing/Leroy/vision-parse use, so a burst of hung placements
# can't starve them. Each placement STILL fans out internally over its accounts
# in its own ThreadPoolExecutor; this pool only caps how many TIPS place at once.
# Gated by PLACEMENT_OFFLOAD_ENABLED; see _run_in_placement_executor. HARD
# PREREQUISITE: the v3 poll hard ceiling bounds each worker (else a hung
# placement pins a worker with no freeze-watchdog recovery).
import concurrent.futures as _cf
_PLACEMENT_EXECUTOR = _cf.ThreadPoolExecutor(
    max_workers=max(1, PLACEMENT_EXECUTOR_WORKERS), thread_name_prefix="place"
)


async def _run_in_placement_executor(fn, *args):
    """Await a synchronous placement fn on the dedicated placement pool so it does
    NOT block the event loop. Mirrors the racing/Leroy run_in_executor offload."""
    return await asyncio.get_running_loop().run_in_executor(_PLACEMENT_EXECUTOR, fn, *args)


def _log_jsonl(path: Path, entry: dict):
    """Append one audit/error record. NEVER raises.

    v6.07 (sweep #15): this used to be bare, and it is the only file I/O on the
    text-tip router's per-tip path (`_audit_tip`, called at the top of the loop
    OUTSIDE any try). A locked `logs/audit.jsonl` (a rotation or the daily-review job
    holding it -> PermissionError WinError 32) or a full disk therefore escaped
    `_process_tip` -- which every caller launches fire-and-forget via
    `asyncio.create_task` with no exception handler -- so the remaining tips in a
    multi-tip message were never routed, never placed and never alerted, leaving only
    asyncio's own "Task exception was never retrieved" in the log. It is also called
    from inside the placement `except` handler, where a raise would mask the real
    error. Audit logging must never be able to cost a bet: log the failure and carry
    on. All 15 call sites are fire-and-forget; none depends on this raising."""
    try:
        entry["timestamp"] = datetime.now().isoformat()
        with _AUDIT_LOCK:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as e:
        try:
            log.warning(f"audit write to {getattr(path, 'name', path)} FAILED "
                        f"(tip processing continues): {e}")
        except Exception:
            pass


def _audit_log_path():
    """v5.76: resolve the audit.jsonl path at call-time so the unit suite never
    pollutes the production logs/audit.jsonl. The suite sets TIPBOT_TESTING;
    v5.43 redirected ONLY _audit_tip, but the placement-path `tip_outcome`
    writers still hard-coded AUDIT_LOG, leaking ~30 test rows into production on
    2026-06-18. Every audit write MUST go through this so the redirect is total."""
    if os.getenv("TIPBOT_TESTING"):
        import tempfile
        return os.path.join(tempfile.gettempdir(), "tipbot_test_audit.jsonl")
    return AUDIT_LOG


# ── Stake Search Ladder ─────────────────────────────────────────────
# Replaces binary search. Try fixed proportions of remaining stake until one
# succeeds, then move on. Stops when step value would drop below STAKE_FLOOR.
STAKE_LADDER = [1.00, 0.75, 0.50, 0.40, 0.25, 0.20, 0.15, 0.10, 0.05]
# Set to 0 so $1 test-channel bets actually get placed. Matches racing/Tip
# Titans floors. Raise once you want to enforce a real minimum stake.
STAKE_FLOOR = 0.0  # Don't bother with bets below this (currently no minimum)
# MINOR (Wilson 2026-06-21): the MLB HRRBI 3-way SGM fills the full $400 unit
# ($133.33 x3 = $399.99), leaving a $0.01 remainder; with STAKE_FLOOR=0.0 the
# spillover single fired a degenerate ~$0.01 bookie-minimum bet on Alex/Ryan
# (06-21 bets 6892885141 / 6892887454, recurring). The spillover single needs a
# real minimum so a rounding remainder doesn't place a token bet — the SGM
# already filled the unit. Env-overridable.
try:
    MLB_HRRBI_SINGLE_MIN_STAKE = float(os.getenv("MLB_HRRBI_SINGLE_MIN_STAKE", "1.0"))
except (TypeError, ValueError):
    MLB_HRRBI_SINGLE_MIN_STAKE = 1.0

# $1 deadband for deciding a sports bet "fully filled" (ignores cent-jitter),
# matching the AFL fan-out's $1 deadband.
MBL_FILLED_DEADBAND = 1.0


def _should_alert_mbl_violation(mbl_violations, unfilled_stake: float,
                                orchestrated: bool = False,
                                deadband: float = MBL_FILLED_DEADBAND) -> bool:
    """Whether to fire the CRITICAL 'MBL VIOLATION (sports)' alert.

    An MBL 'violation' is a stake REJECTED at-or-below our configured cap. But
    the SGM/singles ladder is DESIGNED to start at the cap-max and ladder DOWN on
    a `code 538 stake too high`, then spill — so a 538 on the top rung that then
    FILLS via lower rungs / spillover is the intended behaviour (cf. the racing
    circuit-breaker treating 'stake too high' as BENIGN), NOT a violation worth a
    CRITICAL ping. v5.34 (Wilson): only escalate when the bet did NOT fully fill
    — a GENUINE shortfall (the cap exceeded the live MBL AND we couldn't place
    the intended stake). A fully-filled bet (unfilled <= deadband) or the MLB
    per-account model (orchestrated, where leftover is expected and the unfilled
    alert is itself suppressed) does NOT alert. Fixes the saiyan SGM $600/$600
    false-positive CRITICALs (2026-06-07 09:03/09:05)."""
    if not mbl_violations:
        return False
    if orchestrated:
        return False
    return unfilled_stake > deadband

# ── Tipsters whose suggested_bookie field should be ignored ─────────
# Some tipsters quote odds at bookmakers we can't actually place at, e.g.
# Kev posts "with 365" for NBA but bet365 sports placement on HyperBot
# is broken. AusBets posts "(SB: $1.85)" but we want price-shop discretion
# rather than forced sportsbet routing. For these tipsters we drop the
# suggested_bookie filter entirely and let the sport_filter + priority
# list decide where to place.
#
# 2026-05-01 Kev "Dyson Daniels o20.5pra @ 1.8 with 365" routed to manual
# because suggested_bookie=bet365 narrowed sessions to 53523 only, and
# 53523 is not in NBA_SESSION_PRIORITY (since bet365 sports is broken).
# Adding kev_nba and ausbets_nba here makes both flow through to the
# sportsbet sessions in NBA_SESSION_PRIORITY.
TIPSTERS_IGNORE_SUGGESTED_BOOKIE: set[str] = {"kev_nba", "ausbets_nba", "saiyan_afl", "etr_nba"}

# Tipsters whose tips MUST carry an explicit unit/stake to be placed. A tip with
# no unit (we'd otherwise default it) is NOT a confirmed bet for these cappers —
# route it to manual instead of placing at the default stake (Wilson 2026-06-04:
# AusBets "Knicks 5.5" had no unit yet attempted a $400 line bet).
UNITS_REQUIRED_TIPSTERS: set[str] = {"kev_nba", "ausbets_nba", "dello_afl", "eddie_afl"}

# v5.52 BELT for the gate above: Groq can INVENT a unit value (2026-06-11
# AusBets "nothin today ... none quite get to my price threshold" parsed as
# 4 tips WITH units, units_explicit=True, and Spurs ML PLACED $400). So for
# UNITS_REQUIRED_TIPSTERS the RAW message must ALSO contain a literal unit
# token before units_explicit is trusted. Real formats covered (audit.jsonl,
# 209/222 distinct aus+kev messages carry one; the 13 without are exactly the
# no-bet/commentary/bare messages that SHOULD gate):
#   AusBets: "1U - Miami +6 (SB: $1.9)", "1.5U - Golden State +5.5"
#   Kev:     "... with SB - 1 unit", "- 1.25 units", "- 0.2 units"
# Deliberately does NOT match "2+ threes", "u226.5", "u12.5pra", "under",
# "$1.9", "@ 1.8". [ \t]* (not \s*) so a digit at end-of-line can't pair with
# a 'u' word on the next line; after the 'u' only a word boundary or "nit(s)"
# may follow, so "5 under" / "1 Utah" don't match.
_UNIT_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?[ \t]*u(?:nits?)?\b", re.IGNORECASE)

# v5.52 BRACES (ausbets_nba only): explicit no-bet framing. AusBets posts
# near-miss lines under a "these are NOT bets" header and Groq still parses
# them as tips. Conservative phrases only — validated against all 114 distinct
# ausbets_nba messages in audit.jsonl (exactly 1 hit: the 2026-06-11 incident;
# "That's it for today" after a real bet does NOT trip it).
_NO_BET_FRAMING_RE = re.compile(
    r"nothin'?g?\s+today"                        # "nothin today" / "nothing today"
    # v5.69 i4 + r2 (#17): match a GENERAL no-bet phrase only. The i4 {0,3}-word
    # filler also matched "no bets ON the dogs today, but <real bet> 2u" and
    # suppressed the valid bet (the gate runs on the whole raw_message). Restrict
    # to "no bets today" or a "for"-led form — never an "on <category>" exclusion
    # that leaves room for other bets.
    r"|no\s+bets?\s+today\b"                     # "no bets today"
    r"|no\s+bets?\s+for\s+(?:the\s+|me\s+|us\s+)?(?:today|day)\b"  # "no bets for today/the day/me today"
    r"|none\s+quite"                             # "none quite get to my price threshold"
    r"|close\s+to\s+(?:a\s+)?bets?\s+but\s+no"   # "close to bets but none ..."
    # Dello (2026-07-12): his no-bet / lean vocabulary. A no-bet cue suppresses
    # the WHOLE message even when a full in-band SB selection is present ("I love
    # Salem 25+. Not tipping it ... but I'd have 2U on the o24.5 on SB"). HIGH-
    # PRECISION phrases only (whole-message suppressor -> one false match kills a
    # real bet in the same message).
    r"|not\s+tipping"                            # "not tipping it"
    r"|won[’']?t\s+tip"                     # "won't tip" (straight or curly ')
    r"|won[’']?t\s+track|not\s+tracking"    # "won't track" / "not tracking"
    r"|just\s+(?:a\s+)?leans?\b"                 # "just leans" / "just a lean"
    r"|worth\s+a\s+mention"                      # "worth a mention"
    r"|if\s+you\s+wanted\s+a(?:\s+little)?\s+dabble"  # "if you wanted a (little) dabble"
    r"|chuck\b.*\buntracked",                    # "chuck it in untracked"
    re.IGNORECASE,
)


def _raw_has_unit_token(raw: str) -> bool:
    """True if the raw tipster message contains a literal unit token
    ("1U", "1.5U", "1 unit", "1.25 units"). Belt for UNITS_REQUIRED_TIPSTERS:
    units_explicit from the LLM is only trusted when this is also True."""
    return bool(_UNIT_TOKEN_RE.search(raw or ""))


def _is_no_bet_framing(raw: str) -> bool:
    """True if the raw message explicitly frames its lines as NOT bets
    ("nothin today", "no bets today", "none quite", "close to bets but no")."""
    return bool(_NO_BET_FRAMING_RE.search(raw or ""))

# ── Tipsters HARD-LOCKED to a single bookie ─────────────────────────
# Opposite of the ignore-list: these tips are placed ONLY on the named bookie.
# If that bookie has no active session, the tip routes to manual — it is NEVER
# placed on a different bookie (unlike the soft suggested_bookie filter, which
# falls back to all bookies). EasyMoneyAFL (via X) = sportsbet per Wilson
# (2026-05-31). Driven by config X_TIPSTER + X_FORCE_BOOKIE so it stays in sync.
TIPSTERS_FORCE_BOOKIE: dict[str, str] = {}
if X_TIPSTER and X_FORCE_BOOKIE:
    TIPSTERS_FORCE_BOOKIE[X_TIPSTER] = X_FORCE_BOOKIE
# Eddie's Bets AFL (image-tip channel) places on Sportsbet ONLY — nothing
# else is available for sports (Wilson 2026-06-03). If no Sportsbet session,
# the tip routes to manual (never placed on another bookie).
TIPSTERS_FORCE_BOOKIE["eddie_afl"] = "sportsbet"
# ETR NBA (2026-06-07): sportsbet-only (the blind fan-out targets the 4 sportsbet
# accounts). Guarantees ETR never places on tab/bet365 even if a session list drifts.
TIPSTERS_FORCE_BOOKIE["etr_nba"] = "sportsbet"
# Dello AFL (2026-07-12): Sportsbet-only. _place_dello_single price-checks + places
# on a single SB account; no SB session -> manual (never placed on another bookie).
TIPSTERS_FORCE_BOOKIE["dello_afl"] = "sportsbet"
# DisposalsModel (v6.08): Sportsbet-only, and this one is a STRATEGY constraint, not
# just an availability one — the measured +18.35% ROI was on SPORTSBET's posted lines.
# Another book's line at the same number is a different (untested) bet, so a missing
# SB session must route to manual rather than shop elsewhere.
DISPOSALS_MODEL_TIPSTER = "disposals_model"
TIPSTERS_FORCE_BOOKIE[DISPOSALS_MODEL_TIPSTER] = "sportsbet"

# ── Max-odds CEILING (wrong-selection sanity guard) ────────────────
# Applies to ALL sports tipsters via the global MAX_ODDS_MULT (default 1.25×).
# Per-tipster OVERRIDES go in TIPSTERS_MAX_ODDS_MULT (e.g. the X/EasyMoney
# tipster). If the live (catalog/price-check) odds for the resolved selection
# exceed tipped × mult, the bet routes to manual instead of placing — a
# too-good-to-be-true price almost always means a WRONG line/selection. Pairs
# with the global 0.9× floor (target_odds). Racing has its own ODDS_DRIFT_CEILING.
TIPSTERS_MAX_ODDS_MULT: dict[str, float] = {}
if X_TIPSTER and X_MAX_ODDS_MULT and X_MAX_ODDS_MULT > 1.0:
    TIPSTERS_MAX_ODDS_MULT[X_TIPSTER] = X_MAX_ODDS_MULT
# ETR NBA: IGNORE the quoted odds entirely (Wilson — "pure fast as possible, no
# price-check"). mult <= 1.0 disables the ceiling for the tipster. Belt-and-braces:
# the parser already emits odds=0 (which no-ops both guards) and the blind fan-out
# never captures a live price; this guarantees a leaked quoted price can't arm it.
TIPSTERS_MAX_ODDS_MULT["etr_nba"] = 0.0


def _afl_target_odds(sport, basis_odds, *, _round: bool = True):
    """Price FLOOR (minimum acceptable odds) sent to HyperBot, floored at 1.01.

    AFL widens the tolerance when the BASIS price is OVER $2.00; non-AFL and
    AFL <= $2.00 stay at the standard 10%.
      * v5.9x (Wilson 2026-07-12): AFL > $2.00 reduction is now 20% (mult 0.80),
        raised from the prior 15% (0.85). Himmelberg 20+ @ 2.25 (07-11) floored
        at 1.91 (15%) and rejected SB's live 1.87; at 20% it floors 1.80 and
        accepts. So tipped $2.25 -> 1.80, $2.16 -> 1.73, $2.01 -> 1.61,
        $3.00 -> 2.40. Boundary: exactly $2.00 stays 10% ($2.00 -> 1.80).
    This only LOWERS the floor (accepts shorter odds = lower liability) — never
    raises the bet's risk. Racing keeps its own ODDS_DRIFT floor (racing_placer),
    untouched.
    """
    try:
        b = float(basis_odds or 0)
    except (TypeError, ValueError):
        return None
    if b <= 1.0:
        return None
    mult = 0.80 if ((sport or "").lower() == "afl" and b > 2.00) else 0.90
    val = max(1.01, b * mult)
    return round(val, 2) if _round else val


def _exceeds_odds_ceiling(tipster: str, tipped_odds, matched_odds) -> bool:
    """True if `matched_odds` exceeds the max-odds ceiling (tipped × mult) — the
    wrong-selection sanity guard, applied to ALL sports tipsters. The multiplier
    is the per-tipster override (TIPSTERS_MAX_ODDS_MULT) else the global
    MAX_ODDS_MULT; a mult <= 1.0 disables it for that tipster. Requires a valid
    tipped + matched odds; returns False (no block) otherwise, so a missing
    price never blocks a bet."""
    mult = TIPSTERS_MAX_ODDS_MULT.get(tipster, MAX_ODDS_MULT)
    if not mult or mult <= 1.0:
        return False
    try:
        t = float(tipped_odds or 0)
        m = float(matched_odds or 0)
    except (TypeError, ValueError):
        return False
    if t <= 1.0 or m <= 0:
        return False
    return m > t * mult


def _bookie_max_odds(tipster: str, tipped_odds, target_odds=None):
    """v6.07 (HB API doc 1.7.10x): the SAME ceiling policy as
    _exceeds_odds_ceiling, expressed as the `max_odds` field HyperBot now accepts
    on /v3/place_bet — the bookie auto-rejects a fill priced ABOVE it ("guards
    against wrong-line matching" per the docs).

    Why send it when we already check client-side: our check judges the CATALOG
    price we saw before placing, so it cannot catch drift between the price-check
    and the actual fill, nor a bookie matching a different rung at placement time.
    max_odds closes that window at the bookie. It can only ever PREVENT a bet, never
    place a larger or extra one.

    Returns None (field omitted, unchanged behaviour) when the tipster has no
    ceiling, the tipped odds are unusable, or the computed ceiling would be below
    target_odds (the API requires max_odds >= target_odds).
    Kill-switch: HB_SEND_MAX_ODDS.
    """
    if not HB_SEND_MAX_ODDS:
        return None
    mult = TIPSTERS_MAX_ODDS_MULT.get(tipster, MAX_ODDS_MULT)
    if not mult or mult <= 1.0:
        return None
    try:
        t = float(tipped_odds or 0)
    except (TypeError, ValueError):
        return None
    if t <= 1.0:
        return None
    ceiling = round(t * mult, 2)
    try:
        if target_odds and ceiling < float(target_odds):
            return None  # API requires max_odds >= target_odds
    except (TypeError, ValueError):
        pass
    return ceiling


# Min-odds FLOOR (price-moved / wrong-selection guard), symmetric to the
# ceiling. Live odds >10% BELOW the tip (matched < tipped × 0.9) -> manual.
_ODDS_FLOOR_PCT = 0.9


def _below_odds_floor(tipped_odds, matched_odds) -> bool:
    """True if `matched_odds` is more than 10% BELOW the tipped price
    (matched < tipped × _ODDS_FLOOR_PCT) — the price-moved / wrong-selection
    guard, all sports. Requires valid tipped + matched odds; returns False (no
    block) otherwise, so a missing price never blocks a bet."""
    try:
        t = float(tipped_odds or 0)
        m = float(matched_odds or 0)
    except (TypeError, ValueError):
        return False
    if t <= 1.0 or m <= 0:
        return False
    return m < t * _ODDS_FLOOR_PCT


# ── AFL period / exotic markets (quarter & half handicaps) ──────────────────
# v5.9x (Wilson 2026-07-12): place AFL quarter (1q-4q) + 1st-half TEAM handicaps
# via the bookie's EXACT proposition_id, resolved LIVE from price_check_sports.
# SB-only, gated by AFL_PERIOD_MARKETS_ENABLED (default OFF -> ships DORMANT).
# EXACT match ONLY (period + team + signed line) — NO fuzzy — so an unmatched
# niche market routes to MANUAL, never a wrong proposition_id (Wilson's explicit
# "don't fuck up the prop_id"). tri_bet / winning-margin bands are NOT wired here
# (more parse-ambiguous) -> they stay manual. HB shapes (probe 2026-07-11):
#   quarter_line:      {selection: team, period: '1q'..'4q', line, proposition_id, odds}
#   quarter_time_line: {selection: team, period: 'full_game'|'1h', line, proposition_id}

# Eddie/vision `period` strings -> Sportsbet period codes (supported set only).
_AFL_PERIOD_CODE_MAP = {
    "1q": "1q", "q1": "1q", "1st_quarter": "1q", "first_quarter": "1q", "1st quarter": "1q",
    "2q": "2q", "q2": "2q", "2nd_quarter": "2q", "second_quarter": "2q", "2nd quarter": "2q",
    "3q": "3q", "q3": "3q", "3rd_quarter": "3q", "third_quarter": "3q", "3rd quarter": "3q",
    "4q": "4q", "q4": "4q", "4th_quarter": "4q", "fourth_quarter": "4q", "4th quarter": "4q",
    "1h": "1h", "h1": "1h", "1st_half": "1h", "first_half": "1h", "1st half": "1h",
    # v6.00 (Wilson 2026-07-17): 2nd-half recognised. NOTE: Sportsbet offers NO 2h
    # LINE market (only first_half_total/second_half_total totals + quarter_time_line
    # full_game/1h) — so a 2h HANDICAP resolves to no market -> exact-or-manual =
    # manual; a 2h TOTAL maps to second_half_total (placeable). See _afl_period_*.
    "2h": "2h", "h2": "2h", "2nd_half": "2h", "second_half": "2h", "2nd half": "2h",
    # NOTE (07-12 review #2): a bare "half" is NOT mapped — it's ambiguous (could be
    # a shortened 2nd-half tip) and would place the WRONG half. Every unmapped /
    # ambiguous period -> "" -> manual (the no-guess invariant).
}

# v6.00 (Wilson 2026-07-17): NO-ODDS Eddie player-prop safety band. A lone no-odds
# prop auto-places EXACT-line-only (no ±1 snap) but carries NO tipster-odds price
# ceiling/floor, so the fan-out enforces this band off the LIVE catalog price:
#   - the live price must be ABOVE EDDIE_NOODDS_MIN_ODDS (else the whole tip -> manual;
#     no short-priced blind bets), and
#   - the total stake is capped so potential winnings (to-win) can't exceed
#     EDDIE_NOODDS_MAX_TOWIN. Odds-bearing tips keep the normal ceiling/floor.
EDDIE_NOODDS_MIN_ODDS = float(os.getenv("EDDIE_NOODDS_MIN_ODDS", "1.75"))
EDDIE_NOODDS_MAX_TOWIN = float(os.getenv("EDDIE_NOODDS_MAX_TOWIN", "1500"))


def _afl_team_word_match(team, cat_sel) -> bool:
    """WORD-BOUNDARY AFL team match (v6.00 B-MEDIUM: extracted from
    _match_handicap_in_catalog's nested _team_match so the PERIOD resolvers share
    the SAME fuzzy match the full-game handicap path uses — otherwise a nickname
    tip ('Sydney') never matched SB's full club name ('Sydney Swans') and every
    such quarter/1h handicap fell to manual). Accepts exact, or the shorter name
    being a whole leading/trailing WORD-run of the longer ('adelaide'<->'adelaide
    crows', 'gws'<->'gws giants', 'sydney'<->'sydney swans'), then REJECTS the 3
    cross-club collisions where a distinct club is a trailing word of a DIFFERENT
    club (Adelaide/Port Adelaide, Sydney/Greater Western Sydney,
    Melbourne/North Melbourne). Word-boundary, never a bare substring."""
    t = (team or "").lower().strip()
    c = (cat_sel or "").lower().strip()
    if not t or not c:
        return False
    if c == t:
        return True
    a, b = sorted((t, c), key=len)  # a = shorter, b = longer
    if not a or not (b.startswith(a + " ") or b.endswith(" " + a)):
        return False
    _cross_club = {("adelaide", "port adelaide"),
                   ("sydney", "greater western sydney"),
                   ("melbourne", "north melbourne")}
    return (a, b) not in _cross_club


def _afl_period_code(raw_period) -> str:
    """Map a vision/parser period string to a SUPPORTED SB period code
    (1q/2q/3q/4q/1h), or "" if unsupported (e.g. a 2nd-half line -> stays manual)."""
    p = str(raw_period or "").strip().lower().replace("-", "_")
    return _AFL_PERIOD_CODE_MAP.get(p, "")


def _afl_period_market_for(period_code: str) -> str:
    """The SB market that carries a given period handicap ("" if none)."""
    if period_code in ("1q", "2q", "3q", "4q"):
        return "quarter_line"
    if period_code == "1h":
        return "quarter_time_line"
    return ""


def _resolve_afl_period_prop(markets: dict, period_code: str, team: str, line):
    """EXACT-match an AFL period handicap to its live proposition_id.

    `markets` = price_check_sports(...)['markets']. Returns
    {proposition_id, odds, selection, market, period, line} on an EXACT
    (period + team + signed line) match, else None. NO fuzzy — a miss routes to
    manual, never a wrong niche bet. Team match is case-insensitive on the
    bookie's exact team spelling (a nickname that doesn't match -> None -> manual)."""
    mkt_key = _afl_period_market_for(period_code)
    if not mkt_key or not isinstance(markets, dict):
        return None
    m = markets.get(mkt_key)
    if not isinstance(m, dict):
        return None
    sels = m.get("selections") or m.get("outcomes") or []
    tnorm = (team or "").strip().lower()
    if not tnorm:
        return None
    try:
        target_line = float(line)
    except (TypeError, ValueError):
        return None
    for s in sels:
        if not isinstance(s, dict):
            continue
        if str(s.get("period") or "").strip().lower() != period_code:
            continue
        if not _afl_team_word_match(team, s.get("selection") or s.get("team")):
            continue
        s_line = s.get("line")
        if s_line is None:
            continue
        try:
            if abs(float(s_line) - target_line) > 1e-6:
                continue
        except (TypeError, ValueError):
            continue
        pid = s.get("proposition_id")
        if pid:
            return {
                "proposition_id": str(pid),
                "odds": s.get("odds") or s.get("price"),
                "selection": s.get("selection"),
                "market": mkt_key,
                "period": period_code,
                "line": float(s_line),
            }
    return None


def _afl_period_total_market_for(period_code: str) -> str:
    """The SB market that carries a given period TOTAL ("" if none). v6.00
    (07-17 live probe): 1h->first_half_total, 2h->second_half_total,
    1q-4q->quarter_total. Full-game totals use total_points (not here)."""
    if period_code == "1h":
        return "first_half_total"
    if period_code == "2h":
        return "second_half_total"
    if period_code in ("1q", "2q", "3q", "4q"):
        return "quarter_total"
    return ""


def _resolve_afl_period_total(markets: dict, period_code: str, side, line):
    """EXACT-match an AFL period TOTAL (Over/Under + line) to its live
    proposition_id (D, v6.00). Mirrors _resolve_afl_period_prop but matches
    selection=Over/Under instead of a team. Returns {proposition_id, odds,
    selection, market, period, line} on an EXACT (period + side + line) match,
    else None (miss -> manual, NO fuzzy)."""
    mkt_key = _afl_period_total_market_for(period_code)
    if not mkt_key or not isinstance(markets, dict):
        return None
    m = markets.get(mkt_key)
    if not isinstance(m, dict):
        return None
    sels = m.get("selections") or m.get("outcomes") or []
    snorm = (side or "").strip().lower()
    if snorm not in ("over", "under"):
        return None
    try:
        target_line = float(line)
    except (TypeError, ValueError):
        return None
    for s in sels:
        if not isinstance(s, dict):
            continue
        if str(s.get("period") or "").strip().lower() != period_code:
            continue
        if str(s.get("selection") or "").strip().lower() != snorm:
            continue
        s_line = s.get("line")
        if s_line is None:
            continue
        try:
            if abs(float(s_line) - target_line) > 1e-6:
                continue
        except (TypeError, ValueError):
            continue
        pid = s.get("proposition_id")
        if pid:
            return {
                "proposition_id": str(pid),
                "odds": s.get("odds") or s.get("price"),
                "selection": s.get("selection"),
                "market": mkt_key,
                "period": period_code,
                "line": float(s_line),
            }
    return None


def _is_handicap_sgm(tip) -> bool:
    """True if `tip` is an SGM containing at least one handicap (line /
    first_half_line) leg. Used to route handicap SGMs to manual."""
    if not getattr(tip, "is_sgm", False):
        return False
    return any(
        (getattr(l, "market", "") or "").lower() in ("line", "first_half_line")
        for l in (tip.legs or [])
    )


_HANDICAP_MARKETS = ("line", "first_half_line", "handicap", "margin", "team_line")


def _tip_has_handicap_leg(tip) -> bool:
    """True if ANY leg is a team HANDICAP (line/first_half_line/handicap/margin/
    team_line) OR a mis-parsed team-level leg (no player AND no stat — a handicap
    whose market label got mangled, e.g. the Fremantle +0.5 SGM leg that the
    same-player carry-over turned into a fake player prop). v5.23 (Wilson
    2026-06-06): routes ALL Saiyan handicap bets — SGM or single — to manual."""
    for l in (getattr(tip, "legs", None) or []):
        if (getattr(l, "market", "") or "").lower() in _HANDICAP_MARKETS:
            return True
        if (not (getattr(l, "player", "") or "").strip()
                and not (getattr(l, "stat", "") or "").strip()):
            return True
    return False


# 07-13 re-verify: TEXT handicap tips carry NO period field (leg.period is set on
# the IMAGE path only), and the text parser has no 2nd-half/quarter market — so a
# Saiyan "St Kilda 2nd Half -5.5" / "Q3" handicap is labelled market='line' and
# would place FULL-GAME (wrong market). This deterministic raw-text backstop forces
# any half/quarter-qualified handicap to manual, mirroring the image _partial_period
# guard for the text path. Full-game handicaps (no half/quarter word) are unaffected.
_SAIYAN_PERIOD_HC_RE = re.compile(
    r"\b(?:1st|2nd|3rd|4th|first|second|third|fourth)[-\s]*(?:half|quarter|qtr|q)\b"
    r"|\b(?:half|quarter|qtr)[-\s]*(?:time|line)\b"
    r"|\bhalf[-\s]*time\b|\bht\b"
    # v6.06 re-verify: also catch 'Q 3' / 'Qtr 3' / 'Quarter 3' / '3Q' / '3 Qtr'
    # (the base q[1-4]/[1-4]q only caught the no-space form).
    r"|\bq(?:tr|uarter)?\s*[1-4]\b|\b[1-4]\s*q(?:tr|uarter)?\b"
    r"|\b[12]h\b|\bh[12]\b",
    re.IGNORECASE,
)


def _saiyan_hc_placeable(tip) -> bool:
    """v5.9x (#7, 07-12 review C1): True iff EVERY handicap-ish leg is a clean
    FULL-GAME 'line' handicap — the ONLY handicap the placement resolvers exact-
    match (line/pick_own_line). A `first_half_line` (period) leg would otherwise
    fall back to the FULL-GAME catalog and place the WRONG MARKET; a mangled
    no-player/no-stat leg, an h2h/margin/team_line leg, has no safe resolver. So
    when SAIYAN_HC_SGM_ENABLED, ONLY full-game-line handicaps auto-place; every
    other handicap-ish leg keeps the manual route. Player-prop legs never block."""
    # 07-13 re-verify: text tips have no leg.period, so catch a half/quarter
    # qualifier in the raw message and keep it on manual (never a full-game bet).
    if _SAIYAN_PERIOD_HC_RE.search(getattr(tip, "raw_message", "") or ""):
        return False
    for l in (getattr(tip, "legs", None) or []):
        mkt = (getattr(l, "market", "") or "").lower()
        player = (getattr(l, "player", "") or "").strip()
        stat = (getattr(l, "stat", "") or "").strip()
        _is_hcish = (mkt in _HANDICAP_MARKETS) or (not player and not stat)
        if _is_hcish and mkt != "line":
            return False
    return True

# ── Auto alt-line retry config ─────────────────────────────────────
# When primary line fails to place anywhere, we try tipped_line ±1 for
# player props. Over bets try the lower (easier) line first, under bets
# try the higher (easier) line first. An alt is only placed if its bookie
# odds are within AUTO_ALT_ODDS_TOL of the tipped odds.
AUTO_ALT_ODDS_TOL = 0.10  # 10%

# ── Stat-level fallback (Kev / AusBets) ────────────────────────────
# When a player prop fails because the bookie doesn't carry the stat at
# all (e.g. PRA not offered, only Points), v4 falls through to a chain
# of substitute stats configured in stat_fallbacks.yaml. Tipster-gated
# so Shook (which has explicit alts) is not double-fallen-back.
_stat_fallback_cfg: stat_fallback.StatFallbackConfig = (
    stat_fallback.StatFallbackConfig(enabled_tipsters={}, chains={})
)

# Stat -> HyperBot market name maps. Mirror NBA_OU_MARKETS / AFL_STAT_MARKETS
# in _execute_bet but kept module-level for the stat fallback helper.
# KEEP IN SYNC with the maps in _execute_bet.
_NBA_STAT_TO_MARKET = {
    "points": "player_points",
    "rebounds": "player_rebounds",
    "assists": "player_assists",
    "threes": "player_threes",
    "blocks": "player_blocks",
    "steals": "player_steals",
    "points_rebounds_assists": "player_pra",
    "points_rebounds": "player_pts_rebs",
    "points_assists": "player_pts_asts",
    "assists_rebounds": "player_asts_rebs",
}

_AFL_STAT_TO_MARKET = {
    "disposals": "player_disposals",
    "marks": "player_marks",
    "tackles": "player_tackles",
    "kicks": "player_kicks",
    "handballs": "player_handballs",
    "clearances": "player_clearances",
    "hitouts": "player_hitouts",
    "fantasy_points": "player_fantasy",
}


def _stat_market_map(sport: str) -> dict[str, str]:
    sport_l = (sport or "").lower()
    if sport_l in ("nba", "nbl"):
        return _NBA_STAT_TO_MARKET
    if sport_l == "afl":
        return _AFL_STAT_TO_MARKET
    return {}


def _bookie_event(event: str, bookie: str, sport: str) -> str:
    """
    Translate Squiggle-format event name to bookmaker-specific format
    just before sending to HyperBot. Internal logic still uses the
    Squiggle name everywhere (resolver, audit logs, notifications) — this
    only affects what hits the bookmaker API.

    Sportsbet AFL example: "Greater Western Sydney v North Melbourne"
    -> "GWS Giants v North Melbourne", because Sportsbet doesn't index
    the game under the Squiggle full team name.

    Failure mode: a wrong/missing alias produces "Could not find event"
    from HyperBot, which falls through to next session/manual — same as
    pre-fix behaviour. Adding a new alias is one edit in config.py +
    restart.
    """
    if not event or not bookie:
        return event
    if (sport or "").lower() != "afl":
        return event
    aliases = BOOKIE_AFL_ALIASES.get((bookie or "").lower())
    if not aliases:
        return event
    out = event
    for squiggle_name, bookie_name in aliases.items():
        out = out.replace(squiggle_name, bookie_name)
    return out


def _ladder_steps(remaining: float) -> list[float]:
    """
    Build the list of stake values to try in order, given remaining stake.
    Rounds UP to nearest dollar (up to 1% over is fine). Final step uses raw
    value (no rounding) if rounding would exceed remaining.
    """
    if remaining <= 0:
        return []
    steps = []
    for pct in STAKE_LADDER:
        raw = remaining * pct
        if raw <= STAKE_FLOOR:
            break
        # Round up to nearest dollar
        rounded = float(int(raw) + (1 if raw > int(raw) else 0))
        # Don't exceed remaining
        if rounded > remaining:
            rounded = round(remaining, 2)
        if not steps or steps[-1] != rounded:
            steps.append(rounded)
    return steps


# ── Error Classification ───────────────────────────────────────────
# Errors where the bookie has neither accepted nor rejected — bet is in
# trader review / pending state. From the bookie's perspective the bet
# may still land in your account moments later.
#
# Pointsbet "Bet status: Intercepted" is the canonical case (2026-05-02
# tips 61646, 61647 came back as Intercepted via HyperBot but BOTH bets
# were accepted on the Pointsbet account anyway). HyperBot's pointsbet
# client returns failure immediately on Intercepted, but the trader
# review usually approves seconds-to-minutes later.
#
# Treating these as failures and spilling to another bookie causes
# double-staking. Treating them as successes also breaks (we have no
# bet ID, can't reconcile). Right answer: stop spillover for this stake
# amount AND alert manual to check the bookie account.
AMBIGUOUS_OUTCOME_PATTERNS = [
    "intercepted",          # Pointsbet trader review
    "under review",         # generic
    "pending review",       # generic
    "trader review",        # generic
    "manual review",        # generic, distinct from our internal "manual"
]

# Reject strings that PROVE the bet never reached the bookie's books, so a slow
# response on one of these is still a clean pre-placement reject, NOT a
# maybe-landed bet. These bypass the >5s slow-rejection AMBIGUOUS path below.
# Why this list is deliberately narrow: the slow-rejection guard exists for the
# Erasmus regression (a slow "stake too high" that actually landed). Any string
# that could fire AFTER submission must stay OUT of here, or we re-open that
# hole. So "Bet placement failed" (generic, can fire post-submit on some
# bookies) is intentionally excluded. Only strings meaning "priced/validated and
# refused before any slip was submitted" belong here.
#   - no_runners / failed to get odds: bookie could not price the runner, so no
#     slip was ever submitted (Bet365 empty-scrape, Redcliffe 2026-05-29).
#   - "disabled at one or more hierarchy": Sportsbet refused to build the SGM
#     slip at validation, nothing submitted (NBA SGM, 2026-05-29).
# v6.07 (sweep HIGH #1/#5): the error-text signatures of HyperBot's THREE
# "correlation id never resolved" envelopes (hyperbot_client _unwrap_single_status
# timeout + H22 pending, and _post_v3_async's hard poll deadline). Such a bet CAN
# still land after our poll window, so it must never be downgraded to a clean
# not-placed and re-staked/hand-placed (DOUBLE STAKE). Prefer the structural
# BetResult.cid_unresolved flag; this text match is the belt for any envelope that
# predates it. Keep in sync with hyperbot_client.
_CID_UNRESOLVED_MARKERS = (
    "hard poll deadline",
    "cid timeout",
    "poll budget exhausted",
    "bet status unknown",
)


def _err_is_cid_unresolved(err) -> bool:
    """True if an error string is one of HyperBot's unresolved-cid envelopes."""
    e = str(err or "").lower()
    return any(m in e for m in _CID_UNRESOLVED_MARKERS)


# Mirrors racing_placer.PRE_PLACEMENT_REJECT_PATTERNS - keep the two in sync.
PRE_PLACEMENT_REJECT_PATTERNS = [
    "no_runners",
    "no runners",
    "failed to get odds",
    "disabled at one or more hierarchy",
    "not found. available:",
    # Event/market not carried by the bookie -> HyperBot can't build a slip,
    # so nothing was ever submitted. Definitively pre-placement. Added
    # 2026-05-30 after "Sports event not supported on Sportsbet (event not
    # carried): Adelaide v Geelong" took 7.3s and falsely tripped the
    # slow-rejection AMBIGUOUS guard (debited $1 + blocklisted on a bet that
    # could not possibly have placed).
    "event not supported",
    "event not carried",
    "not carried",
    # "Line moved for 'X': A → B" is generated at slip validation when the
    # requested line isn't available — the bet is never submitted, so it's
    # definitively pre-placement regardless of latency (same class as
    # "event not carried" above). Without this, a slow line-move would
    # falsely trip the slow-rejection AMBIGUOUS guard (debit + blocklist on
    # a bet that could not have placed) and would also pre-empt the
    # equivalent-line retry in _place_sgm_v4. Added 2026-05-31.
    "line moved",
    # "line=6.5 did not match any of N candidates" is the SAME class as
    # "line moved": HyperBot's slip-validation rejects the requested line
    # BEFORE submitting to the bookie, so nothing was placed — definitively
    # pre-placement regardless of latency. Without this, a slow "did not
    # match" falsely tripped the slow-rejection AMBIGUOUS guard (debit +
    # blocklist + stop) instead of correctly walking the alt-line ladder /
    # spilling to the next bookie. Sibling of "line moved" that was missed
    # when that was added. Added 2026-05-31 (v4.6 audit re-verify).
    "did not match",
    # "stale_command" is HyperBot rejecting a slip whose per-session command
    # token went stale BEFORE the bet was submitted to the bookie (observed on
    # all 4 sessions at once / a ~10h idle token, 2026-05-31) — nothing was
    # placed, so it is definitively pre-placement. Without this a SLOW
    # stale_command would falsely trip the AMBIGUOUS guard (debit + blocklist on
    # a bet that could not have landed). Added 2026-06-01 (fix I). NOTE: assumes
    # stale_command ALWAYS means "token stale, never submitted" — CONFIRM with
    # Soup; if it can ever follow a submitted slip, revisit.
    "stale_command",
    "stale command",
    # HTTP 403/401/400 on /place_bet = server refused BEFORE submission (auth /
    # bad request) -> bet never placed -> pre-placement. Fix H (2026-06-01):
    # without this a SLOW 403/400 would falsely trip the ambiguous guard (blind
    # debit + blocklist) instead of spilling to another session. The FAST case
    # is handled in hyperbot_client._post_v3_async (no ambiguous tag). Specific
    # HTTP-error strings only — NOT a bare "client error" (a 5xx may have landed).
    "403 client error",
    "forbidden",
    "401 client error",
    "unauthorized",
    "400 client error",
    "bad request",
]

# Slow-rejection latency threshold (seconds). Mirrors racing_placer.py's
# STAKE_REJECT_LATENCY_THRESHOLD_SEC. Erasmus regression 2026-05-03: a
# $125 "stake too high" rejection took 33s to come back and was actually
# placed on the Sportsbet account. Net exposure $525 on a $400 tip after
# tipbot laddered down and spilled.
#
# Sports placement (this file's _place_singles_v4 / _place_sgm_v4) was
# never given this check when racing got it — only racing_placer.py had
# the latency guard. Dawson AFL 2026-05-21 13:49 had multiple 5-11s
# rejections that fell straight through the gap. Same failure class,
# same threshold, same handling: debit remaining as if placed, blocklist
# bookie, fire critical alert, stop laddering.
#
# Healthy "stake too high" / "selection not found" rejections come back
# in well under 1s. Anything 5s+ means HyperBot was processing the bet
# server-side then lost/delayed the response, which is the danger zone.
STAKE_REJECT_LATENCY_THRESHOLD_SEC = 5.0


def _is_ambiguous_outcome(error: str) -> bool:
    """True if the bookie response is neither accept nor reject — the bet
    is pending trader review and may land in the account later. Caller
    must NOT spill the same stake to another bookie (risk of double-bet)
    and SHOULD route to manual alert for human verification.
    """
    if not error:
        return False
    err = error.lower()
    return any(pat in err for pat in AMBIGUOUS_OUTCOME_PATTERNS)


def _is_definitely_pre_placement(error: str) -> bool:
    """True when the reject string proves the bet was never submitted to the
    bookie (could not price, or refused at validation). Used to skip the
    slow-rejection AMBIGUOUS path: a slow response on one of these is still a
    clean reject, not a maybe-landed bet. See PRE_PLACEMENT_REJECT_PATTERNS for
    why the list is intentionally narrow (Erasmus safety)."""
    if not error:
        return False
    err = error.lower()
    return any(pat in err for pat in PRE_PLACEMENT_REJECT_PATTERNS)


def _reconcile_ambiguous(account_id, *, event, stake, sport, selection, submit_ts):
    """Decide what to do with a SLOW-REJECTION ambiguous outcome by checking
    /api/pending_bets, encoding Wilson's 2026-05-31 decisions. Returns a dict:

      {'action': 'placed',       'match': <pending bet>, 'actual_stake': float}
          -> bet IS on the account. Record it (real bookie_bet_id), debit the
             ACTUAL stake (auto-cap: smaller counts), do NOT spill. (Tier 1 —
             safe regardless of feed lag.)
      {'action': 'spill'}
          -> confirmed NOT placed AND RECONCILE_SPILL on. Treat as a genuine
             reject: ladder/spill to recover the stake. (Tier 2.)
      {'action': 'conservative', 'reason': str}
          -> fall back to today's behaviour (debit-as-placed + blocklist +
             critical alert). Fires when: reconciliation disabled, no
             account_id, pending_bets API failed (decision 3 — never spill on
             uncertainty, but the alert reason carries WHY), or confirmed
             not-found while RECONCILE_SPILL is off (Tier 1).

    Only call this for the slow-rejection class — NOT text-pattern ambiguous
    (Pointsbet "intercepted"), which per decision 2 always stays conservative
    (no spill: it lands after MBL/trader review).
    """
    # Delegates to the shared reconcile.decide_ambiguous so racing_placer.py can
    # reuse the SAME decision logic without importing main (fix B, 2026-06-01).
    import reconcile
    return reconcile.decide_ambiguous(
        hb, account_id, event=event, stake=stake, sport=sport,
        selection=selection, submit_ts=submit_ts,
        reconcile_enabled=RECONCILE_AMBIGUOUS, spill_enabled=RECONCILE_SPILL,
    )


def _emit_sports_ambiguous_alert(tip, ambiguous_outcomes: list[dict]) -> None:
    """Fire a critical Telegram alert when one or more placements were
    flagged as ambiguous outcome (slow rejection). Caller must have
    already debited remaining_stake and blocklisted the bookie. This is
    purely the user-facing notification.

    Mirrors the pattern in tiptitans_processor for racing's ambiguous
    outcomes — same alert shape, same critical channel routing.
    """
    if not ambiguous_outcomes:
        return
    # Escape HTML-sensitive characters in untrusted string values before
    # building detail_str, so Telegram parse_mode=HTML doesn't choke on
    # event names or error strings containing & or < (e.g. "A&B", "<error>").
    # notify_critical also escapes its argument, so these fields get a second
    # pass — cosmetically harmless (& → &amp; displays as &amp;) but safe.
    _esc = _html_mod.escape
    lines = []
    for a in ambiguous_outcomes:
        latency = a.get("elapsed_sec")
        latency_tag = f" (rejection took {latency:.1f}s)" if latency else ""
        _cid = a.get("correlation_id")
        cid_tag = f"\n    cid: {_esc(str(_cid))}" if _cid else ""
        lines.append(
            f"  {_esc(str(a.get('bookie', '')))}:{_esc(str(a.get('session_id', '')))} "
            f"${a.get('stake', 0):.2f} @ {a.get('odds')} - "
            f"{_esc(str(a.get('reason', 'ambiguous')))}{latency_tag}"
            f"\n    err: {_esc(str(a.get('error', ''))[:200])}{cid_tag}"
        )
    detail_str = "\n".join(lines)
    event = _esc(str(getattr(tip, "event", "?") or "?"))
    tipster = _esc(str(getattr(tip, "tipster", "?") or "?"))
    try:
        notifier.notify_critical(
            f"AMBIGUOUS OUTCOME on {tipster} tip. Bookie returned a "
            f"failure but bet MAY have placed on the account. Verify "
            f"manually at bookie.\n"
            f"Event: {event}\n"
            f"{detail_str}"
        )
    except Exception as e:
        log.error(f"notify sports ambiguous outcome failed: {e}")


# Errors that mean "this exact bet won't work on ANY same-bookie account"
# (player not on market, market doesn't exist, etc) - short-circuit instead
# of retrying identical sessions.
SAME_BOOKIE_FATAL_PATTERNS = [
    "selection ", "not found",
    "market ", "player ",
    "no market", "no selection",
    "did not match",            # "line=6.5 did not match any of N candidates"
    "missing required bet data", # bet365 sports → racing-routing bug response
    "could not find",           # "Could not find event: ..." — AFL Squiggle
                                # vs Sportsbet name mismatch. Without this
                                # all 4 Sportsbet sessions failed identically
                                # before the blocklist could short-circuit.
]

# Stake errors are the ONLY ones we keep retrying with smaller stakes for.
# We have to be specific here — generic "stake" matching catches false positives
# like "Missing required bet data (track/race_num/runner/stake)".
STAKE_ERROR_PATTERNS = [
    "stake too high", "stake too low",
    "max stake", "min stake",
    "stake amount", "stake exceeds",
    "below floor", "above ceiling",
    "exceeds maximum", "below minimum",
]


def _is_stake_error(error: str) -> bool:
    """True only for genuine stake-size errors that warrant retrying smaller."""
    if not error:
        return False
    err = error.lower()
    return any(pat in err for pat in STAKE_ERROR_PATTERNS)


def _extract_max_stake(result):
    """
    v5.9x: read the bookie-stated allowable max stake off a stake-too-high (538)
    reject. Accepts EITHER a raw HyperBot response dict OR a BetResult-like
    object. Prefers the structured `max_stake` field (HB v1.7.85 surfaces it as a
    top-level float); falls back to the `max=$X` token in the error string.
    Returns a float > 0, or None when neither is present / parseable.
    """
    if result is None:
        return None
    if isinstance(result, dict):
        ms = result.get("max_stake")
        err = result.get("error") or ""
    else:
        ms = getattr(result, "max_stake", None)
        err = getattr(result, "error", "") or ""
    # 1) structured field (preferred)
    try:
        if ms is not None and float(ms) > 0:
            return float(ms)
    except (TypeError, ValueError):
        pass
    # 2) fallback: parse "max=$86.20" (or "max=86.2") out of the error text
    m = re.search(r"max\s*=\s*\$?([\d,]+(?:\.\d+)?)", err, re.IGNORECASE)
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            return val if val > 0 else None
        except ValueError:
            return None
    return None


def _sb_max_stake_target(resp, bookie, attempted_stake):
    """
    v5.9x max-stake rebet gate. Given a FAILED placement response `resp` (dict),
    the `bookie` it was placed on, and the stake we just tried, return the stake
    to rebet at ONCE — or None to fall through to the old blind ladder.

    Fires only when ALL hold:
      * SPORTSBET_MAX_STAKE_REBET kill-switch on;
      * the reject is on Sportsbet (the only bookie that surfaces the max);
      * it is a genuine stake-size error (538 stake-too-high);
      * the bookie gave us a max >= $1;
      * that max is STRICTLY below what we just tried (a stake-too-HIGH always
        is — so the rebet can NEVER over-stake vs the attempted amount).
    """
    if not SPORTSBET_MAX_STAKE_REBET:
        return None
    if not isinstance(resp, dict) or resp.get("success"):
        return None
    # MONEY-SAFETY: never rebet a maybe-landed bet. A stake-too-high (538) is a
    # clean pre-submission validation reject (nothing hit the exchange), so it is
    # not ambiguous — but if HyperBot ever tags such a response ambiguous (a fast
    # POST fail that may have landed), refuse to rebet: that would double-stake.
    # Symmetric with the racing guard.
    if resp.get("ambiguous"):
        return None
    if "sportsbet" not in str(bookie or "").lower():
        return None
    if not _is_stake_error(resp.get("error") or ""):
        return None
    mx = _extract_max_stake(resp)
    if mx is None or mx < 1.0:
        return None
    try:
        if attempted_stake and mx >= float(attempted_stake):
            return None
    except (TypeError, ValueError):
        return None
    return round(mx, 2)


def _is_price_change_error(error: str) -> bool:
    """
    True if HyperBot rejected because price moved between price_check and
    place_bet. Distinct from stake errors and from same-bookie-fatal errors —
    we retry once on the same session with target_odds suppressed.
    """
    if not error:
        return False
    err = error.lower()
    return "price has changed" in err or "price changed" in err


def _is_same_bookie_fatal(error: str) -> bool:
    """Detect errors that will fail identically on other same-bookie sessions."""
    if not error:
        return False
    if _is_stake_error(error):
        return False
    err = error.lower()
    return any(pat in err for pat in SAME_BOOKIE_FATAL_PATTERNS)


def _extract_available_players(error: str) -> list[str]:
    """
    Parse 'Available: [...]' list from a 'Selection not found' error.
    Returns list of player names found in the available selections.
    """
    if "available:" not in error.lower():
        return []
    try:
        # Available: ['Nikola Jokic (player=Nikola Jokic, line=40.0)', ...]
        idx = error.lower().index("available:")
        tail = error[idx + len("available:"):].strip()
        # Extract player= values
        import re as _re
        names = _re.findall(r"player=([^,)]+)", tail)
        return list(dict.fromkeys(n.strip() for n in names))  # dedupe, preserve order
    except Exception:
        return []


def _extract_available_lines(error: str, player: str = "") -> list[float]:
    """
    Parse 'line=X' values out of a HyperBot error string.
    Used to extract alt lines offered by the bookie when the tipped line
    didn't match. Optionally filters to a specific player.
    Returns sorted list of unique line values.
    """
    if not error:
        return []
    import re as _re
    try:
        # Match 'line=26.5' or 'line=26' (optionally followed by trailing chars)
        # If player specified, only capture lines near that player mention to
        # avoid picking up lines from unrelated candidates
        lines_set = set()
        if player:
            # Find 'player=<player>' chunks with nearby 'line=N'
            pattern = _re.compile(
                r"player='?" + _re.escape(player) + r"'?[^,]*?,\s*line=([0-9]+(?:\.[0-9]+)?)",
                _re.IGNORECASE,
            )
            for m in pattern.finditer(error):
                lines_set.add(float(m.group(1)))
            # Fallback: if no player-keyed hits, fall through to greedy
            if lines_set:
                return sorted(lines_set)
        # Greedy
        for m in _re.finditer(r"line=([0-9]+(?:\.[0-9]+)?)", error):
            lines_set.add(float(m.group(1)))
        return sorted(lines_set)
    except Exception:
        return []


def _extract_moved_line(error: str) -> float | None:
    """
    Parse the 'new line' from a 'Line moved ... X -> Y' or 'X → Y' error.
    Returns Y as a float, or None if no move pattern found.
    """
    if not error:
        return None
    import re as _re
    # Covers: "Line moved for 'Foo': 14.5 → 26.5" and "... 14.5 -> 26.5"
    m = _re.search(
        r"line\s*moved.*?([0-9]+(?:\.[0-9]+)?)\s*(?:->|→|=>)\s*([0-9]+(?:\.[0-9]+)?)",
        error,
        _re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(2))
        except ValueError:
            return None
    return None


def _line_move_acceptable(
    tipped_line: float,
    new_line: float,
    selection: str,
    tolerance: float = 0.10,
) -> bool:
    """
    Decide whether to auto-retry at a new line after HyperBot reports a line
    move.

    Acceptance rules — the move is acceptable if EITHER:

      A) Absolute gap rule: |new_line - tipped_line| <= 1.0 (any direction).
         Wilson's call 2026-04-25 — for small line shifts within a single
         increment, we trust the bookie's market price (odds tolerance is
         enforced separately at placement time as the real safeguard).
         Catches the common case where a tipster sends an alt line (19.5)
         and Sportsbet only offers the main line (20.5).

      OR

      B) Relative + favourable-direction rule (the original logic):
         - |gap| / tipped_line <= tolerance (default 10%)
         - Under X.5: higher new_line is easier (favourable)
         - Over X.5:  lower new_line is easier (favourable)
         - Equal lines: always acceptable

    Non-Under/Over selections (team handicaps, H2H, etc.) fall back to the
    relative-tolerance gate only — direction rules don't apply.

    Examples:
      Over 19.5 -> 20.5: abs gap 1.0, rule A => TRUE  (KAT tip 2026-04-25)
      Under 33.5 -> 34.5: abs gap 1.0, rule A => TRUE
      Under 33.5 -> 26.5: abs gap 7.0, gap 21%, neither rule => FALSE
      Over 20.5 -> 19.5: abs gap 1.0, rule A => TRUE
      Over 20.5 -> 22.0: abs gap 1.5, gap 7%, unfavourable => FALSE
    """
    try:
        tipped_f = float(tipped_line)
        new_f = float(new_line)
    except (TypeError, ValueError):
        return False
    if tipped_f == 0:
        return False

    # Rule A: absolute gap of <= 1.0 always accepted (any direction)
    if abs(new_f - tipped_f) <= 1.0:
        return True

    # Rule B: relative tolerance + favourable direction
    gap = abs(new_f - tipped_f) / abs(tipped_f)
    if gap > tolerance:
        return False

    sel_lower = (selection or "").lower()
    if "under" in sel_lower:
        # Under: higher line is easier (more room below the number)
        return new_f >= tipped_f
    if "over" in sel_lower:
        # Over: lower line is easier (less needed to clear it)
        return new_f <= tipped_f

    # Non-directional market (h2h, team handicap). Tolerance-only gate.
    return True


def _player_name_variants(name: str) -> list[str]:
    """
    Generate name variants to try when 'Selection not found'. Order matters:
    most likely first.
    Examples:
      'Karl-Anthony Towns' -> ['Karl-Anthony Towns', 'Karl Anthony Towns', ...]
      'P.J. Tucker Jr.'    -> ['P.J. Tucker Jr.', 'PJ Tucker Jr.', 'P.J. Tucker', ...]
    """
    if not name:
        return []
    variants = [name]

    # Strip hyphens
    no_hyphen = name.replace("-", " ").replace("  ", " ").strip()
    if no_hyphen != name:
        variants.append(no_hyphen)

    # Strip periods
    no_period = name.replace(".", "")
    if no_period != name and no_period not in variants:
        variants.append(no_period)

    # Strip suffixes
    suffixes = [" Jr.", " Jr", " Sr.", " Sr", " III", " II", " IV"]
    no_suffix = name
    for suf in suffixes:
        if no_suffix.endswith(suf):
            no_suffix = no_suffix[: -len(suf)].strip()
            break
    if no_suffix != name and no_suffix not in variants:
        variants.append(no_suffix)

    # All combined
    combined = name
    for r in [("-", " "), (".", "")]:
        combined = combined.replace(*r)
    for suf in suffixes:
        if combined.endswith(suf):
            combined = combined[: -len(suf)].strip()
            break
    combined = combined.replace("  ", " ").strip()
    if combined not in variants:
        variants.append(combined)

    return variants


# ── Parser Router ───────────────────────────────────────────────────

REGEX_PARSERS = {
    "saiyan_afl": parse_saiyan_message,
}
# ausbets_nba / kev_nba have no regex parser in the sports-only sportsbot fork
# (their modules aren't shipped). Register each only when its parser imported,
# so the router falls through to the Groq path otherwise. In tipbot both are
# always present, so this is identical to the old static dict.
if parse_ausbets_message is not None:
    REGEX_PARSERS["ausbets_nba"] = parse_ausbets_message
if parse_kev_message is not None:
    REGEX_PARSERS["kev_nba"] = parse_kev_message

# v5.80 (5-opus review SHOULD-FIX 1): the Claude TEXT fallback is restricted to
# this ALLOW-LIST of tipsters that place through a PRICE-CHECKED path. It must
# NOT fire for blind/no-price-check fan-out tipsters (etr_nba places at any odds
# with no price gate via _place_etr_nba_fanout) — a Claude-recovered tip there
# would auto-place blind with no odds backstop. Add a tipster here only after
# confirming its placement path price-checks the market.
#   - shook: MLB HRRBI -> price-checked SGM/single path.
#   - saiyan_afl (v5.82): AFL singles/SGM -> price-checked fan-out (_place_afl_fanout
#     resolves the market in the catalog before placing). Added after a real
#     2026-06-21 09:00 Saiyan SGM (LDU 24+/Campbell 13+ @1.88) was lost to a Groq
#     invalid-JSON gibberish failure -> manual, because the v5.80 fallback only
#     covered Groq-ONLY tipsters and saiyan is a REGEX tipster (Groq+regex both fail).
CLAUDE_TEXT_FALLBACK_TIPSTERS = {"shook", "saiyan_afl", "dello_afl"}


# ── FIX B (v5.95): regex-first trust gate ───────────────────────────
# Per-stat WIDE sane line bounds for the Saiyan regex FAST-PATH. A parsed line
# outside these -> treat as a mis-parse and defer to the LLM. Bounds only reject
# absurd values; a borderline-but-real value just falls through to the LLM (safe).
# Keys mirror config.AFL_STAT_MAP's normalised VALUES (what the parser emits).
_SAIYAN_LINE_BOUNDS = {
    "disposals": (5.0, 45.0),
    "kicks": (2.0, 35.0),
    "handballs": (2.0, 30.0),
    "marks": (1.0, 20.0),
    "tackles": (1.0, 20.0),
    "clearances": (1.0, 20.0),
    "hitouts": (1.0, 70.0),
    "goals": (0.5, 8.0),
    "behinds": (0.5, 8.0),
    "fantasy_points": (20.0, 200.0),
}
# Live/in-play markers: if ANY appear, defer to the LLM (which sets is_live ->
# alert_only). The regex parser never sets is_live, so without this a live single
# would auto-place. WIDE net (a false hit just costs an LLM parse, never a bet).
_SAIYAN_LIVE_RE = re.compile(
    r"(?:\blive\b|in.?play|\bIP\b|\bq[1-4]\b"
    r"|[1-4](?:st|nd|rd|th)?\s*(?:q(?:tr|uarter)?|term)\b"
    r"|quarter\s*time|half.?time|\bHT\b|\b[12]H\b|3/4\s*time|\U0001F534)",
    re.IGNORECASE,
)
# An explicit unit token the regex parser IGNORES (it hardcodes default_units) ->
# defer to the LLM which parses per-tip units. Requires a DIGIT before the 'u'
# so the 'u25.5' under-shorthand and bare 'Under' never match; only '1u'/'1.5u'/
# '2 units' fire. Over-detection is safe (defer); under-detection would mis-stake.
_SAIYAN_UNIT_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\s*u(?:nits?)?\b", re.IGNORECASE)


def _saiyan_regex_trusted(text: str, tips: list) -> bool:
    """FIX B high-confidence gate: True ONLY when the deterministic Saiyan regex
    parse is safe enough to SKIP the LLM. CONSERVATIVE — every non-clean branch
    returns False so the tip falls through to the LLM (Claude/Groq). A False NEVER
    drops a tip (the LLM still runs); only True skips the LLM. Restricted to
    SINGLE-leg player props; SGM / team-line / H2H / live / unit-token defer."""
    from config import AFL_TEAMS
    try:
        from parsers.saiyan_afl import _clean as _saiyan_clean
    except Exception:
        return False
    if not tips:
        return False
    cleaned = _saiyan_clean(text)

    # is_live guard: any live marker in the raw OR cleaned text -> LLM.
    if _SAIYAN_LIVE_RE.search(text) or _SAIYAN_LIVE_RE.search(cleaned):
        return False

    # An explicit unit token ANYWHERE (even a standalone '2u' line the parser
    # ignores, not just a bet line) -> the regex can't size it -> defer to the LLM
    # (v5.95 review, cross-cutting). Safe: Saiyan's decimal odds / bookie lists /
    # 'u25.5' under-shorthand never produce a digit-then-'u' word-boundary match.
    if _SAIYAN_UNIT_TOKEN_RE.search(cleaned):
        return False

    # COMPLETENESS: count EVERY '@'-bearing non-comment line as a bet line and
    # require exactly one tip per line. NOT gated on a surviving (XX)/[XX] tag: an
    # all-caps surname whose emoji team-sentinel was stripped in preprocessing
    # loses its tag, the parser skips the line, and a tag-gated counter would still
    # match #tips -> a SILENTLY DROPPED leg (v5.95 review fixB #1). Counting all
    # '@' lines makes count != #tips -> defer, never drop.
    _bet_lines = 0
    for _ln in cleaned.split("\n"):
        _ln = _ln.strip()
        if not _ln or _ln.startswith("*") or "@" not in _ln:
            continue
        # An SGM leg-separator is a '/' in the LEGS portion (BEFORE '@'). A single's
        # bookie list ('@ 1.91 Betr/1.90 Lads/...') ALSO contains '/', but AFTER the
        # '@' -> only a pre-'@' '/' means multi-leg. (Checking the whole line would
        # defer EVERY real Saiyan single on its bookie list = Fix B inert.)
        if "/" in _ln.split("@", 1)[0]:      # multi-leg SGM line -> LLM
            return False
        _bet_lines += 1
    if _bet_lines == 0 or _bet_lines != len(tips):
        return False

    # Per-tip field + sanity checks. SINGLE-leg player props ONLY.
    for _t in tips:
        if getattr(_t, "is_sgm", False) or getattr(_t, "alert_only", False) \
                or getattr(_t, "is_live", False):
            return False
        if len(_t.legs) != 1:
            return False
        _leg = _t.legs[0]
        if _leg.market != "player_prop":
            return False
        if not (getattr(_leg, "player", "") or "").strip():
            return False
        if getattr(_leg, "team_abbr", "") not in AFL_TEAMS:   # resolved real club
            return False
        _stat = (_leg.stat or "").strip().lower()
        _bounds = _SAIYAN_LINE_BOUNDS.get(_stat)
        if _bounds is None:                                    # unknown/unmapped stat
            return False
        try:
            _line = float(_leg.line)
        except (TypeError, ValueError):
            return False
        if not (_bounds[0] <= _line <= _bounds[1]):
            return False
        _is_thresh = bool(getattr(_leg, "_is_threshold", False))
        _sel = (_leg.selection or "").strip().lower()
        if not _is_thresh and _sel not in ("over", "under"):
            return False
        # Sane tipster odds (now populated by the parser) — keeps the downstream
        # odds-ceiling/floor + AFL target_odds 'wrong selection' guards ACTIVE
        # (they DISABLE at odds<=1.0, so a 0.0 would silently weaken them).
        try:
            _odds = float(getattr(_t, "suggested_odds", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        if not (1.01 <= _odds <= 1000.0):
            return False
    return True


def _regex_first_trusted(tipster: str, text: str, tips: list) -> bool:
    """Dispatch the FIX B fast-path trust gate by tipster. Returns False for any
    tipster without a dedicated gate, so adding a tipster to REGEX_FIRST_TIPSTERS
    without a gate here is a safe no-op (the LLM path runs)."""
    if tipster == "saiyan_afl":
        return _saiyan_regex_trusted(text, tips)
    return False


def route_message(
    text: str, tipster: str, sport: str,
    unit_size: float, default_units: float,
) -> tuple[list[ParsedTip], dict]:
    """
    Route a message through Groq first, then regex fallback.
    Returns (tips, timing_dict).
    """
    timing = {}

    # Saiyan AFL emoji preprocessing was previously only applied inside
    # parse_with_groq, which meant when Groq returned invalid JSON and we
    # fell through to the regex parser, the regex parser saw raw
    # <:CODE:digits> emojis and had no team context. 2026-05-16 10:28
    # Simpkin/Daniel NM SGM: Groq response truncated mid-string, regex
    # took over, fuzzy-matched surname-only 'Daniel' to Daniel Turner
    # (Melbourne) instead of Caleb Daniel (NM): wrong player AND wrong
    # team. Promote to the top of route_message so both parsers see
    # preprocessed text. parse_with_groq still calls the preprocessor
    # defensively (it's idempotent on already-rewritten text).
    parser_text = text
    if tipster == "saiyan_afl":
        parser_text = _preprocess_saiyan_emojis(text)

    # FIX B (v5.95) — DETERMINISTIC FAST PATH. For a whitelisted structured-format
    # tipster with a proven regex parser (saiyan_afl), run the regex parser FIRST
    # and SKIP the LLM entirely when its output passes the high-confidence gate
    # (_regex_first_trusted). Reclaims the ~3.5-5.1s Claude parse on the common
    # single-leg case (the decommissioned llama-4-scout was ~1.2s). ANY doubt --
    # 0 tips, SGM, partial/under-match, unknown stat, insane line/odds, live,
    # unit token, unresolved team -- falls THROUGH to the LLM path below UNCHANGED,
    # so a mis-parse can never escape to placement. A regex cannot hallucinate
    # (literal-substring extraction or no match), so this does NOT reintroduce the
    # llama gibberish class; Claude stays the backstop for everything unproven.
    if tipster in REGEX_FIRST_TIPSTERS:
        _rf_fn = REGEX_PARSERS.get(tipster)
        if _rf_fn is not None:
            _rf_t0 = time.time()
            try:
                _rf_tips = _rf_fn(parser_text, default_units=default_units, unit_size=unit_size)
            except Exception as _rf_e:
                log.warning(f"regex-first parser crashed for '{tipster}': {_rf_e} -> LLM path")
                _rf_tips = None
            if _rf_tips and _regex_first_trusted(tipster, parser_text, _rf_tips):
                timing["regex_parse"] = round(time.time() - _rf_t0, 3)
                timing["parser"] = "regex_first"
                log.info(
                    f"REGEX-FIRST: {len(_rf_tips)} trusted tip(s) for '{tipster}' in "
                    f"{timing['regex_parse']:.3f}s -> SKIP LLM parse"
                )
                return _rf_tips, timing
            log.info(f"regex-first not trusted for '{tipster}' -> LLM parse")

    # Step 1: parse. v5.83 CLAUDE PRIMARY skips Groq entirely (scout deprecated +
    # gibberish-prone; the Groq-fail-then-fallback round-trip was too slow). The
    # Claude result flows through the IDENTICAL downstream gates — pure parser
    # swap. Falls back to Groq only when Claude is unavailable (fork without a key).
    if tip_parser._claude_primary_enabled():
        groq_tips, groq_time = tip_parser.parse_text_fallback(
            parser_text, tipster, sport, unit_size, default_units,
        )
        _parser_name = "claude_primary"
    else:
        groq_tips, groq_time = parse_with_groq(
            parser_text, tipster=tipster, sport=sport,
            unit_size=unit_size, default_units=default_units,
        )
        _parser_name = "groq"
    timing["groq_parse"] = round(groq_time, 3)

    if groq_tips:
        timing["parser"] = _parser_name
        return groq_tips, timing

    # Step 2: Fallback to regex parser
    regex_start = time.time()
    regex_fn = REGEX_PARSERS.get(tipster)
    if not regex_fn:
        # Groq-only tipster (Shook) returned no tips. This is usually either
        # (a) a truly non-bet message ("shop odds" / follow-up chatter) that
        # shouldn't alert, or (b) a JSON truncation / parse failure that
        # silently lost a real tip.
        #
        # BUG B (Wilson 2026-06-21): the old gate ("@everyone" + any digit +
        # len>40) FALSE-FIRED a PARSE ERROR on Shook follow-up CHATTER ("No alt
        # for Baldwin, I also sprinkled 2+ Hits for both the SF guys if u
        # wanted...") — the @everyone came from a PREPENDED context msg and "2+"
        # is not a bet. Require a CONCRETE Shook bet signal: a unit token
        # ("0.2u"), an "@ <odds>" price, or the HRR/HRRBI line shape ("M 1.5
        # HRR", "2+ HRRBI") — AND exclude explicit no-bet framing. Chatter with
        # none of these DROPS silently (no manual noise); a real missed HRRBI
        # tip still has a unit/HRR signature so it still alerts.
        _has_bet_signal = (
            _raw_has_unit_token(text)
            or bool(re.search(r"@\s*\d+(?:\.\d+)?", text))
            or bool(re.search(r"\b\d+\s*\+?\s*HRR(?:BI)?\b", text, re.IGNORECASE))
            or bool(re.search(r"\bM\s*\d+(?:\.\d+)?\s*HRR", text, re.IGNORECASE))
        )
        looks_like_bet = (
            len(text) > 40
            and _has_bet_signal
            and not _is_no_bet_framing(text)
        )
        if looks_like_bet:
            # v5.80 RECOVERY: a Groq-only tipster (Shook) lost a real bet to a
            # Groq parse failure. BEFORE routing to manual, retry the parse with
            # Claude (Opus 4.8). Gated on looks_like_bet so it can ONLY fire on a
            # genuine bet-shaped message — never on no-bet/chatter (the AusBets
            # phantom-bet hole). Claude tips re-enter the identical placement
            # gates downstream. 2026-06-20: 4 Shook HRRBI SGMs lost this way.
            # SCOPED to CLAUDE_TEXT_FALLBACK_TIPSTERS (price-checked paths only) —
            # never a blind fan-out tipster like etr_nba (5-opus review).
            if (tipster in CLAUDE_TEXT_FALLBACK_TIPSTERS and tip_parser._claude_fallback_enabled()
                    and not tip_parser._claude_primary_enabled()):  # v5.84: primary already ran Claude
                try:
                    c_tips, c_time = tip_parser.parse_text_fallback(
                        parser_text, tipster, sport, unit_size, default_units,
                    )
                    timing["claude_fallback"] = round(c_time, 3)
                    if c_tips:
                        log.warning(
                            f"CLAUDE FALLBACK recovered {len(c_tips)} text tip(s) "
                            f"for '{tipster}' after Groq returned 0"
                        )
                        timing["parser"] = "claude_fallback"
                        try:
                            notifier.notify_info(
                                f"\U0001f7e2 CLAUDE FALLBACK: recovered {len(c_tips)} "
                                f"'{tipster}' tip(s) after a Groq parse failure"
                            )
                        except Exception:
                            pass
                        return c_tips, timing
                except Exception as e:
                    log.error(f"Claude text fallback failed for {tipster}: {e}")
            # BUG E (Wilson 2026-06-21): label the parse error with the ACTUAL
            # parser. Under CLAUDE_PRIMARY Claude (not Groq) parsed, so the old
            # hardcoded "Groq returned empty tips" was misleading.
            _parser_label = "Claude" if tip_parser._claude_primary_enabled() else "Groq"
            log.warning(
                f"{_parser_label}-only tipster '{tipster}' returned 0 tips on a "
                f"bet-looking message — possible parse failure"
            )
            try:
                notifier.notify_parse_error(
                    tipster,
                    text[:400],
                    f"{_parser_label} returned empty tips (possible truncation/parse failure)",
                )
            except Exception as e:
                log.error(f"Failed to send parse-error notification: {e}")
        else:
            log.info(f"No regex fallback for '{tipster}' (Groq-only)")
        return [], timing

    log.info("Groq returned no tips, falling back to regex parser")
    regex_tips = regex_fn(
        parser_text,
        default_units=default_units,
        unit_size=unit_size,
    )
    timing["regex_parse"] = round(time.time() - regex_start, 3)
    timing["parser"] = "regex" if regex_tips else "none"

    if not regex_tips:
        # v5.69 (m9) + r2 (#5/#7): BOTH Groq AND the regex parser returned 0 tips
        # for a tipster that HAS a regex parser. Alert so a genuinely missed bet
        # doesn't vanish silently — but DON'T alert on the no-bet / recap /
        # commentary messages these tipsters routinely post (which correctly
        # parse to []), or we re-spam the manual channel and contradict the
        # v5.52/i4 no-bet-framing design. So require a STRONG bet signal (a
        # literal unit token OR an "@ <odds>" price) AND explicitly exclude
        # no-bet framing (round-2 #5/#7: the loose 'over/under/+' token list
        # over-fired on banter).
        _low = text.lower()
        _has_strong_bet_token = (
            _raw_has_unit_token(text)
            or bool(re.search(r"@\s*\d+(?:\.\d+)?", text))
        )
        looks_like_bet = (
            len(text) > 40
            and _has_strong_bet_token
            and not _is_no_bet_framing(text)
        )
        if looks_like_bet:
            _pp_log = "Claude" if tip_parser._claude_primary_enabled() else "Groq"
            log.warning(
                f"Regex tipster '{tipster}': {_pp_log} AND regex both returned 0 "
                f"tips on a bet-looking message — possible parse failure"
            )
            # v5.82 RECOVERY: Groq AND the regex parser BOTH missed a bet-looking
            # message (STRONG bet token + NOT no-bet framing — the same gate that
            # decides this is a genuine miss, not chatter). Before routing to
            # manual, retry with Claude (Opus). Scoped to CLAUDE_TEXT_FALLBACK_TIPSTERS
            # (price-checked paths). 2026-06-21 09:00: a real Saiyan SGM was lost
            # here to a Groq invalid-JSON gibberish failure with no fallback.
            if (tipster in CLAUDE_TEXT_FALLBACK_TIPSTERS and tip_parser._claude_fallback_enabled()
                    and not tip_parser._claude_primary_enabled()):  # v5.84: primary already ran Claude
                try:
                    c_tips, c_time = tip_parser.parse_text_fallback(
                        parser_text, tipster, sport, unit_size, default_units,
                    )
                    timing["claude_fallback"] = round(c_time, 3)
                    if c_tips:
                        log.warning(
                            f"CLAUDE FALLBACK recovered {len(c_tips)} text tip(s) for "
                            f"'{tipster}' after Groq+regex both returned 0"
                        )
                        timing["parser"] = "claude_fallback"
                        try:
                            notifier.notify_info(
                                f"\U0001f7e2 CLAUDE FALLBACK: recovered {len(c_tips)} "
                                f"'{tipster}' tip(s) after a Groq+regex parse failure"
                            )
                        except Exception:
                            pass
                        return c_tips, timing
                except Exception as e:
                    log.error(f"Claude text fallback failed for {tipster}: {e}")
            try:
                # BUG E: reflect the actual primary parser (Claude under CLAUDE_PRIMARY).
                _pp = "Claude" if tip_parser._claude_primary_enabled() else "Groq"
                notifier.notify_parse_error(
                    tipster, text[:400],
                    f"{_pp} + regex both returned empty (possible parse failure)",
                )
            except Exception as e:
                log.error(f"Failed to send parse-error notification: {e}")

    return regex_tips, timing


# ── HyperBot Client ─────────────────────────────────────────────────

hb = HyperBotClient()


# ── Event Resolution ────────────────────────────────────────────────

# ── Duplicate Detection ────────────────────────────────────────────
_recent_tips: dict = {}  # {fingerprint: timestamp}
DUPE_WINDOW_SECS = 600  # 10 minutes
# v6.03: fingerprints whose placement is CURRENTLY IN FLIGHT (offloaded place_tip
# running on a worker). Added at claim-before-place, discarded on completion. This
# is NEVER time-purged, so a placement that outlives DUPE_WINDOW_SECS (a slow-HB
# price-poll + place-poll can stack to ~2x the 420s ceiling > 600s) cannot have its
# dedup claim evicted mid-flight -> an identical re-send in that window is still
# caught (else it would fan out a SECOND time onto accounts where attempt #1 may
# have landed = double stake). All access is on the loop thread (like _recent_tips).
_inflight_fps: set = set()
# v5.22: the racing path (image AND text) had NO dedup — a reposted / re-delivered
# racing tip double-placed real money. Fingerprint racing selections here and skip
# a repeat within DUPE_WINDOW_SECS. Keyed in _route_image_racing_tips.
_racing_recent_fps: dict = {}  # {racing fingerprint tuple: timestamp}
# v5.93: the Leroy Betfair-BSP path is a NEW real-money placement path — a telethon
# reconnect catch-up re-delivery / a tipster re-post would double-back on Betfair.
# Keyed per (LEROY, track, race, date, saddle, market), registered BEFORE the place.
# A Leroy race resolves HOURS after the tip, so the 10-min DUPE_WINDOW_SECS is too
# short (an hour-later re-post must still be caught) — use a day-long TTL. See
# _leroy_place_race.
_leroy_recent_fps: dict = {}  # {leroy fingerprint tuple: timestamp}
LEROY_DEDUP_TTL_SEC = 18 * 3600  # 18h — spans a whole race day; prunes stale keys


def _tip_fingerprint(tip: ParsedTip) -> str:
    """Build a fingerprint for dupe detection (player+stat+line+selection+sport)."""
    if tip.is_sgm:
        leg_parts = []
        for leg in tip.legs:
            leg_parts.append(f"{leg.player}|{leg.stat}|{leg.line}|{leg.selection}")
        # Include event so same player-combo SGMs for different games within
        # 10 minutes are not collapsed into a single dedup slot.
        return f"{tip.sport}::SGM::{tip.event or ''}::{'|'.join(leg_parts)}"
    elif tip.legs:
        leg = tip.legs[0]
        # v5.9x (07-12 review #4): include leg.period so a period handicap (e.g. a
        # 1st-half line) isn't collapsed as a duplicate of the full-game line at the
        # same team/line. INERT for normal tips (period=="" for every non-period leg).
        return (f"{tip.sport}::{leg.player}|{leg.team_full}|{leg.stat}|{leg.line}"
                f"|{leg.selection}|{getattr(leg, 'period', '')}")
    # Fallback: raw_message[:100] collides when multiple Shook tips share the
    # same context prefix.  For Shook, fingerprint on the trigger text (the
    # part after "CURRENT MESSAGE:\n") so each distinct tip gets its own slot.
    raw = tip.raw_message or ""
    marker = "CURRENT MESSAGE:\n"
    idx = raw.find(marker)
    if idx != -1:
        trigger = raw[idx + len(marker):]
        return f"shook_trigger::{trigger[:100]}"
    return raw[:100]


def _is_duplicate(tip: ParsedTip) -> bool:
    """Check if this exact tip was placed from the same tipster in the last 10 min.

    Does NOT register the fingerprint — caller must call _register_tip_fingerprint
    AFTER successful placement to avoid permanently locking out tips that fail.
    """
    fp = f"{tip.tipster}::{_tip_fingerprint(tip)}"
    now = datetime.now()

    # Clean expired entries
    expired = [k for k, t in _recent_tips.items()
               if (now - t).total_seconds() > DUPE_WINDOW_SECS]
    for k in expired:
        del _recent_tips[k]

    # v6.03: a fingerprint whose placement is IN FLIGHT is a duplicate even if its
    # _recent_tips claim was time-purged during a >600s slow-HB placement.
    return fp in _recent_tips or fp in _inflight_fps


def _register_tip_fingerprint(tip: ParsedTip, fp: str = None) -> None:
    """Register a tip fingerprint after a successful/ambiguous placement.

    PASS THE PRE-PLACEMENT fp (captured before place_tip). place_tip MUTATES the
    legs — the catalog match rewrites a leg's selection (e.g. 'James Wood Over' ->
    'James Wood'), so recomputing _tip_fingerprint AFTER placement yields a
    DIFFERENT string than the one _is_duplicate checks on a re-send. That defeated
    the dedup: a re-sent Shook HRRBI re-placed an extra $100 on 2026-06-09 (James
    Wood) because the registered (post-mutation) fp never matched the re-send's
    (pre-mutation) fp. Registering the captured pre-placement fp keeps
    check == register. v5.49. (fp=None recomputes — legacy/direct callers.)"""
    if fp is None:
        fp = f"{tip.tipster}::{_tip_fingerprint(tip)}"
    _recent_tips[fp] = datetime.now()


# ── Multi-prop alt merger ───────────────────────────────────────────
# When a tipster posts multiple player-prop tips for the SAME player, they're
# almost always "main + alts" (e.g. Shook: "Fox 21.5 PR" then "18.5 points
# good too"). Merge them into a single tip with alt_lines so placement spills
# unfilled stake from main to each alt in order.
#
# Two cases:
#   1. Same batch (Groq returns 2+ tips from one message) — handled by
#      `_merge_batch_alts()` before the placement loop starts.
#   2. Cross-message (tipster posts main then alt in a follow-up within 60s)
#      — handled by `_recent_primary_tips` lookup on arrival.
# The batch path is simpler and catches the common case (Champagnie style).

_MULTI_PROP_MERGE_WINDOW_SECS = 60


def _make_alt_entry(leg) -> dict:
    """Build an alt_lines entry from a ParsedLeg."""
    return {
        "stat": leg.stat,
        "line": leg.line,
        "selection": leg.selection,
        "market": leg.market,
        "is_threshold": bool(getattr(leg, "_is_threshold", False)),
    }


def _promote_misparsed_sgms(tips: list[ParsedTip]) -> list[ParsedTip]:
    """
    Detect and recover from Groq misparsing an SGM as multiple singles.

    Trigger: 2+ tips in the same Groq batch, same (tipster, sport, player),
    DIFFERENT stats (or different line on the same stat), all single-leg
    player props. The probability of a tipster legitimately posting two
    separate-bets for the same player at different stats inside one
    message is ~zero — that pattern is always a misparsed SGM.

    Recovery: collapse them into a single SGM tip whose legs are the
    original parsed legs in message order. Units/odds taken from the
    first tip (Groq usually copies these across the misparsed singles).

    Brunson 2026-05-01: "u36.5 points/o4.5 assists" arrived as 2 tips
    despite the universal "/" rule. With this defensive layer, the same
    misparse on the next run would yield one SGM tip with both legs
    instead of two singles + a silently-dropped assists leg.
    """
    if len(tips) < 2:
        return tips

    # Group same-batch tips by (tipster, sport, player). Only single-leg
    # player props are eligible — SGMs, alert-only, team bets, racing tips
    # all pass through untouched. SHOOK IS EXCLUDED: Shook posts multiple
    # separate plays per message (different stats/lines on the same player
    # by design) — auto-promoting them as SGM was wrong. Holmgren regression
    # 2026-05-07 17:50: 3 plays auto-promoted to a 3-leg SGM at $80/2.25
    # instead of the intended single $400 on Holmgren O 17.5 P+A. Shook tips
    # are deduped to first-mentioned-only by _dedupe_shook_same_player below.
    groups: dict[tuple[str, str, str], list[ParsedTip]] = {}
    for tip in tips:
        if (
            not tip.legs or len(tip.legs) != 1
            or getattr(tip, "is_sgm", False) or tip.alert_only
            or not tip.legs[0].market or "player" not in tip.legs[0].market
            or not (tip.legs[0].player or "").strip()
            or tip.tipster == "shook"
        ):
            continue
        key = (tip.tipster, tip.sport, tip.legs[0].player.strip().lower())
        groups.setdefault(key, []).append(tip)

    # For each group with 2+ tips and at least one differing (stat, line),
    # promote to SGM. Same-stat-same-line repeats fall through to dedupe.
    sgm_promoted: set[int] = set()  # id() of tips absorbed into an SGM
    promoted_sgms: dict[int, ParsedTip] = {}  # id(primary tip) -> new SGM tip

    for group in groups.values():
        if len(group) < 2:
            continue
        # v5.23 (Wilson 2026-06-06): require 2+ DISTINCT STATS to promote to an
        # SGM. Same player + same stat differing only by LINE is a staggered/
        # ladder play of INDEPENDENT singles (AusBets "Fox over 14.5 P / over
        # 15.5 P"), NOT an SGM — the legs are fully correlated, there is no SGM
        # edge, and merging them placed nothing (routed to manual). A real
        # same-player SGM spans DIFFERENT stats (Brunson "points / assists").
        # Same-stat-same-line repeats also fall through here to the dedupe path.
        stats = {(t.legs[0].stat or "").lower() for t in group}
        if len(stats) < 2:
            continue

        primary = group[0]
        # Build new SGM tip on top of the primary's metadata. Copy legs
        # in batch order so the SGM legs match what the tipster wrote.
        primary.is_sgm = True
        primary.legs = [t.legs[0] for t in group]
        # raw_legs is what _resolve_leg_for_hyperbot reads on SGM path;
        # mirror legs into raw_legs so downstream resolution works.
        try:
            from models import ParsedLeg  # for type only
        except Exception:
            pass
        primary.raw_legs = []
        for t in group:
            leg_dict = {
                "player": t.legs[0].player,
                "team": t.legs[0].team_full or "",
                "stat": t.legs[0].stat or "",
                "line": t.legs[0].line,
                "selection": t.legs[0].selection or "",
                "market": t.legs[0].market or "player_prop",
                "is_threshold": bool(getattr(t.legs[0], "_is_threshold", False)),
            }
            primary.raw_legs.append(leg_dict)

        log.warning(
            f"SGM auto-promote: {primary.tipster} '{primary.legs[0].player}' "
            f"appeared as {len(group)} separate tips with different "
            f"stats/lines — promoting to one SGM with "
            f"{len(primary.legs)} legs. Likely Groq misparsed '/' on a "
            f"single line. Legs: "
            f"{[(l.stat, l.line, l.selection) for l in primary.legs]}"
        )

        promoted_sgms[id(primary)] = primary
        for t in group[1:]:
            sgm_promoted.add(id(t))

    if not promoted_sgms and not sgm_promoted:
        return tips

    # Rebuild list in original order, dropping absorbed tips and keeping
    # the primary (now SGM-shaped).
    out: list[ParsedTip] = []
    for tip in tips:
        if id(tip) in sgm_promoted:
            continue
        out.append(tip)
    return out


def _merge_batch_alts(tips: list[ParsedTip]) -> list[ParsedTip]:
    """
    Scan the tips list for same-tipster, same-sport, same-player single-leg
    player-prop duplicates. First occurrence becomes the primary; subsequent
    ones are appended to its alt_lines and removed from the returned list.
    Order is preserved (first tip wins as primary; alts appear in message order).
    SGMs and team bets are never merged.
    """
    if len(tips) < 2:
        return tips

    merged: list[ParsedTip] = []
    primary_by_key: dict[tuple[str, str, str], ParsedTip] = {}

    for tip in tips:
        # Eligibility: single-leg player prop with a player
        if (
            not tip.legs or len(tip.legs) != 1
            or getattr(tip, "is_sgm", False) or tip.alert_only
            or not tip.legs[0].market or "player" not in tip.legs[0].market
            or not (tip.legs[0].player or "").strip()
        ):
            merged.append(tip)
            continue

        player = tip.legs[0].player.strip().lower()
        # Include stat in the key. Two tips for the same player at the same
        # stat are alts of each other (Champagnie 21.5 PR + 18.5 PR good too).
        # Two tips for the same player at DIFFERENT stats are either an SGM
        # that Groq misparsed into singles, or two genuinely independent
        # bets — either way, never alts. Brunson 2026-05-01: u36.5 points
        # + o4.5 assists arrived as two tips, were collapsed under the old
        # (tipster, sport, player) key into primary+alt, and the assists
        # leg was silently dropped when the points leg placed.
        stat = (tip.legs[0].stat or "").strip().lower()
        key = (tip.tipster, tip.sport, player, stat)

        prior = primary_by_key.get(key)
        if prior:
            # Append as alt to prior
            if prior.alt_lines is None:
                prior.alt_lines = []
            prior.alt_lines.append(_make_alt_entry(tip.legs[0]))
            log.info(
                f"Batch-merged alt for {tip.tipster} {tip.legs[0].player}: "
                f"stat={tip.legs[0].stat} line={tip.legs[0].line} "
                f"sel={tip.legs[0].selection}. Primary now has "
                f"{len(prior.alt_lines)} alt(s)."
            )
        else:
            primary_by_key[key] = tip
            merged.append(tip)

    return merged


def _dedupe_shook_same_player(tips: list[ParsedTip]) -> list[ParsedTip]:
    """
    Shook posts multiple option lines per message for the same player
    (e.g. "Holmgren M 17.5 P+A. 16.5 P > 25.5 P+R > 27.5 PRA"). Per
    Wilson 2026-05-07: the first-mentioned line is the preferred play;
    the rest are alternatives the bot must NOT place.

    Keeps the first tip per (sport, player) for shook only; drops
    subsequent same-player tips. Other tipsters pass through untouched.

    Runs AFTER _promote_misparsed_sgms (which now skips Shook) and BEFORE
    _merge_batch_alts (so the dropped tips can't accidentally be merged
    as alt lines — they were never alts of each other, just separate
    options).
    """
    if len(tips) < 2:
        return tips

    seen_keys: set[tuple[str, str]] = set()
    out: list[ParsedTip] = []
    for tip in tips:
        if (
            tip.tipster != "shook"
            or not tip.legs
            or not (tip.legs[0].player or "").strip()
            or getattr(tip, "is_sgm", False)
            or tip.alert_only
        ):
            out.append(tip)
            continue
        key = (tip.sport, tip.legs[0].player.strip().lower())
        if key in seen_keys:
            log.warning(
                f"Shook dedupe: dropping subsequent tip for "
                f"{tip.legs[0].player} (stat={tip.legs[0].stat}, "
                f"line={tip.legs[0].line}). First-mentioned line wins."
            )
            continue
        seen_keys.add(key)
        out.append(tip)
    return out


def _mlb_hrrbi_leg(src: ParsedLeg, line: float) -> ParsedLeg:
    """Build one O/U-over leg of the HRRBI SGM (same player, h_r_rbi, given
    half-line). market=player_stats so _resolve_leg_for_hyperbot + the SGM
    enricher route it to the single MLB player_stats market."""
    return ParsedLeg(
        market="player_stats",
        team_full=src.team_full,
        player=src.player,
        stat="h_r_rbi",
        line=line,
        selection="over",
        raw_text=src.raw_text,
    )


def _apply_mlb_flat_stake(tip: ParsedTip) -> None:
    """Force an MLB tip to a FLAT dollar stake (MLB_FLAT_STAKE), ignoring the
    tipster's recommended unit.

    Wilson 2026-06-01: MLB is bet at a flat stake no matter what unit Shook
    recommends (his 0.2u/0.25u/0.4u are ignored for MLB). The same knob is the
    validation gate: it's set to $1 for the gated live test, then raised to the
    production value (e.g. $400) once the pipeline is validated. The clamp lives
    in the PLACING process (called from _process_tip, loaded on restart) so an
    .env edit alone can't change the live stake without a restart — the $600
    lesson (2026-06-01: a "$1-capped" test placed a real $600 bet because
    main.py wasn't restarted). Forcing units=1.0 + unit_size=MLB_FLAT_STAKE
    makes stake_dollars == MLB_FLAT_STAKE exactly; downstream liability caps
    only reduce stake further, never raise it. No-op for non-MLB or when
    MLB_FLAT_STAKE <= 0 (then MLB sizes like any Shook tip). Mutates `tip`."""
    if (tip.sport or "").lower() != "mlb" or MLB_FLAT_STAKE <= 0:
        return
    # v5.9x (07-12 review #4): a self-bet carries Wilson's EXPLICIT dollar figure;
    # never clobber it to the Shook-sized MLB_FLAT_STAKE. (MLB self-bets also route
    # to manual in _process_self_bet, so this is belt-and-braces.)
    if tip.tipster == "self_bet":
        return
    flat = round(float(MLB_FLAT_STAKE), 2)
    if abs(tip.stake_dollars - flat) > 0.001:
        log.warning(
            f"MLB flat stake: {tip.tipster} {tip.units}u x ${tip.unit_size} "
            f"= ${tip.stake_dollars} -> forcing flat ${flat} "
            f"(MLB ignores recommended unit; change MLB_FLAT_STAKE + restart)"
        )
    tip.units = 1.0
    tip.unit_size = flat


def _apply_dello_flat_stake(tip: ParsedTip) -> None:
    """Force a Dello tip to a FLAT dollar stake, ignoring Dello's own unit sizing
    (his 0.05u-2.5u are ignored — Wilson flat-stakes). While DELLO_TEST_MODE the
    flat stake is DELLO_TEST_UNIT_SIZE ($1) for the gated live test; flip
    DELLO_TEST_MODE off (+ restart) to place at DELLO_UNIT_SIZE ($400). Like
    _apply_mlb_flat_stake the clamp lives in the PLACING process so an .env edit
    needs a restart to change the live stake (the $600 lesson). units=1.0 +
    unit_size=flat makes stake_dollars == flat exactly; downstream caps only
    reduce. No-op for non-Dello. Mutates `tip`."""
    if tip.tipster != "dello_afl":
        return
    flat = round(float(DELLO_TEST_UNIT_SIZE if DELLO_TEST_MODE else DELLO_UNIT_SIZE), 2)
    if flat <= 0:
        return
    if abs(tip.stake_dollars - flat) > 0.001:
        log.warning(
            f"Dello flat stake: {tip.tipster} {tip.units}u x ${tip.unit_size} "
            f"= ${tip.stake_dollars} -> forcing flat ${flat} "
            f"(DELLO_TEST_MODE={DELLO_TEST_MODE}; change DELLO_TEST_MODE/"
            f"DELLO_UNIT_SIZE + restart for production stake)"
        )
    tip.units = 1.0
    tip.unit_size = flat


# ── DisposalsModel: persisted per-selection exposure ledger (v6.08) ──
# WHY THIS EXISTS. The feed re-offers a tranche with a NEW `key` each tick, so every
# in-memory dedup layer in this file lets it through BY DESIGN (they fingerprint
# content in a 600s window; the retry arrives hours later with different content).
# The upstream contract assumed Sportsbet's own liability limit would throttle the
# repeat. It does not: at $500/7 = $71.43 a share against a floor(124/0.80) = $155
# per-account stake ceiling there is 2.2x headroom, so a re-offer FILLS AGAIN and the
# selection ends up at ~2x its intended size, every placement reporting clean success.
#
# So tipbot tracks it. Keyed on the SELECTION (evt, pid, line, side) rather than the
# message key, because the whole point is to cap what accumulates ACROSS keys:
#   committed  = dollars we believe are on (placed + ambiguous)
#   target     = the largest tranche target seen for this selection
#   keys       = every message key already processed (permanent, restart-surviving)
# A repeat key is an upstream bug -> refuse + critical alert, never a second bet.
_DISPOSALS_STATE_LOCK = threading.Lock()


def _disposals_state_path() -> str:
    """Resolved state path.

    Read from the module global on EVERY call (never cached) so a test can
    monkeypatch DISPOSALS_MODEL_STATE_PATH and be certain it is not writing to the
    live ledger. There is no automatic TIPBOT_TESTING redirect here — tests must set
    the path explicitly, which test_disposals_model.py's `state` fixture does."""
    return DISPOSALS_MODEL_STATE_PATH


def _disposals_load_state() -> dict:
    """Load the exposure ledger. A MISSING file is a clean first run; a CORRUPT one
    is NOT — returning {} there would silently reset every cap and re-admit every
    key, so it is surfaced as corrupt and the caller refuses to place."""
    path = _disposals_state_path()
    if not os.path.exists(path):
        return {"selections": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("selections"), dict):
            raise ValueError("unexpected shape")
        return data
    except Exception as e:
        log.error(f"[disposals_model] state file unreadable ({e}) — FAILING CLOSED")
        return {"selections": {}, "state_corrupt": True}


def _disposals_save_state(state: dict) -> bool:
    """Atomic tmp+replace so a kill mid-write cannot leave truncated JSON (which
    would read back as corrupt and, before the fail-closed above, reset the caps).

    Returns True on success. The CALLER MUST CHECK: a swallowed write failure at
    claim time means the key + reservation were never persisted, so the very next
    offer would be admitted again and double-stake. Fails CLOSED there."""
    path = _disposals_state_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error(f"[disposals_model] failed to save state: {e}")
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def _disposals_committed(row: dict) -> float:
    """Read-only: dollars already committed for this selection. Used so the fills
    file reports a REAL `selection_committed` on the paths that place nothing —
    that field is what the model computes its shortfall from, so writing 0.0 there
    would tell it the whole tranche is still owed."""
    try:
        with _DISPOSALS_STATE_LOCK:
            state = _disposals_load_state()
            sel = state.get("selections", {}).get(_disposals_selection_id(row)) or {}
            return round(float(sel.get("committed") or 0), 2)
    except Exception:
        return 0.0


def _disposals_selection_id(row: dict) -> str:
    """Selection identity: the bet itself, independent of which message carried it.

    NORMALISED, because every cap in the ledger is keyed off this string: an emitter
    that changes `CD_I1001` to `cd_i1001`, pads whitespace, or formats the line as
    `24.50` instead of `24.5` would otherwise mint a BRAND NEW selection with a fresh
    full allowance and walk straight past the exposure cap and the per-fixture cap.
    Case-folded, stripped, and the line canonicalised through float()."""
    def _t(v) -> str:
        return str(v or "").strip().casefold()

    try:
        line = f"{float(row.get('line')):g}"
    except (TypeError, ValueError):
        line = _t(row.get("line"))
    return f"{_t(row.get('evt'))}~{_t(row.get('pid'))}~{line}~{_t(row.get('side'))}"


def _disposals_sweep_sessions() -> list:
    """The SESSION DICTS to sweep for outside exposure: every OWNED Sportsbet session
    carrying an afl.player_disposals liability cap. Returns [] when the scope cannot be
    determined, which every caller must treat as UNKNOWN rather than "no exposure".

    ONE helper for both callers (the claim-time check and the hourly export). They each
    used to fetch and filter separately, and only one of them used the fail-closed
    get_sessions_or_none — a test caught the divergence.

    DERIVED from sessions.yaml rather than hardcoding the seven contract ids, so adding
    or retiring an account cannot leave this list silently stale — the orphan-id class
    sessions.yaml's own header calls 'the 100004 mistake'."""
    out = []
    for sid, s in _disposals_priority_sessions().items():
        out.append(s)
    return out


def _disposals_priority_sessions() -> dict:
    """{session_id: session_dict} for the sweep scope. See _disposals_sweep_sessions."""
    out = {}
    try:
        # get_sessions_or_none, NOT get_sessions: the latter collapses an API failure to
        # [] , and an empty sweep set reads as "no exposure" which is the dangerous
        # direction (2026-05-01: that same collapse made the watchdog fire 9 criticals).
        rows = hb.get_sessions_or_none()
        if rows is None:
            log.warning("[disposals_model] session list unavailable (API failure) — "
                        "sweep scope UNKNOWN, not empty")
            return out
        for s in rows:
            if str(s.get("bookie", "")).lower() != "sportsbet":
                continue
            sid = str(s.get("session_id") or "")
            # _is_owned_session keeps out other PCs sharing the HyperBot key; the cap
            # lookup then narrows to accounts that actually carry a disposals ladder.
            # Both are DERIVED from sessions.yaml, so retiring an account cannot leave
            # this stale (the "100004 mistake" class).
            if not sid or not _is_owned_session(sid):
                continue
            if session_priority.lookup_liability_cap(sid, "afl", "player_disposals") is not None:
                out[sid] = s
    except Exception as e:
        log.error(f"[disposals_model] could not resolve sweep sessions: {e}")
    return out


def _disposals_match_book_player(model_name: str, book_name: str) -> str:
    """Compare the model's canonical spelling to Sportsbet's, for the exposure join.

    Returns "yes" / "no" / "unknown". "unknown" is NOT "no": the caller must treat it
    as unmeasurable exposure and fail closed, because this join's entire job is
    preventing a double-stake and a missed match is the expensive direction.

    Deliberately NOT using roster.fuzzy_match_player: v6.07 shipped four wrong-player
    regressions through it, and a wrong match here would mis-attribute money either way
    (inventing exposure blocks a good bet; missing it permits a double one). So: exact
    casefold match, else surname + first-initial agreement, else UNKNOWN on a shared
    surname, else no.
    """
    a = " ".join(str(model_name or "").split()).casefold()
    b = " ".join(str(book_name or "").split()).casefold()
    if not a or not b:
        return "unknown"
    if a == b:
        return "yes"
    at, bt = a.split(), b.split()
    if at[-1] != bt[-1]:
        return "no"                      # different surname -> different player
    if at[0] == bt[0]:
        return "yes"
    if at[0][:1] == bt[0][:1]:
        # Same surname, same initial, different given name: "Ollie"/"Oliver" (the
        # exact case the handover flagged). Treat as the SAME player.
        return "yes"
    # Same surname, different initial -> could be two real players (the Daicos case).
    return "unknown"


def _disposals_outside_exposure(row: dict):
    """Money already ON this selection from ANY source, read from the book.

    Returns (total, accounts, status) where status is "ok" / "unknown". The v6.08
    exposure ledger only knows THIS tipster's own placements, so it cannot see another
    tipster's fan-out or a hand-placed bet; a 2026-08-02 sweep found $4,049.60 of live
    disposals props across 5 accounts, including $574.50 on one selection, none of it
    visible to the ledger. This closes that.

    status="unknown" on ANY API failure or an ambiguous player join. The caller must
    NOT read that as zero: a failed sweep reading as zero re-arms the double-stake.
    """
    if not DISPOSALS_MODEL_RECONCILE_ENABLED:
        return 0.0, [], "disabled"
    try:
        import reconcile  # imported locally, as every other reconcile caller here does
        sessions = _disposals_sweep_sessions()
        if not sessions:
            return 0.0, [], "unknown"
        exposure, errors = reconcile.collect_prop_exposure(hb, sessions, stat="disposals")
    except Exception as e:
        log.error(f"[disposals_model] exposure sweep failed: {e}")
        return 0.0, [], "unknown"
    if errors:
        log.warning(f"[disposals_model] exposure sweep had {len(errors)} error(s): "
                    f"{errors[:3]} — treating exposure as UNKNOWN, not zero")
        return 0.0, [], "unknown"

    want_line = float(row["line"])
    want_side = str(row["side"]).casefold()
    total, accounts, ambiguous = 0.0, [], False
    for (bp, bline, bside), v in exposure.items():
        if bside != want_side or abs(float(bline) - want_line) > 0.001:
            continue
        verdict = _disposals_match_book_player(row.get("player", ""), v.get("player", ""))
        if verdict == "yes":
            total = round(total + float(v["total"]), 2)
            accounts.extend(v["accounts"])
        elif verdict == "unknown":
            ambiguous = True
            log.warning(
                f"[disposals_model] AMBIGUOUS player join on the exposure sweep: model "
                f"{row.get('player')!r} vs book {v.get('player')!r} at {want_side} "
                f"{want_line} (${v['total']:.2f}) — treating as UNKNOWN")
    if ambiguous:
        return total, accounts, "unknown"
    return total, accounts, "ok"


def _disposals_claim(row: dict, outside: float = 0.0,
                     outside_status: str = "disabled"):
    """Admit or refuse this message, and return how much of it may be placed.

    Returns (allowed_stake, reason). allowed_stake == 0.0 means DO NOT PLACE and
    `reason` explains why (the caller alerts). Claims are written BEFORE placement
    so a crash mid-flight cannot re-admit the same key on restart.
    """
    sel_id = _disposals_selection_id(row)
    key = row["key"]
    requested = float(row["stake"])
    target = float(row["target"])
    with _DISPOSALS_STATE_LOCK:
        state = _disposals_load_state()
        if state.get("state_corrupt"):
            return 0.0, ("exposure ledger is CORRUPT — refusing to place until it is "
                         "repaired (placing blind risks double-staking every open "
                         "selection)")
        sel = state["selections"].setdefault(
            sel_id, {"committed": 0.0, "ambiguous": 0.0, "target": 0.0,
                     "keys": [], "manual": False, "player": row.get("player", "")})

        if key in sel["keys"]:
            return 0.0, (f"repeat key {key!r} — the feed guarantees keys are unique, "
                         f"so this is an UPSTREAM BUG. Dropped, not placed.")

        if sel.get("manual"):
            return 0.0, (f"selection {sel_id} previously routed to MANUAL — refusing "
                         f"to auto-place a further offer against a bet that may have "
                         f"been placed by hand")
        if float(sel.get("ambiguous") or 0) > 0:
            return 0.0, (f"selection {sel_id} has ${sel['ambiguous']:.2f} in an "
                         f"AMBIGUOUS state (may have landed at the bookie after we "
                         f"stopped waiting) — refusing a further offer (double-stake)")

        if row["kind"] == "retry" and not DISPOSALS_MODEL_RETRY_ENABLED:
            return 0.0, "kind=retry and DISPOSALS_MODEL_RETRY_ENABLED=false"

        # Per-fixture player cap. The bound is CORRELATION, not turnover: every bet is
        # an under, so N unders in one match behave close to one bet at N x the size
        # and a high-possession blowout takes them together. The feed enforces this
        # upstream; this is tipbot's independent backstop, because trusting an
        # external system for a money bound is how you find out it regressed.
        # Counts DISTINCT pids already carrying money in this fixture; a further
        # tranche on a player already backed is free (it is the same correlated
        # position, not a new one).
        _cap = int(DISPOSALS_MODEL_MAX_PLAYERS_PER_EVENT or 0)
        _this_live = (float(sel.get("committed") or 0)
                      + float(sel.get("inflight") or 0))
        if _cap > 0 and _this_live <= 0:
            # Normalise the prefix the SAME way _disposals_selection_id does, or the
            # startswith below never matches a stored id and the cap silently stops
            # firing (caught by test_fifth_player_in_one_fixture_is_refused).
            _evt_prefix = f"{str(row.get('evt') or '').strip().casefold()}~"
            _this_pid = str(row.get("pid") or "").strip().casefold()
            # Count INFLIGHT as backed too, else a same-tick burst of five messages
            # for one fixture all pass the cap before any of them commits.
            _backed = {
                sid.split("~")[1]
                for sid, s in state["selections"].items()
                if sid.startswith(_evt_prefix) and len(sid.split("~")) > 1
                and (float(s.get("committed") or 0) + float(s.get("inflight") or 0)) > 0
            }
            if _this_pid not in _backed and len(_backed) >= _cap:
                return 0.0, (
                    f"fixture {row.get('evt', '')} already has {len(_backed)} distinct "
                    f"players backed (cap {_cap}) — refusing a {len(_backed) + 1}th. "
                    f"The model enforces the same bound upstream (ledger.plan, "
                    f"MAX_PLAYERS_PER_EVENT=4), so this firing means either that cap "
                    f"regressed or money reached the selection from elsewhere."
                )

        # Cap against BOTH the tranche target the feed declared and tipbot's own
        # independent per-selection ceiling, so a runaway emitter cannot walk a
        # selection up by inflating `target`.
        sel["target"] = max(float(sel.get("target") or 0), target)
        ceiling = min(sel["target"], float(DISPOSALS_MODEL_MAX_SELECTION_STAKE))
        # RESERVE, don't just check. `inflight` is stake claimed by an offer that is
        # currently being placed but has not committed yet. place_tip runs in an
        # executor, so a second message for the SAME selection is free to claim while
        # the first is still in the air; without counting inflight, both would see the
        # full headroom and both would place it (verified: two claims each returned
        # $500 against a $500 target = $1000 down). This is the v6.03
        # claim-before-place race in a new place.
        inflight = float(sel.get("inflight") or 0)
        # v6.08b: OUTSIDE money (another tipster's fan-out, or a hand-placed bet) counts
        # against the ceiling in "block" mode. In "alert" mode it is recorded and
        # alerted but does not reduce the stake, preserving the handover's relaxed
        # stance on cross-tipster collisions while making them visible.
        _outside = 0.0
        if DISPOSALS_MODEL_RECONCILE_ENABLED and DISPOSALS_MODEL_RECONCILE_MODE == "block":
            if outside_status == "unknown":
                return 0.0, (
                    "the book-exposure sweep FAILED or produced an ambiguous player "
                    "match, so how much is already on this selection is UNKNOWN — "
                    "refusing (reading a failed sweep as $0 is how you double-stake)")
            _outside = round(float(outside or 0), 2)
        headroom = round(ceiling - float(sel.get("committed") or 0) - inflight - _outside, 2)
        if _outside > 0:
            log.info(
                f"[disposals_model] {sel_id}: ${_outside:.2f} already ON from OUTSIDE "
                f"this tipster (book sweep) — counted against the ${ceiling:.2f} ceiling")
        if headroom <= 0:
            return 0.0, (f"selection {sel_id} already has "
                         f"${sel['committed']:.2f} committed"
                         + (f" + ${inflight:.2f} in flight" if inflight > 0 else "")
                         + f" of a ${ceiling:.2f} ceiling — nothing left to place")
        allowed = round(min(requested, headroom), 2)
        # Claim + reserve NOW, before placement, so neither a crash nor a concurrent
        # offer can re-admit this money. Released in _disposals_commit.
        sel["keys"].append(key)
        sel["inflight"] = round(inflight + allowed, 2)
        if not _disposals_save_state(state):
            # FAIL CLOSED. If the claim did not persist, the key and the reservation
            # do not exist as far as the next offer is concerned, so placing now
            # would let the very next message re-place the same money.
            return 0.0, ("could not persist the exposure ledger — refusing to place "
                         "(an unpersisted claim would let the next offer double-stake)")
        if allowed < requested:
            log.info(
                f"[disposals_model] {sel_id}: requested ${requested:.2f} but only "
                f"${allowed:.2f} of headroom (committed ${sel['committed']:.2f} of "
                f"${ceiling:.2f}) — placing the remainder only"
            )
        return allowed, ""


def _disposals_commit(row: dict, placed: float, ambiguous: float,
                      manual: bool, reserved: float = 0.0) -> float:
    """Record the outcome and RELEASE the claim's reservation. Returns the
    selection's new cumulative committed total.

    `reserved` is what _disposals_claim put into `inflight` for this offer; it is
    released here whatever the outcome, and replaced by the amount that actually
    went on. MUST be called even when placement raised, or the reservation leaks and
    permanently blocks the selection (fail-safe, but it would silently stop topping
    up) — the caller uses try/finally for exactly that reason.

    `ambiguous` dollars count toward committed: a maybe-landed bet must be treated as
    ON for exposure purposes, never re-offered against (Guard B's reasoning, v6.03)."""
    sel_id = _disposals_selection_id(row)
    with _DISPOSALS_STATE_LOCK:
        state = _disposals_load_state()
        if state.get("state_corrupt"):
            log.error(f"[disposals_model] cannot commit {sel_id}: state corrupt")
            return 0.0
        sel = state["selections"].setdefault(
            sel_id, {"committed": 0.0, "ambiguous": 0.0, "target": 0.0,
                     "keys": [], "manual": False, "player": row.get("player", "")})
        if reserved:
            sel["inflight"] = round(
                max(0.0, float(sel.get("inflight") or 0) - float(reserved)), 2)
        sel["committed"] = round(float(sel.get("committed") or 0)
                                 + float(placed or 0) + float(ambiguous or 0), 2)
        sel["ambiguous"] = round(float(sel.get("ambiguous") or 0)
                                 + float(ambiguous or 0), 2)
        if manual:
            sel["manual"] = True
        _disposals_save_state(state)
        return sel["committed"]


def _disposals_write_fill(row: dict, *, requested: float, placed: float,
                          ambiguous: float, manual: bool, committed: float,
                          accounts: list, outcome: str, event: str = "") -> None:
    """Append one record to the fills file the MODEL reads back (contract §4).

    This is the fill-feedback loop that lets the model's own ledger populate
    Tranche.filled and emit a retry for the SHORTFALL instead of the full stake.
    Keyed by the model's own `key` so the join is exact rather than fuzzy. Guarded:
    a logging failure must never break placement."""
    try:
        rec = {
            "key": row.get("key", ""),
            "evt": row.get("evt", ""),
            "pid": row.get("pid", ""),
            "player": row.get("player", ""),
            "team": row.get("team", ""),
            "event": event,
            "market": row.get("market", ""),
            "side": row.get("side", ""),
            "line": row.get("line"),
            "kind": row.get("kind", ""),
            "target": round(float(row.get("target") or 0), 2),
            "requested": round(float(requested or 0), 2),
            "placed": round(float(placed or 0), 2),
            "unfilled": round(float(requested or 0) - float(placed or 0), 2),
            "selection_committed": round(float(committed or 0), 2),
            "accounts": accounts,
            "outcome": outcome,
            "ambiguous": round(float(ambiguous or 0), 2),
            "manual": bool(manual),
            "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        path = DISPOSALS_MODEL_FILLS_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"[disposals_model] failed to append fill record: {e}")


def _disposals_book_to_canonical(book_name: str):
    """Best-effort AFL canonical spelling for a Sportsbet player name, or None.

    DETERMINISTIC, not fuzzy: takes the surname, asks the roster for every player with
    that surname, and returns a canonical name ONLY when exactly one candidate survives
    a first-initial check. Anything ambiguous returns None, and None is emitted to the
    model as an explicit null rather than a plausible guess — a plausible guess is
    precisely what a fuzzy matcher produces on the day it is wrong, and this feeds a
    double-stake guard. roster.fuzzy_match_player is deliberately NOT used (four
    wrong-player regressions in v6.07)."""
    toks = " ".join(str(book_name or "").split()).split()
    if len(toks) < 2:
        return None
    try:
        cands = afl_surname_candidates(toks[-1]) or []
    except Exception:
        return None
    if not cands:
        return None
    init = toks[0][:1].casefold()
    hits = [c for c in cands
            if str(c.get("name", "")).strip()[:1].casefold() == init]
    if len(hits) != 1:
        return None
    return str(hits[0].get("name") or "").strip() or None


def _disposals_emit_reconciliation() -> tuple:
    """Sweep the book and append one `type: "reconciliation"` SNAPSHOT record per
    disposals selection carrying money, for the MODEL to read.

    Returns (n_selections, total_dollars, status).

    SNAPSHOT, never a delta: each record is the complete current picture for that
    selection, so a later record REPLACES the earlier one on the model's side. Summing
    successive sweeps would inflate exposure on every poll and progressively suppress
    real bets. Agreed explicitly with the model side.

    These records carry NO `key` — a bet placed outside tipbot never had a message — so
    they are joined on (player, line, side, event). The model's loader accepts both
    shapes; it originally filtered on `key` and would have discarded every one of these
    silently, which is why the shape is spelled out here.

    Deliberately gated on DISPOSALS_MODEL_RECONCILE_ENABLED and NOT on
    DISPOSALS_MODEL_ENABLED: the tipster ships dormant, but the model has live tranches
    and hand-placed money to reconcile RIGHT NOW, so the export must flow while the
    placement side is still switched off."""
    if not DISPOSALS_MODEL_RECONCILE_ENABLED:
        return 0, 0.0, "disabled"
    try:
        import reconcile  # imported locally, as every other reconcile caller here does
        sessions = _disposals_sweep_sessions()
        if not sessions:
            log.warning("[disposals_model] reconciliation sweep: no eligible sessions "
                        "(treated as UNKNOWN, not as zero exposure)")
            return 0, 0.0, "unknown"
        exposure, errors = reconcile.collect_prop_exposure(hb, sessions, stat="disposals")
    except Exception as e:
        log.error(f"[disposals_model] reconciliation sweep crashed: {e}")
        return 0, 0.0, "unknown"

    status = "unknown" if errors else "ok"
    if errors:
        log.warning(f"[disposals_model] reconciliation sweep: {len(errors)} error(s) "
                    f"{errors[:3]} — records marked status=unknown (NOT zero)")
    observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    total = 0.0
    for (_pl, line, side), v in sorted(exposure.items(), key=lambda kv: -kv[1]["total"]):
        book_player = v.get("player", "")
        rec = {
            "type": "reconciliation",
            "source": "pending_bets",
            "status": status,
            "semantics": "snapshot",   # explicit: REPLACES any earlier record, never sums
            "player_book": book_player,
            "player": _disposals_book_to_canonical(book_player),  # null when ambiguous
            "line": line,
            "side": side,
            "market": "player_disposals",
            "events": v.get("events", []),
            "stake_total": round(float(v["total"]), 2),
            "accounts": v.get("accounts", []),
            "observed_at": observed_at,
        }
        total = round(total + rec["stake_total"], 2)
        try:
            path = DISPOSALS_MODEL_FILLS_PATH
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"[disposals_model] failed to append reconciliation record: {e}")
    # v6.08f: ALWAYS emit one sweep record, even for a zero-selection sweep.
    # The per-selection records above are written one PER SELECTION, so a sweep that finds
    # nothing writes nothing at all — and then the model cannot distinguish "the sweep ran
    # and the book is empty" from "the sweep never ran". That is the same silence-vs-
    # evidence gap this whole exchange has been closing (it is why they send a NOOP
    # heartbeat, and why absence of feedback has to mean "assume it is on"). Observed live:
    # the first nine sweeps after enabling all found 0 selections, so the model would have
    # seen an empty file and had no way to tell which case it was in.
    try:
        _sweep = {
            "type": "reconciliation_sweep",
            "source": "pending_bets",
            "status": status,
            "semantics": "snapshot",
            "market": "player_disposals",
            "selections": len(exposure),
            "stake_total": round(total, 2),
            "observed_at": observed_at,
        }
        path = DISPOSALS_MODEL_FILLS_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_sweep, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"[disposals_model] failed to append the sweep record: {e}")
    log.info(
        f"[disposals_model] reconciliation snapshot: {len(exposure)} disposals "
        f"selection(s), ${total:,.2f} on the book, status={status}"
    )
    return len(exposure), total, status


async def _disposals_reconciliation_loop():
    """Hourly book-reconciliation sweep + export. Skips the 23:00-07:00 blackout, where
    every session is intentionally down and a sweep would only produce status=unknown
    noise. Exception-proof: this must never be able to take down the loop."""
    if not DISPOSALS_MODEL_RECONCILE_ENABLED:
        log.info("[disposals_model] reconciliation loop disabled")
        return
    log.info("[disposals_model] reconciliation loop started (hourly, skips the "
             f"{DISPOSALS_MODEL_QUIET_START_HOUR}:00-{DISPOSALS_MODEL_QUIET_END_HOUR}:00 blackout)")
    while True:
        try:
            if _disposals_in_quiet_window():
                log.debug("[disposals_model] reconciliation: inside the blackout, skipping")
            else:
                await _run_in_placement_executor(_disposals_emit_reconciliation)
        except Exception as e:
            log.error(f"[disposals_model] reconciliation loop error: {e}")
        await asyncio.sleep(3600)


# ── v6.08e NOOP LIVENESS DETECTOR ───────────────────────────────────────────────
# The model sends `NOOP|v2|ts=<iso>|seq=<n>` on a tick that produced no bets. Three
# multi-hour "alive but silent" outages in this repo were all found late because
# silence is ambiguous; a positive heartbeat makes it unambiguous.
#
# THE ONE RULE THIS MUST NOT BREAK (v6.07, the X-watcher): a heartbeat stamped BEFORE
# the work measures loop spin, not progress. That stamp stayed fresh for hours while
# the watcher was blind. So the stamp here happens ONLY when a real message from the
# feed's locked sender is actually in hand — it is not a timer and cannot be reached
# without a receive.
#
# WHAT COUNTS AS EXPECTED SILENCE, read off the model's own scheduler and run log on
# 2026-08-03 rather than assumed. This is the part that decides whether the detector is
# useful or just a pager:
#   * The Windows task fires HOURLY, 24/7 (live registration: Interval PT1H, Duration
#     P1D, StartBoundary 00:00; last run 05:00, next 06:00, 0 missed). Note the repo's
#     own install_tip_schedule.ps1 would register 07:00-21:00 instead, so re-running
#     that installer would silently delete every overnight tick.
#   * But the heartbeat is NOT emitted on every tick, despite noop_line's docstring
#     saying so. FOUR silent no-output paths reach no heartbeat at all: three early
#     exits in tip_schedule.py (outside the Thu 07:00 - Sun 21:00 Melbourne block, no
#     upcoming match, next bounce > LOOKAHEAD_HOURS=24h) plus `if found.empty` inside
#     send_tips_telegram.py. A tick that sends a WATCH block skips it too.
#   * Measured consequence: Sun 2026-08-02 17:00, 18:00, 19:00 and 20:00 were all
#     inside the block and all silent (next bounce 98.5h away), right after an
#     afternoon of watch messages.
# So "inside the active window" does NOT imply "a heartbeat is due". Two mitigations
# below: the BURST rule (never expect a message before the feed's first of the window)
# and an honest DISPOSALS_MODEL_SILENCE_HOURS default of 24h. The real fix is upstream:
# heartbeat on all four paths, then the threshold drops to 3h. Raised with them.
_DISPOSALS_LIVENESS_LOCK = threading.Lock()

# One-slot cap on the deferred gap report, so an overnight full of gaps cannot grow the
# state file without bound: later gaps accumulate into the counters, not into a list.
_LIVENESS_KINDS = ("noop", "bet", "other")

# IN-MEMORY MIRROR OF THE ALERT LATCH, and the reason it exists.
#
# The throttle that makes one outage one alert was latched ONLY in the state file, and
# _disposals_save_liveness swallows every write error and returns a bool nobody reads. So
# if the file became unwritable (a Windows AV/backup lock on os.replace, or a full disk)
# after having been written once, every poll re-loaded a state with no latch, re-decided
# that the feed was silent, and re-sent the SAME alert: ~64 identical pages a day at the
# 900s poll, in the detector whose entire design goal is not to cry wolf. Worse, the same
# failure freezes `last_seen`, so the repeated alert is false as well as duplicated.
#
# Mirroring the latch in memory closes it: the file is still the source of truth ACROSS a
# restart (which is what the persistence was for), and the process-local copy covers the
# case the file cannot be written. Either one being set suppresses a repeat.
_DISPOSALS_LIVENESS_MEM = {"silence_alerted_for": None, "cleared_latch": None,
                           "flushed_gap": None}


def _disposals_liveness_path() -> str:
    """Read from the module global on EVERY call (never cached) so a test can
    monkeypatch it, exactly as _disposals_state_path does."""
    return DISPOSALS_MODEL_LIVENESS_PATH


def _disposals_load_liveness() -> dict:
    """Load the liveness state. FAILS OPEN, and that is deliberate.

    The exposure ledger next door fails CLOSED on a corrupt file because losing it
    means losing the caps that stop a double-stake. This file holds no money state at
    all — the worst a lost one costs is a missed liveness sample and one restart's
    worth of blindness. Failing closed here would mean a corrupt file PAGES, which is
    the opposite of what an anti-noise detector should do. So a missing or corrupt
    file both read as a clean slate."""
    path = _disposals_liveness_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("unexpected shape")
        return data
    except Exception as e:
        log.warning(f"[disposals_model] liveness state unreadable ({e}) — starting "
                    f"from a clean slate (this file carries no money state)")
        return {}


def _disposals_save_liveness(state: dict) -> bool:
    """Atomic tmp+replace, same as the exposure ledger. A failure is logged and
    swallowed: it must never be able to stop a message being processed."""
    path = _disposals_liveness_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error(f"[disposals_model] failed to save liveness state: {e}")
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def _disposals_active_window_start(now=None):
    """The datetime the CURRENT active window opened, or None if we are outside it.

    Mirrors the model's own in_match_block (scripts/tip_schedule.py): continuous from
    Thursday 07:00 to Sunday 21:00 local, INCLUDING the intervening nights. Returning
    the window's START (not just a bool) is what lets the burst rule ask "has the feed
    spoken since this window opened?".

    Local naive arithmetic, matching main.py everywhere else. tipbot and the model run
    on the SAME machine, so local == the model's Australia/Melbourne. The only cost is
    that a DST changeover inside a window shifts its end by an hour, twice a year,
    which is immaterial for a liveness boundary.

    Start day == end day AND start hour == end hour disables the gating entirely
    (always active), mirroring how the quiet-window knobs treat start == end."""
    now = now or datetime.now()
    sd = int(DISPOSALS_MODEL_ACTIVE_START_DAY) % 7
    sh = int(DISPOSALS_MODEL_ACTIVE_START_HOUR)
    ed = int(DISPOSALS_MODEL_ACTIVE_END_DAY) % 7
    eh = int(DISPOSALS_MODEL_ACTIVE_END_HOUR)
    duration_h = ((ed - sd) % 7) * 24 + (eh - sh)
    if duration_h <= 0:
        # Degenerate/disabled window: treat the feed as ALWAYS active. Anchoring to the
        # top of the current hour looked reasonable and did the opposite of what it says:
        # the burst rule is `last_seen < window_start`, so a window that starts an hour ago
        # suppresses every silence longer than an hour, i.e. all of them. Setting the knobs
        # to "no gating" switched the detector OFF instead of making it maximally
        # sensitive, silently. Anchor far in the past so the burst rule can never suppress.
        return now - _timedelta(days=3650)
    start = (now - _timedelta(days=(now.weekday() - sd) % 7)).replace(
        hour=sh, minute=0, second=0, microsecond=0)
    if start > now:
        start -= _timedelta(days=7)
    return start if now < start + _timedelta(hours=duration_h) else None


def _disposals_note_feed_message(text: str, now=None) -> list:
    """Stamp a SUCCESSFUL receive from the feed, and read the heartbeat if present.

    Called for EVERY message that clears the channel's locked-sender check — a bet, a
    watch list, a NOOP, or plain chatter. All of them are proof the model is alive and
    that telethon is delivering, which is exactly what is being measured.

    Returns a list of (level, text) alerts for the caller to send. Returning them
    rather than sending inline keeps this function pure enough to unit-test and keeps
    Telegram I/O out of the message-dispatch path."""
    now = now or datetime.now()
    alerts = []
    noop = None
    try:
        if disposals_model_parser is not None:
            noop = disposals_model_parser.parse_noop(text)
    except Exception as e:  # a broken heartbeat must never block a real bet
        log.warning(f"[disposals_model] NOOP parse failed: {e}")

    with _DISPOSALS_LIVENESS_LOCK:
        state = _disposals_load_liveness()
        kind = "noop" if noop else ("bet" if (text and "BET|" in text) else "other")

        # RECOVERY, reported once: only if we actually told anyone it was silent.
        #
        # The in-process view wins while the process lives, and the file only seeds it at
        # startup. That ordering matters when writes are failing: clearing the latch on disk
        # can fail silently, and reading the disk first would then re-announce "feed is
        # BACK" on every subsequent message. `cleared_latch` remembers which latch value
        # this process has already reported on, so a stale one on disk is ignored.
        _mem_latch = _DISPOSALS_LIVENESS_MEM.get("silence_alerted_for")
        latched = _mem_latch if _mem_latch is not None else state.get("silence_alerted_for")
        if latched is not None and latched != _DISPOSALS_LIVENESS_MEM.get("cleared_latch"):
            silent_since = state.get("last_seen_iso") or "?"
            alerts.append((
                "info",
                f"DisposalsModel feed is BACK. First message since {silent_since} "
                f"(a {kind}). The silence alert is cleared."))
            state["silence_alerted_for"] = None
            _DISPOSALS_LIVENESS_MEM["silence_alerted_for"] = None
            _DISPOSALS_LIVENESS_MEM["cleared_latch"] = latched

        # A LINE THAT LOOKS LIKE A HEARTBEAT BUT DID NOT PARSE. This is the one case the
        # silence timer genuinely cannot cover for, because the unreadable message itself
        # refreshes last_seen: the feed looks alive while seq-gap detection is dead. Say so
        # ONCE per distinct shape, not once per message, and clear the latch when a
        # heartbeat reads cleanly again.
        if noop is None and text and disposals_model_parser is not None \
                and getattr(disposals_model_parser, "NOOP_PREFIX", "NOOP|") in text:
            offending = next((ln.strip() for ln in text.splitlines() if "NOOP|" in ln), "")[:80]
            # Latch on the SHAPE, not the text: every heartbeat carries a different seq and
            # ts, so latching on the raw line would alert on every single message, which is
            # the storm this is meant to prevent. Digits collapse to '#', so a changed field
            # layout is a new shape and re-alerts, while a normal counter tick does not.
            shape = re.sub(r"\d+", "#", offending)
            log.warning(f"[disposals_model] a NOOP| line did not parse: {offending!r} — "
                        f"seq-gap detection is blind until the shape is readable")
            if state.get("noop_unparsed_alerted") != shape:
                state["noop_unparsed_alerted"] = shape
                alerts.append((
                    "info",
                    f"DisposalsModel heartbeat is UNREADABLE, so seq-gap detection is off: "
                    f"{offending!r}. The feed still counts as alive (the message arrived), "
                    f"but gaps can no longer be seen. If a field was added to NOOP|, "
                    f"tipbot's parser needs to learn it."))
        elif noop is not None and state.get("noop_unparsed_alerted") is not None:
            state["noop_unparsed_alerted"] = None

        if noop is not None:
            prev = state.get("last_seq")
            keep_high_water = False
            if isinstance(prev, int):
                if noop.seq == prev + 1:
                    pass                      # the normal case
                elif noop.seq > prev + 1:
                    missed = noop.seq - prev - 1
                    # DEFERRED, not sent here: a gap found at 03:00 must not page.
                    # The loop flushes it once outside the blackout. One slot,
                    # accumulating, so a bad night cannot grow this file.
                    pend = state.get("pending_seq_gap") or {}
                    pend["missed_total"] = int(pend.get("missed_total") or 0) + missed
                    pend["gaps"] = int(pend.get("gaps") or 0) + 1
                    pend["from_seq"] = pend.get("from_seq", prev)
                    pend["to_seq"] = noop.seq
                    pend["last_at"] = now.isoformat(timespec="seconds")
                    state["pending_seq_gap"] = pend
                    log.warning(
                        f"[disposals_model] heartbeat SEQ GAP: {prev} -> {noop.seq} "
                        f"({missed} missed), queued for alert")
                elif noop.seq == prev:
                    # A repeat rather than a gap: a resend or a replayed message.
                    log.info(f"[disposals_model] duplicate heartbeat seq={noop.seq}")
                elif prev - noop.seq <= int(DISPOSALS_MODEL_SEQ_RESET_TOLERANCE):
                    # A SMALL step backwards is late or out-of-order delivery, not a
                    # reset. Keep the high-water mark and say nothing: treating this as
                    # a reset would both raise a spurious alert and CLEAR a real pending
                    # gap report, losing it.
                    log.info(f"[disposals_model] out-of-order heartbeat seq={noop.seq} "
                             f"(high-water {prev}) — ignored, not a reset")
                    keep_high_water = True
                else:
                    # A LARGE step backwards means the model's SEQ_FILE was lost and the
                    # counter restarted (it is relative to CWD and gitignored, and its
                    # own comment says a reset "reads as one missed tick"). Not an
                    # outage, and NOT a gap — alerting it as one would be wrong. The
                    # pending gap is cleared because the old numbering is now meaningless.
                    alerts.append((
                        "info",
                        f"DisposalsModel heartbeat counter went BACKWARDS: {prev} -> "
                        f"{noop.seq}. That is a seq-file reset on the model side, not "
                        f"a missed tick. Re-baselining; gap detection resumes from here."))
                    # Deliberately NOT clearing pending_seq_gap. A gap recorded before
                    # the reset is still accurate history (those heartbeats really did
                    # go missing), and dropping it here would discard an unreported
                    # finding just because the counter later restarted.
            if not keep_high_water:
                state["last_seq"] = noop.seq
            state["last_seq_iso"] = now.isoformat(timespec="seconds")
            state["last_noop_ts"] = noop.ts

        state["last_seen_epoch"] = now.timestamp()
        state["last_seen_iso"] = now.isoformat(timespec="seconds")
        state["last_seen_kind"] = kind
        _disposals_save_liveness(state)
    return alerts


def _disposals_liveness_check(now=None) -> list:
    """Decide whether the feed has gone quiet. Returns (level, text) alerts to send.

    Pure apart from the state file, so every branch is unit-testable without a clock.

    THE BURST RULE. Silence only counts once the feed has ALREADY spoken inside the
    current active window. Before its first message of the window the model may simply
    be taking one of its three silent early exits (see the header above), so there is
    nothing to be late for. After it has spoken, an hourly feed going quiet for
    DISPOSALS_MODEL_SILENCE_HOURS is a real signal.

    TWO HONEST LIMITATIONS, both deliberate, both fixed by the same upstream change:
      * a feed that dies BEFORE it first speaks in a window is not detected at all, and
      * the threshold has to default to 24h, because up to ~22h of in-window silence is
        legitimate today (a round whose last game is on Saturday leaves a game-free
        Sunday, and the four silent paths in the header mean nothing is emitted).
    Once the model heartbeats on all four paths, silence becomes real evidence and
    DISPOSALS_MODEL_SILENCE_HOURS drops to 3. Until then this catches a dead feed, not
    a late one, and the seq-gap check does the sensitive work."""
    now = now or datetime.now()
    alerts = []
    with _DISPOSALS_LIVENESS_LOCK:
        state = _disposals_load_liveness()

        # Never page during the HyperBot blackout. Nothing can be placed then, so a
        # quiet feed costs nothing, and the v6.07 lesson was a tipster paging ~1200
        # criticals a day. Detection continues; only the telling waits for 07:00.
        if _disposals_in_quiet_window(now):
            return []

        # Flush any gap found overnight, now that it is a reasonable hour.
        pend = state.get("pending_seq_gap")
        # The flush is recorded by clearing pending_seq_gap ON DISK, so an unwritable
        # state file (full disk, read-only logs dir, or an AV/backup process holding it
        # so os.replace raises) would leave the gap in the reloaded state and re-emit the
        # IDENTICAL alert every poll — ~96 a day, in the detector whose whole design note
        # is about not burying the money alerts (v6.07 #30). _disposals_save_liveness
        # swallows write errors, so disk alone cannot be the latch. Mirror it in memory,
        # exactly as the silence and recovery latches below already do.
        _pend_id = None
        if pend:
            _pend_id = (pend.get("from_seq"), pend.get("to_seq"), pend.get("last_at"))
            if _pend_id == _DISPOSALS_LIVENESS_MEM.get("flushed_gap"):
                pend = None
                state["pending_seq_gap"] = None
        if pend and DISPOSALS_MODEL_SEQ_GAP_ALERT:
            gaps = int(pend.get("gaps") or 0)
            # CAREFUL WITH THE WORDING. seq only increments on the model's NO-BET path,
            # so the missing heartbeats are by construction ticks that produced no bets:
            # no bet was lost with them. What the gap actually evidences is that a
            # message the model sent did not reach us (or was never sent after the
            # counter had already advanced, since seq is consumed BEFORE the send and
            # the result is discarded) — and that same path carries the real bets.
            alerts.append((
                "critical",
                f"DisposalsModel heartbeat GAP: {pend.get('missed_total')} tick(s) "
                f"missing (seq {pend.get('from_seq')} -> {pend.get('to_seq')}"
                f"{f', {gaps} separate gaps' if gaps > 1 else ''}, last "
                f"{pend.get('last_at')}). Those ticks produced no bets by construction, "
                f"so nothing was necessarily lost. What it does mean is that a message "
                f"the model sent never arrived, and the same path carries real bets. "
                f"Check the model's scheduled task log."))
        elif pend:
            log.info(f"[disposals_model] discarding a heartbeat gap report "
                     f"({pend.get('missed_total')} missed): "
                     f"DISPOSALS_MODEL_SEQ_GAP_ALERT is false")
        if pend:
            state["pending_seq_gap"] = None
            _DISPOSALS_LIVENESS_MEM["flushed_gap"] = _pend_id
            _disposals_save_liveness(state)

        last = state.get("last_seen_epoch")
        if not isinstance(last, (int, float)):
            return alerts          # never seen anything: nothing to be late for

        window_start = _disposals_active_window_start(now)
        if window_start is None:
            return alerts          # outside the match block: silence is expected

        if last < window_start.timestamp():
            return alerts          # the burst has not started this window yet

        quiet_h = (now.timestamp() - last) / 3600.0
        if quiet_h < float(DISPOSALS_MODEL_SILENCE_HOURS):
            return alerts

        # THROTTLE: latched on the last-seen stamp that triggered it, PERSISTED so one
        # outage is one alert across restarts, AND mirrored in memory so it survives the
        # file becoming unwritable. Persistence alone was not enough: _disposals_save_
        # liveness swallows write errors, so a locked or full disk turned "one alert per
        # outage" into one alert per poll. Either latch suppresses. It clears on the next
        # receive.
        if last in (state.get("silence_alerted_for"),
                    _DISPOSALS_LIVENESS_MEM.get("silence_alerted_for")):
            return alerts
        state["silence_alerted_for"] = last
        _DISPOSALS_LIVENESS_MEM["silence_alerted_for"] = last
        _disposals_save_liveness(state)
        alerts.append((
            "critical",
            f"DisposalsModel feed has been SILENT for {quiet_h:.1f}h "
            f"(last message {state.get('last_seen_iso')}, a "
            f"{state.get('last_seen_kind')}). It ticks hourly inside its Thu 07:00 - "
            f"Sun 21:00 window and has already spoken in this one. Check the model's "
            f"scheduled task and Telegram. NOTE this is not proof of an outage: the "
            f"model does not heartbeat on its four no-output paths, so a round whose "
            f"last game has already been played is legitimately silent for the rest of "
            f"the window (30.5h at the Grand Final). Silence becomes real evidence, and "
            f"the threshold drops to 3h, once the heartbeat covers those paths."))
    return alerts


def _disposals_send_liveness_alerts(alerts: list) -> None:
    """Send what the detector produced.

    Routed to the MAINTENANCE chat by default, even for level="critical". The critical
    chat carries money pages, and burying those is exactly v6.07 finding #30. The level
    is still carried through so DISPOSALS_MODEL_LIVENESS_CRITICAL can escalate the real
    outage alerts without also promoting the informational ones.

    Plain text only: notify_info and notify_critical both _escape_html their whole
    argument, so a tag would arrive as literal "&lt;b&gt;" rather than as formatting."""
    for level, text in alerts or []:
        try:
            if level == "critical" and DISPOSALS_MODEL_LIVENESS_CRITICAL:
                notifier.notify_critical(text)
            else:
                notifier.notify_info(text)
        except Exception as e:
            log.error(f"[disposals_model] liveness alert failed to send: {e}")


def _disposals_liveness_tick() -> None:
    """One check-and-send, SYNCHRONOUS. Run in a worker thread by both callers.

    notifier._send is a blocking requests POST with a 20s read timeout and one retry,
    so a single alert can pin whatever thread it is on for ~45s. On the event loop that
    stalls every tip in flight, which is a steep price for a diagnostic."""
    _disposals_send_liveness_alerts(_disposals_liveness_check())


async def _disposals_liveness_loop():
    """Periodic silence check. Exception-proof, same shape as the reconciliation loop."""
    if not (DISPOSALS_MODEL_LIVENESS_ENABLED and DISPOSALS_MODEL_ENABLED):
        return
    poll = max(60, int(DISPOSALS_MODEL_LIVENESS_POLL_SEC))
    log.info(f"[disposals_model] NOOP liveness detector started (poll {poll}s, "
             f"silence threshold {DISPOSALS_MODEL_SILENCE_HOURS}h, window "
             f"day{DISPOSALS_MODEL_ACTIVE_START_DAY}@{DISPOSALS_MODEL_ACTIVE_START_HOUR}:00"
             f" -> day{DISPOSALS_MODEL_ACTIVE_END_DAY}@{DISPOSALS_MODEL_ACTIVE_END_HOUR}:00)")
    while True:
        await asyncio.sleep(poll)
        try:
            # Default pool, NOT _PLACEMENT_EXECUTOR: that one is deliberately isolated
            # so a wedged notifier can never consume a placement worker.
            await asyncio.get_running_loop().run_in_executor(
                None, _disposals_liveness_tick)
        except Exception as e:
            log.error(f"[disposals_model] liveness loop error: {e}")


def _apply_disposals_model_stake(tip: ParsedTip) -> None:
    """v6.08: clamp a DisposalsModel tip's stake in the PLACING process.

    The stake arrives as DOLLARS on the BET| line (not units), already set as
    unit_size with units=1.0 so stake_dollars == that figure exactly. This re-applies
    the ceilings HERE rather than trusting the parse, because the clamp must live in
    the process that actually places (the $600 lesson, 2026-06-01: a "$1-capped" test
    placed a real $600 bet because main.py had not been restarted after the .env
    edit). Two ceilings:

      * DISPOSALS_MODEL_TEST_MODE -> ignore the posted stake entirely and use
        DISPOSALS_MODEL_TEST_STAKE. Mirrors _apply_dello_flat_stake.
      * DISPOSALS_MODEL_MAX_STAKE -> hard per-message cap, the accidental-extra-zero
        guard (a feed bug emitting stake=5000 must not place $5,000).

    Downstream liability caps only ever reduce this further. No-op for any other
    tipster. Mutates `tip`."""
    if tip.tipster != DISPOSALS_MODEL_TIPSTER:
        return
    posted = round(float(tip.unit_size or 0), 2)
    if DISPOSALS_MODEL_TEST_MODE:
        flat = round(float(DISPOSALS_MODEL_TEST_STAKE), 2)
        if flat <= 0:
            return
        if abs(posted - flat) > 0.001:
            log.warning(
                f"[disposals_model] TEST MODE: posted stake ${posted} -> forcing "
                f"${flat} (DISPOSALS_MODEL_TEST_MODE=true; set it false + "
                f"RESTART for production stakes)"
            )
        tip.units = 1.0
        tip.unit_size = flat
        return
    capped = min(posted, round(float(DISPOSALS_MODEL_MAX_STAKE), 2))
    if capped <= 0:
        return
    if abs(posted - capped) > 0.001:
        log.warning(
            f"[disposals_model] posted stake ${posted} exceeds the "
            f"${DISPOSALS_MODEL_MAX_STAKE:.0f} cap -> capping to ${capped} "
            f"(accidental-extra-zero guard)"
        )
    tip.units = 1.0
    tip.unit_size = capped


def _apply_self_bet_flat_stake(tip: ParsedTip) -> None:
    """v5.9x (Wilson 2026-07-12, 07-12 review #2): force a self-bet tip to a FLAT
    total stake = the (capped) dollar figure Wilson typed, regardless of any unit
    token the LLM extracted from the free-text selection. Without this, a stray
    '2u'/'3u' in the selection would make stake_dollars = units × unit_size and
    blow past the $SELF_BET_MAX_STAKE hard cap (the accidental-extra-0 guard).
    Forces units=1.0 and re-clamps unit_size to the cap. No-op for non-self-bet.
    Mutates `tip`."""
    if tip.tipster != "self_bet":
        return
    capped = min(round(float(tip.unit_size or 0), 2), SELF_BET_MAX_STAKE)
    if capped <= 0:
        return
    if tip.units != 1.0 or abs(float(tip.unit_size or 0) - capped) > 0.001:
        log.warning(
            f"self-bet flat stake: {tip.units}u x ${tip.unit_size} "
            f"= ${tip.stake_dollars} -> forcing 1u x ${capped:.2f} "
            f"(total-stake cap; ignore any unit token in the selection)"
        )
    tip.units = 1.0
    tip.unit_size = capped


def _apply_image_test_stake(tip: ParsedTip) -> None:
    """Pin an IMAGE-TIP sports tip to the $1/unit test stake while gated.

    Image-tip channels (Eddie AFL etc.) are validated at a tiny stake before
    going to production: while IMAGE_TIPS_TEST_MODE is on, every image-tip
    sports tip is staked at IMAGE_TIPS_TEST_UNIT_SIZE per recommended unit
    (default $1/u, so a 2.5u tip = $2.50). Mirrors _apply_mlb_flat_stake: the
    clamp lives in the PLACING process so an .env edit needs a restart to take
    effect (the $600 lesson). Only `unit_size` is overridden — `units` (the
    tipster's recommendation) is preserved, so stake_dollars = units x $1.
    No-op unless test mode is on AND the tip came from an image-tip parser.
    The racing pipeline applies its own $1/u in process_image_racing_tip, so
    this handles the sports (AFL) side only. Mutates `tip`."""
    if not IMAGE_TIPS_TEST_MODE:
        return
    if tip.tipster not in IMAGE_TIP_PARSERS:
        return
    test_unit = round(float(IMAGE_TIPS_TEST_UNIT_SIZE), 2)
    if abs(tip.unit_size - test_unit) > 0.001:
        log.warning(
            f"Image-tip test stake: {tip.tipster} {tip.units}u x "
            f"${tip.unit_size} -> forcing ${test_unit}/u (= ${round(tip.units * test_unit, 2)}); "
            f"IMAGE_TIPS_TEST_MODE on, change + restart for production stakes"
        )
    tip.unit_size = test_unit


def _apply_saiyan_sgm_unit(tip: ParsedTip) -> None:
    """Saiyan SGMs stake a BIGGER unit than his singles/disposals (Wilson
    2026-06-14). His singles/disposals keep SAIYAN_UNIT_SIZE (600); his SGMs use
    SAIYAN_SGM_UNIT_SIZE (750). The SGM fan-out even-splits the unit across the 3
    SGM accounts (Adam/Wilson/Daniel), so 750 -> $250 stake each. Mirrors
    _apply_mlb_flat_stake: overrides tip.unit_size in place; `units` (the
    tipster's recommendation) is preserved, so a 2u SGM = 2 x $750. No-op unless
    this is a Saiyan SGM (singles/disposals untouched). MUST run AFTER any image
    test-stake override so a real Saiyan SGM isn't pinned to $1."""
    if tip.tipster != "saiyan_afl" or not tip.is_sgm or SAIYAN_SGM_UNIT_SIZE <= 0:
        return
    new_unit = round(float(SAIYAN_SGM_UNIT_SIZE), 2)
    if abs(tip.unit_size - new_unit) > 0.001:
        log.info(
            f"Saiyan SGM unit: {tip.units}u x ${tip.unit_size} -> ${new_unit}/u "
            f"(= ${round(tip.units * new_unit, 2)} total; even-split "
            f"~${round(tip.units * new_unit / 3)}/acct across the 3 SGM accounts)"
        )
    tip.unit_size = new_unit


def _is_mlb_hrrbi_sgm(tip: ParsedTip) -> bool:
    """True iff `tip` is exactly the approved MLB auto-place shape: a 2-leg
    same-player HRRBI SGM [over 0.5 (1+), over 1.5 (2+)] on the player_stats
    market. Any other MLB SGM (a Groq misparse, an unexpected combo) is NOT
    approved for auto-placement and must route to manual — so the only thing
    that ever auto-places for MLB is the live-validated HRRBI correlation edge."""
    if (tip.sport or "").lower() != "mlb" or not getattr(tip, "is_sgm", False):
        return False
    legs = tip.legs or []
    if len(legs) != 2:
        return False
    players: set[str] = set()
    lines: set[float] = set()
    for l in legs:
        if (l.market or "") != "player_stats":
            return False
        if MLB_STAT_MAP.get((l.stat or "").lower(), (l.stat or "").lower()) != "h_r_rbi":
            return False
        sel = (l.selection or "").lower()
        if not (sel.endswith("over") or sel == "over"):
            return False
        players.add((l.player or "").strip().lower())
        try:
            lines.add(round(float(l.line), 1))
        except (TypeError, ValueError):
            return False
    return len(players) == 1 and "" not in players and lines == {0.5, 1.5}


def _mlb_hrrbi_to_sgm(tips: list[ParsedTip]) -> list[ParsedTip]:
    """Transform a Shook MLB "M 1.5 HRR" (2+ HRRBI) single into a 2-leg
    same-player SGM [over 0.5 (1+), over 1.5 (2+)].

    Verified live 2026-06-01: the 2-leg SGM prices >= the single 2+ (Freeman
    1.67 vs 1.62; Ohtani neutral) — a raw correlation edge, NOT boosts. So we
    ALWAYS bet the HRRBI 2+ as this SGM. Only the 2+ (line 1.5) over h_r_rbi
    line is transformed; every other MLB tip passes through unchanged (and,
    being a single with no MLB singles priority list, routes to manual).

    Wilson 2026-06-01: bet ONLY the actual HRRBI play — "Total bases viable"
    and other alt mentions are ignored (the secondary same-player tip is
    already dropped by _dedupe_shook_same_player upstream), and if the SGM
    can't be placed it routes to manual. So we also CLEAR any alt_line/alt_lines
    and set suggested_odds=0 (SGMs have no per-leg odds floor; no alt spill).
    """
    out: list[ParsedTip] = []
    for tip in tips:
        if (
            (tip.sport or "").lower() == "mlb"
            and not getattr(tip, "is_sgm", False)
            and not tip.alert_only
            and not tip.is_live
            and len(tip.legs) == 1
        ):
            leg = tip.legs[0]
            stat = MLB_STAT_MAP.get((leg.stat or "").lower(), (leg.stat or "").lower())
            sel = (leg.selection or "").lower()
            is_over = sel.endswith("over") or sel in ("over", "")
            # v5.53: HRR lines only exist at X.5 (0.5/1.5/2.5/3.5) — an INTEGER
            # 5/15/25/35 is impossible for this market and means the tipster
            # dropped the decimal point. 2026-06-12 Corbin Carroll: Shook posted
            # "M 15 HRR" (= 1.5; the -134 price proves the 2+ line) -> parsed as
            # line 15.0 -> missed the 2+ transform below -> routed to manual
            # instead of auto-placing $400. Snap exactly {5,15,25,35} -> /10
            # with a loud flag; anything else non-standard still goes manual.
            if stat == "h_r_rbi":
                try:
                    _ln = float(leg.line)
                    if _ln in (5.0, 15.0, 25.0, 35.0):
                        leg.line = _ln / 10.0
                        log.warning(
                            f"MLB HRR line snap: {leg.player} line {_ln:g} is "
                            f"impossible (HRR lines are X.5) -> snapped to "
                            f"{leg.line} (tipster dropped the decimal point)"
                        )
                except (TypeError, ValueError):
                    pass
            try:
                line_is_2plus = abs(float(leg.line) - 1.5) < 0.01
            except (TypeError, ValueError):
                line_is_2plus = False
            if (
                stat == "h_r_rbi" and is_over and line_is_2plus
                and (leg.player or "").strip()
            ):
                tip.legs = [_mlb_hrrbi_leg(leg, 0.5), _mlb_hrrbi_leg(leg, 1.5)]
                tip.is_sgm = True
                tip.is_pyo_sgm = False
                tip.alt_line = None
                tip.alt_lines = None
                tip.suggested_odds = 0.0
                tip._is_threshold = False
                log.info(
                    f"MLB HRRBI->SGM: {leg.player} 2+ HRRBI transformed into a "
                    f"2-leg same-player SGM [1+ (over 0.5), 2+ (over 1.5)]"
                )
        out.append(tip)
    return out


def resolve_event(tip: ParsedTip) -> str:
    """
    Resolve event name based on sport type.

    Two-stage NBA logic:
      A) Team given: trust it. Resolve game from team. Player name is used
         only for the bet payload (not for routing).
      B) No team: fall back to roster lookup, biased toward players whose
         team is actually playing today (handles "Wiggins" ambiguity).
    """
    if tip.sport == "afl":
        ev = resolve_afl_event(tip.primary_team)
        if not ev:
            # v5.60 (2026-06-13): an SGM's legs can name different (co-playing)
            # players; if leg 0's player didn't resolve a team (Groq didn't
            # expand "LDU" -> Luke Davies-Uniacke, so primary_team was empty)
            # but ANOTHER leg did (Harry Sheezel -> North Melbourne), resolve
            # the fixture off that team instead of failing "No fixture found
            # for LDU" -> manual. Singles are unaffected (all_teams == {primary}
            # or empty, so the loop is a no-op). A malformed cross-game SGM
            # would still fail at placement on the off-game leg.
            for t in tip.all_teams:
                if t and t != tip.primary_team:
                    ev = resolve_afl_event(t)
                    if ev:
                        log.info(
                            f"AFL event resolved off non-primary leg team "
                            f"'{t}' (primary_team '{tip.primary_team}' didn't "
                            f"resolve — likely an unexpanded player name)"
                        )
                        break
        return ev or ""

    if tip.sport == "mlb":
        # MLB (2026-06-01): resolve the team to a "Home v Away" fixture via
        # ESPN baseball/mlb. Shook USUALLY posts the team in a context line
        # (e.g. "Dodgers" / "LAD/CIN"); Groq emits the full team name and
        # resolve_mlb_event substring-matches it to the ESPN fixture.
        #
        # v5.33 (2026-06-07, Wilson): when Shook OMITS the team (e.g.
        # "Matt Olson 2+ HRRBI" with no team — 15:53 went to manual with
        # "no team"), infer the team from the MLB roster (roster_mlb.json,
        # MLB Stats API) via the PLAYER name, then resolve the fixture. This
        # fires ONLY when no team is announced — a stated team is never
        # overridden. Inference is EXACT full-name match only (see the gated
        # block below) — NO fuzzy, NO bare-surname — so it can never drift to a
        # same-surname player on the wrong team (adversarial-verified).
        team = tip.primary_team
        # Infer/confirm the team from the MLB roster for a full (2+ token) player
        # name via an EXACT match (Shook sends full names). NO fuzzy and NO
        # bare-surname inference: a fuzzy/surname-only match drifts to an
        # arbitrary same-surname player on the WRONG team (e.g. 'Soto' -> Gregory
        # Soto / Pirates when the tip meant Juan Soto / Mets). roster_mlb.json
        # keys are full names + UNAMBIGUOUS surname aliases; an EXACT 2+ token hit
        # means a confident full-name match — everything else (bare surname,
        # partial, typo, unknown) leaves the stated team / -> manual (safe miss).
        # v5.33 added this for the no-team case.
        #
        # v5.72 (Olson 06-16): the roster is now AUTHORITATIVE — it runs even when
        # a team IS stated and OVERRIDES it on an exact full-name disagreement,
        # mirroring the NBA team-override. The old `if not team` gate meant a
        # Groq-HALLUCINATED team (context bleed: a teamless 'Matt Olson 2+ HRRBI'
        # inherited 'MIL' from the prior Turang/MIL play + a standalone 'MIL'
        # header) was trusted and resolved the WRONG game (Brewers v Guardians;
        # Olson is a Brave) -> player absent from the catalog -> manual. Override
        # is EXACT full-name only, and roster_mlb.json is refreshed daily from the
        # MLB Stats API (fresher than Groq's training data), so it can only move a
        # bet from the wrong game to the roster-correct game — never a wrong player.
        player = tip.legs[0].player if (tip.legs and tip.legs[0].player) else ""
        if player and len(player.split()) >= 2:
            from roster import exact_match_player as _exact_mlb
            from roster import is_mlb_name_collision as _mlb_collision
            # Same-name collision guard (2026-06-25; Pete Alonso $399.99 wrong-game
            # FAULT, Max Muncy latent). Two different MLB players can share an exact
            # full name on different teams (a star + a minor-leaguer), so the roster
            # below cannot be trusted to name the right team — the old unconditional
            # override placed real money on the WRONG game with no alert. For a known
            # collision: a tipster-STATED team is the human's explicit intent (not the
            # clobbered roster) so trust it and let resolve_mlb_event decide; with NO
            # stated team there is nothing safe to infer -> route to manual.
            _collision_teams = _mlb_collision(player)
            if _collision_teams:
                if team:
                    log.warning(
                        f"MLB same-name collision: '{player}' exists on "
                        f"{_collision_teams}; NOT applying roster override - using "
                        f"the tipster's stated team '{team}'."
                    )
                    return resolve_mlb_event(team) or ""
                log.warning(
                    f"MLB same-name collision: '{player}' on {_collision_teams} with "
                    f"NO stated team - cannot infer safely; routing to manual."
                )
                return ""
            _rm = _exact_mlb(player, "mlb")
            # BUG D (Wilson 2026-06-21): exact missed -> GUARDED fuzzy fallback for
            # a TYPO/variant of a full name ('Jung Ho Lee' -> 'Jung Hoo Lee', SF
            # Giants). mlb_fuzzy_player is full-string-only + >=0.9 + UNIQUE, so it
            # corrects a near-identical spelling but never drifts to a same-surname
            # player on the wrong team. On a hit, also rewrite the (typo) player
            # name to the roster's exact spelling on EVERY leg carrying it, so the
            # bookie catalog match is deterministic (the HRRBI SGM has 2 same-player
            # legs). Belt-and-braces: the catalog matcher's own game-scoped fuzzy
            # (_resolve_mlb_player) would also canonicalise it once the fixture is
            # right — but resolving the TEAM here is what unblocks the fixture.
            _fuzzy_used = False
            if not (_rm and _rm.get("team")):
                from roster import mlb_fuzzy_player as _fz_mlb
                _fzm = _fz_mlb(player)
                if _fzm and _fzm.get("team"):
                    log.warning(
                        f"MLB fuzzy resolve: '{player}' -> '{_fzm['name']}' "
                        f"({_fzm['team']}) score={_fzm['score']} (exact missed — "
                        f"likely a typo; still gated on the bookie catalog)"
                    )
                    _rm = {"name": _fzm["name"], "team": _fzm["team"]}
                    _fuzzy_used = True
                    _typo_l = player.strip().lower()
                    for _lg in tip.legs:
                        if (_lg.player or "").strip().lower() == _typo_l:
                            _lg.player = _fzm["name"]
            if _rm and _rm.get("team"):
                _roster_team = _rm["team"]
                if not team:
                    team = _roster_team
                    log.info(
                        f"MLB no-team: inferred '{player}' -> '{team}' from roster "
                        f"({'fuzzy' if _fuzzy_used else 'exact full-name'} match)"
                    )
                else:
                    # Team WAS stated. Trust the roster's exact full-name team
                    # (authoritative for the player). Harmless when it's the same
                    # team in a different form ('MIL' vs 'Milwaukee Brewers');
                    # corrects a bled/wrong team (the Olson 'MIL' -> 'Atlanta
                    # Braves' case). resolve_mlb_event handles full team names.
                    if _roster_team.lower() != (team or "").lower():
                        log.info(
                            f"MLB team override: tip said '{team}', roster says "
                            f"'{_roster_team}' for '{player}' "
                            f"({'guarded-fuzzy' if _fuzzy_used else 'exact full-name'}) — "
                            f"using roster's team."
                        )
                    if tip.legs:
                        tip.legs[0].team_full = _roster_team
                    team = _roster_team
        if not team:
            log.warning(
                "MLB tip has no team and no exact-or-guarded-fuzzy roster match — "
                "routing to manual"
            )
            return ""
        return resolve_mlb_event(team) or ""

    if tip.sport in ("nba", "nbl"):
        team = tip.primary_team
        player = ""
        roster_match = None
        if tip.legs and tip.legs[0].player:
            player = tip.legs[0].player

            # Shook always sends full player names - skip fuzzy roster match
            # to avoid mismatches like Jabari Smith -> Malachi Smith (0.89 score).
            if tip.tipster != "shook":
                player = resolve_player_name(player, tip.sport)

            # Look up the roster entry for this player so we can cross-check
            # Groq's team assignment. Groq's training data lags behind real
            # trades (Huerter/Allen/Brooks/Harris were all on old teams per
            # Groq on 2026-04-23). Roster comes from nba_api which is current.
            #
            # Strategy: try EXACT match first (works whenever the tipster
            # sent a full correct name — always for Shook, often for others).
            # Exact match is reliable and gives us the current team straight
            # from the roster.
            #
            # If exact match fails AND tipster is not Shook, fall back to
            # fuzzy with a sanity guard. Shook gets no fuzzy fallback to
            # avoid silent corruptions like Jabari Smith -> Tolu Smith
            # (single-token "Smith" collision that routed an HOU bet to
            # the DET game, 2026-04-25).
            from roster import exact_match_player as _exact, fuzzy_match_player as _fm
            # Pass team_full to scope the match to that team. Same rationale
            # as the SGM leg path — for Saiyan AFL, leg.team_full is always
            # set from the emoji prefix and prevents cross-team collisions.
            # If team_full is empty (NBA tipsters that don't include team),
            # this is a no-op and behaviour is identical to pre-fix.
            team_filter = (tip.legs[0].team_full or "") if tip.legs else ""
            roster_match = _exact(tip.legs[0].player, tip.sport, team=team_filter)

            if not roster_match and tip.tipster != "shook":
                roster_match = _fm(tip.legs[0].player, tip.sport, team=team_filter)

                # Sanity gate on fuzzy match: if matched name shares no
                # ≥3-char tokens with the query, it's probably a false
                # positive (e.g. surname-only collision). Discard.
                if roster_match:
                    matched = (roster_match.get("name") or "").lower()
                    original = tip.legs[0].player.lower()
                    orig_tokens = set(t for t in original.split() if len(t) >= 3)
                    matched_tokens = set(matched.split())
                    if orig_tokens and not (orig_tokens & matched_tokens):
                        log.warning(
                            f"Discarding suspicious fuzzy roster match: "
                            f"'{tip.legs[0].player}' -> '{roster_match.get('name')}' "
                            f"share no name tokens. Trusting tipster's spelling."
                        )
                        roster_match = None

        # Team override: if Groq gave us a team, but the roster is strongly
        # confident the player is on a DIFFERENT team, trust the roster.
        # Threshold 0.9 ensures only high-confidence matches override.
        if (
            team
            and roster_match
            and roster_match.get("score", 0) >= 0.9
            and roster_match.get("team")
            and roster_match["team"].lower() != team.lower()
        ):
            log.info(
                f"Team override: Groq said '{team}', roster says "
                f"'{roster_match['team']}' (score={roster_match['score']}). "
                f"Using roster's team."
            )
            # primary_team is a read-only property derived from legs[0].team_full,
            # so write to the leg's field rather than assigning the property.
            if tip.legs:
                tip.legs[0].team_full = roster_match["team"]
            team = roster_match["team"]

        # Stage A: team given by tipster -> trust it as the routing signal
        if team:
            result = resolve_nba_event(
                team=team, player=player, sport=tip.sport,
            )
            if result:
                log.info(f"Resolved event (team-first): {result}")
            return result or ""

        # Stage B: no team -> disambiguate by checking which candidate
        # for this player has a game scheduled today.
        if player:
            # Pre-check: is the "player" string actually a team alias?
            # Tipsters often write team-only legs (e.g. "Knicks -4.5" for a
            # handicap, "Spurs ML" for h2h) which Groq parses with player=
            # the team token. Without this guard, fuzzy_match_all on "Knicks"
            # finds Nick Smith Jr. (LAL, score 0.64) and the SGM resolves to
            # the wrong event entirely. 2026-05-07 Knicks/KAT regression.
            #
            # Belt-and-braces: if any leg has a line/h2h market, the
            # "player" field is unambiguously a team. Use that as a stronger
            # signal — handicaps don't apply to players (there is no
            # "Doncic +4.5" line bet, only a player prop). Per Wilson:
            # "the handicap is a dead giveaway, no player will have
            # 'Doncic +4.5 HC'".
            from nba_resolver import NBA_TEAM_ALIASES as _NBA_ALIASES
            leg0_market = (tip.legs[0].market or "") if tip.legs else ""
            is_team_market = leg0_market in (
                "line", "first_half_line", "h2h", "total_points",
                "alternate_total",
            )
            alias_team = _NBA_ALIASES.get(player.lower())
            if alias_team and (is_team_market or True):
                # Team alias hit. Treat as team, run Stage A.
                log.info(
                    f"Team alias hit: '{player}' -> '{alias_team}' "
                    f"(market={leg0_market or 'unknown'}). Resolving "
                    f"team-first instead of player disambiguation."
                )
                if tip.legs:
                    tip.legs[0].team_full = alias_team
                result = resolve_nba_event(
                    team=alias_team, player=None, sport=tip.sport,
                )
                if result:
                    log.info(f"Resolved event (team-alias): {result}")
                    return result
                # Fall through if alias didn't resolve to a fixture today
                # — let player disambiguation try as a last resort.
                log.warning(
                    f"Team alias '{alias_team}' has no fixture today; "
                    f"falling back to player disambiguation."
                )

            from roster import fuzzy_match_all
            # v5.69 (M8): generate candidates from the ORIGINAL tipster text for
            # a BARE SURNAME, not the resolve_player_name-collapsed full name.
            # Collapsing "Brown" -> "Bruce Brown" (line 1570) biases
            # fuzzy_match_all so the real intended player (e.g. Jaylen Brown
            # 0.836) drops below the score floor and is silently eliminated,
            # while the in-order loop locks onto the arbitrary collapsed pick.
            # Querying the raw surname keeps all same-surname players at
            # comparable scores so a genuine collision is DETECTED. Multi-token
            # / nickname inputs keep the collapsed name (collapse helps there).
            _orig_player = (tip.legs[0].player if (tip.legs and tip.legs[0].player) else player) or player
            _is_bare_surname = len(_orig_player.split()) == 1
            disambig_query = _orig_player if _is_bare_surname else player
            candidates = fuzzy_match_all(disambig_query, tip.sport)
            if not candidates:
                log.warning(f"No roster candidates for player '{disambig_query}'")
                return ""

            # v5.69-r2 (round-2 #6): for a BARE SURNAME, prefer candidates whose
            # SURNAME (last token) equals the query, so a token that coincidentally
            # matches a prominent player's FIRST name (e.g. 'Grant' -> 'Grant
            # Williams') can't out-rank the real surname match (Jerami Grant). Fall
            # back to all candidates if NONE share the surname (a first-name-famous
            # tip like 'Giannis', whose surname is Antetokounmpo).
            if _is_bare_surname:
                _q = _orig_player.strip().lower()
                _surname_hits = [
                    c for c in candidates
                    if (c.get("name") or "").split()
                    and (c["name"].split()[-1].lower() == _q)
                ]
                if _surname_hits:
                    candidates = _surname_hits

            if len(candidates) == 1:
                # Only one player matches - use their team
                c = candidates[0]
                log.info(f"Single candidate for '{disambig_query}': {c['name']} ({c['team']})")
                result = resolve_nba_event(team=c["team"], player=c["name"], sport=tip.sport)
                if result:
                    log.info(f"Resolved event (single candidate): {result}")
                return result or ""

            # Multiple candidates. v5.69 (M8): build the set of candidates that
            # actually have a game today, THEN decide — never return on the
            # first one found (the old in-order loop placed on whichever
            # same-surname player came first when 2+ were within the floor and
            # both played). Rule:
            #   - exactly one playable           -> use it (unambiguous)
            #   - top playable clearly ahead     -> use it (next playable is
            #     more than CANDIDATE_SCORE_FLOOR below = a confident match,
            #     e.g. a full-name tip; the weaker same-surname guy is noise)
            #   - 2+ playable within the floor    -> genuine collision -> MANUAL
            # Prevents the 2026-04-30 Walter Clayton/Terrence Shannon and the
            # Bruce/Jaylen Brown wrong-player placements. Tunable via env.
            CANDIDATE_SCORE_FLOOR = float(
                os.getenv("DISAMBIG_SCORE_FLOOR", "0.10")
            )
            log.info(
                f"Multiple candidates for '{disambig_query}': "
                f"{[(c['name'], c['team'], c['score']) for c in candidates[:5]]}"
            )
            playable = []
            for c in candidates:
                r = resolve_nba_event(team=c["team"], player=c["name"], sport=tip.sport)
                if r:
                    playable.append((c, r))

            if not playable:
                log.warning(
                    f"None of {len(candidates)} candidates for '{disambig_query}' "
                    f"have a game scheduled"
                )
                return ""
            if len(playable) == 1:
                c, r = playable[0]
                log.info(
                    f"Disambiguation: only '{c['name']}' ({c['team']}, "
                    f"score={c['score']}) has a game today -> using it (unambiguous)"
                )
                tip.legs[0].player = c["name"]
                return r
            # 2+ playable: candidates are score-sorted, so playable[0] is the
            # highest-scoring one that plays.
            best_c, best_r = playable[0]
            # v5.69-r2 (round-2 #2): a FULL-NAME tip is a confident, specific
            # match — use the top playable rather than routing to manual just
            # because a same-surname (or similar-first-name) sibling also plays
            # within the floor ('Jalen Williams' vs 'Jaylin Williams'). The
            # strict clear-leader/collision gate applies ONLY to an ambiguous
            # BARE SURNAME query.
            if not _is_bare_surname:
                log.info(
                    f"Resolved event (full-name): {best_r} via {best_c['name']} "
                    f"({best_c['team']}, score={best_c['score']}) — confident "
                    f"full-name match, top playable"
                )
                tip.legs[0].player = best_c["name"]
                return best_r
            # Bare surname: only trust the top if it is a CLEAR leader over the
            # next playable; otherwise it's a genuine same-surname tie -> manual.
            second_score = playable[1][0].get("score", 0)
            if (best_c.get("score", 0) - second_score) > CANDIDATE_SCORE_FLOOR:
                log.info(
                    f"Resolved event (disambiguated): {best_r} via {best_c['name']} "
                    f"({best_c['team']}, score={best_c['score']}) — clear leader "
                    f"over next playable (score={second_score})"
                )
                tip.legs[0].player = best_c["name"]
                return best_r
            log.warning(
                f"Disambiguation: {len(playable)} same-surname candidates within "
                f"the score floor have games today "
                f"({[p[0]['name'] for p in playable]}) — ambiguous, routing to "
                f"manual instead of guessing the player"
            )
            return ""

        log.warning("No team and no player to resolve event from")
        return ""

    # All other sports (MLB, NFL, soccer, tennis, etc.)
    # Use team/player from Groq parsing, let HyperBot fuzzy match
    team = tip.primary_team
    if team:
        log.info(f"Using Groq team for {tip.sport}: '{team}'")
        return team

    # If no team but has player, use player name as search hint
    if tip.legs and tip.legs[0].player:
        player = tip.legs[0].player
        log.info(f"Using player for {tip.sport} event resolution: '{player}'")
        return player

    return ""


# ── Bet Placement ───────────────────────────────────────────────────

def place_tip(tip: ParsedTip) -> list[BetResult]:
    """Place a tip through the full pipeline: alert, resolve, place."""
    # LIVE bets always alert
    if tip.is_live:
        log.info(f"LIVE tip, alerting: {tip.alert_reason}")
        notifier.notify_manual_alert(tip)
        return []

    # Alert-only tips (non-LIVE reasons).
    # v5.69 (M6): the short-circuit is UNCONDITIONAL — it used to be
    # `and not tip.is_sgm`, which exempted SGMs. That carve-out let an
    # alert_only tip that had been promoted to an SGM (e.g. an AusBets/Kev
    # no-unit / no-bet-framing message collapsed into an NBA SGM by
    # _promote_misparsed_sgms BEFORE the gates fire) sail past the
    # "do not place" flag straight into _place_sgm_v4/_place_sgm_fanout
    # (neither re-checks alert_only). An SGM the operator was told to place
    # by hand must NOT auto-place — this is the v5.52 money-safety gate.
    if tip.alert_only:
        log.info(f"Alert-only tip: {tip.alert_reason}")
        notifier.notify_manual_alert(tip)
        return []

    if not tip.legs:
        log.warning("No legs in tip, skipping")
        return []

    # Resolve event. v5.37: time the resolve step on its own (was lumped into the
    # _process_tip "resolve+place" log) + stash it on tip._timing so the BET
    # PLACED summary can show a reconciling parse/resolve/place-wall breakdown.
    # tip._timing carries t0 + parse_sec from the message handler; we ADD
    # resolve_sec here. If the handler didn't stamp t0 (a direct place_tip call /
    # test), the notifier ignores the phase split — resolve_sec is then unused.
    _t_resolve = time.time()
    event = resolve_event(tip)
    _tm = getattr(tip, "_timing", None)
    if not isinstance(_tm, dict):
        _tm = {}
        try:
            tip._timing = _tm
        except Exception:
            _tm = None
    if _tm is not None:
        _tm["resolve_sec"] = round(time.time() - _t_resolve, 3)
    if not event:
        search = tip.primary_team or (tip.legs[0].player if tip.legs else "unknown")
        log.warning(f"No fixture found for {search}")
        notifier.notify_event_not_found(
            tip.tipster, search, tip.raw_message, tip=tip,
        )
        return [BetResult(
            success=False, tip=tip, error=f"No fixture found for {search}",
            timestamp=datetime.now(),
        )]

    tip.event = event

    # AFL log header: one entry per AFL tip, summarising parsed legs and
    # resolved event. Lets us correlate downstream PAYLOAD/PLACED/FAILED
    # entries back to the original tip context without sifting tipbot.log.
    if (tip.sport or "").lower() == "afl":
        legs_summary = []
        for l in tip.legs:
            legs_summary.append(
                f"{l.player or l.selection or '?'} "
                f"{l.selection or ''} {l.line or ''} {l.stat or l.market or ''}"
                .strip()
            )
        _afl_log.info(
            f"[{tip.tipster}] TIP event='{tip.event}' is_sgm={tip.is_sgm} "
            f"is_live={tip.is_live} units={tip.units} "
            f"legs={legs_summary}"
        )

    # Dello AFL (2026-07-12): dedicated SINGLES-only Sportsbet placement with the
    # band + rule-3 gates. Handles single-vs-SGM/multi/exotic internally (anything
    # not a clean player-prop single -> manual). Sits after event resolution and
    # BEFORE the generic SGM/single dispatch so Dello never reaches the fan-out /
    # SGM paths (HyperBot can't auto-place his multis; SGM line bug -> manual).
    if tip.tipster == "dello_afl":
        return _place_dello_single(tip)

    # Saiyan handicap bets (SGM OR single) -> MANUAL (Wilson 2026-06-06): a
    # mis-parsed "FRE +0.5" handicap leg broke a Saiyan SGM ("over not found").
    # Wilson wants ALL Saiyan handicap/team-line bets placed by hand rather than
    # risk a mis-placed team-line leg. Catches both placement paths below, incl.
    # a handicap leg whose market label was mangled (no player + no stat).
    # v5.9x (#7): when SAIYAN_HC_SGM_ENABLED, Saiyan handicap bets are NO LONGER
    # force-routed to manual — they fall through to the normal placement paths
    # (single -> AFL fan-out; SGM -> SGM fan-out), where the handicap leg resolves
    # via the EXACT _match_handicap_in_catalog (line/pick_own_line; a miss -> manual,
    # never a wrong line) and the max-stake rebet fills at the SB max across all
    # accounts. Unplaced -> manual. Ships DORMANT (flag default False -> the old
    # manual routing is unchanged until the 07-12 review + a probe confirm it).
    if (tip.tipster == "saiyan_afl" and _tip_has_handicap_leg(tip)
            and not (SAIYAN_HC_SGM_ENABLED and _saiyan_hc_placeable(tip))):
        # 07-12 review C1: even with the flag on, ONLY a clean full-game 'line'
        # handicap auto-places; a first_half_line/period, mangled, or ML/margin/
        # team_line leg stays on the manual route (never a full-game fallback).
        log.info("Saiyan handicap/team-line bet -> routing to manual (Wilson 2026-06-06)")
        if not tip.alert_reason:
            tip.alert_reason = "Saiyan handicap/team-line bet — routed to manual (not auto-placed)"
        notifier.notify_manual_alert(tip)
        return []

    # ETR NBA is SINGLES-ONLY (Wilson 2026-06-07): the blind fixed-ladder fan-out
    # handles ONE player prop per message. A multi/SGM from ETR routes to MANUAL
    # rather than falling through to _place_sgm_v4 (a different path/accounts that
    # was never designed for ETR's blind no-price-check placement).
    if (tip.tipster or "").lower() == "etr_nba" and tip.is_sgm:
        log.info("ETR multi/SGM -> routing to manual (ETR auto-places singles only)")
        if not tip.alert_reason:
            tip.alert_reason = "ETR multi/SGM — routed to manual (ETR auto-places singles only)"
        notifier.notify_manual_alert(tip)
        return []

    # SGM: v4.0 uses _place_sgm_v4 (priority list + liability cap from yaml
    # 'sgm' key + boost-eligible flag from yaml). USE_LEGACY_PLACEMENT=true
    # falls back to v3.10 _place_sgm (legacy SESSION_PRIORITY + binary
    # search + env-driven boost list).
    if tip.is_sgm:
        # Handicap SGMs -> MANUAL (Wilson 2026-05-31): handicap legs inside SGMs
        # (pick_own_line) have been unreliable, so alert rather than risk a
        # mis-placed leg. Toggle via AUTO_MANUAL_HANDICAP_SGM.
        # v5.9x (#7): a SAIYAN handicap SGM bypasses this generic manual rule when
        # SAIYAN_HC_SGM_ENABLED (it places via the SGM fan-out; the handicap leg is
        # enriched to the exact pick_own_line prop or -> manual). Every OTHER
        # tipster's handicap SGM still routes to manual here.
        if (AUTO_MANUAL_HANDICAP_SGM and _is_handicap_sgm(tip)
                and not (tip.tipster == "saiyan_afl" and SAIYAN_HC_SGM_ENABLED)):
            log.info("Handicap SGM -> routing to manual (AUTO_MANUAL_HANDICAP_SGM)")
            tip.alert_reason = "Handicap SGM — auto-routed to manual (not auto-placed)"
            notifier.notify_manual_alert(tip)
            return []
        # MLB HRRBI: per-account placement (SGM on SGM-capable accounts + the
        # 2+ single on single-only accounts, shared intended, leftover->manual).
        if (tip.sport or "").lower() == "mlb" and _is_mlb_hrrbi_sgm(tip):
            return _place_mlb_hrrbi(tip)
        if not USE_LEGACY_PLACEMENT:
            # v5.38: AFL SGMs (saiyan) place CONCURRENTLY (even-split + per-account
            # liability ladder off est combined odds) via _place_sgm_fanout, which
            # delegates back to _place_sgm_v4 on any non-happy-path. NBA SGMs stay
            # on the sequential _place_sgm_v4. Gated by SGM_CONCURRENT_FANOUT.
            if SGM_CONCURRENT_FANOUT and (tip.sport or "").lower() == "afl":
                return _place_sgm_fanout(tip)
            return _place_sgm_v4(tip)
        return _place_sgm(tip)

    # v6.08 DisposalsModel: its exact-line, price-floor and even-split guards all live
    # inside _place_afl_fanout, so they are scoped to the PATH rather than to the
    # tipster. AFL_CONCURRENT_FANOUT defaults true and is not pinned in .env, and
    # config.py documents "set AFL_CONCURRENT_FANOUT=false to revert" — so ONE flag
    # flip would route this feed to _place_singles_v4, which calls _execute_bet with
    # presolved=None. That re-resolves with BARE DEFAULTS (exact_only=False), so
    # _match_afl_player_prop silently snaps 24.5 -> 23.5 at full stake and reports a
    # clean success, and _compute_alt_line_candidates then deliberately walks an under
    # UP to line+1.0. Exactly the failure this feature exists to prevent.
    # Refuse rather than place on an unguarded path: missing a bet is cheap.
    if tip.tipster == DISPOSALS_MODEL_TIPSTER and (
            USE_LEGACY_PLACEMENT or not AFL_CONCURRENT_FANOUT):
        log.error(
            "[disposals_model] AFL_CONCURRENT_FANOUT/USE_LEGACY_PLACEMENT would route "
            "this tip to an UNGUARDED singles path (no exact-line lock, no price "
            "floor, and an alt-line walk) -> routing to MANUAL instead"
        )
        tip.alert_only = True
        tip.alert_reason = (
            "DisposalsModel requires the AFL concurrent fan-out (exact-line lock + "
            "price floor). AFL_CONCURRENT_FANOUT=false / USE_LEGACY_PLACEMENT=true — "
            "place by hand at the EXACT line or re-enable the fan-out."
        )
        notifier.notify_manual_alert(tip)
        return []

    # Single bet placement.
    # v4.0 uses _place_singles_v4 (liability-capped, multi-bookmaker price
    # comparison). Set USE_LEGACY_PLACEMENT=true in .env to fall back to
    # v3.10 behaviour (raw SESSION_PRIORITY + stake ladder, no liability caps).
    if not USE_LEGACY_PLACEMENT:
        # ETR NBA: BLIND concurrent fan-out (no price-check, fixed [100,90,80,70]
        # ladder) across the 4 sportsbet accounts. Gated by ETR_NBA_CONCURRENT_FANOUT.
        # Singles only (multi/SGM already routed to manual above).
        if (ETR_NBA_CONCURRENT_FANOUT and (tip.tipster or "").lower() == "etr_nba"
                and (tip.sport or "").lower() == "nba"):
            return _place_etr_nba_fanout(tip)
        # AFL (Saiyan + Eddie): concurrent fan-out across all eligible
        # Sportsbet accounts (v5.11) instead of the sequential spillover.
        # Gated by AFL_CONCURRENT_FANOUT; NBA/MLB keep _place_singles_v4.
        if AFL_CONCURRENT_FANOUT and (tip.sport or "").lower() == "afl":
            return _place_afl_fanout(tip)
        return _place_singles_v4(tip)

    # ── Legacy v3.10 path (USE_LEGACY_PLACEMENT=true) ───────────────
    sessions = _get_sessions_for_bookie(tip)
    if not sessions:
        log.warning("No active sessions")
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip, error="No active HyperBot sessions",
            timestamp=datetime.now(),
        ))
        return [BetResult(
            success=False, tip=tip, error="No active sessions",
            timestamp=datetime.now(),
        )]

    sessions = _price_check_and_sort(tip, sessions)
    return _place_with_spillover(tip, sessions)


def _apply_session_priority(sessions: list[dict]) -> list[dict]:
    """
    Filter and reorder sessions by SESSION_PRIORITY env var.

    SESSION_PRIORITY is a comma-separated list of session IDs in priority
    order (e.g. "65465,53522,65463"). Sessions listed go in that exact order.
    Sessions NOT in the list are DROPPED (Wilson's rule 2026-04-24:
    unlisted = excluded from auto-placement entirely, manual alert only).

    If the env var is missing/empty, sessions are returned unchanged (no
    regression for existing behaviour).
    """
    priority_env = os.getenv("SESSION_PRIORITY", "").strip()
    if not priority_env:
        return sessions

    priority_ids = [s.strip() for s in priority_env.split(",") if s.strip()]
    if not priority_ids:
        return sessions

    by_id = {str(s.get("session_id", "")): s for s in sessions}
    ordered = [by_id[pid] for pid in priority_ids if pid in by_id]

    dropped = [
        str(s.get("session_id", "")) for s in sessions
        if str(s.get("session_id", "")) not in priority_ids
    ]
    if dropped:
        log.info(
            f"SESSION_PRIORITY filter: kept {len(ordered)} session(s), "
            f"dropped {len(dropped)} unlisted: {dropped}"
        )
    return ordered


def _get_sessions_for_bookie(tip: ParsedTip) -> list[dict]:
    """Get active HyperBot sessions, optionally filtered by bookie."""
    sessions = hb.get_sessions()
    if not sessions:
        return []
    # Filter foreign sessions (other PCs sharing HyperBot key) — same rule
    # as watchdog/v4 path. _is_owned_session is a no-op when
    # USE_LEGACY_PLACEMENT=true (yaml unloaded), preserving v3.10 behaviour.
    sessions = [s for s in sessions if _is_owned_session(s.get("session_id", ""))]

    # Filter by suggested bookie if specified, except for tipsters in the
    # ignore-list (Kev, AusBets) where we want to price-shop instead.
    bookie = tip.suggested_bookie
    if bookie and tip.tipster not in TIPSTERS_IGNORE_SUGGESTED_BOOKIE:
        filtered = [s for s in sessions if s.get("bookie", "") == bookie]
        if filtered:
            sessions = filtered

    # Filter by sport compatibility (only restrict known incompatibilities).
    # bet365 excluded from nba/nbl: HyperBot's bet365 sports endpoint returned
    # "Missing required bet data (track/race_num/runner/stake)" for the
    # 2026-04-21 Hawks +6.5 NBA bet, suggesting bet365 is racing-only on
    # HyperBot's side. Add bet365 back here once that's confirmed working.
    sport_filter = {
        "afl": ["sportsbet", "bet365", "tab", "ladbrokes"],
        "nba": ["sportsbet", "tab"],
        "nbl": ["sportsbet", "tab"],
        "mlb": ["sportsbet"],
    }
    allowed = sport_filter.get(tip.sport)
    if allowed:
        filtered = [s for s in sessions if s.get("bookie", "") in allowed]
        if filtered:
            sessions = filtered

    # Apply SESSION_PRIORITY filter + order. Runs here (not only in
    # _price_check_and_sort) so every path that fetches sessions — singles,
    # SGMs, racing, and anything future — honours the list uniformly.
    sessions = _apply_session_priority(sessions)

    log.info(f"Active sessions: {len(sessions)} ({', '.join(s.get('bookie','') for s in sessions)})")
    return sessions


def _bulk_price_check_player(
    tip: ParsedTip, sessions: list[dict],
) -> dict | None:
    """
    Single bulk call to /api/price_check covering ALL sessions in one request.
    Filters server-side to just the player on the tip's first leg.

    Returns the raw price_check response dict on success, or None on failure.
    Caller iterates `result["selections"]` to extract whichever line they need
    (primary line, alt line, etc) — this lets one bulk call serve both the
    initial price-and-sort step and any subsequent alt-line search.
    """
    if not tip.legs or not tip.legs[0].player:
        return None
    first_leg = tip.legs[0]
    session_ids = [str(s["session_id"]) for s in sessions if s.get("session_id")]
    if not session_ids:
        return None

    try:
        result = hb.price_check_multi_session(
            session_ids=session_ids,
            sport=tip.sport,
            event=tip.event,
            player=first_leg.player,
        )
    except Exception as e:
        log.warning(f"Bulk price check failed: {e}")
        return None

    if not result or not result.get("success"):
        log.debug(f"Bulk price check returned non-success: {result.get('error') if result else 'no response'}")
        return None
    return result


def _odds_by_bookie_from_bulk(
    bulk_response: dict, sessions: list[dict],
    player: str, line: float, direction: str,
) -> dict:
    """
    Walk a bulk price_check response and return {bookie: best_odds} for the
    selection matching (player, line, direction). `direction` is 'over'/'under'
    (case-insensitive substring match against the response's `selection` field).
    Same-bookie sessions get deduped — the best price wins.
    """
    if not bulk_response or not player:
        return {}

    # Map session_id -> bookie for quick lookup (response uses session_id keys).
    # Normalise to lowercase so blocklist comparisons work regardless of whether
    # HyperBot returns 'Sportsbet' or 'sportsbet' (H9 fix 2026-05-30).
    sid_to_bookie = {
        str(s["session_id"]): (s.get("bookie", "") or "").lower() for s in sessions
        if s.get("session_id")
    }

    odds_by_bookie: dict[str, float] = {}
    direction_lower = direction.lower()
    player_lower = player.lower()

    for sel in bulk_response.get("selections", []):
        sel_player = (sel.get("player") or "").lower()
        sel_line_raw = sel.get("line")
        sel_name = (sel.get("selection") or "").lower()

        if player_lower not in sel_player:
            continue
        try:
            sel_line = float(sel_line_raw) if sel_line_raw is not None else None
        except (TypeError, ValueError):
            continue
        if sel_line is None or abs(sel_line - line) > 0.01:
            continue
        if direction_lower not in sel_name:
            continue

        # Found matching selection — pull per-session prices
        for entry in sel.get("prices", []):
            sid = str(entry.get("session_id", ""))
            bookie = sid_to_bookie.get(sid) or (entry.get("siteName") or "").lower()
            try:
                price = float(entry.get("price", 0))
            except (TypeError, ValueError):
                continue
            if price > 0 and bookie:
                if bookie not in odds_by_bookie or price > odds_by_bookie[bookie]:
                    odds_by_bookie[bookie] = price

    return odds_by_bookie


def _price_check_and_sort(tip: ParsedTip, sessions: list[dict]) -> list[dict]:
    """
    Sort sessions for best-odds routing using ONE bulk price_check call across
    all sessions, then dedupe to {bookie: best_odds} and sort sessions so the
    best-odds bookie's sessions go first.

    All Sportsbet accounts share the same odds (they only differ in stake
    limits), so dedup by bookie. Bet365's sports price API isn't reliably
    exposed; bet365 sessions sort to the back when no price was returned.

    For non-player-prop markets (h2h, line, total), per-bookie pricing isn't
    worth the latency — just apply priority order and let placement discover
    the price.
    """
    # SESSION_PRIORITY is applied upstream in _get_sessions_for_bookie now,
    # so incoming `sessions` already has unlisted sessions dropped and is in
    # priority order. We just need to handle price-check re-sorting here.

    # Only price-check for player props with a resolvable player
    if not tip.legs or not tip.legs[0].player:
        return sessions

    first_leg = tip.legs[0]
    if not first_leg.market or "player" not in first_leg.market:
        return sessions

    # If only one bookie is in play, price check adds no value — skip it.
    bookies_present = {s.get("bookie", "") for s in sessions}
    if len(bookies_present) <= 1:
        log.debug(f"Single bookie in play ({bookies_present}), skipping price check")
        return sessions

    bulk = _bulk_price_check_player(tip, sessions)
    if not bulk:
        log.info("Bulk price check empty/failed, using priority order")
        return sessions

    odds_by_bookie = _odds_by_bookie_from_bulk(
        bulk, sessions,
        player=first_leg.player,
        line=first_leg.line,
        direction=first_leg.selection or "",
    )

    if not odds_by_bookie:
        log.info("No matching bookie prices in bulk response, using priority order")
        return sessions

    for bookie, odds in odds_by_bookie.items():
        log.info(f"Price on {bookie} = {odds}")

    # Sort: bookies with a known price go first (best odds first); bookies that
    # couldn't be priced (e.g. bet365) go after. Stable sort preserves priority.
    def sort_key(s):
        bookie = s.get("bookie", "")
        if bookie in odds_by_bookie:
            return (0, -odds_by_bookie[bookie])
        return (1, 0)

    return sorted(sessions, key=sort_key)


def _try_place_with_name_variants(
    tip: ParsedTip, session: dict, stake: float, original_player: str,
) -> BetResult:
    """
    Attempt placement on a single session at the given stake, retrying with
    player name variants if the first attempt fails with 'Selection not found'.
    Also fuzzy-matches against the available player list returned by the bookie.
    """
    # First attempt: name as-is
    result = _execute_bet(tip, session, stake)
    if result.success:
        return result

    err = result.error or ""
    err_lower = err.lower()

    # ── Path A: handicap line sign convention retry ─────────────────
    # Wilson's call: keep this in for now even though today's tests didn't
    # exercise it. If a line/handicap bet fails with "did not match", flip
    # the line sign once and retry on the same session. Risk: if the bet
    # genuinely doesn't exist on the bookie (line moved or wrong direction
    # tipped), this places the OTHER side of the spread instead of failing.
    # Wilson is aware and will revert if it causes wrong-side bets in prod.
    # Runs BEFORE the player-variant return so team bets reach it.
    # Gated off once the handicap catalog was consulted (_execute_bet): the
    # catalog already resolved the exact line / pick_own_line rung or proved
    # it isn't carried, so blind sign-flipping just burns dead retries.
    if (
        "did not match" in err_lower and tip.legs
        and not getattr(tip, "_hc_catalog_consulted", False)
    ):
        leg0 = tip.legs[0]
        # v6.07 (sweep HIGH #2): FULL-GAME "line" ONLY. This blind sign-flip used to
        # arm for `first_half_line` too, but the disarm flag (_hc_catalog_consulted)
        # is only ever set in _execute_bet's `market == "line"` branch, so a PERIOD
        # handicap never disarmed it — and the flipped retry resolves against the
        # FULL-GAME catalog. An AusBets NBA "1u - Lakers 1st Half -5.5" could
        # therefore place a FULL-GAME -5.5/+5.5 (wrong market, at $400/unit). A period
        # handicap must stay EXACT-OR-MANUAL: no blind flip, no alt-line walk.
        if leg0.market == "line" and leg0.line is not None:
            saved_line = leg0.line
            try:
                flipped = -float(saved_line)
            except (TypeError, ValueError):
                flipped = None
            if flipped is not None and flipped != saved_line:
                log.info(
                    f"Handicap line {saved_line} did not match on "
                    f"{session.get('bookie')}; retrying with flipped sign {flipped}"
                )
                leg0.line = flipped
                retry = _execute_bet(tip, session, stake)
                if retry.success:
                    # C2 (2026-05-31): restore the ORIGINAL tipped line before
                    # returning. The bet placed at `flipped` is already captured
                    # in `retry`; leaving the leg flipped would make the NEXT
                    # spillover session (a different bookie) place the OPPOSITE
                    # side of the spread. Mirrors the v4 line-move restore
                    # (`tip.legs[0].line = original_line  # restore for next session`).
                    leg0.line = saved_line
                    return retry
                # Restore line for any subsequent attempts on later sessions
                leg0.line = saved_line
                # If the retry produced a different error class, surface it
                if "did not match" not in str(retry.error or "").lower():
                    return retry

    # ── Path A.5: handicap alt-line retry ───────────────────────────
    # Wilson's rule (2026-05-07): bookies offer alts at 0.5 increments
    # around the main line. If tipped line doesn't match, try better lines
    # first (bettor-favourable, lower odds) then worse lines.
    #   tipped -10  -> -9.5, -9.0  (less to cover, lower odds)   - "better"
    #               -> -10.5, -11  (more to cover, higher odds)  - "worse"
    #   tipped +10  -> +10.5, +11  (more head-start, lower odds) - "better"
    #               -> +9.5, +9.0  (less head-start, higher odds) - "worse"
    # In additive terms, "better" is always line+0.5 / line+1.0 and
    # "worse" is line-0.5 / line-1.0, regardless of sign. Try better
    # first per Wilson's preference. Spurs -10 regression 2026-05-07
    # 11:04 — bookie main was -9.5, no auto-retry, tip went to manual.
    # Sits AFTER Path A so sign-flip still gets first crack (it's a
    # different HyperBot quirk, not an alt-line issue).
    if (
        "did not match" in (result.error or "").lower() and tip.legs
        and not getattr(tip, "_hc_catalog_consulted", False)
    ):
        leg0 = tip.legs[0]
        # v6.07 (sweep HIGH #2): FULL-GAME "line" ONLY — same reasoning as Path A
        # above. Walking ±0.5/±1.0 off a PERIOD handicap resolves against the
        # full-game catalog, i.e. a wrong-market bet. Period -> exact-or-manual.
        if leg0.market == "line" and leg0.line is not None:
            try:
                base_line = float(leg0.line)
            except (TypeError, ValueError):
                base_line = None
            if base_line is not None:
                saved_line = leg0.line
                # Order matches Wilson's preference: better first, then worse.
                # Round to nearest 0.5 to dodge float artefacts (-9.4999 etc.).
                alt_offsets = [0.5, 1.0, -0.5, -1.0]
                for offset in alt_offsets:
                    alt = round((base_line + offset) * 2) / 2
                    if alt == base_line:
                        continue
                    log.info(
                        f"Handicap alt-line retry: tipped {base_line} -> "
                        f"{alt} (offset {offset:+.1f}) on "
                        f"{session.get('bookie')} session "
                        f"{session.get('session_id')}"
                    )
                    leg0.line = alt
                    retry = _execute_bet(tip, session, stake)
                    if retry.success:
                        # H-A.5 (2026-05-31): the placed bet is captured in
                        # `retry` at line `alt`; restore the ORIGINAL tipped
                        # line before returning so the next spillover session
                        # doesn't inherit this bookie's adjusted line (and place
                        # a different handicap). Same family as C1/C2.
                        leg0.line = saved_line
                        return retry
                    err_retry = (retry.error or "").lower()
                    # Different error class (e.g. stake-too-high) -> stop
                    # alt-line walk and surface that error. The line existed
                    # but a different problem cropped up.
                    if "did not match" not in err_retry:
                        leg0.line = saved_line
                        return retry
                # All alt lines exhausted - restore original and continue.
                leg0.line = saved_line

    # ── Path B: name variant retry for selection/player not found ──
    if not original_player:
        return result  # Team bet with no player to vary - nothing further to try
    if "selection " not in err_lower and "player " not in err_lower:
        return result  # Different error class - don't retry with variants

    variants = _player_name_variants(original_player)
    available_players = _extract_available_players(err)

    # Add fuzzy-matched variant from the available player list
    if available_players:
        from difflib import get_close_matches
        matches = get_close_matches(original_player, available_players, n=1, cutoff=0.6)
        if matches and matches[0] not in variants:
            log.info(f"Fuzzy-matched '{original_player}' to bookie list -> '{matches[0]}'")
            variants.insert(1, matches[0])  # try right after as-is variant

    if len(variants) <= 1:
        return result  # No alternative variants to try

    # Try each variant (skip the first - already tried as-is)
    saved_player = tip.legs[0].player
    for variant in variants[1:]:
        log.info(f"Retry with name variant: '{variant}' on session {session.get('session_id')}")
        tip.legs[0].player = variant
        retry = _execute_bet(tip, session, stake)
        if retry.success:
            tip.legs[0].player = saved_player  # restore original
            return retry
        # Stop retrying if error class changed (e.g. now stake error)
        if "selection " not in str(retry.error or "").lower() and "player " not in str(retry.error or "").lower():
            tip.legs[0].player = saved_player
            return retry

    tip.legs[0].player = saved_player  # restore
    return result  # All variants failed - return original error


# ── Auto alt-line retry helpers ────────────────────────────────────

def _compute_alt_line_candidates(tip: ParsedTip) -> list[float]:
    """
    Build ordered list of alt lines to try when a player prop primary fails.
    Overs: lower line first (easier to hit, lower odds).
    Unders: higher line first (easier to hit, lower odds).
    Returns [] for non-player-prop markets, threshold tips, or missing lines.
    """
    if not tip.legs:
        return []
    # Threshold markets use integer lines via pick-your-own-line; ±1 doesn't
    # apply the same way. Skip to avoid sending bad payloads.
    if getattr(tip, "_is_threshold", False):
        return []
    # v6.08 DisposalsModel: NEVER walk this tipster's line. For every other tipster an
    # alt line is a helpful fill; here the line IS the trigger and this function
    # deliberately moves an under UP by 1.0, which is a materially different bet the
    # model never selected. Exact line or nothing.
    if getattr(tip, "tipster", "") == DISPOSALS_MODEL_TIPSTER:
        return []
    leg = tip.legs[0]
    market = leg.market or ""
    if "player_" not in market and market != "player_prop":
        return []
    if not leg.line or leg.line <= 0:
        return []
    sel = (leg.selection or "").lower()
    if "over" in sel:
        return [leg.line - 1.0, leg.line + 1.0]
    if "under" in sel:
        return [leg.line + 1.0, leg.line - 1.0]
    return []


def _odds_within_tolerance(new_odds: float, tipped_odds: float, tol: float = AUTO_ALT_ODDS_TOL) -> bool:
    """True if |new - tipped| / tipped <= tol."""
    if not new_odds or not tipped_odds or tipped_odds <= 1.0:
        return False
    return abs(new_odds - tipped_odds) / tipped_odds <= tol


def _find_alt_line_odds(tip: ParsedTip, sessions: list[dict], new_line: float) -> dict:
    """
    Query bulk /api/price_check for an alt line and return {bookie: best_odds}
    for bookies offering this exact line + direction. Single API call covers
    all sessions at once.
    """
    if not tip.legs or not tip.legs[0].player:
        return {}
    leg = tip.legs[0]
    sel_lower = (leg.selection or "").lower()
    if "over" in sel_lower:
        direction = "over"
    elif "under" in sel_lower:
        direction = "under"
    else:
        return {}

    bulk = _bulk_price_check_player(tip, sessions)
    if not bulk:
        return {}

    return _odds_by_bookie_from_bulk(
        bulk, sessions,
        player=leg.player,
        line=new_line,
        direction=direction,
    )


def _try_auto_alt_lines(
    tip: ParsedTip, sessions: list[dict], intended_stake: float,
) -> list[BetResult]:
    """
    Try automatic ±1 alt-line retry for player props.

    Called when primary (and any tipster-specified alt_line) failed to place
    anything. For each candidate alt:
      1. Price-check across unique bookies
      2. Keep only bookies whose odds are within 10% of the tipped odds
      3. Place with spillover, sorted by best odds first
    Stops at the first alt line that fills (partially or fully).
    """
    tipped_odds = tip.suggested_odds
    candidates = _compute_alt_line_candidates(tip)
    if not candidates:
        return []
    if not tipped_odds or tipped_odds <= 1.0:
        log.info("Auto alt skipped: no tipped odds to compare against")
        return []

    log.info(
        f"Primary failed. Auto alt candidates: {candidates} "
        f"(tipped odds {tipped_odds}, tol {int(AUTO_ALT_ODDS_TOL*100)}%)"
    )

    leg = tip.legs[0]
    original_line = leg.line
    new_results: list[BetResult] = []
    # H8 fix 2026-05-30: initialise remaining_stake once before the candidates
    # loop, not once per candidate. Previously a partial fill on candidate N
    # (where alt_success was not set) would reset remaining_stake to intended_stake
    # on candidate N+1, causing it to retry the full stake.
    remaining_stake = intended_stake

    for new_line in candidates:
        if new_line <= 0:
            log.info(f"Skipping alt line {new_line} (non-positive)")
            continue

        odds_by_bookie = _find_alt_line_odds(tip, sessions, new_line)
        if not odds_by_bookie:
            log.info(f"Alt line {new_line}: no bookie offers it")
            continue

        in_tol = {
            b: o for b, o in odds_by_bookie.items()
            if _odds_within_tolerance(o, tipped_odds)
        }
        if not in_tol:
            log.info(
                f"Alt line {new_line}: all bookie odds outside "
                f"{int(AUTO_ALT_ODDS_TOL*100)}% of tipped {tipped_odds}: "
                f"{odds_by_bookie}"
            )
            continue

        log.info(
            f"Alt line {new_line}: in-tolerance bookies {in_tol} "
            f"(placing best-odds first)"
        )

        # Sessions on in-tolerance bookies, sorted by best alt odds
        alt_sessions = [s for s in sessions if s.get("bookie", "") in in_tol]
        alt_sessions.sort(key=lambda s: -in_tol.get(s.get("bookie", ""), 0))

        # Apply new line to the leg for placement
        leg.line = new_line

        # Stash per-bookie alt-line odds for the placer to use as the basis
        # for target_odds. Without this the placer would compute target_odds
        # from the ORIGINAL tipped odds (e.g. tipped 1.87 -> target 1.68),
        # letting HyperBot fill at any price >= 1.68 — including prices well
        # below the alt line's actual odds. Caused Clingan 19.5 PR @ 1.87
        # alt-line attempt to fill at 1.80 instead of around the 2.02 the
        # alt line was showing (2026-04-25).
        tip._alt_target_odds_by_bookie = dict(in_tol)

        bookie_blocklist: set[str] = set()
        alt_success = False
        # L24 fix 2026-05-30: track start index so we can mark intermediate
        # ladder-rung failures (all but the last per candidate) as is_intermediate.
        _alt_start = len(new_results)

        for session in alt_sessions:
            if remaining_stake <= STAKE_FLOOR:
                break
            bookie = session.get("bookie", "")
            if bookie in bookie_blocklist:
                continue

            original_player = leg.player
            steps = _ladder_steps(remaining_stake)

            for step_stake in steps:
                result = _try_place_with_name_variants(
                    tip, session, step_stake, original_player,
                )
                new_results.append(result)

                if result.success:
                    placed = result.stake or step_stake
                    remaining_stake -= placed
                    alt_success = True
                    log.info(
                        f"Alt line {new_line} placed ${placed:.2f} on {bookie} "
                        f"(remaining ${remaining_stake:.2f})"
                    )
                    break
                if _is_same_bookie_fatal(result.error or ""):
                    log.warning(
                        f"Same-bookie fatal on {bookie} at alt line {new_line}, "
                        f"blocklisting"
                    )
                    bookie_blocklist.add(bookie)
                    break
                if not _is_stake_error(result.error or ""):
                    break

        # Mark all failed intermediate ladder rungs from this candidate (all
        # but the last appended result) so the notification layer can suppress
        # them and only surface the final outcome.
        _alt_end = len(new_results)
        if _alt_end - _alt_start > 1:
            for _r in new_results[_alt_start:_alt_end - 1]:
                if not _r.success:
                    _r.is_intermediate = True

        if alt_success:
            log.info(f"Alt line {new_line} filled. Stopping alt attempts.")
            tip._alt_target_odds_by_bookie = None
            # v5.69 (m8): restore the tipped line on the leg before returning.
            # The placed BetResult already records the actual placed_line, so the
            # placement record is unaffected, but leaving leg.line on the alt
            # value corrupted any downstream re-read (re-audit / dedup
            # fingerprint / bet-record). Matches the C1/C2 restore convention.
            leg.line = original_line
            return new_results

        # Revert for next candidate
        leg.line = original_line

    # All candidates exhausted without success
    leg.line = original_line
    tip._alt_target_odds_by_bookie = None
    return new_results


def _try_stat_fallback(
    tip: ParsedTip, sessions: list[dict], intended_stake: float,
) -> list[BetResult]:
    """
    Walk the configured stat fallback chain when the primary stat has been
    exhausted and the tipster doesn't supply explicit alt props
    (Kev / AusBets — Shook is excluded by config).

    Strategy: for each priority session in order, single-session
    price_check_sports(markets_filter=["player_props"]) to fetch all
    available player-prop markets for that session's bookie. Walk the
    fallback chain, find the closest line per stat with odds inside the
    10% tolerance band, place at the first match. Move to next session if
    nothing on this bookie fits.

    Cain 2026-04-30: primary placement failed because Sportsbet didn't
    carry PRA. With this helper, we'd price-check Sportsbet, see Cain
    Points 13.5 @ ~2.0 available, find it within tolerance of the tipped
    1.89, and place there.

    Returns a list of BetResult objects (placement attempts). Empty list
    if no fallback configured, no candidates fit, or every price-check
    failed.
    """
    if not tip.legs or not tip.legs[0].player or tip.is_sgm:
        return []

    leg = tip.legs[0]
    primary_stat = (leg.stat or "").lower()
    if not primary_stat:
        return []

    chain = _stat_fallback_cfg.chain_for(tip.sport, primary_stat)
    if not chain:
        log.info(
            f"stat_fallback: no chain for {tip.sport}.{primary_stat}, skipping"
        )
        return []

    market_map = _stat_market_map(tip.sport)
    if not market_map:
        return []

    sel_lower = (leg.selection or "").lower()
    if "over" in sel_lower:
        direction = "over"
    elif "under" in sel_lower:
        direction = "under"
    else:
        log.info(
            f"stat_fallback: leg selection '{leg.selection}' has no clear "
            f"direction, skipping"
        )
        return []

    tipped_odds = float(tip.suggested_odds or 0)
    if not tipped_odds or tipped_odds <= 1.0:
        log.info("stat_fallback: no usable tipped odds, skipping")
        return []

    tipped_line = float(leg.line or 0)
    odds_lo = tipped_odds * (1.0 - AUTO_ALT_ODDS_TOL)
    odds_hi = tipped_odds * (1.0 + AUTO_ALT_ODDS_TOL)
    LINE_RANGE = 5.0  # max gap from tipped line for any fallback candidate

    log.info(
        f"stat_fallback: searching chain {chain} for {leg.player} "
        f"({direction} {tipped_line}, tipped odds {tipped_odds}, "
        f"band {odds_lo:.2f}..{odds_hi:.2f})"
    )

    # Snapshot original leg state so we can restore on the way out.
    original_stat = leg.stat
    original_line = leg.line
    original_market = leg.market

    new_results: list[BetResult] = []
    remaining = intended_stake
    bookie_blocklist: set[str] = set()
    seen_attempts: set[tuple[str, str]] = set()  # (session_id, stat)

    for session in sessions:
        if remaining <= STAKE_FLOOR:
            break

        sid = str(session.get("session_id", ""))
        bookie = (session.get("bookie", "") or "").lower()
        if bookie in bookie_blocklist:
            continue

        # Single-session price-check, filtered to player props. Same call
        # the within-1.0 line tolerance block uses, so we know the response
        # shape: {"markets": {market_name: {"selections": [...]}}}.
        try:
            price_resp = hb.price_check_sports(
                session_id=sid, sport=tip.sport, event=tip.event,
                markets_filter=["player_props"],
            )
        except Exception as e:
            log.debug(f"stat_fallback price check failed on {sid}: {e}")
            continue
        if not price_resp.get("success"):
            continue

        markets_data = price_resp.get("markets") or {}
        player_l = leg.player.lower()

        # Walk the fallback chain on THIS session. First viable stat wins
        # on this session; fall through to next session if none fit.
        chosen = None
        for fallback_stat in chain:
            target_market = market_map.get(fallback_stat)
            if not target_market:
                continue
            market_data = markets_data.get(target_market) or {}
            selections = market_data.get("selections", []) or []
            if not selections:
                continue

            cands = []
            for s in selections:
                sel_player = (s.get("player") or "").lower()
                if sel_player != player_l:
                    continue
                sel_text = (s.get("selection") or "").lower()
                if direction not in sel_text:
                    continue
                try:
                    ln = float(s.get("line", 0))
                    od = float(s.get("odds", 0))
                except (TypeError, ValueError):
                    continue
                if abs(ln - tipped_line) > LINE_RANGE:
                    continue
                if not (odds_lo <= od <= odds_hi):
                    continue
                cands.append((ln, od))

            if cands:
                cands.sort(key=lambda c: (abs(c[0] - tipped_line), -c[1]))
                chosen = (fallback_stat, target_market, cands[0][0], cands[0][1])
                break

        if not chosen:
            log.debug(
                f"stat_fallback: nothing in chain {chain} fits on "
                f"{bookie}:{sid} for {leg.player}"
            )
            continue

        fb_stat, fb_market, fb_line, fb_odds = chosen

        attempt_key = (sid, fb_stat)
        if attempt_key in seen_attempts:
            continue
        seen_attempts.add(attempt_key)

        # Mutate leg to reflect the fallback target, then place via the
        # existing v4 ladder with liability cap. Restored on exit.
        leg.stat = fb_stat
        leg.line = fb_line
        leg.market = fb_market

        log.info(
            f"stat_fallback: placing {leg.player} {direction} {fb_line} "
            f"{fb_stat} @ {fb_odds} on {bookie} session {sid} "
            f"(remaining=${remaining:.2f})"
        )

        try:
            max_stake, cap_reason = session_priority.resolve_max_stake(
                sid, tip.sport, fb_market, fb_odds, remaining,
            )
        except Exception:
            max_stake = remaining
            cap_reason = "no-cap"

        if max_stake <= 0:
            continue

        steps = _v4_ladder_steps(max_stake)
        ladder_attempts: list[BetResult] = []
        success_here = False
        for step_stake in steps:
            res = _try_place_with_name_variants(
                tip, session, step_stake, leg.player,
            )
            ladder_attempts.append(res)
            if res.success:
                placed = res.stake or step_stake
                remaining -= placed
                log.info(
                    f"stat_fallback: placed ${placed:.2f} via "
                    f"{fb_stat}@{fb_line}, remaining ${remaining:.2f}"
                )
                success_here = True
                break
            err = res.error or ""
            if _is_same_bookie_fatal(err):
                bookie_blocklist.add(bookie)
                break
            if not _is_stake_error(err):
                break

        # Tag intermediate ladder failures (same scheme as v4 main path)
        if ladder_attempts:
            if success_here:
                for r in ladder_attempts[:-1]:
                    if not r.success:
                        r.is_intermediate = True
            else:
                for r in ladder_attempts[:-1]:
                    r.is_intermediate = True
            new_results.extend(ladder_attempts)

    # Restore primary leg state. Per-placement actuals live on each
    # BetResult.placed_* via _execute_bet.
    leg.stat = original_stat
    leg.line = original_line
    leg.market = original_market

    return new_results


def _place_with_spillover(tip: ParsedTip, sessions: list[dict]) -> list[BetResult]:
    """
    Place tip across sessions until full stake is filled.

    Behaviour:
      - Stake ladder per session: try 100/75/50/40/25/20/15/10/5% of remaining
        (rounded up to whole dollar) until one succeeds, then move on.
      - Same-bookie short-circuit: if a 'Selection/Market not found' error fires
        on one session, skip remaining same-bookie sessions (same odds, same
        markets, would fail identically) and try the next bookie.
      - Name variant retry: on 'Selection not found', try variants of player
        name (no hyphens, no periods, no suffixes, fuzzy match against bookie's
        available player list).
      - Tipster-specified alt_line fallback: if primary fails and the tipster
        provided an explicit alt (e.g. Shook "X pts same unit alt"), try that.
      - Automatic ±1 alt-line retry: if still nothing placed, try tipped_line
        ±1 for player props. Overs try lower first, unders try higher first.
        Only placed if bookie odds are within 10% of the tipped odds.
      - Consolidated notification: one Telegram message per tip with all
        placements + remaining unfilled stake (if any) flagged for manual.
    """
    intended_stake = tip.stake_dollars
    remaining_stake = intended_stake
    results: list[BetResult] = []
    bookie_blocklist: set[str] = set()  # bookies that returned fatal error this tip
    ambiguous_outcomes: list[dict] = []  # slow-rejection AMBIGUOUS_OUTCOME tracking

    for session in sessions:
        if remaining_stake <= STAKE_FLOOR:
            break

        bookie = session.get("bookie", "")
        if bookie in bookie_blocklist:
            log.info(f"Skipping {bookie} session {session.get('session_id')} - bookie blocklisted this tip")
            continue

        original_player = tip.legs[0].player if tip.legs else ""

        # Try stake ladder until something succeeds (or all rungs fail)
        steps = _ladder_steps(remaining_stake)
        success_on_session = False
        last_failure: BetResult | None = None
        # L23: snapshot results length so intermediate ladder failures can be
        # tagged after the loop (mirrors the pattern in _place_singles_v4).
        _session_results_start = len(results)

        for step_stake in steps:
            log.info(
                f"Stake ladder step ${step_stake:.2f} on {bookie} "
                f"session {session.get('session_id')} (remaining ${remaining_stake:.2f})"
            )
            result = _try_place_with_name_variants(
                tip, session, step_stake, original_player,
            )
            results.append(result)

            if result.success:
                placed = result.stake or step_stake
                remaining_stake -= placed
                log.info(f"Placed ${placed:.2f} on {bookie}, remaining: ${remaining_stake:.2f}")
                success_on_session = True
                break

            last_failure = result
            err_lower = str(result.error or "").lower()

            # ── Slow-rejection AMBIGUOUS_OUTCOME check (H1 fix 2026-05-30) ─
            # Mirrors _place_singles_v4. If the rejection took >= 5s and the
            # error is not a confirmed pre-placement reject, treat as ambiguous:
            # debit remaining_stake, blocklist bookie, break ladder, fire alert.
            _elapsed = result.elapsed_sec or 0.0
            _slow = _elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
            if (
                (
                    _slow
                    or getattr(result, "is_ambiguous", False)  # C5: fast ambiguous
                )
                and not _is_definitely_pre_placement(result.error or "")
            ):
                _amb_reason = "slow_rejection" if _slow else "fast_ambiguous"
                log.error(
                    f"v3: AMBIGUOUS OUTCOME ({_amb_reason.replace('_', ' ')}) "
                    f"{bookie}:{session.get('session_id')} stake=${step_stake:.2f} "
                    f"elapsed={_elapsed:.1f}s "
                    f"(threshold={STAKE_REJECT_LATENCY_THRESHOLD_SEC}s) "
                    f"err='{(result.error or '')[:80]}'. Debiting as placed, "
                    f"blocklisting bookie, stopping ladder."
                )
                ambiguous_outcomes.append({
                    "bookie": bookie,
                    "session_id": str(session.get("session_id", "")),
                    "stake": round(step_stake, 2),
                    "odds": result.odds or 0,
                    "elapsed_sec": round(_elapsed, 2),
                    "error": (result.error or "")[:200],
                    "reason": _amb_reason,
                })
                # Mark ambiguous so it's excluded from placed_results AND
                # failed_results, and is not re-bet by the alt-chain/auto-alt
                # below (NEW/H1 fix 2026-05-30: the stake was debited as placed
                # but success=False meant the alt paths re-attempted it).
                result.is_ambiguous = True
                remaining_stake -= step_stake
                bookie_blocklist.add(bookie)
                break

            # ── Smart line-move retry (singles only) ────────────────
            # If HyperBot reports the line moved or the tipped line didn't
            # match the bookie's candidates, try to find an acceptable line
            # among the alts returned in the error. Only retries once per
            # tip per session.
            #
            # Acceptance rules in _line_move_acceptable:
            #   A) Absolute gap <= 1.0 (any direction) — for the common case
            #      where a tipster sends 19.5 but the bookie main is 20.5
            #   B) Relative gap <= 10% AND direction favourable for the
            #      selection (Under easier with higher line, Over easier
            #      with lower line)
            # The odds tolerance is enforced separately by target_odds
            # at placement time, so a tougher line at the same odds gets
            # rejected by HyperBot via the target_odds floor.
            if (
                not tip.is_sgm
                and tip.legs
                and tip.legs[0].market not in ("h2h", "head_to_head")
                and not getattr(tip, "_line_move_retried", False)
            ):
                original_line = tip.legs[0].line or 0
                selection = tip.legs[0].selection or ""
                new_line: float | None = None

                # Try "Line moved X → Y" parse first (explicit new line)
                moved_to = _extract_moved_line(result.error or "")
                if moved_to is not None:
                    if _line_move_acceptable(original_line, moved_to, selection):
                        new_line = moved_to

                # Fall back to "Available: [line=A, line=B]" parse
                if new_line is None and "did not match" in err_lower:
                    candidates = _extract_available_lines(
                        result.error or "", tip.legs[0].player or ""
                    )
                    # Pick the best acceptable candidate: closest to tipped line.
                    # Exclude original_line: greedy 'line=N' regex also catches
                    # the "line=X did not match" preamble, so tipped_line ends
                    # up in candidates. Without this filter, closest pick = tipped
                    # line itself and the != guard below skips the retry.
                    acceptable = [
                        c for c in candidates
                        if c != original_line
                        and _line_move_acceptable(original_line, c, selection)
                    ]
                    if acceptable:
                        new_line = min(acceptable, key=lambda c: abs(c - original_line))

                if new_line is not None and new_line != original_line:
                    log.info(
                        f"Line-move retry: tipped={original_line} "
                        f"selection='{selection}' → trying new_line={new_line} "
                        f"(within 10% + favourable direction)"
                    )
                    tip.legs[0].line = new_line
                    # Recalculate target_odds proportionally if we have tipped odds.
                    # Rough heuristic: for Under, higher line = lower odds; for
                    # Over, higher line = higher odds. Keep this simple — preserve
                    # the 90%-of-tipped-odds baseline and let the bookie return
                    # whatever the actual odds are at the new line.
                    tip._line_move_retried = True
                    retry_result = _try_place_with_name_variants(
                        tip, session, step_stake, original_player,
                    )
                    results.append(retry_result)
                    if retry_result.success:
                        placed = retry_result.stake or step_stake
                        remaining_stake -= placed
                        log.info(
                            f"Line-move retry PLACED ${placed:.2f} on {bookie} "
                            f"at new line {new_line}"
                        )
                        success_on_session = True
                        # C1 (2026-05-31): restore the tipped line before the
                        # break (the restore below only runs on FAILURE), so a
                        # successful line-move doesn't leave the leg mutated for
                        # the next spillover session.
                        tip.legs[0].line = original_line
                        break
                    # Retry failed — restore and fall through to normal handling
                    log.info(
                        f"Line-move retry failed: {retry_result.error[:120] if retry_result.error else ''}"
                    )
                    tip.legs[0].line = original_line
                    last_failure = retry_result
                    err_lower = str(retry_result.error or "").lower()

            # Same-bookie fatal error: blocklist this bookie, stop trying
            # rungs on this session, move to next session.
            # H10 fix 2026-05-30: use last_failure.error (tracks the most
            # recent attempt including line-move retries) rather than result.error
            # (which still holds the original error after a retry).
            _last_err = (last_failure.error if last_failure else result.error) or ""
            if _is_same_bookie_fatal(_last_err):
                log.warning(
                    f"Same-bookie fatal error on {bookie}: {_last_err[:120]}... "
                    f"Blocklisting {bookie} for this tip."
                )
                bookie_blocklist.add(bookie)
                break

            # Stake error: keep dropping (only for genuine stake-size errors)
            if _is_stake_error(_last_err):
                continue

            # "price has changed" — HyperBot rejected because the snapshot
            # price moved between our price_check and place_bet. The current
            # market price is often still inside our 10% tolerance, so retry
            # once on the same session with target_odds dropped (let HyperBot
            # fill at current market). Caused 4-session waste + alt-line
            # escalation on Clingan 19.5 PR @ 1.87 → 1.80 on 2026-04-25.
            if _is_price_change_error(_last_err) and not getattr(
                tip, "_price_change_retried_on", set()
            ).__contains__(str(session.get("session_id", ""))):
                if not hasattr(tip, "_price_change_retried_on"):
                    tip._price_change_retried_on = set()
                tip._price_change_retried_on.add(str(session.get("session_id", "")))
                log.info(
                    f"Price-change retry: re-placing on {bookie} session "
                    f"{session.get('session_id')} without target_odds"
                )
                tip._skip_target_odds = True
                try:
                    retry_result = _try_place_with_name_variants(
                        tip, session, step_stake, original_player,
                    )
                finally:
                    tip._skip_target_odds = False
                results.append(retry_result)
                if retry_result.success:
                    placed = retry_result.stake or step_stake
                    remaining_stake -= placed
                    # Post-fill tolerance check (informational only — bet placed)
                    # BUG C: AFL >$2.00 uses the 15% floor, else 10% (matches the
                    # actual target_odds the bet was placed with).
                    actual_odds = retry_result.odds or 0
                    _floor = _afl_target_odds(tip.sport, tip.suggested_odds)
                    if (
                        tip.suggested_odds
                        and actual_odds
                        and _floor
                        and actual_odds < _floor
                    ):
                        log.warning(
                            f"Price-change retry filled OUTSIDE tolerance: "
                            f"actual {actual_odds} vs tipped {tip.suggested_odds} "
                            f"(floor {_floor}). "
                            f"Bet placed but flag for manual review."
                        )
                    else:
                        log.info(
                            f"Price-change retry PLACED ${placed:.2f} on {bookie} "
                            f"@ {actual_odds}"
                        )
                    success_on_session = True
                    last_failure = None
                    break
                # Retry failed — fall through to normal handling
                log.info(
                    f"Price-change retry failed: "
                    f"{retry_result.error[:120] if retry_result.error else ''}"
                )
                last_failure = retry_result

            # Other error: try next session (don't keep dropping stake)
            log.warning(f"Non-stake error, abandoning this session: {_last_err}")
            break

        if not success_on_session and last_failure:
            log.warning(
                f"All ladder steps failed on {bookie} session {session.get('session_id')}: "
                f"{last_failure.error}"
            )

        # L23: tag intermediate ladder failures for this session's results slice.
        # Mirrors _place_singles_v4: on success, all but the final result are
        # intermediate; on failure, all but the last failure are intermediate
        # (keep the last failure visible so the user sees what stopped placement).
        session_slice = results[_session_results_start:]
        if session_slice:
            if success_on_session:
                for r in session_slice[:-1]:
                    if not r.success:
                        r.is_intermediate = True
            else:
                for r in session_slice[:-1]:
                    r.is_intermediate = True

    # ── Tipster-specified alt props: fill unfilled stake into alts ──
    # Build ordered list of alt props to try. Supports both the legacy single
    # `alt_line` dict (kept for back-compat with existing Groq output) and the
    # new `alt_lines` list populated by _merge_batch_alts for multi-prop tips.
    # Each alt is tried in order, each starting with whatever stake remains
    # unfilled from the previous attempt. Runs whenever any stake is unfilled
    # — not only on total failure — so partial stake-cap hits can spill.
    total_placed_so_far = sum(r.stake or 0 for r in results if r.success)
    # Ambiguous stake was debited "as placed" in-loop; subtract it here so the
    # alt-chain does NOT re-bet a portion that may already have landed (NEW/H1).
    _ambiguous_total = round(sum(a.get("stake", 0) for a in ambiguous_outcomes), 2)
    remaining_for_alts = round(intended_stake - total_placed_so_far - _ambiguous_total, 2)

    alt_chain: list[dict] = []
    if tip.alt_line:
        alt_chain.append(tip.alt_line)
        tip.alt_line = None  # consumed
    if tip.alt_lines:
        alt_chain.extend(tip.alt_lines)
        tip.alt_lines = None  # consumed

    if alt_chain and remaining_for_alts > 0 and tip.legs:
        log.info(
            f"Primary left ${remaining_for_alts:.2f} unfilled. Trying "
            f"{len(alt_chain)} tipster alt prop(s) in order."
        )
        alt_leg = tip.legs[0]
        # L25: save original leg values before the alt-chain loop mutates them.
        # After the loop exits the leg would otherwise hold the last alt's values.
        _orig_leg = copy.copy(alt_leg)

        for alt_idx, alt in enumerate(alt_chain, 1):
            if remaining_for_alts <= STAKE_FLOOR:
                break

            # Apply alt's stat/line/selection/market to the leg in place
            alt_leg.stat = alt.get("stat", alt_leg.stat)
            try:
                alt_leg.line = float(alt.get("line", alt_leg.line))
            except (TypeError, ValueError):
                log.warning(f"Alt {alt_idx} has non-numeric line, skipping: {alt}")
                continue
            alt_leg.selection = alt.get("selection", alt_leg.selection)
            alt_leg.market = alt.get("market", alt_leg.market)
            tip._is_threshold = bool(alt.get("is_threshold", False))

            log.info(
                f"Alt {alt_idx}/{len(alt_chain)}: stat={alt_leg.stat} "
                f"line={alt_leg.line} sel={alt_leg.selection} "
                f"(trying to fill ${remaining_for_alts:.2f})"
            )

            # Reset blocklist per alt — different prop may succeed on bookies
            # that blocklisted the primary. Remaining stake carries forward.
            alt_bookie_blocklist: set[str] = set()
            alt_remaining = remaining_for_alts
            original_player = alt_leg.player

            for session in sessions:
                if alt_remaining <= STAKE_FLOOR:
                    break
                bookie = session.get("bookie", "")
                if bookie in alt_bookie_blocklist:
                    continue

                steps = _ladder_steps(alt_remaining)
                for step_stake in steps:
                    result = _try_place_with_name_variants(
                        tip, session, step_stake, original_player,
                    )
                    results.append(result)

                    if result.success:
                        placed = result.stake or step_stake
                        alt_remaining -= placed
                        log.info(
                            f"Alt {alt_idx} placed ${placed:.2f} on {bookie}, "
                            f"remaining ${alt_remaining:.2f}"
                        )
                        break
                    if _is_same_bookie_fatal(result.error or ""):
                        alt_bookie_blocklist.add(bookie)
                        break
                    if not _is_stake_error(result.error or ""):
                        break

            remaining_for_alts = alt_remaining  # carry forward to next alt

        # L25: restore original leg values after all alts have been tried.
        tip.legs[0].__dict__.update(_orig_leg.__dict__)

    # ── Automatic ±1 alt-line retry (player props only) ────────────
    # Skip when an ambiguous outcome occurred — the ±1 line is close to the
    # maybe-placed selection; re-betting risks a duplicate (NEW/H1).
    placed_results = [r for r in results if r.success]
    if not placed_results and not ambiguous_outcomes:
        auto_alt_results = _try_auto_alt_lines(tip, sessions, intended_stake)
        results.extend(auto_alt_results)

    # ── Consolidated notification + audit ───────────────────────────
    placed_results = [r for r in results if r.success]
    # Exclude ambiguous results from failures — their stake was debited as
    # placed and is accounted for separately; surfacing them would prompt a
    # manual re-place of a bet that may already exist (NEW/H1).
    failed_results = [
        r for r in results
        if not r.success and not getattr(r, "is_ambiguous", False)
    ]
    total_placed = sum(r.stake or 0 for r in placed_results)
    unfilled = round(intended_stake - total_placed - _ambiguous_total, 2)

    # Audit
    _log_jsonl(_audit_log_path(), {
        "type": "tip_outcome",
        "tipster": tip.tipster,
        "event": tip.event,
        "intended_stake": round(intended_stake, 2),
        "placed_stake": round(total_placed, 2),
        "unfilled_stake": unfilled,
        "placements": [
            {
                "session_id": r.session_id,
                "bookie": r.bookie,
                "stake": r.stake,
                "fill_odds": r.odds,
                "bet_id": r.bet_id,
            }
            for r in placed_results
        ],
        "failures": [
            {"session_id": r.session_id, "bookie": r.bookie, "error": r.error}
            for r in failed_results
        ],
    })

    # Consolidated notification
    if placed_results:
        notifier.notify_tip_placed_summary(tip, placed_results, intended_stake, unfilled)

    # Only flag as underfilled when there's genuinely unfilled stake.
    # Previously this used `>= STAKE_FLOOR` which fires on $0 unfilled when
    # the floor is 0, producing false-positive "Tip underfilled" warnings on
    # successful full placements.
    if unfilled > 0:
        log.warning(
            f"Tip underfilled: ${total_placed:.2f} of ${intended_stake:.2f} "
            f"placed, ${unfilled:.2f} unfilled"
        )
        notifier.notify_tip_unfilled_with_placements(
            tip, intended_stake, total_placed, unfilled,
            placed_results, failed_results,
        )
        _log_jsonl(ERROR_LOG, {
            "type": "tip_unfilled",
            "tipster": tip.tipster,
            "event": tip.event,
            "intended_stake": intended_stake,
            "placed_stake": total_placed,
            "unfilled_stake": unfilled,
            "last_error": failed_results[-1].error if failed_results else None,
            "message": tip.raw_message,
        })

    # Slow-rejection ambiguous outcomes — same handling as v4 (H1 fix 2026-05-30).
    if ambiguous_outcomes:
        _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)

    return results


# ────────────────────────────────────────────────────────────────────
# v4.0 — Liability-capped, multi-bookmaker singles placement
# ────────────────────────────────────────────────────────────────────
# Replaces _place_with_spillover when USE_LEGACY_PLACEMENT=false. Differs
# from v3.10 in three ways:
#
#  1. Per-bookmaker price comparison drives bookie selection, not pure
#     priority order. Best odds wins (with 10%-below-tipped floor); ties
#     broken by highest-priority unused session.
#
#  2. Per-session liability caps from sessions.yaml drive max stake. We
#     compute floor(cap / (live_odds - 1)) at placement time. Reused stake
#     ladder if HyperBot still rejects with "stake too high".
#
#  3. Each placement marks the session "used"; subsequent loop iterations
#     skip it. Re-prices between iterations because filling on bookie X
#     can move bookie X's price.
#
# All v3.10 fixes preserved: NBSP cleanup, JSON repair, line-move retry,
# alt-line target_odds, price-change retry, AGS handling. This function
# delegates the actual HTTP placement to _try_place_with_name_variants
# (same as v3.10), so all the per-call logic carries over.
#
# Wired into _process_tip when USE_LEGACY_PLACEMENT=false. Tipster
# alt-prop fallback and auto ±1 alt-line retry both run on top of v4
# placement results — same paths as v3.10.

def _v4_get_active_sessions_unfiltered(tip: ParsedTip) -> list[dict]:
    """
    Get HyperBot active sessions with sport-compatibility + suggested_bookie
    filtering applied, but WITHOUT the legacy SESSION_PRIORITY filter.

    v4 paths apply per-sport priority via session_priority.filter_and_order_sessions
    later in the flow, so we want raw sessions here without legacy filtering.
    """
    sessions = hb.get_sessions()
    if not sessions:
        return []
    # Filter foreign sessions (other PCs sharing HyperBot key) before any
    # downstream price-check or placement. Owned-session whitelist matches
    # the watchdog filter.
    sessions = [s for s in sessions if _is_owned_session(s.get("session_id", ""))]

    bookie = tip.suggested_bookie
    if bookie and tip.tipster not in TIPSTERS_IGNORE_SUGGESTED_BOOKIE:
        filtered = [s for s in sessions if s.get("bookie", "") == bookie]
        if filtered:
            sessions = filtered

    # HARD bookie lock (EasyMoneyAFL = sportsbet). Unlike the soft filter above,
    # this does NOT fall back to other bookies: if the forced bookie has no
    # session, `sessions` becomes empty and the tip routes to manual. This is
    # the "sportsbet specific" guarantee — never place EasyMoney on Tab/etc.
    forced_bookie = TIPSTERS_FORCE_BOOKIE.get(tip.tipster)
    if forced_bookie:
        sessions = [s for s in sessions if (s.get("bookie", "") or "").lower() == forced_bookie]
        if not sessions:
            log.warning(
                f"Forced-bookie '{forced_bookie}' for tipster '{tip.tipster}' has "
                f"no active session — tip will route to manual (not placed elsewhere)."
            )

    sport_filter = {
        "afl": ["sportsbet", "bet365", "tab", "ladbrokes"],
        "nba": ["sportsbet", "tab"],
        "nbl": ["sportsbet", "tab"],
        "mlb": ["sportsbet"],
    }
    allowed = sport_filter.get(tip.sport)
    if allowed:
        filtered = [s for s in sessions if s.get("bookie", "") in allowed]
        if filtered:
            sessions = filtered

    return sessions


def _is_ambiguous_result(r: BetResult) -> bool:
    """A maybe-landed outcome: a fast API-level ambiguous flag (timeout / 5xx /
    dropped connection, set by _execute_bet from the client) OR a slow rejection
    (elapsed >= threshold) that is not clearly pre-placement. The bet MAY be on
    the account, so callers must NOT ladder down or re-prompt a manual re-place
    (the Erasmus/Dawson double-stake). Shared by the fan-out ladder + rollup.

    NOTE: named distinctly from the STRING-based _is_ambiguous_outcome(error: str)
    near the top of this file — they must NOT collide. v5.12 originally reused that
    name and shadowed the string version, breaking the legacy-SGM ambiguous guard
    (AttributeError on str). Fixed v5.13."""
    if r.success:
        return False
    # v5.53: Tier-1 reconcile POSITIVELY confirmed nothing landed (a clean
    # /api/pending_bets miss) — NOT ambiguous regardless of elapsed/flags.
    # Set only by _reconcile_fanout_ambiguous; keeps the rollups' at-risk and
    # CRITICAL-alert buckets honest (2026-06-12 Eddie Geelong $250 case).
    if getattr(r, "_reconcile_confirmed_not_placed", False):
        return False
    if getattr(r, "is_ambiguous", False):
        return True
    _el = getattr(r, "elapsed_sec", None) or 0.0
    return (
        _el >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
        and not _is_definitely_pre_placement(r.error or "")
    )


def _reconcile_fanout_ambiguous(tip, sess: dict, r, step: float, label: str):
    """v5.53: Tier-1 reconciliation for a fan-out AMBIGUOUS rung (shared by the
    AFL singles worker `_fanout_place_account` and the SGM worker
    `_sgm_fanout_place_account`). Polls /api/pending_bets via
    reconcile.decide_ambiguous — gated by RECONCILE_AMBIGUOUS, spill ALWAYS off
    (never re-bet; trader-review bets can land >30s after the poll window):

      'placed'      -> the bet IS on the books: stay COMMITTED but debit the
                       ACTUAL stake (auto-cap: smaller counts) and mark the
                       error CONFIRMED PLACED so the alert reads confirmed,
                       not maybe.
      'not_placed'  -> pending_bets POSITIVELY confirmed nothing landed:
                       convert to a CLEAN failure (no debit, no ambiguous
                       CRITICAL; the stake surfaces as UNFILLED -> the normal
                       unfilled/manual alert). 2026-06-12 Eddie Geelong o178.5:
                       $250 on 65465 was debited maybe-placed + CRITICAL'd;
                       Wilson manually confirmed at the bookie it never landed
                       — this branch automates that check.
      'conservative'/'spill'/error -> unchanged Erasmus default: debit-as-
                       placed, ambiguous CRITICAL, ladder stays stopped.

    NEVER continues the ladder, whatever the outcome — a confirmed not-placed
    re-bet is Tier-2 spill, deliberately OFF for sports."""
    import time as _t
    import reconcile as _recon
    # Guard B (v6.03, BROADENED v6.07): did this ambiguous come from an UNRESOLVED
    # CORRELATION ID? If so HB never confirmed the cid resolved AT ALL, so a
    # pending_bets 'not found' is far less trustworthy than in the normal
    # fast/slow-reject case (the bet can still land after the ~30s reconcile window).
    # See the not_placed branch below.
    # v6.07 (sweep HIGH #1/#5): this used to string-match ONLY "hard poll deadline",
    # but hyperbot_client emits THREE unresolved-cid envelopes (cid timeout / poll
    # budget exhausted / hard poll deadline). The other two were downgraded to a clean
    # not-placed and then re-staked on sibling accounts AND hand-placed -> DOUBLE STAKE
    # (2026-07-16 AFL s65465 $120; 2026-06-19 racing tip 62247). Now read the
    # STRUCTURAL cid_unresolved flag, with the text match kept as a belt for any
    # envelope that predates the flag.
    _hard_deadline = bool(getattr(r, "cid_unresolved", False)) or _err_is_cid_unresolved(
        getattr(r, "error", "")
    )
    try:
        leg0 = tip.legs[0] if getattr(tip, "legs", None) else None
        sel = ""
        if leg0 is not None:
            sel = (getattr(leg0, "player", "") or getattr(leg0, "selection", "") or "")
        _el = getattr(r, "elapsed_sec", None) or 0.0
        # v5.9x (max-stake review, BUG C): reconcile/debit against the ACTUAL stake
        # attempted on this account (after a max-stake rebet r.stake is the smaller
        # SB max, not `step`), so pending_bets matches the landed amount and the
        # debit doesn't over-charge. Falls back to `step` when r carries no stake.
        _eff_step = float(getattr(r, "stake", None) or step)
        # v5.69 (M3): reconcile against the BOOKIE-aliased event name, the same
        # string placement sent to HyperBot (_execute_bet uses _bookie_event),
        # so /api/pending_bets matches. tip.event is the internal Squiggle name
        # ("Greater Western Sydney") but the bet lands under the alias ("GWS
        # Giants" for Sportsbet) — passing the un-translated name made
        # pending_bet_matches' substring gate fail and a genuinely-landed bet
        # read as not-found -> converted to a clean failure -> manual re-bet
        # (double bet by hand) + lost ledger row.
        _recon_event = _bookie_event(tip.event, sess.get("bookie", ""), tip.sport)
        decision = _recon.decide_ambiguous(
            hb, sess.get("account_id"), event=_recon_event, stake=_eff_step,
            sport=(tip.sport or "afl"), selection=sel,
            submit_ts=_t.time() - _el,
            reconcile_enabled=RECONCILE_AMBIGUOUS, spill_enabled=False,
        )
    except Exception as e:
        log.error(f"{label}: reconcile errored ({e}) — staying conservative "
                  f"(debit-as-placed)")
        return r
    action = decision.get("action")
    if action == "placed":
        try:
            actual = float(decision.get("actual_stake", _eff_step) or _eff_step)
        except (TypeError, ValueError):
            actual = _eff_step
        # v5.55 (audit, verified-critical): convert to a full SUCCESS — the
        # bet IS on the books (pending_bets confirmed it), so it must flow
        # into placed accounting / the BET PLACED summary / the ledger, NOT
        # sit in the ambiguous bucket firing a maybe-landed CRITICAL with a
        # contradictory CONFIRMED PLACED error text (the v5.53 behaviour).
        # NOTE clearing is_ambiguous alone is NOT enough — the elapsed-time
        # prong of _is_ambiguous_result would still classify it; success=True
        # short-circuits that correctly.
        match = decision.get("match") or {}
        r.success = True
        r.is_ambiguous = False
        r.stake = actual
        r.error = None
        if not getattr(r, "bet_id", None):
            r.bet_id = match.get("bookie_bet_id") or match.get("id")
        if not getattr(r, "odds", None):
            r.odds = match.get("odds")
        try:
            r._requested_stake = actual
            r._reconcile_confirmed_placed = True
        except Exception:
            pass
        log.warning(f"{label}: reconcile CONFIRMED placed ${actual:.2f} "
                    f"(bet_id={r.bet_id}) — recording as PLACED at the "
                    f"actual stake")
        return r
    if action == "not_placed":
        # Guard B (v6.03): NEVER downgrade a HARD-poll-deadline ambiguous to a clean
        # not-placed. decide_ambiguous returns not_placed when /api/pending_bets is
        # reachable but the bet isn't visible YET — fine for a fast/slow reject, but a
        # hard-deadline means the cid NEVER resolved, so it can still land after the
        # poll window. Converting to UNFILLED would prompt a manual re-place -> a LATE
        # land = DOUBLE STAKE. Stay conservative (debit-as-placed + ambiguous CRITICAL).
        if _hard_deadline:
            log.warning(f"{label}: reconcile says not-placed BUT this was a HARD poll "
                        f"deadline (cid never resolved) — staying CONSERVATIVE "
                        f"(debit-as-placed), NOT converting to unfilled (Guard B)")
            return r
        r.is_ambiguous = False
        r.stake = 0
        try:
            r._requested_stake = 0
            r._reconcile_confirmed_not_placed = True
        except Exception:
            pass
        r.error = f"confirmed not-placed (reconcile): {(r.error or '')[:120]}"
        log.info(f"{label}: reconcile CONFIRMED not-placed — clean failure, "
                 f"stake stays UNFILLED (no debit, no ambiguous critical)")
        return r
    log.info(f"{label}: reconcile inconclusive "
             f"({decision.get('reason', action)}) — conservative "
             f"debit-as-placed stands")
    return r


def _fanout_place_account(tip, sess: dict, ladder: list, resolved: dict) -> BetResult:
    """Place ONE account in the AFL fan-out, walking its liability ladder: try the
    top bracket first; on a stake-too-high / MBL rejection, drop to the next
    bracket (e.g. $100→$74→$50 liability) and retry — same graceful degradation
    _place_singles_v4 does per session, but here every account ladders in its OWN
    thread so all accounts still START together. Stops on the first success, on a
    non-stake error, or on an AMBIGUOUS (maybe-landed) outcome — never ladder past
    an ambiguous, that could double-stake. Returns the terminal BetResult; the rung
    requested is stashed as `_requested_stake` for auto-cap detection."""
    sid = str(sess.get("session_id", ""))
    bk = sess.get("bookie", "unknown")
    last: BetResult | None = None
    for i, step in enumerate(ladder):
        r = _execute_bet(tip, sess, step, presolved=resolved)
        try:
            r._requested_stake = step
        except Exception:
            pass
        if r.success:
            if i > 0:
                log.info(
                    f"AFL fan-out: {bk}:{sid} laddered to rung {i + 1} (${step:.2f}) "
                    f"after {i} stake-reject(s)"
                )
            return r
        if _is_ambiguous_result(r):
            log.warning(
                f"AFL fan-out: {bk}:{sid} AMBIGUOUS on ${step:.2f} — stopping ladder "
                f"(bet may have landed; not retrying a lower rung)"
            )
            # v5.53: Tier-1 confirm/deny via /api/pending_bets. Ladder stays
            # stopped either way; only the accounting + alert severity change.
            return _reconcile_fanout_ambiguous(
                tip, sess, r, step, f"AFL fan-out {bk}:{sid}")
        last = r
        if not _is_stake_error(r.error or ""):
            # v5.68 (Wilson): ONE retry on a TRANSIENT pre-placement reject (proxy
            # 403 / auth refusal / network) — the bet was NEVER submitted, so
            # retrying the SAME rung once carries ZERO double-stake risk. Alex
            # 65463's Bailey $150 on 06-14 was a one-off proxy 403 that placed
            # fine minutes later -> a retry would have filled it instead of
            # dropping to Manual. `_is_definitely_pre_placement` is the narrow
            # provably-not-placed gate. (Stake-rejects ladder DOWN below;
            # AMBIGUOUS/maybe-landed already stopped above and is NEVER retried.)
            if AFL_FANOUT_PREPLACEMENT_RETRY and _is_definitely_pre_placement(r.error or ""):
                _orig_err = r.error or ""
                _el = _orig_err.lower()
                # v5.92 (Wilson): short problem label for the bet-log / manual note.
                # v5.92 review NIT: only call it a proxy-403 when 403 co-occurs with a
                # proxy/forbidden/client-error marker (a bare "403" substring in an
                # unrelated error must not be mislabelled a proxy error).
                _prob = ("403 proxy error"
                         if ("proxy responded with non 200" in _el
                             or ("403" in _el and ("proxy" in _el or "forbidden" in _el
                                                   or "client error" in _el)))
                         else (_orig_err[:50] or "pre-placement reject"))
                log.info(
                    f"AFL fan-out: {bk}:{sid} transient pre-placement reject on "
                    f"${step:.2f} ({_orig_err[:60]}) — retrying SAME rung once"
                )
                time.sleep(AFL_FANOUT_RETRY_DELAY_SEC)
                r = _execute_bet(tip, sess, step, presolved=resolved)
                try:
                    r._requested_stake = step
                except Exception:
                    pass
                if r.success:
                    # v5.92: recovered on the re-bet — tag so the bets_placed.csv
                    # `note` column + the BET PLACED summary say "account X hit XX,
                    # fixed on re-bet" (same stake; a 403 is a clean no-debit reject).
                    try:
                        r._recovered_note = f"{bk}:{sid} {_prob} — fixed on re-bet"
                    except Exception:
                        pass
                    log.info(f"AFL fan-out: {bk}:{sid} retry PLACED ${step:.2f} "
                             f"(recovered after a {_prob} on the first attempt)")
                    return r
                if _is_ambiguous_result(r):
                    return _reconcile_fanout_ambiguous(
                        tip, sess, r, step, f"AFL fan-out {bk}:{sid} (retry)")
                last = r
                if _is_stake_error(r.error or ""):
                    log.info(f"AFL fan-out: {bk}:{sid} retry hit stake-reject "
                             f"${step:.2f}, laddering down")
                    continue
                # v5.92: FAILED TWICE on the same message (pre-placement both times,
                # e.g. the proxy 403) — tag so the manual/UNFILLED alert flags it.
                try:
                    r._retry_failed_twice = True
                    r._retry_note = f"{bk}:{sid} {_prob} — FAILED TWICE on this message"
                except Exception:
                    pass
                # retry ALSO a non-stake error -> fall through to abandon.
            log.info(
                f"AFL fan-out: {bk}:{sid} non-stake error on ${step:.2f} — abandoning "
                f"ladder: {(r.error or '')[:80]}"
            )
            return r
        # v5.9x: the Sportsbet max-stake rebet lives inside _execute_bet (the
        # shared chokepoint), so by the time a stake-reject bubbles up here the
        # single max-fill attempt already happened. This just continues the old
        # blind ladder as the backstop (non-SB bookies, or SB when the field was
        # absent / the max itself re-rejected).
        log.info(f"AFL fan-out: {bk}:{sid} stake-reject ${step:.2f}, laddering down")
    return last if last is not None else BetResult(
        success=False, tip=tip, session_id=sid, bookie=bk,
        error="fan-out: empty ladder", timestamp=datetime.now())


def _afl_fanout_weights(sessions: list, sport: str, market: str) -> dict:
    """v5.65: capacity weight per session = its TOP liability bracket for
    `market` (the cap drives how much that account can carry). Returns
    {sid: weight}, or {} if ANY account lacks a clean numeric cap
    (unlimited / mbl / none) — the caller then falls back to the even split so
    weighting never mis-sizes. De-dupes by session_id. PURE (no network), so
    unit-tested directly. For the uniform 4.5x account (Ryan) vs 1x accounts
    the weights come out 4.5:1:1:1:1 — exactly Wilson's spec."""
    weights: dict[str, float] = {}
    seen: set[str] = set()
    for s in sessions:
        sid = str(s.get("session_id", ""))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        cap = session_priority.lookup_liability_cap(sid, sport, market)
        if isinstance(cap, (list, tuple)) and cap:
            weights[sid] = float(cap[0])
        elif isinstance(cap, (int, float)) and cap > 0:
            weights[sid] = float(cap)
        else:
            return {}  # non-numeric cap -> abandon weighting (even split)
    return weights


def _afl_fanout_targets(sessions: list, sport: str, market: str,
                        intended: float, tipster: str, units: float):
    """v5.66: per-account TARGET stake + ladder mode for the AFL singles fan-out.

    Returns (targets {sid: stake}, decay_factor):
      - decay_factor is None  -> ladder via the yaml liability brackets
        (resolve_stake_steps), as before.
      - decay_factor is a float (e.g. 0.9) -> ladder is a step-down from the
        target by that factor (Eddie big-bet mode).
      - targets {} (with None) -> caller falls back to the even split.

    Shapes (Wilson 2026-06-14):
      NORMAL (Saiyan any size; Eddie units <= EDDIE_FANOUT_BIG_UNITS):
        capacity-weighted, high-cap account CLAMPED to AFL_FANOUT_RATIO_CAP x the
        smallest weight -> 4:1:1:1:1. $75 ea + $300 Ryan (Saiyan @600);
        $125 ea + $500 Ryan (Eddie @2.5u=1000). Scales with the unit.
      EDDIE BIG (units > EDDIE_FANOUT_BIG_UNITS): v5.77 EVEN split (intended/n) +
        10% step-down decay ladder per account. The SB accounts are equal-cap now,
        so the old "$150 floor on the limited accounts + dump the remainder on the
        single highest-cap account" shape was RETIRED (it concentrated ~$600 on
        whichever account sorted first — 06-19 Uwland on Adam). The disposals-overs
        redistribute top-up mops up any unfilled remainder.
    PURE (no network) so it's unit-tested directly."""
    weights = _afl_fanout_weights(sessions, sport, market)
    if not weights:
        return {}, None

    # Eddie big bets (units > EDDIE_FANOUT_BIG_UNITS): EVEN split + decay ladder.
    # v5.77 (Wilson 2026-06-20): was "$150 floor on the limited accounts + dump the
    # REMAINDER on the single highest-cap account" — designed when Ryan 102506 was
    # the 4.5x account. Ryan was limited (v5.73) and Adam equalised (v5.77), so all
    # 4 SB sports accounts are now equal-cap and each can take ~$393; the old logic
    # arbitrarily concentrated ~$400-600 on whichever account sorted first (Adam,
    # 06-19 Uwland). Now split intended/n EVENLY and let each account's DECAY ladder
    # fill up to its own bookie limit; the disposals-overs redistribute top-up
    # (main.py) mops up any account that failed/under-filled.
    if tipster == "eddie_afl" and (units or 0) > EDDIE_FANOUT_BIG_UNITS:
        n = len(weights)
        per = round(intended / n, 2) if n else 0.0
        targets = {s: per for s in weights if per > 0}
        return targets, (EDDIE_FANOUT_DECAY if EDDIE_FANOUT_DECAY and 0 < EDDIE_FANOUT_DECAY < 1 else 0.9)

    # Normal: clamp the high-cap account to RATIO_CAP x the smallest -> 4:1:1:1:1.
    if AFL_FANOUT_RATIO_CAP and AFL_FANOUT_RATIO_CAP > 0:
        min_w = min(weights.values())
        if min_w > 0:
            weights = {s: min(w, AFL_FANOUT_RATIO_CAP * min_w) for s, w in weights.items()}
    total = sum(weights.values())
    if total <= 0:
        return {}, None
    return {s: round(intended * w / total, 2) for s, w in weights.items()}, None


def _is_afl_disposals_over(tip: ParsedTip) -> bool:
    """v5.79 (Wilson 2026-06-20): True for an AFL DISPOSALS-OVER fan-out single (any
    tipster — Saiyan or Eddie) — the scope for the redistribute-to-successful reroute.
    OVERS ONLY: v5.78 briefly widened it to unders too, but the UNDER per-account cap
    ([124,99,74,50]) is far below the OVER/threshold cap ([300,250,200,150]), so the
    accounts that already placed an under have little headroom — rerouting an under's
    leftover just re-rejects. Overs have the headroom, so ONLY overs reroute; an under
    that doesn't fill first time is left as-is -> manual ("it is what it is"). SGMs are
    on a different path (never reach _place_afl_fanout) so they're excluded anyway."""
    if (getattr(tip, "sport", "") or "").lower() != "afl":
        return False
    leg = tip.legs[0] if getattr(tip, "legs", None) else None
    if not leg:
        return False
    stat = (getattr(leg, "stat", "") or "").lower()
    mkt = (getattr(leg, "market", "") or "").lower()
    if not ("disposals" in stat or "disposals" in mkt):
        return False
    side = (getattr(leg, "selection", "") or "").strip().lower()
    return ((side == "over" or side.endswith(" over")
             or getattr(tip, "_is_threshold", False)) and "under" not in side)


def _afl_redistribute_topup(tip, placed_results, unfilled,
                            sessions_by_sid, resolved_by_sid,
                            exclude_sids=None, max_passes=AFL_REDISTRIBUTE_MAX_PASSES):
    """v5.94 (Wilson 2026-07-05, from the 07-04 failure review): re-fan an AFL fan-out's
    UNFILLED remainder onto the accounts PROVEN healthy, in BOUNDED MULTI-PASSES.

    v5.78/79 was a SINGLE even-split pass across the base-pass successes: if a top-up leg
    itself failed (a re-403 on a dead-proxy session — 102506/65463 on 07-04), that slice
    was abandoned to a manual crumb ($143 lost over Connor Macdonald/Shai Bolton/Cripps),
    and it never fired at all for under/total/goalscorer markets ($1,267 lost). Now:

      PASS 0 splits the remainder across the base-pass successes; each later PASS re-splits
      whatever is STILL unfilled across ONLY the accounts whose top-up JUST SUCCEEDED
      (proven-healthy this second) — so a re-403'd crumb re-shops onto a working sibling
      instead of dropping. Stop when: remainder < AFL_FANOUT_MIN_STAKE, no healthy target
      remains, a pass places nothing, or max_passes is hit.

    - `exclude_sids`: sessions that failed the base pass for a NON-CAP reason (403/auth/508)
      are never targeted — a dead proxy / dry account won't recover mid-tip. A session that
      re-fails pre-placement DURING a pass is added to the exclusion for the next pass.
    - If the even share `remainder/n` falls below the bookie $min, CONCENTRATE onto the
      fewest accounts that each clear the min (so a small remainder actually places in one
      shot rather than fragmenting into sub-min crumbs that drop).

    Returns all top-up BetResults across passes (caller classifies + merges).
    SAFE: `remaining` decreases each pass by the COMMITTED stake — clean placed PLUS any
    maybe-landed/ambiguous top-up (its at-risk rung), treated as committed so a landed-but-
    uncertain leg is NEVER re-shopped onto a sibling — so base + top-ups can never exceed the
    unit (no over-stake, no ambiguous double-stake); reuses _fanout_place_account (ambiguous
    STOPS its ladder); bounded passes."""
    import concurrent.futures
    excluded = set(str(s) for s in (exclude_sids or []))

    def _healthy(results):
        sids, seen = [], set()
        for r in results:
            sid = str(getattr(r, "session_id", "") or "")
            if (sid and sid not in seen and sid not in excluded
                    and sid in sessions_by_sid and sid in resolved_by_sid):
                seen.add(sid)
                sids.append(sid)
        return sids

    out: list[BetResult] = []
    remaining = round(unfilled, 2)
    targets = _healthy(placed_results)          # PASS 0 targets = base-pass successes
    for _pass in range(max_passes):
        if remaining <= AFL_FANOUT_MIN_STAKE or not targets:
            break
        n = len(targets)
        per = round(remaining / n, 2)
        if per < AFL_FANOUT_MIN_STAKE:
            # too thin to split n ways -> concentrate on the FEWEST accounts that each
            # clear the $min, so the remainder lands instead of fragmenting into crumbs.
            k = max(1, int(remaining // AFL_FANOUT_MIN_STAKE))
            targets = targets[:k]
            n = len(targets)
            per = round(remaining / n, 2)
        ladder = [round(per * f, 2) for f in (1.0, 0.9, 0.8, 0.7)]
        ladder = [s for s in ladder if s >= AFL_FANOUT_MIN_STAKE] or [round(per, 2)]
        log.info(
            f"AFL redistribute pass {_pass + 1}/{max_passes}: ${remaining:.2f} unfilled -> "
            f"${per:.2f} top-up across {n} healthy account(s) {targets} (ladder {ladder})"
        )
        jobs = [(sessions_by_sid[s], list(ladder), resolved_by_sid[s]) for s in targets]
        pass_out: list[BetResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {ex.submit(_fanout_place_account, tip, s, l, res): s
                    for (s, l, res) in jobs}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    pass_out.append(fut.result())
                except Exception as e:
                    log.error(f"AFL redistribute: top-up raised: {e}")
        out.extend(pass_out)
        placed_now = round(sum(r.stake or 0 for r in pass_out if r.success), 2)
        # A maybe-landed (AMBIGUOUS) top-up is treated as COMMITTED (it may have landed at
        # the bookie): subtract its at-risk stake from `remaining` too, NOT just the clean
        # successes. Otherwise the next pass re-shops that slice onto a healthy sibling and —
        # if the ambiguous bet DID land — the tip OVER-STAKES its unit. Mirrors the call-site
        # ambiguous_total accounting (same _is_ambiguous_result predicate; at-risk = the rung
        # fired, stashed by _fanout_place_account as _requested_stake). A reconcile-confirmed-
        # NOT-placed leg is not ambiguous (_is_ambiguous_result False) so it correctly stays
        # re-placeable in `remaining`. The break stays keyed on REAL successes so a pass that
        # produced only an ambiguous still stops (never spins).
        committed_now = round(placed_now + sum(
            (getattr(r, "_requested_stake", None) or r.stake or 0)
            for r in pass_out if (not r.success and _is_ambiguous_result(r))), 2)
        remaining = round(max(0.0, remaining - committed_now), 2)
        if placed_now <= 0:
            break  # no REAL fill this pass -> stop (don't spin on dead/ambiguous targets)
        # a session that re-failed pre-placement (403/auth) this pass is dropped from the
        # next; the next pass targets ONLY the accounts that JUST succeeded (proven-healthy).
        for r in pass_out:
            if not r.success and _is_definitely_pre_placement(getattr(r, "error", "") or ""):
                excluded.add(str(getattr(r, "session_id", "") or ""))
        targets = _healthy([r for r in pass_out if r.success])
    if remaining > AFL_FANOUT_MIN_STAKE:
        log.info(f"AFL redistribute: ${remaining:.2f} still unfilled after "
                 f"{max_passes} pass(es) -> manual")
    return out


# Dello AFL: standard placeable player-prop stats. ANY other stat (e.g. an
# exotic "most disposals" / "most clearances") -> manual.
_DELLO_STD_STATS = {
    "disposals", "goals", "marks", "tackles", "kicks",
    "handballs", "clearances", "hitouts", "fantasy_points",
}


def _place_dello_single(tip: ParsedTip) -> list[BetResult]:
    """Dello AFL placement — SINGLES ONLY, Sportsbet ONLY, flat/$1-test stake.

    Wilson's rules (dello_integration_plan.md): auto-place a clean player-prop
    SINGLE only when the LIVE Sportsbet price is in [DELLO_BAND_LO, DELLO_BAND_HI].
    A live price above the band (>HI, e.g. >3.00) OR below it (<LO) -> manual. A
    live SB price >DELLO_SB_WORSE_GATE below Dello's quoted price -> manual (his
    other-book/multi price was materially better). SGMs / cross-game multis /
    exotics / team markets -> manual: HyperBot has NO cross-game-multi primitive,
    and the AFL SGM line bug routes SGMs to manual like Saiyan. Anything
    unresolved / ambiguous -> manual (real money: default to manual).

    The stake is already flat/$1-clamped by _apply_dello_flat_stake before
    place_tip; the event is already resolved (tip.event). Places ONE bet on the
    top-priority Sportsbet account — no fan-out (flat stake, not split)."""
    def _manual(reason: str) -> list:
        tip.alert_only = True
        tip.alert_reason = reason
        log.info(f"[dello_afl] -> MANUAL: {reason}")
        notifier.notify_manual_alert(tip)
        return []

    # 1. SGM handling (v6.00): a SAME-GAME player-prop SGM (Dello's 2-player disposals
    #    combos, e.g. the 07-15 Geelong v St Kilda SGMs) now AUTO-PLACES via the SGM
    #    fan-out (like Saiyan) instead of manual — a same-game SGM is a real HyperBot
    #    ticket. _place_sgm_fanout scopes EVERY leg to the event's two teams and forbids
    #    the cross-game global-roster fallback, so a genuinely CROSS-game leg fails to
    #    resolve -> the SGM routes to manual/unfilled (never a wrong cross-game bet).
    #    Dello stays $1-test (stake already clamped by _apply_dello_flat_stake) and
    #    places on the AFL SGM priority (Daniel). Non-player-prop / non-standard-stat
    #    legs stay manual. A non-SGM multi stays manual. A single flows to step 2.
    if getattr(tip, "is_sgm", False):
        _sgm_legs = tip.legs or []
        _all_std_pp = len(_sgm_legs) >= 2 and all(
            (l.market or "").lower() == "player_prop"
            and (l.stat or "").lower() in _DELLO_STD_STATS
            for l in _sgm_legs
        )
        if _all_std_pp:
            log.info(
                f"[dello_afl] SAME-GAME player-prop SGM ({len(_sgm_legs)} legs) -> SGM "
                f"fan-out (Dello $1-test, ${tip.stake_dollars}): {tip.event}"
            )
            return _place_sgm_fanout(tip)
        return _manual("SGM with a non-player-prop / non-standard-stat leg — place by hand")
    if len(tip.legs) != 1:
        return _manual("multi-leg (non-SGM) — place by hand")
    leg = tip.legs[0]
    if (leg.market or "").lower() != "player_prop":
        return _manual(f"non-player-prop market '{leg.market}' (team/exotic) — place by hand")
    if (leg.stat or "").lower() not in _DELLO_STD_STATS:
        return _manual(f"non-standard stat '{leg.stat}' (exotic, e.g. 'most X') — place by hand")

    # 2. Sportsbet session (TIPSTERS_FORCE_BOOKIE already restricts to SB); top priority.
    raw_sessions = _v4_get_active_sessions_unfiltered(tip)
    sessions = (
        session_priority.filter_and_order_sessions(raw_sessions, "afl", is_sgm=False)
        if raw_sessions else []
    )
    seen_sids: set[str] = set()
    ordered: list[dict] = []
    for s in sessions:
        _sid = str(s.get("session_id", ""))
        if not _sid or _sid in seen_sids:
            continue
        seen_sids.add(_sid)
        ordered.append(s)
    if not ordered:
        return _manual("no active Sportsbet session for Dello — place by hand")
    sess = ordered[0]

    # 3. Resolve the exact catalog line + LIVE SB odds. KEEP the wrong-selection
    #    ceiling (live > 1.25x tipped -> manual); the band below is Dello's own
    #    floor so apply_floor=False. A catalog miss -> (None, BetResult) -> manual.
    resolved, manual_br = _resolve_single_for_placement(
        tip, sess, apply_ceiling=True, apply_floor=False, exact_only=True,
    )
    if manual_br is not None:
        return _manual(f"Sportsbet resolve -> manual: {getattr(manual_br, 'error', '') or 'unresolved line/selection'}")
    live = resolved.get("live_odds")
    if live is None:
        return _manual("Sportsbet has no live price for this exact line/selection — place by hand")
    live = float(live)

    # 4. Odds band (rules 1 & 2): only [DELLO_BAND_LO, DELLO_BAND_HI] auto-places.
    if live > DELLO_BAND_HI:
        return _manual(f"live SB {live} > {DELLO_BAND_HI} (only singles <= {DELLO_BAND_HI} auto-place) — place by hand")
    if live < DELLO_BAND_LO:
        return _manual(f"live SB {live} < {DELLO_BAND_LO} floor — place by hand")

    # 5. Rule 4: NO tipster odds in the text -> we can't apply the rule-3 price
    #    gate, and the resolver's wrong-selection ceiling is disabled when
    #    tipped<=1.0, so the bet would place on the band alone -> route to manual.
    ref = float(tip.suggested_odds or 0) or None
    if ref is None:
        return _manual("no tipster odds in the text — can't apply the rule-3 price gate; place by hand")
    # Rule 3: live SB materially worse than Dello's quoted price -> manual.
    if live < ref * (1.0 - DELLO_SB_WORSE_GATE):
        return _manual(
            f"live SB {live} is >{int(DELLO_SB_WORSE_GATE * 100)}% worse than Dello's quoted {ref} "
            f"(his other-book/multi price is better) — place by hand"
        )

    # 6. PLACE the single on the one SB account at the flat/$1-test stake.
    stake = tip.stake_dollars
    log.info(
        f"[dello_afl] PLACING single on {sess.get('bookie', 'sportsbet')}:{sess.get('session_id')} "
        f"— {leg.player} {leg.selection} {leg.line} {leg.stat} @ live {live} "
        f"(Dello ref {ref}) stake ${stake}"
    )
    return [_execute_bet(tip, sess, stake, presolved=resolved)]


def _place_afl_fanout(tip: ParsedTip) -> list[BetResult]:
    """
    v5.11 AFL concurrent fan-out placement (Saiyan + Eddie).

    Replaces the sequential one-account-at-a-time spillover of
    _place_singles_v4 for AFL singles (gated by AFL_CONCURRENT_FANOUT):

      1. Pull eligible Sportsbet sessions (sport filter + force-bookie +
         per-sport priority list — identical gating to _place_singles_v4).
      2. Resolve the leg's exact catalog {market, line, selection, prop_id}
         ONCE per bookie via _resolve_single_for_placement(apply_ceiling=True,
         apply_floor=False). A catalog miss on every bookie -> whole tip to
         manual (line resolver KEPT). v5.13: the WRONG-SELECTION ceiling (live >
         1.25x tipped -> manual) is KEPT — it runs off the resolve-time catalog
         odds already in hand, no extra call; the price-FLOOR is dropped so a
         shorter-than-tipped live price still places (Wilson's choice). No
         per-account price check; we never blind-POST an unresolved line.
      3. Split the intended unit stake EVENLY across the eligible accounts and
         build each account's LIABILITY LADDER (sessions.yaml top bracket first,
         then the lower brackets), sized off the catalog odds captured during the
         resolve-once step (falling back to tipped odds) so the cap holds against
         a near-live price — no extra price check. session_ids are de-duped, each
         rung floored at AFL_FANOUT_MIN_STAKE, and a running total caps the fan-out
         at the intended unit size (so the floor can't overstake and a surplus of
         accounts stops once intended is met).
      4. Fire ALL accounts CONCURRENTLY (ThreadPoolExecutor): each account walks
         its OWN ladder in its thread (top bracket; drop a bracket on a stake-too-
         high / MBL reject) so all accounts START together. initial_post_max_attempts
         =1 per rung + the ladder STOPS on an ambiguous/maybe-landed rung (never
         retries a lower one) = no double-staking.
      5. Roll up into the same consolidated placed / unfilled / ambiguous
         notifications and audit log as _place_singles_v4 (a maybe-landed
         outcome is NOT re-prompted for manual placement).

    "Unfilled" here = (unit size − placed − maybe-landed) — the FULL remainder vs
    the intended unit (Wilson 2026-06-05, v5.20). With 4 SB accounts capped at
    ~$99-$117 each the even split exceeds each cap, so the unit rarely fills from
    Sportsbet alone; the WHOLE shortfall (the structural bracket headroom AND any
    ladder-down) is routed to Manual Bets so Wilson places the rest by hand.
    (Was previously treated as expected headroom, NOT unfilled — reversed v5.20.)

    RESIDUAL RISK (accepted): the wrong-selection ceiling is back (v5.13), but the
    price-FLOOR is off, so a fill at LONGER odds than the sized catalog/tipped
    price can still push realised liability above the configured bracket. Sizing
    off the captured catalog odds minimises (not eliminates) this; bookie MBL +
    AUTO-CAP detection backstop. (h2h with no tipped/catalog odds also sizes with
    no liability conversion — bookie MBL is the only cap there.)
    """
    import time as _time_mod
    import concurrent.futures
    _t_start = _time_mod.time()

    sport = (tip.sport or "afl").lower()
    intended_stake = tip.stake_dollars
    first_leg = tip.legs[0] if tip.legs else None
    if not first_leg:
        log.warning("AFL fan-out: tip has no legs")
        return [BetResult(success=False, tip=tip, error="no legs",
                          timestamp=datetime.now())]
    market = first_leg.market or ""
    tipped_odds = tip.suggested_odds

    # Resolve the HyperBot market name once for liability-cap lookup (same as
    # _place_singles_v4: the parser leaves player props as generic
    # "player_prop", but sessions.yaml is keyed by the resolved name).
    liability_market = market
    try:
        _rlm = _resolve_leg_for_hyperbot(
            first_leg, sport,
            is_threshold=getattr(tip, "_is_threshold", False),
            tipster=tip.tipster,
            event_teams=(_afl_event_teams(tip.event) if sport == "afl" else None),
        )
        liability_market = _rlm.get("market") or market
    except Exception as e:
        log.warning(
            f"AFL fan-out: leg resolve for liability failed: {e}; using '{market}'"
        )

    # ── Eligible sessions (identical gating to _place_singles_v4) ──────
    raw_sessions = _v4_get_active_sessions_unfiltered(tip)
    if not raw_sessions:
        log.warning("AFL fan-out: no active sessions after sport filter")
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip, error="No active HyperBot sessions",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip, error="No active sessions",
                          timestamp=datetime.now())]

    configured_priority = session_priority.get_priority_for(sport, is_sgm=False)
    if not configured_priority:
        log.info(f"AFL fan-out: no priority list for {sport} — routing to manual")
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error=f"No auto-placement configured for {sport} singles (manual only)",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip,
                          error=f"{sport} singles route to manual",
                          timestamp=datetime.now())]

    sessions = session_priority.filter_and_order_sessions(
        raw_sessions, sport, is_sgm=False,
    )
    if not sessions:
        log.warning(
            f"AFL fan-out: no priority sessions for {sport} — routing to manual"
        )
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error=f"No priority sessions configured for {sport} singles",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip,
                          error="No priority sessions configured",
                          timestamp=datetime.now())]

    # De-dup by session_id BEFORE computing the split: a duplicated
    # AFL_SESSION_PRIORITY entry (operator .env typo) would otherwise inflate
    # n_accounts and silently under-stake the unit (the seen_sids guard below
    # only stops the second POST, after the split was already wrong). v5.13.
    _seen_pre: set[str] = set()
    _deduped: list[dict] = []
    for s in sessions:
        _sid = str(s.get("session_id", ""))
        if _sid and _sid in _seen_pre:
            log.warning(f"AFL fan-out: duplicate session {_sid} in priority — de-duped")
            continue
        _seen_pre.add(_sid)
        _deduped.append(s)
    sessions = _deduped

    n_accounts = len(sessions)
    per_account_target = round(intended_stake / n_accounts, 2)

    # ── Direction-aware OVER liability cap (Task A, 2026-06-07) ─────────
    # v5.69 (m6): this OVER->threshold flip MUST run BEFORE _afl_fanout_targets,
    # so the capacity-weighted split is computed off the SAME (threshold) caps
    # the per-account ladder will be sized against (resolve_stake_steps uses
    # liability_market). It previously ran AFTER the targets call, so an OVER
    # bet was weighted on the BASE O/U caps while placed on the threshold ladder.
    #
    # Over and under SHARE one placement market (the live catalog has NO
    # dedicated *_threshold market for disposals/marks/etc — only goals do — so a
    # disposals OVER places on the base player_disposals market, dir=over). The
    # OVER-specific liability ladder therefore CANNOT be selected by market name;
    # it is selected HERE, at sizing time, by the bet's DIRECTION. The placement
    # market is UNCHANGED (already resolved by the resolve-once step /
    # _match_afl_player_prop) — only the cap-lookup key `liability_market` flips
    # to the *_threshold ladder for an OVER. Detect OVER explicitly so an
    # ambiguous/empty selection falls to the UNDER (smaller/safer) ladder — never
    # overstake an under with the $300 over ladder. Gate on EVERY eligible session
    # carrying the threshold cap (else stay on the base O/U cap), so a missing
    # yaml key can never leave an over UNCAPPED (the goalscorer_threshold_afl risk
    # — its _threshold_afl form normalises to a sibling that doesn't exist -> None).
    _side = (first_leg.selection or "").strip().lower()
    _is_over = (
        (_side == "over" or _side.endswith(" over")
         or getattr(tip, "_is_threshold", False))
        and "under" not in _side
    )
    if sport == "afl" and _is_over:
        _stat = _afl_stat_from_leg(
            {"stat": first_leg.stat, "market": liability_market})
        _thr = _AFL_THRESHOLD_MARKET_BY_STAT.get(_stat)  # disposals -> player_disposals_threshold
        _elig_sids = [str(s.get("session_id", "")) for s in sessions]
        if _thr and _elig_sids and all(
            session_priority.lookup_liability_cap(_s, sport, _thr) is not None
            for _s in _elig_sids
        ):
            log.info(
                f"AFL fan-out: OVER detected (stat={_stat}, sel='{_side}') — "
                f"sizing off threshold ladder '{_thr}' "
                f"(placement market unchanged at sizing time)"
            )
            liability_market = _thr

    # v5.65 (Wilson): CAPACITY-WEIGHTED split. Give each account a target
    # PROPORTIONAL to its liability cap for this market (top bracket) instead of
    # an even 1/n, so the high-cap account (Ryan 102506 @ 4.5x) carries
    # proportionally more and the limited accounts get a share that fits their
    # cap -> fills more of the unit without overflowing to Manual. For a uniform
    # 4.5x multiplier the ratio is 4.5:1:1:1:1 (exactly Wilson's spec) and it
    # self-adjusts if a cap changes. FAIL-SAFE: if any account lacks a clean
    # numeric cap (unlimited / mbl / none) we abandon weighting and keep the
    # even split, so weighting can never mis-size. The per-account targets still
    # get budget-capped at the unit + laddered DOWN on reject downstream.
    # v6.08 DisposalsModel: EXPLICITLY EVEN, never capacity-weighted. Today the
    # weighting would happen to produce an even split anyway (all seven accounts carry
    # an identical player_disposals ladder [124, 99, 74, 50], so cap[0] is equal for
    # each and the RATIO_CAP clamp is a no-op) — but that is an ACCIDENT OF YAML
    # PARITY. Raise one account's cap for any unrelated reason and the split silently
    # re-skews toward it, with no error and no test failing. The contract requires an
    # equal 1:1 share, so take the even path deliberately rather than by coincidence.
    _force_even_split = tip.tipster == DISPOSALS_MODEL_TIPSTER
    fanout_targets, fanout_decay = (
        _afl_fanout_targets(sessions, sport, liability_market, intended_stake,
                            tip.tipster, tip.units)
        if (AFL_FANOUT_WEIGHTED and not _force_even_split) else ({}, None)
    )
    use_weighted = bool(fanout_targets)
    if use_weighted:
        _mode = (f"decay {int(round((1 - fanout_decay) * 100))}%/step"
                 if fanout_decay else "yaml-bracket ladder")
        log.info(
            f"AFL fan-out: {n_accounts} session(s), intended ${intended_stake:.2f} "
            f"-> WEIGHTED split {fanout_targets} ({_mode}; tipster={tip.tipster}, "
            f"{tip.units}u), tipped_odds={tipped_odds}"
        )
    else:
        log.info(
            f"AFL fan-out: {n_accounts} session(s), intended ${intended_stake:.2f} "
            f"-> ${per_account_target:.2f}/account (even split), tipped_odds={tipped_odds}"
        )

    # ── Resolve the catalog line ONCE per bookie ───────────────────────
    # All fan-out sessions are the same bookie (Sportsbet) in practice, so this
    # is a single catalog lookup; per-bookie caching keeps it correct if the
    # priority list ever spans bookies (each bookie carries its own lines).
    resolved_by_bookie: dict[str, dict | None] = {}
    manual_by_bookie: dict[str, BetResult] = {}
    _pc_t0 = _time_mod.time()  # v5.37: time the resolve-once (catalog price-check)
    for sess in sessions:
        bk = (sess.get("bookie", "") or "").lower()
        if bk in resolved_by_bookie:
            continue
        # v6.00 (review A-HIGH fix): a tip with NO tipster odds (the lone no-odds
        # Eddie player-prop fall-through) has an INERT odds ceiling/floor, so force
        # EXACT-line matching (NO +/-1.0 nearest-line snap). A wrong-line snap on a
        # blind-price bet is the exact money risk the review flagged (e.g. tip 22.5,
        # catalog 23.5 -> snap to 23.5 at any price). Odds-bearing tips keep the ±1
        # snap (their ceiling catches a bad snap). A no-odds miss -> manual.
        _no_tipster_odds = not (tip.suggested_odds and float(tip.suggested_odds) > 1.0)
        # v6.08 DisposalsModel: the LINE IS THE TRIGGER, so the ±1.0 nearest-line snap
        # must be OFF even though the tip carries a price. A silent 24.5 -> 23.5 snap
        # places a bet the model never selected, at full stake, and reports it as a
        # clean success; one disposal is worth roughly 7 probability points against a
        # 3.5-disposal edge. Exact line or manual — missing a bet is cheap.
        _exact_only = _no_tipster_odds or tip.tipster == DISPOSALS_MODEL_TIPSTER
        _resolved, _manual = _resolve_single_for_placement(
            tip, sess, apply_ceiling=True, apply_floor=False,
            exact_only=_exact_only,
        )
        resolved_by_bookie[bk] = _resolved  # None when routed to manual
        if _manual is not None:
            manual_by_bookie[bk] = _manual
    # v5.37: stash the resolve-once span (the per-bookie catalog price-check) so
    # the summary's reconciling breakdown can show "price-check Ns" as its own
    # phase (it happens BEFORE the concurrent place-wall, so it's additive).
    _tm = getattr(tip, "_timing", None)
    if isinstance(_tm, dict):
        _tm["price_check_sec"] = round(_time_mod.time() - _pc_t0, 3)

    # v6.08 DisposalsModel price gates (Wilson 2026-08-01). Two floors, both applied
    # to the LIVE catalog price, on top of the existing 1.25x MAX_ODDS_MULT ceiling:
    #   (1) ABSOLUTE floor DISPOSALS_MODEL_MIN_ODDS (1.70) — the model itself refuses
    #       to TIP below 1.70, so tipbot must refuse to PLACE below it. Break-even is
    #       ~1.57 at the measured 63.8% hit rate, so a drifted line is real edge lost.
    #   (2) WORSE gate — live more than DISPOSALS_MODEL_WORSE_GATE (10%) below the
    #       posted price means the line moved against us since the tip fired.
    # Either -> the WHOLE tip routes to manual, never a partial placement at a price
    # the strategy was not measured at.
    if tip.tipster == DISPOSALS_MODEL_TIPSTER:
        _dm_live = next((r.get("live_odds") for r in resolved_by_bookie.values()
                         if r and r.get("live_odds")), None)
        try:
            _dm_live = float(_dm_live) if _dm_live is not None else None
        except (TypeError, ValueError):
            _dm_live = None
        _dm_posted = float(tip.suggested_odds or 0) or None
        _dm_reason = ""
        if _dm_live is None:
            if any(r for r in resolved_by_bookie.values() if r):
                _dm_reason = "no live Sportsbet price for this exact line"
        elif _dm_live < float(DISPOSALS_MODEL_MIN_ODDS):
            _dm_reason = (f"live SB {_dm_live} is below the "
                          f"{float(DISPOSALS_MODEL_MIN_ODDS)} floor (the model would "
                          f"not have tipped it at this price)")
        elif (_dm_posted is not None
              and _dm_live < _dm_posted * (1.0 - float(DISPOSALS_MODEL_WORSE_GATE))):
            _dm_reason = (f"live SB {_dm_live} is >"
                          f"{int(float(DISPOSALS_MODEL_WORSE_GATE) * 100)}% below the "
                          f"posted {_dm_posted} — the line moved against us")
        if _dm_reason:
            log.info(f"[disposals_model] price gate -> WHOLE TIP MANUAL: {_dm_reason}")
            for _bk in list(resolved_by_bookie):
                resolved_by_bookie[_bk] = None
                manual_by_bookie.setdefault(_bk, BetResult(
                    success=False, tip=tip, bookie=_bk,
                    error=f"disposals price gate: {_dm_reason}",
                    timestamp=datetime.now(),
                ))

    # v6.00 (review A-HIGH + Wilson's band): a NO-tipster-odds bet (the lone no-odds
    # Eddie player-prop fall-through) has an inert odds ceiling/floor, so enforce
    # Wilson's explicit band off the LIVE catalog price: (1) the live price must be
    # ABOVE EDDIE_NOODDS_MIN_ODDS (1.75) else the whole tip -> manual (no short-priced
    # blind bets); (2) cap the total stake so potential winnings (to-win) <=
    # EDDIE_NOODDS_MAX_TOWIN ($1500) at the live price (the `allocated` running total
    # below enforces the reduced intended). Odds-bearing tips are untouched.
    if not (tip.suggested_odds and float(tip.suggested_odds) > 1.0):
        _band_lo = next((r.get("live_odds") for r in resolved_by_bookie.values()
                         if r and r.get("live_odds")), None)
        try:
            _band_lo = float(_band_lo) if _band_lo is not None else None
        except (TypeError, ValueError):
            _band_lo = None
        _has_resolved = any(r for r in resolved_by_bookie.values() if r)
        if _band_lo is None and _has_resolved:
            # v6.00 (verify residual #1): a no-tipster-odds bet with NO live price
            # can't be bounded by the band -> route to manual (never place a truly
            # price-less blind bet). Unrealistic on a live SB catalog, defensive.
            log.info("AFL fan-out: no-odds bet has NO live price -> whole tip to MANUAL")
            for _bk in list(resolved_by_bookie):
                manual_by_bookie.setdefault(_bk, BetResult(
                    success=False, tip=tip, bookie=_bk,
                    error="no-odds prop with no live price — place manually",
                    timestamp=datetime.now()))
                resolved_by_bookie[_bk] = None
        elif _band_lo is not None and _band_lo <= EDDIE_NOODDS_MIN_ODDS:
            log.info(
                f"AFL fan-out: no-odds bet live price {_band_lo} <= floor "
                f"{EDDIE_NOODDS_MIN_ODDS} -> whole tip to MANUAL"
            )
            for _bk in list(resolved_by_bookie):
                manual_by_bookie.setdefault(_bk, BetResult(
                    success=False, tip=tip, bookie=_bk,
                    error=(f"no-odds prop live odds {_band_lo} <= "
                           f"{EDDIE_NOODDS_MIN_ODDS} floor — place manually"),
                    timestamp=datetime.now()))
                resolved_by_bookie[_bk] = None
        elif _band_lo is not None:
            _max_total = EDDIE_NOODDS_MAX_TOWIN / (_band_lo - 1.0)
            if intended_stake > _max_total:
                log.info(
                    f"AFL fan-out: no-odds to-win cap ${EDDIE_NOODDS_MAX_TOWIN:.0f} "
                    f"@ {_band_lo} -> intended ${intended_stake:.2f} capped to "
                    f"${_max_total:.2f}"
                )
                intended_stake = round(_max_total, 2)

    if not any(resolved_by_bookie.values()):
        log.info(
            "AFL fan-out: line not carried on any eligible bookie — routing to manual"
        )
        _example = next(iter(manual_by_bookie.values()), None)
        notifier.notify_bet_failed(_example or BetResult(
            success=False, tip=tip,
            error="AFL fan-out: line not carried in catalog — routing to manual",
            timestamp=datetime.now()))
        return [_example or BetResult(
            success=False, tip=tip,
            error="line not carried — manual", timestamp=datetime.now())]

    # ── Size each account: top liability bracket, capped by the split ──
    # - Dedup by session_id: a duplicated AFL_SESSION_PRIORITY entry must NOT
    #   produce two concurrent POSTs to the same account (double-stake). This
    #   mirrors the used_session_ids guard _place_singles_v4 has.
    # - Size off the catalog odds captured during the resolve-once step when
    #   available (resolved["live_odds"]), falling back to tipped odds, so the
    #   liability cap is honoured against a near-live price — no extra call.
    # - A running `allocated` total caps the fan-out at the intended unit size,
    #   so the per-account floor (Eddie $1/u test mode) can never overstake and
    #   a future surplus of accounts stops filling once intended is met.
    jobs: list[tuple[dict, list, dict]] = []  # (session, ladder, resolved)
    seen_sids: set[str] = set()
    allocated = 0.0
    for sess in sessions:
        sid = str(sess.get("session_id", ""))
        if sid in seen_sids:
            log.warning(
                f"AFL fan-out: duplicate session {sid} in priority list — "
                f"skipping the duplicate (no double-stake)"
            )
            continue
        seen_sids.add(sid)
        bk = (sess.get("bookie", "") or "").lower()
        resolved = resolved_by_bookie.get(bk)
        if resolved is None:
            log.info(f"AFL fan-out: {bk} session {sid} skipped (line not carried)")
            continue
        remaining_budget = round(intended_stake - allocated, 2)
        if remaining_budget <= 0:
            log.info(
                f"AFL fan-out: intended ${intended_stake:.2f} fully allocated — "
                f"{bk} session {sid} not needed (skip)"
            )
            continue
        # Coerce catalog odds defensively (HyperBot could serialise odds as a
        # JSON string on provider drift; a raw "2.5" > 1.0 compare would TypeError
        # and drop the whole tip to a misleading parse-error). v5.13.
        _lo = resolved.get("live_odds")
        try:
            _lo = float(_lo) if _lo is not None else None
        except (TypeError, ValueError):
            _lo = None
        sizing_odds = _lo or tipped_odds
        # resolve_stake_steps returns the FULL descending ladder: a list cap
        # (player_disposals [100,74,50]) -> a stake per liability bracket; a
        # scalar cap -> the percentage ladder. Each account walks this ladder
        # (top first, drop a bracket on a stake-too-high reject) in its thread.
        # v5.65/v5.66: per-account target from the weighted split when active
        # (else even 1/n). In Eddie big-bet mode the ladder is a 10% step-down
        # from the target; otherwise it's the yaml liability brackets. Either way
        # each rung is budget-capped at remaining_budget just below.
        acct_target = fanout_targets.get(sid, per_account_target) if use_weighted else per_account_target
        if use_weighted and fanout_decay:
            steps, _s = [], float(acct_target)
            while _s >= AFL_FANOUT_MIN_STAKE and len(steps) < 40:
                steps.append(round(_s, 2))
                _s *= fanout_decay
            cap_reason = f"eddie-big decay {int(round((1 - fanout_decay) * 100))}%/step from ${acct_target:.2f}"
            is_list_mode = True
        else:
            steps, cap_reason, is_list_mode = session_priority.resolve_stake_steps(
                sid, sport, liability_market,
                sizing_odds if (sizing_odds and sizing_odds > 1.0) else 0,
                acct_target,
                _v4_ladder_steps,
            )
        # Budget-cap every rung at the remaining unit size (so a surplus of
        # accounts or a small intended can't be exceeded), drop non-positive.
        steps = [round(min(s, remaining_budget), 2) for s in steps if s and s > 0]
        # Drop sub-floor rungs (a sub-min POST just rejects). If that empties the
        # ladder but there WAS a positive step, keep ONE floored rung so a tiny
        # split (Eddie $1/u test mode) still reaches the bookie.
        ladder = [s for s in steps if s >= AFL_FANOUT_MIN_STAKE]
        if not ladder and steps:
            ladder = [round(min(AFL_FANOUT_MIN_STAKE, remaining_budget), 2)]
        # Dedup consecutive equal rungs (the per-account split can flatten two
        # brackets to the same stake).
        _dedup: list = []
        for s in ladder:
            if s > 0 and (not _dedup or _dedup[-1] != s):
                _dedup.append(s)
        ladder = _dedup
        if not ladder:
            log.info(
                f"AFL fan-out: {bk} session {sid} no usable stake "
                f"({cap_reason}) — skipping"
            )
            continue
        allocated = round(allocated + ladder[0], 2)  # budget tracks the top rung
        log.info(
            f"AFL fan-out: {bk} session {sid} -> ladder {ladder} "
            f"({cap_reason}, list_mode={is_list_mode}, sizing_odds={sizing_odds})"
        )
        jobs.append((sess, ladder, resolved))

    if not jobs:
        log.warning(
            "AFL fan-out: no placeable accounts after sizing — routing to manual"
        )
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error="AFL fan-out: no placeable accounts (caps/odds) — manual",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip,
                          error="no placeable accounts — manual",
                          timestamp=datetime.now())]

    # ── Fire all accounts CONCURRENTLY ─────────────────────────────────
    # HTTP client is thread-safe (fresh requests.post per call, immutable
    # headers); each thread builds its own payload from the shared read-only
    # resolved dict and a distinct session/ladder. No shared mutable state, so
    # no lock is needed. Each account walks its OWN liability ladder in its
    # thread (_fanout_place_account) — all accounts START together; within an
    # account, rungs are sequential. _execute_bet keeps initial_post_max_attempts
    # =1 so a single rung's transient failure is never re-fired (no double-stake),
    # and the ladder STOPS on an ambiguous/maybe-landed rung (never retries lower).
    # v5.91 NOTE: under concurrent IMAGE tips (_route_image_afl_tips PASS 2) two
    # DIFFERENT tips can now fan out to the SAME session_id at once. Each still POSTs
    # a distinct bet (different selection); HyperBot serialises per-session
    # server-side. WATCH the first live multi-tip slate for same-session contention;
    # a per-session_id lock is the belt-and-braces if it ever shows up.
    results: list[BetResult] = []
    log.info(f"AFL fan-out: firing {len(jobs)} concurrent placement(s) (each ladders)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {
            ex.submit(_fanout_place_account, tip, sess, ladder, resolved): sess
            for (sess, ladder, resolved) in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            sess = futures[fut]
            sid = str(sess.get("session_id", ""))
            try:
                results.append(fut.result())
            except Exception as e:
                log.error(f"AFL fan-out: placement on {sid} raised: {e}")
                results.append(BetResult(
                    success=False, tip=tip, session_id=sid,
                    bookie=sess.get("bookie", "unknown"),
                    error=f"fan-out placement exception: {e}",
                    timestamp=datetime.now()))

    # ── Roll up: notify + audit (mirrors _place_singles_v4 tail) ───────
    # Classify each result once into placed / ambiguous / failed. AMBIGUOUS
    # (maybe-landed) is EXCLUDED from both placed and the manual re-prompt (see
    # _is_ambiguous_outcome) — counting it as unfilled would prompt a re-place of
    # a bet that may have landed (Erasmus/Dawson double-stake). Single pass —
    # BetResult is a value-equality dataclass, so `in` membership is unreliable.
    placed_results: list[BetResult] = []
    ambiguous_results: list[BetResult] = []
    failed_results: list[BetResult] = []
    for r in results:
        if r.success:
            placed_results.append(r)
        elif _is_ambiguous_result(r):
            ambiguous_results.append(r)
        else:
            failed_results.append(r)

    top_by_sid = {str(s.get("session_id", "")): ladder[0]
                  for (s, ladder, _r) in jobs}
    odds_by_sid = {str(s.get("session_id", "")): (resolved or {}).get("live_odds")
                   for (s, _l, resolved) in jobs}

    def _at_risk_stake(r: BetResult) -> float:
        # v5.9x (max-stake review 2026-07-12, BUG C residual): prefer the ACTUAL
        # stake fired. _execute_bet now sets r.stake on failures too, and after a
        # max-stake rebet that is the SMALLER SB max — the true maybe-landed
        # exposure. Using `_requested_stake` (the pre-rebet ladder rung the fan-out
        # stashes) over-counted the ambiguous bucket by (rung - max), under-routing
        # that never-submitted gap. Fall back to _requested_stake then the top rung
        # so the ambiguous alert/audit never show $0. r.stake is always <= the rung,
        # so this only ever LOWERS at-risk (routes the gap to redistribute/manual).
        return round(
            (r.stake or getattr(r, "_requested_stake", None)
             or top_by_sid.get(str(r.session_id), 0.0) or 0.0), 2)

    attempted_stake = round(sum(top_by_sid.values()), 2)  # sum of top rungs
    total_placed = round(sum(r.stake or 0 for r in placed_results), 2)
    # Ambiguous stake is treated as COMMITTED (may have landed). Use the AT-RISK
    # stake (rung fired), NOT r.stake (None on a failure) — v5.13 fix so the
    # maybe-landed exposure isn't reported as $0.
    ambiguous_total = round(sum(_at_risk_stake(r) for r in ambiguous_results), 2)
    # "Unfilled" = the FULL gap between the intended UNIT and what landed (Wilson
    # 2026-06-05, v5.20): intended_stake - placed - ambiguous(maybe-landed).
    # PRIOR (v5.13) a ladder-DOWN was treated as "expected, not unfilled", so the
    # remainder was silently dropped — e.g. Tim English placed $340 of a $600 unit
    # (4 accts laddered $114->$85) yet the summary read "placed $340 of $340".
    # Wilson wants the WHOLE remainder routed to Manual Bets: BOTH the ladder-down
    # shortfall AND the part the accounts' liability brackets could never hold (the
    # 4 SB accounts cap well under the per-account split), so the rest is placed by
    # hand. Ambiguous (maybe-landed) stake is treated as COMMITTED (not unfilled)
    # so a bet that may have landed is not re-placed. ALL AFL fan-out (Saiyan +
    # Eddie). The old per-account failed-rung + auto-cap shortfall are subsumed by
    # this unit gap (a failed/short account placed less -> counts in intended-placed).
    unfilled = round(max(0.0, intended_stake - total_placed - ambiguous_total), 2)

    # v5.78/79 (Wilson 2026-06-20) → v5.94 (2026-07-05, 07-04 failure review): redistribute
    # an AFL fan-out's UNFILLED remainder onto proven-healthy accounts in BOUNDED MULTI-
    # PASSES (see _afl_redistribute_topup — a re-403'd top-up crumb re-shops onto a working
    # sibling instead of dropping to manual). Fires for the original disposals-OVERS case,
    # AND now for any single-account NON-CAP failure (403 / auth / 508 low-balance) on ANY
    # market — under / team-total / goalscorer included: a proxy or funds failure is not a
    # moved-line risk, so the v5.79 overs-only guard (which existed to avoid piling stake on
    # a moved disposals line) does not apply. A pure stake-cap (538) miss on a non-overs
    # market still does NOT redistribute (the whole book is capped -> nowhere with headroom).
    # Base-pass NON-CAP failures are excluded as top-up targets (a dead proxy / dry account
    # won't recover mid-tip). SAFE: total placed can never exceed the unit (remainder only
    # decreases by actual placed stake).
    _noncap_fail = any(not _is_stake_error(r.error or "") for r in failed_results)
    if (AFL_DISPOSALS_REDISTRIBUTE and unfilled > AFL_FANOUT_MIN_STAKE and placed_results
            and (_is_afl_disposals_over(tip) or _noncap_fail)):
        _sess_by_sid = {str(s.get("session_id", "")): s for (s, _l, _r) in jobs}
        _res_by_sid = {str(s.get("session_id", "")): _r for (s, _l, _r) in jobs}
        _exclude = [str(r.session_id) for r in failed_results
                    if not _is_stake_error(r.error or "")]
        _topups = _afl_redistribute_topup(
            tip, placed_results, unfilled, _sess_by_sid, _res_by_sid, exclude_sids=_exclude)
        for _tr in _topups:
            results.append(_tr)
            if _tr.success:
                placed_results.append(_tr)
            elif _is_ambiguous_result(_tr):
                ambiguous_results.append(_tr)
            else:
                failed_results.append(_tr)
        if _topups:
            total_placed = round(sum(r.stake or 0 for r in placed_results), 2)
            ambiguous_total = round(sum(_at_risk_stake(r) for r in ambiguous_results), 2)
            unfilled = round(max(0.0, intended_stake - total_placed - ambiguous_total), 2)
            log.info(
                f"AFL redistribute: topped up "
                f"${sum((r.stake or 0) for r in _topups if r.success):.2f} across "
                f"{sum(1 for r in _topups if r.success)}/{len(_topups)} top-up leg(s); "
                f"unfilled now ${unfilled:.2f}"
            )

    # Displayed "intended" is the true unit, so the summary reads "placed $X of
    # $UNIT" + "Unfilled $Y" honestly (no longer lowered to what landed).
    display_intended = round(intended_stake, 2)

    ambiguous_outcomes = [
        {
            "bookie": r.bookie,
            "session_id": r.session_id,
            "stake": _at_risk_stake(r),
            "odds": (r.odds or odds_by_sid.get(str(r.session_id)) or 0),
            "elapsed_sec": round(getattr(r, "elapsed_sec", None) or 0.0, 2),
            "error": (r.error or "")[:200],
            "reason": ("fast_ambiguous" if getattr(r, "is_ambiguous", False)
                       else "slow_rejection"),
            "correlation_id": getattr(r, "correlation_id", None),
        }
        for r in ambiguous_results
    ]

    session_timing = [
        {
            "session_id": r.session_id,
            "bookie": r.bookie,
            "elapsed_sec": getattr(r, "elapsed_sec", None) or 0.0,
            "attempts": 1,
            "fails": 0 if r.success else 1,
            "succeeded": r.success,
        }
        for r in results
    ]

    _log_jsonl(_audit_log_path(), {
        "type": "tip_outcome",
        "tipster": tip.tipster,
        "event": tip.event,
        "intended_stake": round(intended_stake, 2),
        "attempted_stake": attempted_stake,
        "placed_stake": total_placed,
        "ambiguous_stake": ambiguous_total,
        "unfilled_stake": unfilled,
        "fanout": True,
        "accounts": len(jobs),
        "placements": [
            {"session_id": r.session_id, "bookie": r.bookie, "stake": r.stake,
             "fill_odds": r.odds, "bet_id": r.bet_id}
            for r in placed_results
        ],
        "ambiguous": [
            {"session_id": r.session_id, "bookie": r.bookie,
             "stake": _at_risk_stake(r),
             "error": r.error, "correlation_id": getattr(r, "correlation_id", None)}
            for r in ambiguous_results
        ],
        "failures": [
            {"session_id": r.session_id, "bookie": r.bookie, "error": r.error}
            for r in failed_results
        ],
    })

    if placed_results:
        notifier.notify_tip_placed_summary(
            tip, placed_results, display_intended, unfilled,
            total_elapsed_sec=round(_time_mod.time() - _t_start, 2),
            session_timing=session_timing,
            concurrent_bookies=True,  # fan-out: bookie wall-clock = MAX not SUM
        )
        log.info(
            f"AFL fan-out: placed ${total_placed:.2f} across "
            f"{len(placed_results)}/{len(jobs)} account(s) "
            f"({len(failed_results)} failed, {len(ambiguous_results)} ambiguous)"
        )

    # Manual top-up alert: a hard failure OR any remainder vs the intended unit
    # (v5.20: includes the ladder-down + the bracket headroom no SB account could
    # hold). $1 deadband ignores cent-jitter. Routes the rest to Manual Bets.
    # v5.90 (2026-06-27): gate the manual/unfilled alert ONLY on real unfilled
    # stake, not on "an account failed". The Crisp 06-27 case — Ryan 102506
    # proxy-403'd but the overs redistribute topped the $120 onto the working
    # accounts (unfilled -> $0.00) — still fired a ⚠️ BET UNFILLED ("manual")
    # alert because failed_results was non-empty, even though the unit was FULLY
    # placed. If unfilled <= $1 there is nothing to place by hand, so suppress it
    # (the BET PLACED summary already reports the failed/recovered account; the
    # daily review tracks the per-account failure pattern).
    # v6.08: DisposalsModel owns its own shortfall — the model re-offers the unfilled
    # remainder on its next tick, and tipbot's exposure ledger caps the top-up at the
    # tranche target. Telling Wilson to ALSO place it by hand is therefore a genuine
    # double-stake vector (hand-place + the model's re-offer), so the alert is
    # downgraded to a log line for this tipster only. Every other tipster is
    # untouched: for them an unfilled remainder really does need a human.
    if unfilled > 1.0 and tip.tipster == DISPOSALS_MODEL_TIPSTER:
        log.info(
            f"[disposals_model] ${unfilled:.2f} unfilled of ${display_intended:.2f} "
            f"— NOT alerting for a hand-place (the model re-offers the shortfall and "
            f"the exposure ledger caps the top-up at the tranche target)"
        )
    elif unfilled > 1.0:
        log.warning(
            f"AFL fan-out: ${unfilled:.2f} unfilled "
            f"({len(failed_results)} account(s) failed)"
        )
        notifier.notify_tip_unfilled_with_placements(
            tip, display_intended, total_placed, unfilled,
            placed_results, failed_results,
            session_timing=session_timing,
            total_elapsed_sec=round(_time_mod.time() - _t_start, 2),
            concurrent_bookies=True,  # fan-out: bookie wall-clock = MAX not SUM
        )
        _log_jsonl(ERROR_LOG, {
            "type": "tip_unfilled",
            "tipster": tip.tipster,
            "event": tip.event,
            "intended_stake": intended_stake,
            "attempted_stake": attempted_stake,
            "placed_stake": total_placed,
            "unfilled_stake": unfilled,
            "last_error": failed_results[-1].error if failed_results else None,
            "message": tip.raw_message,
            "fanout": True,
        })

    # AMBIGUOUS critical alert (maybe-landed) — fires regardless of fill state
    # so Wilson verifies at the bookie. Mirrors the _place_singles_v4 tail.
    if ambiguous_outcomes:
        _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)

    return results


def _place_etr_nba_fanout(tip: ParsedTip) -> list[BetResult]:
    """ETR NBA BLIND concurrent fan-out (2026-06-07, Wilson).

    A dedicated sibling of _place_afl_fanout for the ETR tipster. ETR posts
    obfuscated NBA player props and wants the FASTEST possible placement, so this
    path differs from the AFL fan-out in three deliberate ways:

      1. BLIND — NO price-check. The leg is resolved by the PURE transform
         _resolve_leg_for_hyperbot (stat -> player_points/player_pra/..., selection
         -> "Player Name Over/Under", no network) and that payload is POSTed
         straight away. NBA player props place WITHOUT a proposition_id, so no
         catalog lookup is needed. The quoted odds are IGNORED (target_odds=None ->
         fills at any price); the odds ceiling/floor are disabled for etr_nba.
      2. FIXED stake ladder ETR_NBA_FIXED_LADDER ([100,90,80,70]) — NOT the
         liability-bracket ladder. $400 unit / 4 accounts = $100 top each, then
         90/80/70 on a stake-too-high reject. (No sessions.yaml cap lookup; the
         bookie MBL is the only backstop above the fixed ladder — the explicit
         "blind, ignore caps" tradeoff.)
      3. Sessions are the FIXED ETR_NBA_SESSION_IDS (the 4 sportsbet accounts),
         not a priority list — kept separate from NBA_SESSION_PRIORITY.

    Everything else mirrors the AFL fan-out: concurrent ThreadPoolExecutor, each
    account ladders down on a stake reject in its own thread (_fanout_place_account),
    stops on success/ambiguous/non-stake-error (no double-stake), and the FULL
    remainder vs the $400 unit (incl. any account that couldn't place) routes to
    Manual Bets. ETR is SINGLES-ONLY (a multi/SGM is routed to manual upstream in
    place_tip). Event is resolved by resolve_event() in place_tip before dispatch;
    an unresolved fixture already routes to manual there.

    TEST GATE: when ETR_NBA_TEST_MODE the intended stake is ETR_NBA_UNIT_SIZE_TEST
    (default $1) so only the first account places (the rest are budget-skipped) —
    one blind $1 probe end-to-end. Default OFF (Wilson launched live at $400).
    """
    import time as _time_mod
    import concurrent.futures
    _t_start = _time_mod.time()

    first_leg = tip.legs[0] if tip.legs else None
    if not first_leg:
        log.warning("ETR fan-out: tip has no legs")
        return [BetResult(success=False, tip=tip, error="no legs",
                          timestamp=datetime.now())]

    # ETR is SINGLES-ONLY (Wilson). A multi/SGM is routed to manual upstream
    # (the is_sgm guard in place_tip), and the parser only builds >1 leg when
    # is_sgm=True — so a multi-leg tip should never reach here. This is
    # defense-in-depth: a misparsed multi-leg (is_sgm=False with >1 leg) must NOT
    # silently place only leg[0] and drop the rest — route the whole tip to manual.
    if len(tip.legs) != 1:
        log.warning(
            f"ETR fan-out: expected exactly 1 leg, got {len(tip.legs)} — "
            f"routing to manual (no silent leg-drop)"
        )
        if not tip.alert_reason:
            tip.alert_reason = "ETR multi-leg tip — routed to manual (ETR auto-places singles only)"
        notifier.notify_manual_alert(tip)
        return [BetResult(success=False, tip=tip,
                          error="ETR multi-leg — manual", timestamp=datetime.now())]

    # Stake: the unit ($400) split /4, or the $1 test stake (test gate enforced in
    # CODE, not just .env — the $600 lesson).
    if ETR_NBA_TEST_MODE:
        intended_stake = round(ETR_NBA_UNIT_SIZE_TEST * (tip.units or 1.0), 2)
        log.info(
            f"ETR fan-out: TEST MODE — staking ${intended_stake:.2f} "
            f"(not the ${tip.stake_dollars:.2f} unit)"
        )
    else:
        intended_stake = tip.stake_dollars

    # ── BLIND resolve (no price-check): pure leg -> payload transform ──
    try:
        _r = _resolve_leg_for_hyperbot(
            first_leg, "nba", is_threshold=False, tipster=tip.tipster)
    except Exception as e:
        log.warning(f"ETR fan-out: leg resolve failed: {e} — routing to manual")
        _r = {}
    resolved = {
        "market": _r.get("market"),
        "selection": _r.get("selection"),
        "player": _r.get("player"),
        "stat": _r.get("stat"),
        "line": _r.get("line"),
        "target_odds": None,       # ignore odds -> no floor sent -> fills at any price
        "proposition_id": None,    # NBA player props place blind (no prop_id)
        "live_odds": None,
    }
    # Validate the resolved market against the KNOWN player-prop O/U markets, NOT
    # just truthiness: when a stat doesn't map (unknown/unsupported), the market
    # stays the unmapped "player_prop" sentinel, which is truthy but HyperBot
    # rejects ("Market player_prop not found"). Whitelisting routes that straight
    # to manual instead of burning the fast-placement window on a doomed blind POST.
    if resolved["market"] not in _OU_PLAYER_PROP_MARKETS or not resolved["selection"]:
        log.info(
            f"ETR fan-out: leg did not resolve to a known NBA player-prop market "
            f"(player={first_leg.player!r} stat={first_leg.stat!r} "
            f"sel={first_leg.selection!r} -> market={resolved['market']!r}) — "
            f"routing to manual (no blind POST)"
        )
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error=f"ETR fan-out: leg did not resolve to a known market "
                  f"({resolved['market']}) — manual",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip,
                          error="ETR leg unresolved — manual",
                          timestamp=datetime.now())]

    # ── Eligible sessions: the FIXED 4 sportsbet accounts (active + owned) ──
    raw_sessions = _v4_get_active_sessions_unfiltered(tip)
    _ids = set(ETR_NBA_SESSION_IDS)
    sessions = [
        s for s in (raw_sessions or [])
        if str(s.get("session_id", "")) in _ids
        and (s.get("bookie", "") or "").lower() == "sportsbet"
    ]
    if not sessions:
        log.warning("ETR fan-out: no active sportsbet sessions from ETR_NBA_SESSION_IDS — manual")
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error="ETR fan-out: no active sportsbet sessions — manual",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip,
                          error="ETR no active sessions — manual",
                          timestamp=datetime.now())]
    if len(sessions) < len(_ids):
        _missing = _ids - {str(s.get("session_id", "")) for s in sessions}
        log.warning(
            f"ETR fan-out: only {len(sessions)}/{len(_ids)} ETR accounts active "
            f"(missing {sorted(_missing)}); their share routes to Manual Bets"
        )

    log.info(
        f"ETR fan-out: {len(sessions)} session(s), intended ${intended_stake:.2f}, "
        f"fixed ladder {ETR_NBA_FIXED_LADDER}, market={resolved['market']} "
        f"sel='{resolved['selection']}' (BLIND, no price-check)"
    )

    # ── Build each account's FIXED ladder, budget-capped, de-duped ─────
    jobs: list[tuple[dict, list, dict]] = []
    seen_sids: set[str] = set()
    allocated = 0.0
    for sess in sessions:
        sid = str(sess.get("session_id", ""))
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        remaining_budget = round(intended_stake - allocated, 2)
        if remaining_budget <= 0:
            log.info(
                f"ETR fan-out: intended ${intended_stake:.2f} fully allocated — "
                f"sportsbet {sid} not needed (skip)"
            )
            continue
        steps = [round(min(s, remaining_budget), 2) for s in ETR_NBA_FIXED_LADDER if s and s > 0]
        ladder: list = []
        for s in steps:
            if s > 0 and (not ladder or ladder[-1] != s):
                ladder.append(s)
        if not ladder:
            continue
        allocated = round(allocated + ladder[0], 2)  # budget tracks the top rung
        log.info(f"ETR fan-out: sportsbet {sid} -> ladder {ladder}")
        jobs.append((sess, ladder, resolved))

    if not jobs:
        log.warning("ETR fan-out: no placeable accounts after sizing — manual")
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error="ETR fan-out: no placeable accounts — manual",
            timestamp=datetime.now()))
        return [BetResult(success=False, tip=tip,
                          error="ETR no placeable accounts — manual",
                          timestamp=datetime.now())]

    # ── Fire all accounts CONCURRENTLY (reuses the AFL per-account ladder) ──
    results: list[BetResult] = []
    log.info(f"ETR fan-out: firing {len(jobs)} concurrent BLIND placement(s)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {
            ex.submit(_fanout_place_account, tip, sess, ladder, resolved): sess
            for (sess, ladder, resolved) in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            sess = futures[fut]
            sid = str(sess.get("session_id", ""))
            try:
                results.append(fut.result())
            except Exception as e:
                log.error(f"ETR fan-out: placement on {sid} raised: {e}")
                results.append(BetResult(
                    success=False, tip=tip, session_id=sid,
                    bookie=sess.get("bookie", "unknown"),
                    error=f"ETR fan-out placement exception: {e}",
                    timestamp=datetime.now()))

    # ── Roll up: placed / ambiguous / unfilled->manual (mirrors AFL fan-out) ──
    placed_results: list[BetResult] = []
    ambiguous_results: list[BetResult] = []
    failed_results: list[BetResult] = []
    for r in results:
        if r.success:
            placed_results.append(r)
        elif _is_ambiguous_result(r):
            ambiguous_results.append(r)
        else:
            failed_results.append(r)

    top_by_sid = {str(s.get("session_id", "")): ladder[0]
                  for (s, ladder, _r) in jobs}

    def _at_risk_stake(r: BetResult) -> float:
        # v5.9x (max-stake review 2026-07-12, BUG C residual): prefer the ACTUAL
        # stake fired (r.stake — set on failures by _execute_bet, = the smaller SB
        # max after a max-stake rebet) over the pre-rebet rung stashed in
        # _requested_stake, so the maybe-landed exposure isn't over-counted (which
        # under-routed the never-submitted gap). Mirrors the AFL fan-out fix.
        # r.stake <= the rung, so this only ever lowers at-risk. ETR NBA fans out
        # via _fanout_place_account -> _execute_bet, so it IS rebet-reachable.
        return round(
            (r.stake or getattr(r, "_requested_stake", None)
             or top_by_sid.get(str(r.session_id), 0.0) or 0.0), 2)

    attempted_stake = round(sum(top_by_sid.values()), 2)
    total_placed = round(sum(r.stake or 0 for r in placed_results), 2)
    ambiguous_total = round(sum(_at_risk_stake(r) for r in ambiguous_results), 2)
    unfilled = round(max(0.0, intended_stake - total_placed - ambiguous_total), 2)
    display_intended = round(intended_stake, 2)

    ambiguous_outcomes = [
        {
            "bookie": r.bookie, "session_id": r.session_id,
            "stake": _at_risk_stake(r), "odds": (r.odds or 0),
            "elapsed_sec": round(getattr(r, "elapsed_sec", None) or 0.0, 2),
            "error": (r.error or "")[:200],
            "reason": ("fast_ambiguous" if getattr(r, "is_ambiguous", False)
                       else "slow_rejection"),
            "correlation_id": getattr(r, "correlation_id", None),
        }
        for r in ambiguous_results
    ]
    session_timing = [
        {
            "session_id": r.session_id, "bookie": r.bookie,
            "elapsed_sec": getattr(r, "elapsed_sec", None) or 0.0,
            "attempts": 1, "fails": 0 if r.success else 1, "succeeded": r.success,
        }
        for r in results
    ]

    _log_jsonl(_audit_log_path(), {
        "type": "tip_outcome", "tipster": tip.tipster, "event": tip.event,
        "intended_stake": round(intended_stake, 2), "attempted_stake": attempted_stake,
        "placed_stake": total_placed, "ambiguous_stake": ambiguous_total,
        "unfilled_stake": unfilled, "fanout": "etr_nba", "accounts": len(jobs),
        "placements": [
            {"session_id": r.session_id, "bookie": r.bookie, "stake": r.stake,
             "fill_odds": r.odds, "bet_id": r.bet_id}
            for r in placed_results
        ],
        "ambiguous": [
            {"session_id": r.session_id, "bookie": r.bookie, "stake": _at_risk_stake(r),
             "error": r.error, "correlation_id": getattr(r, "correlation_id", None)}
            for r in ambiguous_results
        ],
        "failures": [
            {"session_id": r.session_id, "bookie": r.bookie, "error": r.error}
            for r in failed_results
        ],
    })

    if placed_results:
        notifier.notify_tip_placed_summary(
            tip, placed_results, display_intended, unfilled,
            total_elapsed_sec=round(_time_mod.time() - _t_start, 2),
            session_timing=session_timing,
            concurrent_bookies=True,  # fan-out: bookie wall-clock = MAX not SUM
        )
        log.info(
            f"ETR fan-out: placed ${total_placed:.2f} across "
            f"{len(placed_results)}/{len(jobs)} account(s) "
            f"({len(failed_results)} failed, {len(ambiguous_results)} ambiguous)"
        )

    # v5.90 (2026-06-27): same fix as the AFL fan-out — gate on real unfilled
    # stake, not on "an account failed", so a failure the ladder/redistribute
    # fully covered (unfilled -> $0) no longer fires a spurious manual alert.
    if unfilled > 1.0:
        log.warning(
            f"ETR fan-out: ${unfilled:.2f} unfilled "
            f"({len(failed_results)} account(s) failed)"
        )
        notifier.notify_tip_unfilled_with_placements(
            tip, display_intended, total_placed, unfilled,
            placed_results, failed_results,
            session_timing=session_timing,
            total_elapsed_sec=round(_time_mod.time() - _t_start, 2),
            concurrent_bookies=True,  # fan-out: bookie wall-clock = MAX not SUM
        )
        _log_jsonl(ERROR_LOG, {
            "type": "tip_unfilled", "tipster": tip.tipster, "event": tip.event,
            "intended_stake": intended_stake, "attempted_stake": attempted_stake,
            "placed_stake": total_placed, "unfilled_stake": unfilled,
            "last_error": failed_results[-1].error if failed_results else None,
            "message": tip.raw_message, "fanout": "etr_nba",
        })

    if ambiguous_outcomes:
        _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)

    return results


def _place_singles_v4(tip: ParsedTip) -> list[BetResult]:
    """
    v4.0 singles placement.

    Flow:
      1. Get sessions (sport-filtered) without legacy SESSION_PRIORITY
      2. Filter + order via per-sport priority list (NBA_SESSION_PRIORITY etc)
      3. Loop:
         - Bulk price-check across unused priority sessions
         - Pick best-odds bookie within 10% of tipped (no upper bound)
         - Pick highest-priority unused session on that bookie
         - Compute max_stake from liability cap at LIVE odds
         - Stake ladder from max_stake on rejections
         - Mark session used regardless of outcome
      4. Tipster alt-prop fallback (existing)
      5. Auto ±1 alt-line retry (existing, only if nothing placed)
      6. Notify + audit (existing)

    Failure mode: if no eligible bookmakers OR all priority sessions used,
    remaining stake routes to manual via existing notify_tip_unfilled_with_placements.
    """
    intended_stake = tip.stake_dollars
    remaining_stake = intended_stake
    results: list[BetResult] = []
    used_session_ids: set[str] = set()
    bookie_blocklist: set[str] = set()
    # Ladder + MBL tracking. Same shape as racing's result["ladder_attempts"]
    # / result["mbl_details"]. ladder_attempts collects every stake-too-high
    # rejection; mbl_violations is the subset where the rejected stake was
    # at or below the liability-capped max_stake (account being limited
    # below its legally-guaranteed floor, or balance too low). Notifier
    # routes to Maintenance / Critical respectively.
    ladder_attempts: list[dict] = []
    mbl_violations: list[dict] = []
    # Slow-rejection AMBIGUOUS_OUTCOME tracking. Mirrors racing_placer's
    # result["ambiguous_outcomes"]. Each entry: {bookie, session_id,
    # stake, odds, elapsed_sec, error, reason}. Failure case to watch:
    # Erasmus 2026-05-03 (racing) + Dawson 2026-05-21 (sports) where
    # multi-second "stake too high" rejections landed as real bets and
    # tipbot kept spilling. Now caught here too.
    ambiguous_outcomes: list[dict] = []
    # Per-session timing for the bet-log alert. One entry per session
    # ATTEMPTED (success or not), in order. Notifier renders these next
    # to each placement line so slow bookies / many-rung ladders are
    # visible at a glance. Failed sessions also render so Wilson can see
    # which accounts ate clock without filling. 2026-05-17 v4.2 addition.
    session_timing: list[dict] = []
    # End-to-end timing for the success alert. From here (entry of v4
    # singles flow) through to the final notify_tip_placed_summary call.
    # Per-placement timing is captured separately in _execute_bet.
    import time as _time_mod
    _v4_t_start = _time_mod.time()

    sport = tip.sport or "nba"
    first_leg = tip.legs[0] if tip.legs else None
    market = (first_leg.market if first_leg else "") or ""
    tipped_odds = tip.suggested_odds

    # Resolve the HyperBot market name ONCE for liability sizing. The parser
    # leaves player props as the generic "player_prop" market, but the yaml
    # only has the post-translation HyperBot names (player_disposals,
    # player_points_threshold, etc). Without this step liability lookup
    # silently misses on AFL player props and falls through to "no-cap",
    # ignoring Wilson's caps entirely. NBA props mostly happen to use
    # HyperBot-compatible market names already so the bug is AFL-loud.
    # Regression watch: AFL player props sized at full intended stake
    # despite yaml caps in 2026-04-26 logs (no-cap reason).
    liability_market = market
    if first_leg:
        try:
            _resolved = _resolve_leg_for_hyperbot(
                first_leg, sport,
                is_threshold=getattr(tip, "_is_threshold", False),
                tipster=tip.tipster,
            )
            liability_market = _resolved.get("market") or market
        except Exception as e:
            # Resolver runs again per-attempt in _execute_bet, so a hiccup
            # here is non-fatal — fall back to raw market for sizing.
            log.warning(f"v4: leg resolve for liability failed: {e}; using raw market '{market}'")

    # Pull sport-filtered active sessions, then apply v4 per-sport priority
    raw_sessions = _v4_get_active_sessions_unfiltered(tip)
    if not raw_sessions:
        log.warning("v4: no active sessions after sport filter")
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip, error="No active HyperBot sessions",
            timestamp=datetime.now(),
        ))
        return [BetResult(
            success=False, tip=tip, error="No active sessions",
            timestamp=datetime.now(),
        )]

    # Sports without a configured priority list (e.g. MLB) should route
    # straight to manual per spec — don't fall back to placing on all sessions.
    # filter_and_order_sessions returns sessions unchanged when priority is
    # empty (legacy fallback for callers that want it), so we check the
    # priority list directly here to detect the "no auto-placement" case.
    configured_priority = session_priority.get_priority_for(sport, is_sgm=False)
    if not configured_priority:
        log.info(
            f"v4: no priority list configured for {sport} singles — routing to manual"
        )
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error=f"No auto-placement configured for {sport} singles (manual only)",
            timestamp=datetime.now(),
        ))
        return [BetResult(
            success=False, tip=tip,
            error=f"{sport} singles route to manual",
            timestamp=datetime.now(),
        )]

    priority_sessions = session_priority.filter_and_order_sessions(
        raw_sessions, sport, is_sgm=False,
    )
    if not priority_sessions:
        log.warning(
            f"v4: no priority sessions for {sport} singles — all candidates "
            f"unlisted. Routing to manual."
        )
        notifier.notify_bet_failed(BetResult(
            success=False, tip=tip,
            error=f"No priority sessions configured for {sport} singles",
            timestamp=datetime.now(),
        ))
        return [BetResult(
            success=False, tip=tip,
            error="No priority sessions configured",
            timestamp=datetime.now(),
        )]

    log.info(
        f"v4 singles: {len(priority_sessions)} priority session(s), "
        f"intended ${intended_stake:.2f}"
    )

    # ── Outer loop: pick bookie, pick session, place, repeat ────────
    # Per-session set for line-move retry tracking. Using a set (keyed on sid)
    # rather than a tip-level boolean ensures each session gets exactly one
    # retry attempt — the old boolean was set True on the first retry and
    # never cleared, so only the first session ever got a line-move retry.
    _line_move_retried_sessions: set = getattr(
        tip, "_line_move_retried_sessions", set()
    )
    while remaining_stake > 0:
        # Sessions still available (not used + bookie not blocklisted)
        unused = [
            s for s in priority_sessions
            if str(s.get("session_id", "")) not in used_session_ids
            and (s.get("bookie", "") or "").lower() not in bookie_blocklist
        ]
        if not unused:
            log.info("v4: all priority sessions exhausted, stopping placement loop")
            break

        # Price-check unused sessions to get per-bookie live odds
        live_odds_for_bookie: dict[str, float] = {}
        chosen_bookie: str | None = None
        chosen_session: dict | None = None
        live_odds: float | None = None

        # Bulk price-check only matters if leg is a player prop with a player.
        # For h2h/line/total or no-player legs, fall back to priority order
        # and use tipped_odds for liability sizing (spec: "tip odds similar
        # to live, fine to fall back").
        is_player_prop = bool(
            first_leg and first_leg.player and "player" in market
        )
        if is_player_prop:
            bulk = _bulk_price_check_player(tip, unused)
            if bulk:
                live_odds_for_bookie = _odds_by_bookie_from_bulk(
                    bulk, unused,
                    player=first_leg.player,
                    line=first_leg.line,
                    direction=first_leg.selection or "",
                )

        # Try price-driven bookie pick. If price-check returned nothing, fall
        # back to priority order (first unused session) — per spec, place blind
        # and let stake-too-high or "player not found" trigger session-skip.
        if live_odds_for_bookie:
            for bookie, odds in live_odds_for_bookie.items():
                log.info(f"v4: live odds on {bookie} = {odds}")
            chosen_bookie = session_priority.pick_best_bookie_for_tip(
                live_odds_for_bookie,
                priority_sessions,
                tipped_odds=tipped_odds,
                used_session_ids=used_session_ids,
                odds_floor_pct=0.9,
            )
            if chosen_bookie is None:
                log.info(
                    f"v4: no bookmaker meets 10% floor (tipped={tipped_odds}). "
                    f"Routing remaining ${remaining_stake:.2f} to manual."
                )
                break
            live_odds = live_odds_for_bookie[chosen_bookie]
            chosen_session = session_priority.first_unused_session_on_bookie(
                chosen_bookie, priority_sessions, used_session_ids,
            )
        else:
            # No price data — use first unused session in priority order.
            # live_odds = tipped_odds for liability math (good enough).
            chosen_session = unused[0]
            chosen_bookie = (chosen_session.get("bookie", "") or "").lower()
            live_odds = tipped_odds or 0
            # Distinguish team markets (price-check intentionally skipped, not
            # an error) from genuine empty results so log scanning isn't
            # misleading. Regression watch: was logging "price-check empty"
            # for h2h/line/total even though we never tried.
            if not is_player_prop:
                log.info(
                    f"v4: team market — placing on priority #1 "
                    f"({chosen_bookie} session {chosen_session.get('session_id')})"
                )
            else:
                log.info(
                    f"v4: price-check returned no data — placing blind on priority #1 unused "
                    f"({chosen_bookie} session {chosen_session.get('session_id')})"
                )

        if not chosen_session:
            log.warning(f"v4: no unused session on chosen bookie {chosen_bookie}")
            break

        sid = str(chosen_session.get("session_id", ""))

        # Fix J/E (2026-06-01): AFL + NBA player-prop singles size off the
        # WORKING single price_check_sports (/v3/price) catalog when the dead
        # bulk price_check_multi_session (/v3/price_check) returned nothing (the
        # "placing blind" path for sportsbet). Real per-session odds let
        # resolve_stake_steps size each sessions.yaml liability step precisely
        # instead of off tipped_odds/0. Primary AFL case: Saiyan over/under
        # disposals. Reuses the catalog matchers; a catalog MISS is handled
        # (route-to-manual for AFL) in _execute_bet, so a None here just leaves
        # the tipped-odds fallback for sizing (no behaviour change on miss).
        if (
            is_player_prop and not live_odds_for_bookie
            and tip.sport in ("afl", "nba", "nbl")
        ):
            try:
                _ev_j = (_bookie_event(tip.event, chosen_bookie, tip.sport)
                         if tip.sport == "afl" else tip.event)
                _pcj = hb.price_check_sports(
                    session_id=sid, sport=tip.sport, event=_ev_j,
                    markets_filter=["player_props"],
                )
                if _pcj.get("success"):
                    # Use liability_market (the HyperBot-RESOLVED market, e.g.
                    # player_points), NOT the raw generic "player_prop" — the
                    # /v3/price catalog is keyed by the resolved market, so the
                    # generic name would always miss (no-op). _match_afl_player_prop
                    # ignores this field (derives from stat), so AFL is unaffected;
                    # this is what makes the NBA sizing lookup actually hit.
                    _legd_j = {
                        "market": liability_market,
                        "selection": first_leg.selection or "",
                        "player": first_leg.player, "stat": first_leg.stat,
                        "line": first_leg.line,
                    }
                    _mkts_j = _pcj.get("markets") or {}
                    _mj = (
                        _match_afl_player_prop(_legd_j, _mkts_j)
                        if tip.sport == "afl"
                        else _match_nba_player_prop(_legd_j, _mkts_j)
                    )
                    if _mj and _mj.get("odds") and float(_mj["odds"]) > 1.0:
                        live_odds = float(_mj["odds"])
                        log.info(
                            f"v4 J/E: {tip.sport} sizing odds from /v3/price on "
                            f"{sid}: {first_leg.player} {first_leg.stat} "
                            f"-> {live_odds}"
                        )
            except Exception as e:
                log.debug(f"v4 J/E: sizing price-check skipped on {sid}: {e}")

        log.info(
            f"v4: placing on {chosen_bookie} session {sid} @ live odds {live_odds}"
        )

        # Compute stake steps from liability cap. live_odds may be 0 (no
        # price AND no tipped odds) — resolve_stake_steps guards that case
        # for both scalar and list cap modes.
        # Use liability_market (HyperBot-resolved) so AFL/NBA player props
        # match their yaml caps; falling back to raw `market` here would
        # silently miss every AFL player_prop cap in the yaml.
        # 2026-05-17: switched from resolve_max_stake -> resolve_stake_steps
        # to support AFL liability ladder. List caps (e.g. player_disposals:
        # [300, 250, 200]) produce 3 explicit steps; scalar caps fall
        # through to _v4_ladder_steps unchanged.
        steps, cap_reason, is_list_mode = session_priority.resolve_stake_steps(
            sid, sport, liability_market,
            live_odds if (live_odds and live_odds > 1.0) else 0,
            remaining_stake,
            _v4_ladder_steps,
        )
        # Single human-readable summary line — matches the old log
        # format so post-hoc grep/comparison still works.
        sizing_odds = live_odds if (live_odds and live_odds > 1.0) else 0
        first_step = steps[0] if steps else 0
        log.info(
            f"v4: liability sizing for {sid} {sport}.{liability_market} "
            f"@ {sizing_odds} -> first_step=${first_step:.0f} "
            f"({cap_reason}, remaining=${remaining_stake:.2f}, "
            f"steps={len(steps)}, list_mode={is_list_mode})"
        )

        if not steps:
            log.warning(f"v4: no usable stake steps on session {sid}, skipping")
            used_session_ids.add(sid)
            # Record a zero-attempt timing entry so the notifier renders a
            # placeholder for this session rather than ghosting it.
            session_timing.append({
                "session_id": sid,
                "bookie": chosen_bookie,
                "elapsed_sec": 0.0,
                "attempts": 0,
                "fails": 0,
                "succeeded": False,
                "skip_reason": "no usable stake",
            })
            continue

        # `max_stake` is retained for MBL violation comparison. In list
        # mode this is the FIRST step (largest liability target). MBL
        # tracking is suppressed entirely in list mode (see below) since
        # subsequent rejections are expected graceful degradation.
        max_stake = steps[0]

        success_on_session = False
        original_player = first_leg.player if first_leg else ""
        # Snapshot results length so we can tag this session's intermediate
        # failures at the end. Without this, every rejected ladder step
        # (e.g. $574 -> $431 -> $287 on Sicily AFL 2026-04-30 where $230
        # ultimately succeeded) gets logged as a top-level WARNING
        # "FAILED: stake too high" and looks like 4 failures when really
        # it was 1 success after laddering down.
        session_results_start = len(results)

        # Per-session timing — wraps ALL ladder attempts on this session.
        # 2026-05-17: was previously captured per-placement only (via
        # BetResult.elapsed_sec), which missed the ladder rungs that
        # failed before the eventual success. Wilson wanted the bookie-
        # specific elapsed to include those fails so slow-to-reject
        # accounts are visible.
        _session_t_start = _time_mod.time()
        _session_attempts = 0
        _session_fails = 0

        for step_stake in steps:
            log.info(
                f"v4 ladder step ${step_stake:.2f} on {chosen_bookie} session {sid}"
            )
            _session_attempts += 1
            result = _try_place_with_name_variants(
                tip, chosen_session, step_stake, original_player,
            )
            results.append(result)

            if result.success:
                placed = result.stake or step_stake
                remaining_stake -= placed
                log.info(
                    f"v4: placed ${placed:.2f} on {chosen_bookie} session {sid}, "
                    f"remaining ${remaining_stake:.2f}"
                )
                success_on_session = True
                break

            _session_fails += 1
            err = result.error or ""
            err_lower = err.lower()

            # SLOW REJECTION detection — Erasmus class. Fires BEFORE any
            # other failure handling because if the bet was actually
            # placed at the bookie but reported as failed, we must NOT
            # ladder down, NOT line-move retry, NOT spill to another
            # bookie. Debit remaining as if placed, blocklist bookie,
            # break ladder, fire critical alert.
            # Failure case to watch: slow "stake too high" rejections
            # where HyperBot took 5s+ to come back and the bet actually
            # landed on the account. Dawson AFL 2026-05-21 13:50 had an
            # 11s rejection that fell straight through to the next
            # ladder rung before this check existed.
            _elapsed = result.elapsed_sec or 0.0
            _slow = _elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
            # v5.9x (max-stake review, BUG C): after an internal max-stake rebet
            # result.stake is the smaller SB max actually attempted; reconcile +
            # debit against THAT, not the original ladder step (else over-debit
            # `remaining` -> under-fill spillover). No-op when no rebet happened.
            _eff_stake = float(getattr(result, "stake", None) or step_stake)
            if (
                not result.success
                and (
                    _slow
                    or getattr(result, "is_ambiguous", False)  # C5: fast ambiguous
                )
                and not _is_definitely_pre_placement(err)
            ):
                # Fix B (2026-06-01, GATED reconciliation): before the blind
                # debit, check /api/pending_bets. With RECONCILE_AMBIGUOUS off
                # (default) this returns 'conservative' -> the existing
                # debit+blocklist+break below runs UNCHANGED (zero behaviour
                # change while gated). When enabled after live feed validation:
                # 'spill' (Tier 2) recovers the stake on the next session instead
                # of dropping it; 'placed' debits the ACTUAL (auto-capped) stake.
                _recon_b = _reconcile_ambiguous(
                    chosen_session.get("account_id"),
                    # v5.69-r2 (#10/#11): the M3 bookie-alias fix was applied to
                    # the fan-out + SGM reconciles but MISSED here, the sequential
                    # v4 singles reconcile. Harmless today (AFL singles use the
                    # fan-out), but arms the missed-landed-GWS-bet class if
                    # AFL_CONCURRENT_FANOUT is reverted. Translate the event too.
                    event=_bookie_event(tip.event, chosen_session.get("bookie", ""), tip.sport),
                    stake=_eff_stake, sport=tip.sport,
                    selection=(result.placed_selection
                               or (tip.legs[0].selection if tip.legs else "")
                               or original_player or ""),
                    submit_ts=_time_mod.time() - _elapsed,
                )
                if _recon_b["action"] == "spill":
                    log.warning(
                        f"v4: reconcile confirmed NOT placed on {chosen_bookie}:"
                        f"{sid} — recovering ${step_stake:.2f} to spill "
                        f"(no debit/blocklist)"
                    )
                    break  # outer loop places remaining on the next session
                # v5.69 (M1): a reconcile-CONFIRMED placed bet must flow into
                # placed accounting / BET PLACED summary / ledger, NOT sit in
                # the ambiguous bucket firing a contradictory "MAY have placed"
                # CRITICAL with no ledger row. Mirrors the v5.55 fan-out fix
                # (_reconcile_fanout_ambiguous) which was never back-ported to
                # this sequential path used by NBA/MLB singles.
                if _recon_b["action"] == "placed":
                    try:
                        _actual = float(_recon_b.get("actual_stake", _eff_stake) or _eff_stake)
                    except (TypeError, ValueError):
                        _actual = _eff_stake
                    _match = _recon_b.get("match") or {}
                    result.success = True
                    result.is_ambiguous = False
                    result.stake = _actual
                    result.error = None
                    if not getattr(result, "bet_id", None):
                        result.bet_id = _match.get("bookie_bet_id") or _match.get("id")
                    if not getattr(result, "odds", None):
                        result.odds = _match.get("odds")
                    try:
                        result._requested_stake = _actual
                        result._reconcile_confirmed_placed = True
                    except Exception:
                        pass
                    log.warning(
                        f"v4: reconcile CONFIRMED placed on {chosen_bookie}:{sid} "
                        f"actual=${_actual:.2f} bet_id={result.bet_id} — "
                        f"recording as PLACED (not ambiguous)"
                    )
                    remaining_stake -= _actual
                    success_on_session = True
                    break
                _debit_b = _eff_stake
                # C5/v4.6: label the actual trigger so a fast API-level ambiguous
                # (elapsed<5s) isn't recorded as a contradictory "slow_rejection".
                _amb_reason = "slow_rejection" if _slow else "fast_ambiguous"
                log.error(
                    f"v4: AMBIGUOUS OUTCOME ({_amb_reason.replace('_', ' ')}) "
                    f"{chosen_bookie}:{sid} stake=${_debit_b:.2f} "
                    f"elapsed={_elapsed:.1f}s "
                    f"(threshold={STAKE_REJECT_LATENCY_THRESHOLD_SEC}s) "
                    f"err='{err[:80]}'. Debiting as placed, blocklisting "
                    f"bookie, stopping ladder."
                )
                ambiguous_outcomes.append({
                    "bookie": chosen_bookie,
                    "session_id": sid,
                    "stake": round(_debit_b, 2),
                    "odds": result.odds or 0,
                    "elapsed_sec": round(_elapsed, 2),
                    "error": err[:200],
                    "reason": _amb_reason,
                })
                # Mark the result ambiguous so it is excluded from BOTH
                # placed_results and failed_results downstream — it must not be
                # counted as filled, and must not prompt manual re-placement of a
                # bet that may already have landed.
                result.is_ambiguous = True
                # Debit + blocklist + break ladder. Same handling as racing.
                remaining_stake -= _debit_b
                bookie_blocklist.add(chosen_bookie)
                break

            # Capture stake-too-high rejections for the Maintenance ladder
            # alert and check for MBL violations (rejection at or below the
            # liability-capped max stake, when a real cap was applied). Same
            # logic as racing_placer's mbl_rejection — keywords picked to
            # match Sportsbet's response text. Skip when cap_reason is
            # "no-cap" / "no-odds": no ground truth to compare against.
            # 2026-05-17: also skip in list_mode — Wilson's AFL ladder
            # design (e.g. player_disposals: [300, 250, 200]) EXPECTS
            # rejections at 250 and 200 as graceful degradation. Firing
            # MBL violations on every list rung would be noise, not signal.
            if _is_stake_error(err):
                ladder_attempts.append({
                    "bookie": chosen_bookie,
                    "session_id": sid,
                    "stake_rejected": round(step_stake, 2),
                    "error": err[:120],
                })
                cap_reason_lower = (cap_reason or "").lower()
                cap_known = not cap_reason_lower.startswith(("no-cap", "no-odds"))
                if (
                    cap_known
                    and not is_list_mode
                    and step_stake <= max_stake
                    and any(
                        kw in err_lower
                        for kw in ("limit", "max", "exceed", "too high", "restricted")
                    )
                ):
                    log.error(
                        f"v4: stake reject below our cap (bookie max-bet limit) "
                        f"{chosen_bookie}:{sid} rejected "
                        f"${step_stake:.2f} but cap allows ${max_stake:.2f} "
                        f"({cap_reason})"
                    )
                    mbl_violations.append({
                        "bookie": chosen_bookie,
                        "session_id": sid,
                        "stake_tried": round(step_stake, 2),
                        "mbl_max": round(max_stake, 2),
                        # Reverse the max_stake math to get back the
                        # liability cap for the alert message. live_odds
                        # is guaranteed >1.0 by the cap_known guard above.
                        "liability_cap": round(
                            max_stake * (live_odds - 1) if live_odds else 0, 2
                        ),
                        "odds": live_odds,
                        "error": err[:200],
                        "market": liability_market,
                    })

            # Reuse v3.10 line-move + price-change retries inline so they
            # work in v4 too. Keeping them inside the ladder loop preserves
            # the exact behaviour from _place_with_spillover.
            #
            # 2026-05-09 Josh Daicos: tipped 27.5, sportsbet only offered
            # 26.5, single retry to 26.5 failed (HyperBot quirk), bookie
            # got blocklisted, alt-line fallback couldn't recover because
            # sportsbet was excluded. Wilson's call: ladder through every
            # acceptable line within 1.0 (either direction) before giving
            # up on the bookie. Sources are HyperBot's 'Available' hint
            # plus a synthetic ±0.5/±1.0 ladder, in distance-from-tipped
            # order, deduped.
            #
            # 2026-05-10 Joel Embiid Shook regression: ladder was firing
            # synthetic candidates on ANY error including MBL violations,
            # wasting 4 retries before the stake-ladder kicked in. Gate
            # the entire block on err being line-related: bookie either
            # gave a moved-to hint, or said "did not match" with an
            # Available list. Otherwise fall through to the next ladder
            # iteration (smaller stake) without doing line gymnastics.
            _moved_to_hint = _extract_moved_line(err)
            _has_did_not_match = "did not match" in err_lower
            _line_error = (_moved_to_hint is not None) or _has_did_not_match
            if (
                _line_error
                and not tip.is_sgm
                and tip.legs
                and tip.legs[0].market not in ("h2h", "head_to_head")
                # AFL player props are resolved against the live catalog in
                # _execute_bet (exact market/over-line). The synthetic ±0.5/
                # ±1.0 ladder here would blind-guess lines on the wrong market
                # (player_disposals carries only the main line), so skip it —
                # a catalog miss means "not carried" -> route to manual.
                and not (tip.sport == "afl" and tip.legs[0].player)
                # Handicaps are likewise catalog-resolved (line / pick_own_line,
                # ±0.5) in _execute_bet once the catalog has been consulted;
                # the blind synthetic ladder here is what caused the 55s /
                # 30-retry churn on "giants +50.5hc" (2026-05-31).
                and not getattr(tip, "_hc_catalog_consulted", False)
                and sid not in _line_move_retried_sessions
            ):
                original_line = tip.legs[0].line or 0
                selection = tip.legs[0].selection or ""
                # Build candidates in three groups, then concat in priority
                # order. Bookie-hinted lines (groups 1 + 2) come BEFORE
                # synthetic guesses (group 3) because the bookie has
                # already confirmed those lines exist; synthetics are
                # speculative and may just re-fail with the same
                # "did not match" error. Within each group, sort by
                # distance from tipped (closest tried first).
                moved_candidates: list[float] = []
                extracted_candidates: list[float] = []
                synthetic_candidates: list[float] = []

                # Source 1: HyperBot-suggested moved line.
                if _moved_to_hint is not None and _line_move_acceptable(
                    original_line, _moved_to_hint, selection
                ):
                    moved_candidates.append(_moved_to_hint)

                # Source 2: lines extracted from HyperBot's "Available:" list.
                if _has_did_not_match:
                    extracted = _extract_available_lines(
                        err, tip.legs[0].player or ""
                    )
                    for c in extracted:
                        if c == original_line:
                            continue
                        if not _line_move_acceptable(original_line, c, selection):
                            continue
                        if c in moved_candidates:
                            continue
                        if c not in extracted_candidates:
                            extracted_candidates.append(c)

                # Source 3: synthetic ±0.5/±1.0 ladder. Catches cases
                # where the bookie offers an alt that HyperBot didn't
                # mention in its error text.
                if original_line > 0:
                    for offset in (0.5, -0.5, 1.0, -1.0):
                        synth = round((original_line + offset) * 2) / 2
                        if synth <= 0 or synth == original_line:
                            continue
                        if not _line_move_acceptable(
                            original_line, synth, selection
                        ):
                            continue
                        if synth in moved_candidates or synth in extracted_candidates:
                            continue
                        if synth not in synthetic_candidates:
                            synthetic_candidates.append(synth)

                # Sort each group by distance, then concat in priority order
                moved_candidates.sort(key=lambda c: abs(c - original_line))
                extracted_candidates.sort(key=lambda c: abs(c - original_line))
                synthetic_candidates.sort(key=lambda c: abs(c - original_line))
                candidate_lines = (
                    moved_candidates + extracted_candidates + synthetic_candidates
                )

                if candidate_lines:
                    _line_move_retried_sessions.add(sid)
                    tip._line_move_retried_sessions = _line_move_retried_sessions
                    retry_succeeded = False
                    _last_err = err  # track last error across line-move retries
                    for new_line in candidate_lines:
                        log.info(
                            f"v4 line-move retry: tipped={original_line} -> "
                            f"{new_line}"
                        )
                        tip.legs[0].line = new_line
                        retry_result = _try_place_with_name_variants(
                            tip, chosen_session, step_stake, original_player,
                        )
                        _last_err = retry_result.error or ""
                        results.append(retry_result)
                        if retry_result.success:
                            placed = retry_result.stake or step_stake
                            remaining_stake -= placed
                            log.info(
                                f"v4: line-move retry placed ${placed:.2f} on "
                                f"{chosen_bookie} session {sid} at line "
                                f"{new_line}"
                            )
                            success_on_session = True
                            retry_succeeded = True
                            # C1 (2026-05-31): restore the tipped line BEFORE the
                            # break. The placed bet (at new_line) is already in
                            # `retry_result`/results; the restore at the bottom of
                            # this block only runs on FAILURE, so without this a
                            # successful line-move left the leg mutated and the
                            # NEXT spillover session placed on the wrong line.
                            tip.legs[0].line = original_line
                            break
                        # If error class changed (e.g. stake too high or
                        # market gone), the line existed but a different
                        # problem cropped up — stop the line ladder.
                        retry_err_lower = (retry_result.error or "").lower()
                        if "did not match" not in retry_err_lower:
                            break
                    if retry_succeeded:
                        break  # exit step_stake ladder
                    # Use the last error from the retry loop for post-loop
                    # classification so a "stake too high" retry (vs original
                    # "did not match") fires the correct branch below.
                    err = _last_err
                    err_lower = err.lower()
                    tip.legs[0].line = original_line  # restore for next session

            # Same-bookie fatal error: blocklist whole bookie this tip
            if _is_same_bookie_fatal(err):
                log.warning(
                    f"v4: same-bookie fatal on {chosen_bookie}: {err[:120]}. "
                    f"Blocklisting bookie for this tip."
                )
                bookie_blocklist.add(chosen_bookie)
                break

            # Stake error: ladder continues
            if _is_stake_error(err):
                continue

            # Price changed: retry once on same session w/o target_odds
            if _is_price_change_error(err) and sid not in getattr(
                tip, "_price_change_retried_on", set()
            ):
                if not hasattr(tip, "_price_change_retried_on"):
                    tip._price_change_retried_on = set()
                tip._price_change_retried_on.add(sid)
                log.info(f"v4: price-change retry on session {sid}")
                tip._skip_target_odds = True
                try:
                    retry_result = _try_place_with_name_variants(
                        tip, chosen_session, step_stake, original_player,
                    )
                finally:
                    tip._skip_target_odds = False
                results.append(retry_result)
                if retry_result.success:
                    placed = retry_result.stake or step_stake
                    remaining_stake -= placed
                    log.info(
                        f"v4: price-change retry placed ${placed:.2f} on {sid}"
                    )
                    # v5.69 (m7): the price-change retry sends NO target_odds
                    # (fills at current market). Player props are still
                    # protected by the pre-place _below_odds_floor guard
                    # (_resolved_live_odds), but h2h/total markets capture no
                    # live odds, so a blind fill can land well below the tipped
                    # price. The bet already landed (cannot un-place), so flag a
                    # below-floor fill to Maintenance rather than letting it pass
                    # silently.
                    try:
                        _tipped = float(getattr(tip, "suggested_odds", 0) or 0)
                        _fill = float(retry_result.odds or 0)
                    except (TypeError, ValueError):
                        _tipped = _fill = 0.0
                    if _tipped > 1.0 and _fill > 0 and _below_odds_floor(_tipped, _fill):
                        log.warning(
                            f"v4: PRICE-CHANGE FILL BELOW FLOOR on {chosen_bookie}:"
                            f"{sid}: filled @ {_fill} vs tipped {_tipped} "
                            f"(floor {_tipped * _ODDS_FLOOR_PCT:.2f}) — review"
                        )
                        try:
                            notifier._send_maintenance(
                                f"⚠️ Price-change retry filled BELOW odds floor: "
                                f"{tip.tipster} {tip.event} @ {_fill} "
                                f"(tipped {_tipped}, floor "
                                f"{_tipped * _ODDS_FLOOR_PCT:.2f}) on "
                                f"{chosen_bookie}:{sid}. Bet landed — verify price."
                            )
                        except Exception:
                            pass
                    success_on_session = True
                    break

            # Other error: stop laddering this session
            log.warning(f"v4: non-stake error on {sid}, abandoning: {err}")
            break

        # Mark session used regardless of success/failure (single-shot per
        # session within v4 outer loop, matches spec)
        used_session_ids.add(sid)

        # Record per-session elapsed + attempt counts for the placement
        # alert. 2026-05-17: was previously not captured for failed
        # sessions, so a session that ladders 5 times and abandons would
        # be invisible in the success message. Wilson wants the bookie-
        # specific elapsed (incl. failed rungs) so slow bookies stand out.
        session_timing.append({
            "session_id": sid,
            "bookie": chosen_bookie,
            "elapsed_sec": round(_time_mod.time() - _session_t_start, 2),
            "attempts": _session_attempts,
            "fails": _session_fails,
            "succeeded": success_on_session,
        })

        # Tag intermediate failures for this session's slice of `results`.
        # Rules:
        #   - On success: tag every preceding failure as intermediate; the
        #     successful result stays untagged. The user only sees the win.
        #   - On final failure: keep the LAST failure visible (so the user
        #     knows what stopped placement on this session) and tag earlier
        #     ladder steps as intermediate.
        session_slice = results[session_results_start:]
        if session_slice:
            if success_on_session:
                for r in session_slice[:-1]:
                    if not r.success:
                        r.is_intermediate = True
            else:
                for r in session_slice[:-1]:
                    r.is_intermediate = True

        if not success_on_session:
            log.warning(f"v4: no placement on session {sid}")

    # ── Tipster-specified alt props (same as v3.10) ─────────────────
    total_placed_so_far = sum(r.stake or 0 for r in results if r.success)
    # Subtract ambiguous stake (debited as placed in-loop) so the alt-chain
    # does not re-bet a portion that may already have landed (NEW/H1).
    _ambiguous_total = round(sum(a.get("stake", 0) for a in ambiguous_outcomes), 2)
    remaining_for_alts = round(intended_stake - total_placed_so_far - _ambiguous_total, 2)

    alt_chain: list[dict] = []
    if tip.alt_line:
        alt_chain.append(tip.alt_line)
        tip.alt_line = None
    if tip.alt_lines:
        alt_chain.extend(tip.alt_lines)
        tip.alt_lines = None

    if alt_chain and remaining_for_alts > 0 and tip.legs:
        log.info(
            f"v4: primary left ${remaining_for_alts:.2f} unfilled. Trying "
            f"{len(alt_chain)} tipster alt prop(s)."
        )
        # For tipster alts we drop back to priority-order placement (no
        # re-pricing per alt — alts are explicit tipster instructions, not
        # market-driven). Reuses _try_place_with_name_variants on each.
        alt_leg = tip.legs[0]
        # L25: save original leg values before the alt-chain loop mutates them.
        # After the loop exits the leg would otherwise hold the last alt's values.
        _orig_leg = copy.copy(alt_leg)

        for alt_idx, alt in enumerate(alt_chain, 1):
            if remaining_for_alts <= STAKE_FLOOR:
                break
            alt_leg.stat = alt.get("stat", alt_leg.stat)
            try:
                alt_leg.line = float(alt.get("line", alt_leg.line))
            except (TypeError, ValueError):
                log.warning(f"v4 alt {alt_idx} non-numeric line, skipping")
                continue
            alt_leg.selection = alt.get("selection", alt_leg.selection)
            alt_leg.market = alt.get("market", alt_leg.market)
            tip._is_threshold = bool(alt.get("is_threshold", False))

            log.info(
                f"v4 alt {alt_idx}/{len(alt_chain)}: stat={alt_leg.stat} "
                f"line={alt_leg.line} sel={alt_leg.selection}"
            )
            # v5.69 (m17): an alt prop can be a DIFFERENT market/stat to the
            # primary, so sizing it off the PRIMARY's liability_market + the
            # primary's stale live_odds applied the wrong cap (e.g. a points
            # alt sized on the rebounds cap). Re-resolve the alt leg's own
            # HyperBot market for cap selection, and size with no-odds (0) so
            # resolve_stake_steps uses the conservative no-odds fallback for
            # this market rather than the primary selection's price.
            _alt_liability_market = alt_leg.market or liability_market
            try:
                _alt_rlm = _resolve_leg_for_hyperbot(
                    alt_leg, sport,
                    is_threshold=getattr(tip, "_is_threshold", False),
                    tipster=tip.tipster,
                )
                _alt_liability_market = _alt_rlm.get("market") or _alt_liability_market
            except Exception as _alt_e:
                log.warning(
                    f"v4 alt {alt_idx}: leg resolve for liability failed "
                    f"({_alt_e}); using market '{_alt_liability_market}'"
                )
            alt_remaining = remaining_for_alts
            alt_blocklist: set[str] = set()
            for sess in priority_sessions:
                if str(sess.get("session_id", "")) in used_session_ids:
                    continue
                if alt_remaining <= STAKE_FLOOR:
                    break
                bookie = (sess.get("bookie", "") or "").lower()
                if bookie in alt_blocklist:
                    continue
                alt_sid = str(sess.get("session_id", ""))
                steps, _alt_cap_reason, _alt_list_mode = session_priority.resolve_stake_steps(
                    alt_sid, sport, _alt_liability_market,
                    0,
                    alt_remaining,
                    _v4_ladder_steps,
                )
                for step_stake in steps:
                    res = _try_place_with_name_variants(
                        tip, sess, step_stake, alt_leg.player,
                    )
                    results.append(res)
                    if res.success:
                        placed = res.stake or step_stake
                        alt_remaining -= placed
                        break
                    if _is_same_bookie_fatal(res.error or ""):
                        alt_blocklist.add(bookie)
                        break
                    if not _is_stake_error(res.error or ""):
                        break
            remaining_for_alts = alt_remaining

        # L25: restore original leg values after all alts have been tried.
        tip.legs[0].__dict__.update(_orig_leg.__dict__)

    # ── Auto ±1 alt-line retry (only if nothing placed yet) ─────────
    # Guard: if any ambiguous outcome was detected during primary placement,
    # the bookie may have already placed the bet.  Do NOT fire auto-alt or
    # stat-fallback — that would place a duplicate if the original landed.
    # NB: we must NOT early-return here — the ambiguous critical alert is
    # emitted in the notify block below. Gate the retries instead so control
    # falls through to alerting (H6 fix 2026-05-30: the prior early-return
    # silenced the AMBIGUOUS alert entirely).
    placed_results = [r for r in results if r.success]
    if not placed_results and not ambiguous_outcomes:
        # Reuse v3.10 helper directly. It walks priority_sessions itself.
        auto_alt_results = _try_auto_alt_lines(tip, priority_sessions, intended_stake)
        results.extend(auto_alt_results)

    # ── Stat-level fallback (Kev / AusBets only) ────────────────────
    # Last resort when the bookie doesn't carry the requested stat at all.
    # Tipster-gated via _stat_fallback_cfg.is_enabled. Walks the configured
    # chain on each priority session in turn (e.g. PRA -> PR -> PA -> P
    # for NBA). Cain 2026-04-30 was the case study: PRA wasn't offered on
    # any bookie, primary failed, no auto-alt placed it because alt-line
    # retry only varies the line. Stat fallback catches that case.
    # Guarded by `not ambiguous_outcomes` for the same reason as auto-alt.
    placed_results = [r for r in results if r.success]
    if (
        not placed_results
        and not ambiguous_outcomes
        and tip.legs
        and tip.legs[0].player
        and not tip.is_sgm
        and _stat_fallback_cfg.is_enabled(tip.sport or "", tip.tipster)
    ):
        stat_results = _try_stat_fallback(tip, priority_sessions, intended_stake)
        if stat_results:
            results.extend(stat_results)

    # ── Audit + notification (identical to v3.10) ───────────────────
    placed_results = [r for r in results if r.success]
    # Drop intermediate ladder/retry failures: only true session-level
    # failures belong in the audit log and unfilled notification. Without
    # this filter, "last_error" can be a $574-step stake-too-high rejection
    # rather than the actual final reason placement stopped.
    # Also drop ambiguous-outcome results: their stake was debited "as placed"
    # and is accounted for separately below — surfacing them as failures would
    # tell Wilson to manually re-place a bet that may already have landed.
    failed_results = [
        r for r in results
        if not r.success
        and not getattr(r, "is_intermediate", False)
        and not getattr(r, "is_ambiguous", False)
    ]
    total_placed = sum(r.stake or 0 for r in placed_results)
    # Ambiguous stake was already debited from remaining_stake in-loop; treat it
    # as committed (not unfilled) so the unfilled alert doesn't prompt a re-bet.
    ambiguous_total = round(sum(a.get("stake", 0) for a in ambiguous_outcomes), 2)
    unfilled = round(intended_stake - total_placed - ambiguous_total, 2)

    _log_jsonl(_audit_log_path(), {
        "type": "tip_outcome",
        "tipster": tip.tipster,
        "event": tip.event,
        "intended_stake": round(intended_stake, 2),
        "placed_stake": round(total_placed, 2),
        "unfilled_stake": unfilled,
        "v4": True,
        "placements": [
            {
                "session_id": r.session_id,
                "bookie": r.bookie,
                "stake": r.stake,
                "fill_odds": r.odds,
                "bet_id": r.bet_id,
            }
            for r in placed_results
        ],
        "failures": [
            {"session_id": r.session_id, "bookie": r.bookie, "error": r.error}
            for r in failed_results
        ],
    })

    if placed_results:
        notifier.notify_tip_placed_summary(
            tip, placed_results, intended_stake, unfilled,
            total_elapsed_sec=round(_time_mod.time() - _v4_t_start, 2),
            session_timing=session_timing,
        )

    if unfilled > 0:
        log.warning(
            f"v4: tip underfilled: ${total_placed:.2f} of ${intended_stake:.2f} "
            f"placed, ${unfilled:.2f} unfilled"
        )
        notifier.notify_tip_unfilled_with_placements(
            tip, intended_stake, total_placed, unfilled,
            placed_results, failed_results,
            session_timing=session_timing,
        )
        _log_jsonl(ERROR_LOG, {
            "type": "tip_unfilled",
            "tipster": tip.tipster,
            "event": tip.event,
            "intended_stake": intended_stake,
            "placed_stake": total_placed,
            "unfilled_stake": unfilled,
            "last_error": failed_results[-1].error if failed_results else None,
            "message": tip.raw_message,
            "v4": True,
        })

    # Ladder + bookie-stake-cap alerts. The stake-cap alert takes priority —
    # the ladder maintenance alert would just duplicate the same stakes/
    # sessions on the same Maintenance channel (both non-critical since
    # v5.52: a bookie capping our stake is NOT a violation of OUR cap).
    # Same suppression rule as racing in tiptitans_processor. Both are
    # safe-to-skip on exception.
    if _should_alert_mbl_violation(mbl_violations, unfilled):
        try:
            notifier.notify_sports_mbl_violation(tip, mbl_violations)
        except Exception as e:
            log.error(f"notify sports MBL violation failed: {e}")
    elif mbl_violations:
        log.info(
            f"v4: {len(mbl_violations)} stake-too-high ladder-down(s) on a "
            f"fully-filled tip — benign (designed ladder behaviour), no MBL alert"
        )
    elif ladder_attempts:
        try:
            notifier.notify_sports_ladder_maintenance(tip, ladder_attempts)
        except Exception as e:
            log.error(f"notify sports ladder maintenance failed: {e}")

    # Slow-rejection ambiguous outcomes go to critical regardless of MBL/
    # ladder state — same handling as racing in tiptitans_processor. The
    # debit + blocklist already happened in-loop; this is purely the
    # user-facing manual-verification alert.
    if ambiguous_outcomes:
        _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)

    return results


def _v4_ladder_steps(max_stake: float) -> list[float]:
    """
    v4 stake ladder: same proportions as v3.10's _ladder_steps but starts
    from the liability-capped max_stake, not raw remaining. Spec: "ladder
    from liability-capped stake" so HyperBot stake-too-high rejections drop
    via 75/50/40/25/20/15/10/5%.
    """
    if max_stake <= 0:
        return []
    steps = []
    for pct in STAKE_LADDER:
        raw = max_stake * pct
        if raw <= STAKE_FLOOR:
            break
        rounded = float(int(raw) + (1 if raw > int(raw) else 0))
        if rounded > max_stake:
            rounded = round(max_stake, 2)
        if not steps or steps[-1] != rounded:
            steps.append(rounded)
    return steps


def _is_team_plus_total_sgm(tip: ParsedTip) -> bool:
    """
    Wilson's rule: SGMs combining a team market (h2h/ML/handicap/spread) with
    a game total are routed to manual placement only — different sizing rules
    apply to that bet structure than standard SGMs.

    Returns True only if BOTH a team-side market AND a total leg are present.
    Pure player-prop SGMs and team-only SGMs continue through auto-placement.
    """
    if not tip.is_sgm or not tip.legs:
        return False

    has_team_market = False
    has_total = False
    for leg in tip.legs:
        market = (leg.market or "").lower()
        if market in ("h2h", "ml", "moneyline", "money_line",
                      "line", "first_half_line"):
            has_team_market = True
        if market in ("total", "team_total", "first_half_total"):
            has_total = True

    return has_team_market and has_total


# ── SGM prop_id lookup (HyperBot direction-flip workaround) ──────────
#
# HyperBot's O/U place_bet matcher has a bug where, when the selection
# contains a line suffix like "Jalen Brunson Under 33.5", it keys on
# (player, line) and returns the first hit — which is always the Over side
# because Overs are listed first. Confirmed 2026-04-24 via Sportsbet log:
#   Match discriminators: selection='Jalen Brunson Under 33.5', line=33.5
#   Matched: Jalen Brunson Over 33.5 @ 4.6
#
# Fix: look up the exact proposition_id via price_check and send it in the
# leg payload. HyperBot match discriminators include prop_id, which takes
# priority over selection string. Alt lines place correctly, direction is
# preserved, and odds are verified before submission.
#
# Only applied to SGMs — Wilson's rule is that singles stay on main line
# only and don't use alt lines.


_OU_PLAYER_PROP_MARKETS = {
    "player_points", "player_rebounds", "player_assists", "player_threes",
    "player_blocks", "player_steals", "player_pra",
    "player_pts_rebs", "player_pts_asts", "player_asts_rebs",
    "player_disposals", "player_marks", "player_tackles", "player_kicks",
    "player_handballs", "player_clearances", "player_hitouts", "player_fantasy",
}


def _leg_is_ou_player_prop(leg_dict: dict) -> bool:
    """True if this resolved leg is an O/U player prop that needs prop_id lookup."""
    return leg_dict.get("market", "") in _OU_PLAYER_PROP_MARKETS


# AFL catalog market names by stat — confirmed live 2026-05-31 via
# price_check_sports("Melbourne v GWS Giants", sportsbet 65465). The THRESHOLD
# market carries an OVER-only ladder of half-lines: Sportsbet keys "23+
# disposals" as the proposition "over 22.5" (selection = bare player name,
# direction='over'), with one entry per half-line (22.5, 23.5, 24.5 ...) each
# with its own proposition_id. The base O/U market carries the single main line
# with both Over and Under. Mirrors AFL_THRESH_MARKETS / AFL_STAT_MARKETS in
# _resolve_leg_for_hyperbot. (Some stats only have one of the two markets on
# Sportsbet; the matcher falls back gracefully when a market is absent.)
_AFL_THRESHOLD_MARKET_BY_STAT = {
    "disposals": "player_disposals_threshold",
    "goals": "goalscorer_threshold_afl",
    "marks": "player_marks_threshold",
    "tackles": "player_tackles_threshold",
    "kicks": "player_kicks_threshold",
    "handballs": "player_handballs_threshold",
    "clearances": "player_clearances_threshold",
    "hitouts": "player_hitouts_threshold",
    "fantasy_points": "player_fantasy_threshold",
    # The text parser's _normalise_stat leaves "fantasy" un-canonicalised (it
    # does NOT map fantasy -> fantasy_points like the image path does), so a
    # text fantasy over carries stat="fantasy". Alias it so the over still
    # selects the threshold ladder (else it under-sizes off the base cap). 2026-06-07.
    "fantasy": "player_fantasy_threshold",
}
_AFL_OU_MARKET_BY_STAT = {
    "disposals": "player_disposals",
    "goals": "goalscorer_threshold_afl",
    "marks": "player_marks",
    "tackles": "player_tackles",
    "kicks": "player_kicks",
    "handballs": "player_handballs",
    "clearances": "player_clearances",
    "hitouts": "player_hitouts",
    "fantasy_points": "player_fantasy",
    "fantasy": "player_fantasy",  # text-path alias (see threshold dict above)
}


def _is_afl_player_prop_market(market: str) -> bool:
    """True for AFL player-prop markets we catalog-match: the O/U markets
    (player_disposals, player_marks, ...) and any threshold variant
    (player_disposals_threshold, ..._threshold_afl, goalscorer_threshold_afl)."""
    if not market:
        return False
    m = market.lower()
    if m in _OU_PLAYER_PROP_MARKETS:
        return True
    if "_threshold" in m and (m.startswith("player_") or m.startswith("goalscorer")):
        return True
    return False


def _afl_stat_from_leg(leg_dict: dict) -> str:
    """Best-effort stat ('disposals', 'goals', ...) for an AFL player-prop leg.
    Uses the explicit `stat` field (always set by _resolve_leg_for_hyperbot for
    AFL legs); derives from the market name as a fallback."""
    stat = (leg_dict.get("stat") or "").strip().lower()
    if stat:
        return stat
    m = (leg_dict.get("market") or "").lower()
    if m.startswith("goalscorer"):
        return "goals"
    if m.startswith("player_"):
        base = m[len("player_"):]
        for suf in ("_threshold_afl", "_threshold"):
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        return base
    return ""


def _catalog_lookup(markets: dict, market_name: str, player_l: str,
                    direction: str, target_line: float):
    """Find a single proposition in a price_check_sports catalog market.
    Matches on player (lowercased) + direction (the catalog's `direction`
    field, falling back to the `selection` text) + exact line. Returns
    {market, line, selection, proposition_id, odds} or None."""
    if not market_name:
        return None
    mdata = markets.get(market_name)
    if isinstance(mdata, dict):
        sels = mdata.get("selections", []) or []
    elif isinstance(mdata, list):
        sels = mdata
    else:
        return None
    for s in sels:
        if (s.get("player") or "").lower() != player_l:
            continue
        sdir = (s.get("direction") or "").lower()
        if sdir:
            if sdir != direction:
                continue
        else:
            # No explicit direction field (the live catalog always has one, so
            # this is defensive). Require the direction as a TRAILING word
            # ("Clayton Oliver Over"), not a loose substring, so a name that
            # happens to contain "over"/"under" can never match the wrong side.
            ssel = (s.get("selection") or "").lower()
            if not (ssel.endswith(f" {direction}") or ssel == direction):
                continue
        try:
            if abs(float(s.get("line", -999)) - target_line) > 0.01:
                continue
        except (TypeError, ValueError):
            continue
        pid = s.get("proposition_id")
        if pid is None:
            continue
        return {
            "market": market_name,
            "line": float(s.get("line")),
            "selection": s.get("selection"),
            "player": s.get("player"),
            "proposition_id": pid,
            "odds": s.get("odds", s.get("price")),
        }
    return None


def _catalog_nearest(markets: dict, market_name: str, player_l: str,
                     direction: str, target_line: float, max_gap: float = 1.0):
    """Like _catalog_lookup, but when the EXACT line isn't carried, return the
    carried selection whose line is closest to target_line within max_gap (in
    EITHER direction). Auto-snaps an AFL O/U bet to the nearest line the bookie
    actually offers when the exact tipped line isn't carried — e.g. tip 'Under
    17.5' but the catalog only has 'Under 16.5' (Wilson 2026-06-01: nearest
    within ±1.0, either direction). The odds floor (0.9×) and ceiling (1.25×)
    guards in _execute_bet still gate the SNAPPED price, so a badly-priced snap
    routes to manual. Returns the match dict (same shape as _catalog_lookup) or
    None when nothing is carried within max_gap."""
    if not market_name:
        return None
    mdata = markets.get(market_name)
    if isinstance(mdata, dict):
        sels = mdata.get("selections", []) or []
    elif isinstance(mdata, list):
        sels = mdata
    else:
        return None
    best = None
    best_gap = None
    for s in sels:
        if (s.get("player") or "").lower() != player_l:
            continue
        sdir = (s.get("direction") or "").lower()
        if sdir:
            if sdir != direction:
                continue
        else:
            ssel = (s.get("selection") or "").lower()
            if not (ssel.endswith(f" {direction}") or ssel == direction):
                continue
        if s.get("proposition_id") is None:
            continue
        try:
            sline = float(s.get("line", -999))
        except (TypeError, ValueError):
            continue
        gap = abs(sline - target_line)
        if gap > max_gap + 0.01:
            continue
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = {
                "market": market_name,
                "line": sline,
                "selection": s.get("selection"),
                "player": s.get("player"),
                "proposition_id": s.get("proposition_id"),
                "odds": s.get("odds", s.get("price")),
            }
    return best


# Curated AFL first-name short<->full equivalences for catalog name-matching
# (roster short form vs Sportsbet formal full name). ONLY genuine diminutives
# where the short form IS that full name's nickname — NOT generic prefixes:
# 'Jack'/'Jackson' and 'Sam'/'Samuel' can be DIFFERENT players, so a same-
# surname sibling whose first name merely shares a prefix must NEVER resolve.
# Each set is one equivalence group (lowercased). Add pairs as needed.
_AFL_FIRST_NAME_GROUPS = [
    {"brad", "bradley"}, {"matt", "matthew", "matty"}, {"josh", "joshua"},
    {"mitch", "mitchell"}, {"nick", "nicholas", "nic"}, {"will", "william"},
    {"ben", "benjamin"}, {"dan", "daniel", "danny"}, {"alex", "alexander"},
    {"zac", "zach", "zachary", "zak"}, {"tom", "thomas"}, {"charlie", "charles"},
    {"nat", "nathan", "nate"}, {"ollie", "oliver", "oli"}, {"sam", "samuel"},
    {"gus", "angus"}, {"ed", "edward"}, {"cam", "cameron"},
    {"lachie", "lachlan"}, {"paddy", "patrick"},
    # 2026-07-06: genuine diminutive<->formal pairs seen across AFL lists vs
    # Sportsbet's formal spelling. Only fires as a same-surname tiebreak with a
    # UNIQUE candidate (see _afl_canonical_catalog_player), so a shared first-name
    # prefix can never resolve to a different same-surname player.
    #
    # DELIBERATELY EXCLUDED: {"harry", "harrison"}. Unlike Brad<->Bradley (Bradley
    # IS Brad's formal name), "Harry" is overwhelmingly a STANDALONE AFL given name
    # (or short for Harold/Henry), NOT a diminutive of "Harrison" (an independent
    # name). The uniqueness guard does NOT protect a LONE same-surname candidate
    # who is a different person, and the first-name group is that path's only
    # different-person guard — so a tip 'Harry <Surname>' not carried in the live
    # catalog, with exactly one same-surname 'Harrison <Surname>' carried (e.g. a
    # real Harrison Jones), would resolve to and BET the WRONG player. Removed after
    # the v5.96 adversarial review. A confirmed Harry<->Harrison case goes in the
    # collision-guarded afl_name_overrides.json, never a broad nickname pair.
    {"jake", "jacob"}, {"joe", "joseph"},
]
_AFL_FIRST_NAME_CANON: dict = {}
for _grp in _AFL_FIRST_NAME_GROUPS:
    _ck = sorted(_grp)[0]
    for _v in _grp:
        _AFL_FIRST_NAME_CANON[_v] = _ck


def _afl_first_name_compatible(a: str, b: str) -> bool:
    """First names match for AFL catalog name resolution: exactly equal, OR a
    curated short<->full nickname pair (Brad<->Bradley). Deliberately NOT a
    generic prefix — 'Jack'/'Jackson', 'Sam'/'Samuel'(*) are distinct names, so
    we never silently resolve to a same-surname sibling. (*) genuine diminutive
    pairs are in the allowlist; anything else requires exact equality."""
    a, b = (a or "").lower(), (b or "").lower()
    if a == b:
        return True
    return _AFL_FIRST_NAME_CANON.get(a, a) == _AFL_FIRST_NAME_CANON.get(b, b)


def _afl_canonical_catalog_player(markets: dict, market_names: list, player_l: str):
    """Resolve a tip's AFL player name to the EXACT spelling the live catalog
    uses, tolerating a roster short-form vs the catalog's formal full name
    (roster 'Brad Hill' vs Sportsbet 'Bradley Hill'). Scoped to `market_names`
    and SURNAME-ANCHORED + unambiguous, so it never guesses a different player.

    Returns the catalog player name LOWERCASED (ready for _catalog_lookup), or
    None when no confident, unambiguous match exists (caller keeps player_l and
    will miss -> manual; never a wrong-player bet).

    Why (v5.33, 2026-06-07): _catalog_lookup matches the player by EXACT
    (lowercased) string. The AFL roster canonicalises names ('Bradley Hill' ->
    'Brad Hill', score 0.945), but Sportsbet lists formal full names
    ('Bradley Hill'), so the exact match missed and a live 'Brad Hill over 19.5'
    (which WAS carried) routed to manual. Surname is the anchor (as in the Eddie
    matcher); a short/full first-name form (Brad<->Bradley, Matt<->Matthew) is
    only the collision tiebreak.
    """
    def _norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())

    q = _norm(player_l)
    if not q:
        return None
    # Distinct catalog player names across the relevant markets: {norm: lower}.
    names: dict = {}
    for mn in market_names:
        if not mn:
            continue
        mdata = markets.get(mn)
        if isinstance(mdata, dict):
            sels = mdata.get("selections", []) or []
        elif isinstance(mdata, list):
            sels = mdata
        else:
            sels = []
        for s in sels:
            p = (s.get("player") or "").strip()
            if p:
                names[_norm(p)] = p.lower()
    if not names:
        return None
    # 1) Exact (accent/case-normalised) match -> the catalog's own spelling.
    if q in names:
        return names[q]
    # 2) Surname-anchored (need a first + last name to have a surname).
    q_parts = q.split()
    if len(q_parts) < 2:
        return None
    q_first, q_last = q_parts[0], q_parts[-1]
    same_surname = [(nrm, low) for nrm, low in names.items()
                    if nrm.split() and nrm.split()[-1] == q_last]
    # Require first-name compatibility for EVERY candidate, INCLUDING a lone
    # same-surname hit. The exact match already failed, so a single same-surname
    # candidate could be a DIFFERENT player who happens to share the surname
    # (e.g. tip 'Archer Reid' but only 'Harley Reid' is carried). Resolving on
    # surname alone would place a wrong-player bet; the prior exact-only code
    # routed that to manual. So: keep only candidates whose first name is equal
    # or a curated nickname pair (Brad<->Bradley); resolve iff EXACTLY ONE
    # remains, else None (-> manual, never a guess). Hardened after the
    # adversarial verification flagged the surname-only + prefix paths.
    compat = [low for nrm, low in same_surname
              if nrm.split() and _afl_first_name_compatible(q_first, nrm.split()[0])]
    if len(compat) == 1:
        return compat[0]
    # 3) Curated alias candidates (afl_name_overrides.json 'aliases'): try each
    # alternate Sportsbet spelling for this player against the LIVE catalog and use
    # whichever the board actually carries — Jordon<->Jordan, Archie<->Archer, a
    # Jr/Jnr suffix, Bailey J. Williams<->Bailey Williams, etc. This is the "list
    # both, let the live board decide" path: a SINGLE catalog lookup, NO extra POST
    # (so no double-stake retry risk). EXACT accent/case-normalised match only, so a
    # curated variant resolves to the intended player or nothing — never a different
    # player. It is SAFE to list an UNconfirmed variant here (the catalog gates it),
    # unlike a rename in 'overrides' which must be Sportsbet-confirmed.
    try:
        from roster import afl_name_aliases as _afl_name_aliases
        for _alt in _afl_name_aliases().get(q, []):
            _an = _norm(_alt)
            if _an in names:
                return names[_an]
    except Exception:
        pass
    return None


def _match_afl_player_prop(leg_dict: dict, markets: dict, exact_only: bool = False):
    """Resolve an AFL player-prop SGM leg against the live catalog and return
    the exact proposition to bet, or None if it isn't carried.

    v5.69 (m13): exact_only=True disables the ±1.0 nearest-line snap. The snap
    is fine for SINGLES (the odds guards in _resolve_single_for_placement gate
    the snapped price), but SGM legs are placed with NO odds floor, so a silent
    30+ -> 29+ snap would change the bet. SGM enrichment passes exact_only=True
    -> a missing exact line returns None -> that bookie routes to manual.

    An "N+ stat" / "over X" bet maps to the OVER half-line
    `over_line = ceil(tip_line) - 0.5` in the stat's threshold ladder
    (Sportsbet encodes "23+ disposals" as over 22.5, "24+" as over 23.5, ...).
    This is the EXACT-EQUIVALENT encoding fix (Wilson 2026-05-31): it never
    changes the bet, it sends the line/market Sportsbet actually carries. An
    "under X" bet maps to the base O/U market at the tipped line. Returns
    {market, line, selection, proposition_id, odds} or None.
    """
    player_l = (leg_dict.get("player") or "").lower()
    stat = _afl_stat_from_leg(leg_dict)
    sel = (leg_dict.get("selection") or "").lower()
    line = leg_dict.get("line")
    if not player_l or not stat or line is None:
        return None
    try:
        tip_line = float(line)
    except (TypeError, ValueError):
        return None

    # Resolve the tip player to the catalog's EXACT spelling before looking up
    # the line (roster short-form 'Brad Hill' vs catalog 'Bradley Hill').
    # Surname-anchored + unambiguous; leaves player_l unchanged on no confident
    # match. v5.33 (2026-06-07 Brad Hill over 19.5 wrongly routed to manual).
    _canon = _afl_canonical_catalog_player(
        markets,
        [_AFL_OU_MARKET_BY_STAT.get(stat), _AFL_THRESHOLD_MARKET_BY_STAT.get(stat)],
        player_l,
    )
    if _canon and _canon != player_l:
        log.info(f"AFL catalog name-match: '{player_l}' -> '{_canon}' (catalog spelling)")
        player_l = _canon

    if sel.endswith(" under") or sel == "under":
        # Under bets live only in the base O/U market, at the tipped line.
        ou_market = _AFL_OU_MARKET_BY_STAT.get(stat)
        cand = _catalog_lookup(markets, ou_market, player_l, "under", tip_line)
        if cand:
            return cand
        # Exact line not carried -> snap to the nearest carried under line
        # within ±1.0 (Wilson 2026-06-01). Odds guards still gate the price.
        if exact_only:
            return None
        return _catalog_nearest(markets, ou_market, player_l, "under", tip_line)

    # Over / threshold: the over-equivalent half-line (ceil(tip) - 0.5).
    ceil_line = int(tip_line) + (1 if tip_line > int(tip_line) else 0)
    over_line = ceil_line - 0.5
    thr_market = _AFL_THRESHOLD_MARKET_BY_STAT.get(stat)
    ou_market = _AFL_OU_MARKET_BY_STAT.get(stat)
    cand = _catalog_lookup(markets, thr_market, player_l, "over", over_line)
    if cand:
        return cand
    # v5.28 (2026-06-06): REVERTED the v5.27 disposals-threshold-only routing.
    # Live catalog probe confirmed Sportsbet carries NO separate disposals/marks/
    # etc threshold market (only goalscorer_threshold_afl for goals) — the over
    # lives in the BASE O/U market's over ladder (selection = bare player name,
    # direction=over). So overs MUST fall back to the base market, else they all
    # route to manual (the v5.27 regression). Over/under-specific liability
    # ladders are applied at SIZING via liability_market (see HANDOFF), not here.
    cand = _catalog_lookup(markets, ou_market, player_l, "over", over_line)
    if cand:
        return cand
    # Exact half-line not carried -> snap to the nearest carried over line
    # within ±1.0 (threshold ladder first, then base O/U). Wilson 2026-06-01.
    if exact_only:
        return None
    return (_catalog_nearest(markets, thr_market, player_l, "over", over_line)
            or _catalog_nearest(markets, ou_market, player_l, "over", over_line))


def _match_nba_player_prop(leg_dict: dict, markets: dict, max_gap: float = 1.0):
    """Resolve an NBA/NBL player-prop leg against the live price_check_sports
    catalog: exact (player, direction, line) else the nearest carried line
    within max_gap. NBA O/U markets carry DIRECT lines (no AFL-style 'N+'
    half-line encoding), so this just reuses _catalog_lookup/_catalog_nearest on
    the resolved market with the direction parsed from the selection text.
    Returns {market, line, selection, proposition_id, odds} or None. Fix E
    (2026-06-01): gives NBA singles real catalog odds for liability sizing
    instead of the dead bulk price-check (price_check_multi_session). Degrades
    safely to None (-> tipped-odds fallback) if the catalog lacks a direction."""
    market = leg_dict.get("market") or ""
    player_l = (leg_dict.get("player") or "").lower()
    sel = (leg_dict.get("selection") or "").lower()
    line = leg_dict.get("line")
    if not market or not player_l or line is None:
        return None
    if sel.endswith(" under") or sel == "under":
        direction = "under"
    elif sel.endswith(" over") or sel == "over":
        direction = "over"
    else:
        return None
    try:
        tip_line = float(line)
    except (TypeError, ValueError):
        return None
    return (_catalog_lookup(markets, market, player_l, direction, tip_line)
            or _catalog_nearest(markets, market, player_l, direction, tip_line, max_gap))


def _resolve_mlb_player(tip_player: str, selections: list):
    """Resolve a tip's MLB player name to the catalog's canonical spelling,
    scoped to the players in THIS game's player_stats market.

    There is no MLB roster, so the matcher would otherwise need an EXACT name
    (a "Shohai"/"Shohei" typo or an accent difference -> "not carried" ->
    manual; live 2026-06-01). The team context already picked the fixture, so
    the candidate set is just this game's ~20-40 players (all distinct
    surnames) — a tight, authoritative pool. Resolution: accent/case-normalised
    EXACT match first; else a GUARDED fuzzy match — high ratio (>=0.82) AND a
    shared >=3-char token (surname overlap) AND an unambiguous winner (the
    runner-up must be clearly lower). Anything short of a confident,
    unambiguous match returns None -> caller routes to manual. This can fix a
    typo/accent but NEVER drifts to a different player. Returns the catalog's
    exact `player` string or None."""
    import difflib

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())

    # Generational suffixes ('Jr.'/'III'/...) are noise for surname matching: the
    # MLB catalog often carries them ('Vladimir Guerrero Jr.') while the tip does
    # not ('Vlad Guerrero'), which silently defeated the surname anchor below and
    # sent a clearly-unique player to manual (2026-06-25 Vlad Guerrero $400 miss).
    # Drop a trailing suffix token from EITHER side before taking the surname. Two
    # genuinely distinct people (a Jr. AND a Sr. in the SAME game) still both match
    # the stripped surname -> 2 hits -> the anchor's uniqueness check refuses them
    # (manual), so this never drifts to a different player.
    _SUFFIX_TOKENS = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}

    def _surname_token(parts: list) -> str:
        p = list(parts)
        while len(p) > 1 and p[-1] in _SUFFIX_TOKENS:
            p = p[:-1]
        return p[-1] if p else ""

    tip_n = _norm(tip_player)
    if not tip_n:
        return None
    # Distinct catalog players (normalised -> canonical spelling).
    cat: dict[str, str] = {}
    for s in selections:
        p = s.get("player")
        if p:
            cat.setdefault(_norm(p), p)
    if not cat:
        # v5.16: log the empty pool — distinguishes a HyperBot-side dropped
        # catalog (suspended rows / pool=[]) from a name-match miss.
        log.info(
            f"MLB player resolve: '{tip_player}' -> manual (catalog pool EMPTY: "
            f"no players in price_check selections — likely HyperBot-side)"
        )
        return None
    # Exact (normalised) match — covers correct spellings and accent-only diffs.
    if tip_n in cat:
        return cat[tip_n]
    # Guarded fuzzy fallback.
    tip_parts = tip_n.split()
    tip_surname = _surname_token(tip_parts)
    tip_first = tip_parts[0] if len(tip_parts) >= 2 else ""
    tip_tokens = {t for t in tip_parts if len(t) >= 3}
    scored = []
    for cn, orig in cat.items():
        ratio = difflib.SequenceMatcher(None, tip_n, cn).ratio()
        shares = bool(tip_tokens & {t for t in cn.split() if len(t) >= 3})
        scored.append((ratio, shares, orig))
    scored.sort(key=lambda x: -x[0])
    best_ratio, best_shares, best_orig = scored[0]

    # SURNAME ANCHOR (v5.16, fixes the Kyle Tucker 'not carried' miss): a batter
    # is keyed by surname. If EXACTLY ONE catalog player in this game shares the
    # tip's surname AND the first name is compatible (same, shared initial, a
    # prefix/abbrev, or ratio>=0.6), match it regardless of first-name spelling.
    # The first-name guard means a same-surname DIFFERENT player ("Kyle"->"Cole
    # Tucker") is NOT matched — it falls through to manual. So this rescues
    # variants/typos without ever drifting to a different player.
    if len(tip_surname) >= 3:
        surname_hits = []
        for cn, orig in cat.items():
            cp = cn.split()
            if not cp or _surname_token(cp) != tip_surname:
                continue
            cf = cp[0] if len(cp) >= 2 else ""
            first_ok = (
                not tip_first or not cf
                or tip_first == cf or tip_first[0] == cf[0]
                or tip_first.startswith(cf) or cf.startswith(tip_first)
                or difflib.SequenceMatcher(None, tip_first, cf).ratio() >= 0.6
            )
            if first_ok:
                surname_hits.append(orig)
        if len(surname_hits) == 1:
            log.info(
                f"MLB player surname-anchored: '{tip_player}' -> "
                f"'{surname_hits[0]}' (unique '{tip_surname}' in {len(cat)} "
                f"game players, first-name compatible)"
            )
            return surname_hits[0]

    if best_ratio < 0.82 or not best_shares:
        log.info(
            f"MLB player NOT matched: '{tip_player}' best='{best_orig}' "
            f"ratio={best_ratio:.2f} shares={best_shares} -> manual. "
            f"Pool({len(cat)})={list(cat.values())}"
        )
        return None
    # Ambiguity guard: the runner-up must be clearly behind (e.g. two players
    # who share a surname) — else refuse and route to manual. The surname anchor
    # above already handles the common unique-surname case, so this now only
    # bites on a genuine near-tie between DIFFERENT surnames.
    if len(scored) > 1 and scored[1][0] >= best_ratio - 0.08:
        log.info(
            f"MLB player AMBIGUOUS: '{tip_player}' top2=["
            f"{best_orig} {best_ratio:.2f}, {scored[1][2]} {scored[1][0]:.2f}] "
            f"-> manual. Pool({len(cat)})={list(cat.values())}"
        )
        return None
    log.info(
        f"MLB player fuzzy-matched: '{tip_player}' -> '{best_orig}' "
        f"(ratio={best_ratio:.2f}, scoped to {len(cat)} game players)"
    )
    return best_orig


def _match_mlb_player_prop(leg_dict: dict, markets: dict, max_gap: float = 0.5):
    """Resolve an MLB player-prop leg against the live price_check_sports catalog.
    MLB props all live in ONE 'player_stats' market, distinguished by a per-
    selection `stat` field (hits/total_bases/rbis/runs/home_runs/strikeouts/
    singles/doubles/triples/stolen_bases/h_r_rbi — confirmed live 2026-06-01),
    with the BARE player name as the selection text and the side in `direction`
    (mostly 'over', e.g. "1+ hits" = over 0.5). Match on player + canonical stat
    + direction + line. MLB lines are 1.0 apart (all X.5), so the default
    max_gap=0.5 is effectively EXACT-match — no risky cross-line snap — which is
    what we want for the $1 gated rollout. Returns {market, line, selection,
    proposition_id, odds, stat} or None. 2026-06-01."""
    from config import MLB_STAT_MAP
    raw_stat = (leg_dict.get("stat") or "").strip().lower()
    stat = MLB_STAT_MAP.get(raw_stat, raw_stat)
    sel = (leg_dict.get("selection") or "").lower()
    line = leg_dict.get("line")
    if not (leg_dict.get("player") or "").strip() or not stat or line is None:
        return None
    direction = "under" if (sel.endswith(" under") or sel == "under") else "over"
    try:
        tip_line = float(line)
    except (TypeError, ValueError):
        return None
    mdata = markets.get("player_stats")
    if isinstance(mdata, dict):
        sels = mdata.get("selections", []) or []
    elif isinstance(mdata, list):
        sels = mdata
    else:
        return None
    # Resolve the tip's player to the catalog's canonical spelling (fuzzy,
    # scoped to this game's players — fixes typos/accents without an MLB roster,
    # never drifts to a different player). None -> route to manual.
    canonical = _resolve_mlb_player(leg_dict.get("player") or "", sels)
    if canonical is None:
        return None
    best = None
    best_gap = None
    for s in sels:
        if (s.get("player") or "") != canonical:
            continue
        if (s.get("stat") or "").lower() != stat:
            continue
        if (s.get("direction") or "").lower() != direction:
            continue
        if s.get("proposition_id") is None:
            continue
        try:
            sline = float(s.get("line", -999))
        except (TypeError, ValueError):
            continue
        gap = abs(sline - tip_line)
        if gap > max_gap + 0.01:
            continue
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = {
                "market": "player_stats", "line": sline,
                "selection": s.get("selection"),
                "proposition_id": s.get("proposition_id"),
                "odds": s.get("odds", s.get("price")), "stat": stat,
            }
    return best


# Handicap line-match tolerance — Wilson 2026-05-31: ±0.5 for ALL sports
# (was effectively ±1.0 via the blind alt-line ladder). A tipped 3.5 can take
# 4.0 or 3.0; player props keep their own ±1.0 (the AFL player-prop matcher is
# exact, NBA props use the within-1.0 block in _execute_bet).
_HC_LINE_TOLERANCE = 0.5

# "GWS GIANTS (+50.5)" / "Melbourne (-94.5)" -> (team, signed_line)
_PYO_LINE_RE = re.compile(r"^(.*?)\s*\(([+-]?\d+(?:\.\d+)?)\)\s*$")


def _parse_pyo_line(selection: str):
    """Parse a pick_own_line selection "<Team> (<+/-line>)" into
    (team, line_float), or None if it doesn't match that shape."""
    if not selection:
        return None
    m = _PYO_LINE_RE.match(selection.strip())
    if not m:
        return None
    try:
        return (m.group(1).strip(), float(m.group(2)))
    except (TypeError, ValueError):
        return None


def _match_handicap_in_catalog(team_selection: str, tipped_line, markets: dict):
    """Resolve a team handicap leg against the live catalog (±0.5, all sports).

    1. Standard `line` market: team at the tipped line within ±0.5.
    2. Fallback to `pick_own_line`: selection "<Team> (<+/-line>)" within ±0.5
       — this is where specific handicaps (e.g. +50.5) live; the `line` market
       only carries the main line (~-0.5). pick_own_line has NO `line` field,
       so the proposition_id is returned for placement disambiguation.

    Returns {market, selection, line, proposition_id, odds} (closest within
    tolerance) or None when not carried -> caller routes to manual.
    """
    team_l = (team_selection or "").lower().strip()
    if not team_l or tipped_line is None:
        return None
    try:
        tip = float(tipped_line)
    except (TypeError, ValueError):
        return None

    def _team_match(cat_sel):
        # v6.00 (B-MEDIUM): delegate to the shared module-level word-boundary match
        # (extracted so the period resolvers use the SAME logic + cross-club blocklist).
        return _afl_team_word_match(team_l, cat_sel)

    # 1. Standard `line` market (selection=team, has `line` field).
    best = None
    for s in (_catalog_selections(markets, "line")):
        if not _team_match(s.get("selection")):
            continue
        try:
            ln = float(s.get("line"))
        except (TypeError, ValueError):
            continue
        gap = abs(ln - tip)
        if gap <= _HC_LINE_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, {
                "market": "line", "selection": s.get("selection"), "line": ln,
                "proposition_id": s.get("proposition_id"), "odds": s.get("odds"),
            })
    if best:
        return best[1]

    # 2. pick_own_line: line is baked into the selection text.
    best = None
    for s in (_catalog_selections(markets, "pick_own_line")):
        parsed = _parse_pyo_line(s.get("selection"))
        if not parsed:
            continue
        team_part, ln = parsed
        if not _team_match(team_part):
            continue
        gap = abs(ln - tip)
        if gap <= _HC_LINE_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, {
                "market": "pick_own_line", "selection": s.get("selection"),
                "line": None, "proposition_id": s.get("proposition_id"),
                "odds": s.get("odds"),
            })
    if best:
        return best[1]
    return None


def _match_total_in_catalog(side: str, tipped_line, markets: dict):
    """Resolve a match-TOTAL leg (Over/Under) against the live catalog.

    1. Standard `total_points` market: Over/Under at the tipped line within
       ±1.0 (totals aren't handicaps, so the wider tolerance is kept).
    2. Fallback to `pick_own_total`: the every-0.5 alt lines (selection
       'Over (+172.5)' / 'Under (+172.5)', WITH a `line` field) — this is where
       an off-main total lives; the `total_points` market only carries the main
       line (~166.5). pick_own_total's line is baked into the selection, so the
       proposition_id is returned for placement disambiguation.

    Returns {market, selection, line, proposition_id, odds} (closest within
    tolerance) or None when not carried -> caller routes to manual. Mirrors
    _match_handicap_in_catalog (line/pick_own_line). 2026-06-03: added so Eddie
    AFL totals at an off-main line (e.g. 172.5 vs a 166.5 main) place at the
    exact alt line instead of failing/snapping to the wrong line."""
    s_side = (side or "").lower().strip()
    if s_side not in ("over", "under") or tipped_line is None:
        return None
    try:
        tip = float(tipped_line)
    except (TypeError, ValueError):
        return None

    # 1. total_points main market (selection 'Over'/'Under', has `line`). ±1.0.
    best = None
    for s in _catalog_selections(markets, "total_points"):
        if (s.get("selection") or "").lower().strip() != s_side:
            continue
        try:
            ln = float(s.get("line"))
        except (TypeError, ValueError):
            continue
        gap = abs(ln - tip)
        if gap <= 1.0 and (best is None or gap < best[0]):
            best = (gap, {
                "market": "total_points", "selection": s.get("selection"),
                "line": ln, "proposition_id": s.get("proposition_id"),
                "odds": s.get("odds"),
            })
    if best:
        return best[1]

    # 2. pick_own_total alt lines ('Over (+172.5)' with a `line` field). ±0.5.
    best = None
    for s in _catalog_selections(markets, "pick_own_total"):
        if not (s.get("selection") or "").lower().strip().startswith(s_side):
            continue
        try:
            ln = float(s.get("line"))
        except (TypeError, ValueError):
            continue
        gap = abs(ln - tip)
        if gap <= _HC_LINE_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, {
                "market": "pick_own_total", "selection": s.get("selection"),
                "line": None, "proposition_id": s.get("proposition_id"),
                "odds": s.get("odds"),
            })
    if best:
        return best[1]
    return None


def _catalog_selections(markets: dict, market_name: str) -> list:
    """Return the selection list for a catalog market, tolerating both the
    wrapped {market: {selections: [...]}} shape (post-adapter) and the raw
    {market: [...]} list shape."""
    v = markets.get(market_name)
    if isinstance(v, dict):
        return v.get("selections", []) or []
    if isinstance(v, list):
        return v
    return []


def _find_prop_id_in_sports_catalog(leg_dict: dict, markets: dict) -> tuple:
    """Find (proposition_id, odds) for a resolved O/U player-prop SGM leg in a
    single-session price_check_sports catalog.

    Catalog shape is {market_name: {"selections": [entry, ...]}} where each
    entry carries a separate `player` field plus `selection` (which contains
    the Over/Under direction), `line`, `odds`, and `proposition_id` — the same
    fields the singles stat-fallback + line-auto-adjust paths already read
    (main.py:2036-2068, 5341-5358), so these names are production-proven.

    Match on player + direction + exact line. This is robust to whether the
    catalog's `selection` is bare ("Over") or player-prefixed ("Clayton
    Oliver Over") — unlike the old bulk-shape matcher which required an exact
    "{player} {direction}" string. Returns (None, None) when the selection
    isn't carried.
    """
    market = leg_dict.get("market", "")
    player = (leg_dict.get("player") or "").lower()
    sel = (leg_dict.get("selection") or "").lower()
    line = leg_dict.get("line")
    if not market or not player or line is None:
        return (None, None)

    if sel.endswith(" over") or sel == "over":
        direction = "over"
    elif sel.endswith(" under") or sel == "under":
        direction = "under"
    else:
        return (None, None)

    mdata = markets.get(market)
    if isinstance(mdata, dict):
        selections = mdata.get("selections", []) or []
    elif isinstance(mdata, list):
        selections = mdata
    else:
        return (None, None)

    try:
        tipped_line = float(line)
    except (TypeError, ValueError):
        return (None, None)

    for s in selections:
        if (s.get("player") or "").lower() != player:
            continue
        # v5.69 (m14): match direction on the catalog's `direction` field, else
        # a TRAILING word — NOT a loose substring. The old `direction not in
        # selection` test let a player name containing "over"/"under" (Dover,
        # Grover, Andover) satisfy the wrong side and bet the opposite
        # proposition. Mirrors _catalog_lookup's exact-direction logic.
        sdir = (s.get("direction") or "").lower()
        if sdir:
            if sdir != direction:
                continue
        else:
            ssel = (s.get("selection") or "").lower()
            if not (ssel.endswith(f" {direction}") or ssel == direction):
                continue
        try:
            if abs(float(s.get("line", -999)) - tipped_line) > 0.01:
                continue
        except (TypeError, ValueError):
            continue
        prop_id = s.get("proposition_id")
        if prop_id is None:
            # Carried but no prop_id in the catalog. The prop_id is the
            # workaround for HyperBot's O/U direction-flip on alt-line
            # matches; placing without it risks a flipped leg. Preserve the
            # existing safety guarantee: treat as not-found so the caller
            # skips to manual rather than risk the wrong side.
            return (None, None)
        return (prop_id, s.get("odds", s.get("price")))

    return (None, None)


def _sgm_unnormalised_leg(legs: list):
    """Return the first SGM leg whose market never got normalised to a real
    Sportsbet market — a generic 'player_prop' (Groq slip: player set but stat
    missing, so _resolve_leg_for_hyperbot couldn't map it to player_disposals
    etc.) or an empty market — else None. Such a leg would be POSTed as-is and
    rejected "Market 'player_prop' not found", which trips the same-bookie-fatal
    blocklist and kills the WHOLE SGM (fix D, 2026-06-01). The caller skips the
    session to manual instead of POSTing a known-bad leg."""
    for leg in legs:
        m = (leg.get("market") or "").strip().lower()
        if m in ("", "player_prop", "player_props"):
            return leg
    return None


def _enrich_sgm_legs_with_prop_ids(
    hb_legs: list, tip, session_id: str, bookie: str = "", odds_out: list = None,
) -> tuple:
    """
    Resolve each player-prop SGM leg against the live single-session catalog
    (price_check_sports) and inject what the bookie actually carries. Team
    legs (h2h, line, total) pass through unchanged.

    v5.38: when `odds_out` (a list) is passed, the per-leg CATALOG ODDS are
    appended to it IN LEG ORDER (None for a leg with no catalog price, e.g. a
    team leg). The caller multiplies them to ESTIMATE the combined SGM odds (an
    SGM has no pre-placement price) for liability sizing. The odds are NEVER put
    on the leg dict (place_sgm_bet sends `legs` verbatim — see hyperbot_client),
    so this side-channel can't pollute the wire payload. Backward-compatible:
    existing callers omit odds_out -> no behaviour change.

    2026-05-31 — TWO problems this fixes (both confirmed against the live
    "Melbourne v GWS Giants" sportsbet catalog):

      1. Bulk price-check is dead for sportsbet. The old enricher used
         price_check_multi_session (bulk /v3/price_check) which returns
         "Sports price check not implemented for sportsbet" -> every sportsbet
         O/U SGM leg failed enrichment and the session was skipped blind. Now
         uses the single price_check_sports (/v3/price), which WORKS for
         sportsbet and returns the full player-props catalog.

      2. Line/market ENCODING. Sportsbet keys "N+ disposals" as the OVER
         half-line (N-0.5) in the *_threshold ladder (e.g. "23+" == over 22.5,
         "24+" == over 23.5), NOT as integer line N. The bot was sending
         integer N, so HyperBot snapped to the wrong line ("Line moved 23.0 ->
         22.5") or rejected. For AFL player-prop legs we now REWRITE the leg to
         the catalog's exact {market, line, selection, proposition_id} via
         _match_afl_player_prop (over-line = ceil(tip)-0.5). This is the
         exact-equivalent encoding fix (Wilson 2026-05-31): the bet is
         unchanged, only the wire encoding is corrected.

    The event is translated to the bookie's form (_bookie_event) before the
    price check, so GWS etc. resolve ("Greater Western Sydney" -> "GWS Giants").

    AFL legs not carried (e.g. a line beyond the offered ladder), non-AFL O/U
    legs with no prop_id, or an unavailable price check all return
    (hb_legs, error) -> caller skips this session -> ultimately routes to manual.
    """
    sport = (getattr(tip, "sport", "") or "").lower()

    def _afl_pp(leg):
        return bool(
            sport == "afl"
            and leg.get("player")
            and _is_afl_player_prop_market(leg.get("market") or "")
        )

    def _mlb_pp(leg):
        # MLB player prop — all live in the single `player_stats` market,
        # keyed by a per-selection `stat` field. Resolved via the exact,
        # stat+direction-safe _match_mlb_player_prop (no cross-line snap).
        return bool(
            sport == "mlb"
            and leg.get("player")
            and (leg.get("market") or "") == "player_stats"
        )

    def _ou_pp(leg):
        return bool(_leg_is_ou_player_prop(leg) and leg.get("player"))

    def _hc_leg(leg):
        # Team FULL-GAME handicap leg — resolved against line / pick_own_line
        # (±0.5). 07-12 review C1: first_half_line / period handicaps are NOT
        # resolved here (they would fall back to the FULL-GAME catalog = wrong
        # market). Only the full-game 'line' handicap enriches.
        return (leg.get("market") or "") == "line"

    # 07-12 review C1 safety net: a period/half handicap leg has no safe in-SGM
    # resolver, so refuse to enrich it -> the caller skips the session -> manual
    # (never a full-game fallback). Mirrors the #5 single-path invariant. The
    # Saiyan un-gate already keeps these on manual; this is defence-in-depth for
    # any other caller.
    _period_hc = next(
        (leg for leg in hb_legs
         if (leg.get("market") or "") in ("first_half_line", "second_half_line",
                                          "quarter_line", "quarter_time_line")),
        None)
    if _period_hc:
        return (
            hb_legs,
            f"period/half handicap leg not supported in an SGM "
            f"(market='{_period_hc.get('market')}') on session {session_id} — manual",
        )

    needs_catalog = [
        leg for leg in hb_legs
        if _afl_pp(leg) or _mlb_pp(leg) or _ou_pp(leg) or _hc_leg(leg)
    ]
    if not needs_catalog:
        # Team-only (h2h/total) SGM: no catalog fetch needed. Still guard a
        # leftover un-normalised player_prop leg (fix D) — it would POST blind.
        _bad = _sgm_unnormalised_leg(hb_legs)
        if _bad:
            return (
                hb_legs,
                f"leg not normalised (market='{_bad.get('market')}', "
                f"sel='{_bad.get('selection')}') on session {session_id}",
            )
        return (hb_legs, None)

    # Translate the event to the bookie's form (matches what placement sends).
    event_for_hb = _bookie_event(tip.event, bookie, sport) if bookie else tip.event

    # One catalog fetch covers every leg for this event+session.
    # 07-12 review C2: a handicap ('line') leg resolves against the line /
    # pick_own_line markets, which the player_props-only filter EXCLUDES (the
    # singles handicap path fetches with NO filter for this reason). So when any
    # handicap leg is present, fetch the FULL catalog; a pure player-prop SGM keeps
    # the cheaper player_props filter.
    _has_hc = any(_hc_leg(leg) for leg in needs_catalog)
    try:
        price_resp = hb.price_check_sports(
            session_id=str(session_id),
            sport=tip.sport,
            event=event_for_hb,
            **({} if _has_hc else {"markets_filter": ["player_props"]}),
        )
    except Exception as e:
        return (hb_legs, f"price_check_sports failed: {e}")
    if not price_resp.get("success"):
        # Clean, real error (e.g. "event not carried") instead of the old
        # blind "not implemented for sportsbet" skip.
        return (hb_legs, f"price check unavailable: {price_resp.get('error', 'unknown')}")

    markets = price_resp.get("markets") or {}

    enriched = []
    for leg in hb_legs:
        new_leg = dict(leg)
        _leg_odds = None  # v5.38: per-leg catalog odds for the combined-odds estimate
        if _afl_pp(leg):
            # v5.69 (m13): SGM legs place with no odds floor, so disable the
            # ±1.0 nearest-line snap — a missing exact line routes this bookie
            # to manual rather than silently betting a different (easier) line.
            match = _match_afl_player_prop(leg, markets, exact_only=True)
            if match is None:
                return (
                    hb_legs,
                    f"AFL leg not carried: {leg.get('player')} "
                    f"{_afl_stat_from_leg(leg)} line={leg.get('line')} "
                    f"sel='{leg.get('selection')}' on session {session_id}",
                )
            # Rewrite to the exact catalog proposition: correct market (the
            # threshold over-ladder for "N+" bets), the over-equivalent
            # half-line, the catalog's selection string, and the prop_id.
            new_leg["market"] = match["market"]
            new_leg["line"] = match["line"]
            new_leg["selection"] = match["selection"]
            new_leg["proposition_id"] = match["proposition_id"]
            # v5.96: adopt the catalog's canonical player spelling (roster short-
            # form 'Brad Hill' -> catalog 'Bradley Hill'), mirroring the MLB leg
            # below. AFL SGM legs place by proposition_id so a stale `player`
            # string was masked, but keeping the payload's player field in step
            # with the matched prop_id/selection removes the latent mismatch.
            if match.get("player"):
                new_leg["player"] = match["player"]
            _leg_odds = match.get("odds")
            log.info(
                f"SGM AFL leg catalog-matched: {leg.get('player')} "
                f"{_afl_stat_from_leg(leg)} tip-line={leg.get('line')} -> "
                f"market={match['market']} line={match['line']} "
                f"sel='{match['selection']}' prop_id={match['proposition_id']} "
                f"odds={match['odds']}"
            )
        elif _mlb_pp(leg):
            # MLB player prop: resolve against the single `player_stats` market
            # (exact stat + direction + line via _match_mlb_player_prop, default
            # max_gap 0.5 == exact for the X.5 ladder — no risky cross-line
            # snap). Rewrite to the catalog's exact line/selection/prop_id;
            # keep player + stat (the verified place_sgm_bet payload carries
            # them). A miss (stat/line not carried, or no prop_id) returns an
            # error -> caller skips this session -> manual (no blind POST).
            match = _match_mlb_player_prop(leg, markets)
            if match is None:
                return (
                    hb_legs,
                    f"MLB leg not carried: {leg.get('player')} "
                    f"{leg.get('stat')} line={leg.get('line')} "
                    f"sel='{leg.get('selection')}' on session {session_id}",
                )
            new_leg["market"] = match["market"]
            new_leg["line"] = match["line"]
            new_leg["selection"] = match["selection"]
            new_leg["proposition_id"] = match["proposition_id"]
            new_leg["stat"] = match["stat"]
            # Use the catalog's canonical player spelling (the matcher may have
            # fuzzy-resolved a typo/accent) so the payload's player field agrees
            # with the matched prop_id/selection.
            new_leg["player"] = match["selection"]
            _leg_odds = match.get("odds")
            log.info(
                f"SGM MLB leg catalog-matched: {leg.get('player')} "
                f"{match['stat']} tip-line={leg.get('line')} -> "
                f"line={match['line']} sel='{match['selection']}' "
                f"prop_id={match['proposition_id']} odds={match['odds']}"
            )
        elif _ou_pp(leg):
            # Non-AFL O/U player prop (e.g. NBA): prop_id enrichment only, leg
            # market/line unchanged (NBA thresholds + O/U lines already place).
            prop_id, bookie_odds = _find_prop_id_in_sports_catalog(leg, markets)
            if prop_id is None:
                return (
                    hb_legs,
                    f"No prop_id for {leg.get('selection')} "
                    f"line={leg.get('line')} on session {session_id}",
                )
            new_leg["proposition_id"] = prop_id
            _leg_odds = bookie_odds
            log.info(
                f"SGM leg prop_id: '{leg.get('selection')}' line={leg.get('line')} "
                f"→ prop_id={prop_id}, bookie_odds={bookie_odds}"
            )
        elif _hc_leg(leg):
            # Handicap leg: match `line` market (±0.5) then pick_own_line.
            # A specific handicap (e.g. +50.5) lives in pick_own_line as
            # "GWS GIANTS (+50.5)" — same fix as singles, applied per leg.
            hm = _match_handicap_in_catalog(
                leg.get("selection"), leg.get("line"), markets,
            )
            if hm is None:
                return (
                    hb_legs,
                    f"handicap leg not carried: {leg.get('selection')} "
                    f"line={leg.get('line')} on session {session_id}",
                )
            new_leg["market"] = hm["market"]
            new_leg["selection"] = hm["selection"]
            if hm.get("proposition_id"):
                new_leg["proposition_id"] = hm["proposition_id"]
            # Handicap legs have no player; pick_own_line carries the line in
            # the selection text (no `line` field). Drop both so the leg
            # matches the catalog entry exactly.
            new_leg.pop("player", None)
            new_leg.pop("stat", None)
            if hm["market"] == "pick_own_line":
                new_leg.pop("line", None)
            else:
                new_leg["line"] = hm["line"]
            _leg_odds = hm.get("odds")
            log.info(
                f"SGM handicap leg catalog-matched: '{leg.get('selection')}' "
                f"line={leg.get('line')} -> market={hm['market']} "
                f"sel='{hm['selection']}' prop_id={hm.get('proposition_id')} "
                f"odds={hm['odds']}"
            )
        # v5.38: record this leg's catalog odds (None for a team leg with no
        # price) so the caller can estimate the combined SGM odds = product.
        if odds_out is not None:
            try:
                odds_out.append(float(_leg_odds) if _leg_odds is not None else None)
            except (TypeError, ValueError):
                odds_out.append(None)
        enriched.append(new_leg)

    # Fix D (2026-06-01): final guard — never return a leg list containing an
    # un-normalised 'player_prop'/empty market (it would POST blind and the
    # "Market player_prop not found" reject blocklists the bookie + kills the
    # SGM). Route the session to manual instead.
    _bad = _sgm_unnormalised_leg(enriched)
    if _bad:
        return (
            enriched,
            f"leg not normalised (market='{_bad.get('market')}', "
            f"sel='{_bad.get('selection')}') on session {session_id}",
        )
    return (enriched, None)


def _place_sgm(tip: ParsedTip) -> list[BetResult]:
    """Attempt to place an SGM bet. Falls back to alert on failure."""

    # Wilson's rule: team ML/handicap + total SGMs always go to manual.
    # Different bet sizing rules apply for this combination — skip auto-place.
    if _is_team_plus_total_sgm(tip):
        log.info(
            "SGM contains team market + total: routing to manual alert "
            "(Wilson's bet-sizing rule for this combination)"
        )
        tip.alert_reason = (
            "SGM with team ML/handicap + total — different sizing rules, "
            "manual placement only"
        )
        notifier.notify_manual_alert(tip)
        return []

    sessions = _get_sessions_for_bookie(tip)
    if not sessions:
        log.warning("No active sessions for SGM")
        notifier.notify_manual_alert(tip)
        return []

    # Filter out sessions banned from SGMs (e.g. account flagged by the bookie
    # as SGM-ineligible). Env var SGM_BLACKLIST_SESSIONS is a comma-separated
    # list of session IDs to skip. Keeps the session in regular placement for
    # singles but never tries it for multis.
    sgm_blacklist_env = os.getenv("SGM_BLACKLIST_SESSIONS", "").strip()
    if sgm_blacklist_env:
        blacklist = {s.strip() for s in sgm_blacklist_env.split(",") if s.strip()}
        filtered = [s for s in sessions if str(s.get("session_id")) not in blacklist]
        skipped = len(sessions) - len(filtered)
        if skipped:
            log.info(
                f"SGM: skipping {skipped} blacklisted session(s) "
                f"({', '.join(sorted(blacklist))})"
            )
        sessions = filtered
        if not sessions:
            log.warning("All sessions blacklisted for SGM")
            notifier.notify_manual_alert(tip)
            return []

    # Build legs array per HyperBot API format
    hb_legs = []
    for leg in tip.legs:
        # Per-leg threshold flag (set by Groq parser)
        leg_is_threshold = getattr(leg, '_is_threshold', False)

        # For PYO SGMs, keep thresholds as PYO-compatible
        # HyperBot pick_own_line market takes any line value
        if tip.is_pyo_sgm and leg_is_threshold:
            # Treat as O/U at line-0.5 for PYO market
            leg_is_threshold = False
            if leg.line and leg.line == int(leg.line):
                leg.line = float(leg.line) - 0.5
            if not leg.selection or leg.selection in ("", "over"):
                leg.selection = "over"

        # Use shared resolver for each leg
        resolved = _resolve_leg_for_hyperbot(
            leg, tip.sport, is_threshold=leg_is_threshold, for_sgm=True,
            tipster=tip.tipster,
            # v5.87: AFL SGM legs are same-game — scope the surname to BOTH event
            # teams + forbid the cross-game global roster fallback (the BUG A fix,
            # extended from singles to SGM legs). Non-AFL SGM (MLB/NBA) -> None.
            event_teams=(_afl_event_teams(tip.event)
                         if (tip.sport or "").lower() == "afl" else None),
        )

        hb_leg = {"market": resolved["market"], "selection": resolved["selection"]}
        if resolved["player"]:
            hb_leg["player"] = resolved["player"]
        if resolved["stat"]:
            hb_leg["stat"] = resolved["stat"]
        if resolved["line"] is not None:
            hb_leg["line"] = resolved["line"]

        hb_legs.append(hb_leg)
        log.info(f"SGM leg: {json.dumps(hb_leg)}")

    if tip.is_pyo_sgm:
        log.info(f"PYO SGM: {len(hb_legs)} legs treated as alt/custom lines")

    # Target odds (90% of suggested, floored at 1.01 — decimal odds below
    # 1.00 are nonsensical and HyperBot may reject them)
    target_odds = None
    if tip.suggested_odds and tip.suggested_odds > 1.0:
        target_odds = _afl_target_odds(tip.sport, tip.suggested_odds)

    stake = tip.stake_dollars

    # ── Boost session priority ─────────────────────────────────────
    # Only Adam Tran's account (session 65465) gets price-boost tokens for
    # SGMs from Sportsbet. Move boost-eligible sessions to the front so the
    # first attempt uses the boost. Env-configurable via SGM_BOOST_SESSIONS.
    # Default "" so boosts are OFF unless explicitly enabled (2026-05-30). A
    # missing env var must not silently re-enable a boost session.
    boost_sessions_env = os.getenv("SGM_BOOST_SESSIONS", "").strip()
    boost_session_ids = {
        s.strip() for s in boost_sessions_env.split(",") if s.strip()
    }
    if boost_session_ids:
        boost_first = [
            s for s in sessions if str(s.get("session_id")) in boost_session_ids
        ]
        others = [
            s for s in sessions if str(s.get("session_id")) not in boost_session_ids
        ]
        sessions = boost_first + others

    # Errors where retrying without boost won't help (the bet itself is bad).
    # Anything else we see on a boost attempt gets a no-boost retry.
    _BOOST_DONT_RETRY_PATTERNS = (
        "line moved", "selection ", "player ", "market ",
        "odds too low", "suspended", "not found", "stake",
    )

    def _should_retry_without_boost(err: str) -> bool:
        """Return True if the error is likely boost-related (not bet-data)."""
        if not err:
            return False
        low = err.lower()
        return not any(pat in low for pat in _BOOST_DONT_RETRY_PATTERNS)

    # Try on each session with stake binary search on failure
    resp = {}
    sgm_ambiguous_outcomes: list[dict] = []  # slow-rejection tracking (H2 fix 2026-05-30)
    # When an ambiguous outcome is detected, the FULL stake may have landed on
    # this bookie. Legacy SGM places the full stake per session (no remaining
    # accounting), so spillover to the next session would re-bet the whole
    # amount — a double-bet. This flag aborts the outer session loop entirely.
    # (round-2 fix 2026-05-30: the prior `continue`/inner-`break` did NOT stop
    # spillover.)
    _sgm_ambiguous_abort = False
    for session in sessions:
        sid = str(session["session_id"])
        bookie = session.get("bookie", "unknown")
        boost_eligible = sid in boost_session_ids

        # Translate event name for AFL bookies (Squiggle vs bookie team names).
        # Missing in legacy path caused GWS tips to fail on non-Sportsbet
        # sessions with "Could not find event".  _place_sgm_v4 already does
        # this; applying here too for consistency.
        event_for_hb = _bookie_event(tip.event, bookie, tip.sport)

        log.info(f"SGM on {bookie} (session {sid}): {len(hb_legs)} legs, ${stake}")

        # Resolve player-prop legs against the live catalog (prop_id + AFL
        # line/market encoding fix). Team legs pass through. If any leg isn't
        # carried, skip this session.
        session_hb_legs, enrich_err = _enrich_sgm_legs_with_prop_ids(
            hb_legs, tip, sid, bookie=bookie,
        )
        if enrich_err:
            log.warning(
                f"SGM prop_id lookup failed on {bookie} session {sid}: "
                f"{enrich_err}. Skipping this session."
            )
            continue

        # First attempt: use boost if this session is eligible
        if boost_eligible:
            log.info(f"SGM with boost attempt on {bookie} session {sid}")
            resp = hb.place_sgm_bet(
                session_id=sid, sport=tip.sport, event=event_for_hb,
                legs=session_hb_legs, stake=stake, target_odds=target_odds,
                use_boost=True,
            )

            if resp.get("success"):
                result = BetResult(
                    success=True, tip=tip, session_id=sid, bookie=bookie,
                    bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                    stake=stake, timestamp=datetime.now(),
                    placed_leg_summary=_format_tip_placement_summary(tip),
                    used_boost=True,
                )
                log.info(
                    f"SGM placed WITH BOOST: {result.bet_id} on {bookie} "
                    f"@ {result.odds}"
                )
                notifier.notify_bet_placed(result)
                return [result]

            # Boost attempt failed. If error looks bet-data-related, don't
            # retry — fall through to binary-search-stake logic below. If
            # it looks boost-related (no tokens, promo disabled, etc),
            # retry once WITHOUT boost on same session before giving up.
            boost_err = str(resp.get("error", ""))
            # v5.69 (M2): never re-fire after a maybe-landed boost attempt
            # (ambiguous-tagged or ambiguous-pattern error) — a no-boost retry
            # on the same legs/stake/session would double-stake. Erasmus.
            _boost_maybe_landed = (
                bool(resp.get("ambiguous")) or _is_ambiguous_outcome(boost_err)
            )
            if _should_retry_without_boost(boost_err) and not _boost_maybe_landed:
                log.warning(
                    f"SGM boost failed ({boost_err[:80]}), retrying "
                    f"without boost on same session"
                )
                resp = hb.place_sgm_bet(
                    session_id=sid, sport=tip.sport, event=event_for_hb,
                    legs=session_hb_legs, stake=stake, target_odds=target_odds,
                    use_boost=False,
                )
                if resp.get("success"):
                    result = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bookie,
                        bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                        stake=stake, timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip),
                        used_boost=False,
                    )
                    log.info(
                        f"SGM placed (no-boost retry): {result.bet_id} on "
                        f"{bookie} @ {result.odds}"
                    )
                    notifier.notify_bet_placed(result)
                    return [result]
                # Both failed — fall through to binary search path
            # else: bet-data error, skip no-boost retry
        else:
            # Non-boost-eligible session: normal place call
            resp = hb.place_sgm_bet(
                session_id=sid, sport=tip.sport, event=event_for_hb,
                legs=session_hb_legs, stake=stake, target_odds=target_odds,
            )

            if resp.get("success"):
                result = BetResult(
                    success=True, tip=tip, session_id=sid, bookie=bookie,
                    bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                    stake=stake, timestamp=datetime.now(),
                    placed_leg_summary=_format_tip_placement_summary(tip),
                )
                log.info(f"SGM placed: {result.bet_id} on {bookie} @ {result.odds}")
                notifier.notify_bet_placed(result)
                return [result]

        error = str(resp.get("error", "")).lower()
        log.warning(f"SGM failed on {bookie}: {resp.get('error')}")

        # ── Ambiguous-outcome check on initial failure (H2 fix 2026-05-30) ─
        # Wire _is_ambiguous_outcome into the text-pattern failure path. If the
        # initial SGM placement returned an ambiguous error, fire the alert and
        # skip to the next session (don't binary-search on a maybe-placed bet).
        if _is_ambiguous_outcome(error):
            log.error(
                f"SGM: AMBIGUOUS OUTCOME (text pattern) "
                f"{bookie}:{sid} stake=${stake:.2f} "
                f"err='{error[:80]}'. Aborting spillover — full stake may have "
                f"landed on this bookie; trying another would double-bet."
            )
            sgm_ambiguous_outcomes.append({
                "bookie": bookie,
                "session_id": sid,
                "stake": round(stake, 2),
                "odds": resp.get("odds") or 0,
                "elapsed_sec": 0.0,
                "error": error[:200],
                "reason": "ambiguous_text_pattern",
            })
            _sgm_ambiguous_abort = True
            break

        # Binary-search stake if failure is stake-related. Uses no-boost
        # placements (boost is all-or-nothing, not a sizing issue).
        if ("stake" in error and "too high" in error) or "max stake" in error:
            log.info(f"Binary searching max stake for SGM on {bookie}")
            lo, hi = 0.5, stake
            best_stake = None
            while hi - lo > 0.5:
                mid = round((lo + hi) / 2, 2)
                _sgm_bs_t0 = time.time()
                test_resp = hb.place_sgm_bet(
                    session_id=sid, sport=tip.sport, event=event_for_hb,
                    legs=session_hb_legs, stake=mid, target_odds=target_odds,
                )
                _sgm_bs_elapsed = round(time.time() - _sgm_bs_t0, 2)
                if test_resp.get("success"):
                    best_stake = mid
                    result = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bookie,
                        bet_id=test_resp.get("bet_id"), odds=test_resp.get("odds"),
                        stake=mid, timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip),
                    )
                    log.info(f"SGM placed at binary-searched stake ${mid}")
                    notifier.notify_bet_placed(result)
                    return [result]
                # ── Slow-rejection AMBIGUOUS check inside binary search ──
                # H2 part-2 fix (2026-05-30): gate on `not _is_definitely_pre_
                # placement` (the Erasmus predicate used by every other slow
                # guard at 2164/2932/4471), NOT `_is_ambiguous_outcome`. The
                # exact regression this exists for is a slow "stake too high"
                # that actually landed — that string is NOT in
                # AMBIGUOUS_OUTCOME_PATTERNS, so the old guard was skipped and
                # the binary search kept probing DOWN, re-submitting smaller
                # stakes on a bet that may already exist.
                _bs_err = str(test_resp.get("error", ""))
                # Fix I (2026-06-01): also abort on the FAST-ambiguous flag
                # (test_resp['ambiguous'] — a fast-failing POST that may have
                # landed), matching the other five slow-rejection guards. This
                # sub-path previously gated ONLY on elapsed>=5s, so a sub-5s
                # ambiguous (e.g. a connection-drop timeout tagged ambiguous)
                # would keep probing DOWN and could double-bet.
                _bs_ambiguous_flag = bool(test_resp.get("ambiguous"))
                _bs_slow = _sgm_bs_elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
                if (
                    (_bs_slow or _bs_ambiguous_flag)
                    and not _is_definitely_pre_placement(_bs_err)
                ):
                    _bs_reason = "slow_rejection" if _bs_slow else "fast_ambiguous"
                    log.error(
                        f"SGM binary-search: AMBIGUOUS OUTCOME "
                        f"({_bs_reason.replace('_', ' ')}) {bookie}:{sid} "
                        f"stake=${mid:.2f} elapsed={_sgm_bs_elapsed:.1f}s "
                        f"err='{_bs_err[:80]}'. Aborting spillover — bet may have "
                        f"landed; trying another session would double-bet."
                    )
                    sgm_ambiguous_outcomes.append({
                        "bookie": bookie,
                        "session_id": sid,
                        "stake": round(mid, 2),
                        "odds": test_resp.get("odds") or 0,
                        "elapsed_sec": _sgm_bs_elapsed,
                        "error": _bs_err[:200],
                        "reason": _bs_reason,
                    })
                    _sgm_ambiguous_abort = True
                    break
                lo = mid if "stake" in str(test_resp.get("error", "")).lower() else lo
                hi = mid if "stake" not in str(test_resp.get("error", "")).lower() else hi

        # Ambiguous outcome detected on this session — stop spillover entirely.
        # The full stake may already be on this bookie; trying another session
        # at full stake would double-bet.
        if _sgm_ambiguous_abort:
            break

    # Ambiguous outcomes from binary search / text-pattern checks (H2 fix 2026-05-30)
    if sgm_ambiguous_outcomes:
        _emit_sports_ambiguous_alert(tip, sgm_ambiguous_outcomes)

    # On an ambiguous abort the full stake MAY already be on the bookie. Do NOT
    # also fire the "place manually" alert — that contradicts the ambiguous
    # critical alert and could prompt Wilson to manually place a bet that landed
    # (double-bet). The critical AMBIGUOUS alert above is the operator signal;
    # return a non-success result with ambiguous flagged so the caller logs it.
    if _sgm_ambiguous_abort:
        return [BetResult(
            success=False, tip=tip,
            error=f"SGM ambiguous outcome: {resp.get('error', 'unknown')}",
            timestamp=datetime.now(),
            placed_leg_summary=_format_tip_placement_summary(tip),
            is_ambiguous=True,
        )]

    # All sessions failed, alert for manual
    log.warning("SGM auto-placement failed on all sessions, alerting")
    tip.alert_reason = f"SGM auto-place failed: {resp.get('error', 'unknown')}"
    notifier.notify_manual_alert(tip)
    return [BetResult(
        success=False, tip=tip, error=f"SGM failed: {resp.get('error')}",
        timestamp=datetime.now(),
        placed_leg_summary=_format_tip_placement_summary(tip),
    )]


# ────────────────────────────────────────────────────────────────────
# v4.0 — SGM placement via per-sport priority + liability cap
# ────────────────────────────────────────────────────────────────────
# Mirrors _place_singles_v4 but for SGMs. Differences from v3.10 _place_sgm:
#
#  1. Sessions filtered + ordered via NBA_SGM_SESSION_PRIORITY /
#     AFL_SGM_SESSION_PRIORITY (instead of legacy SESSION_PRIORITY).
#     Sessions not listed are excluded from auto-placement.
#  2. Liability cap from sessions.yaml `sgm` key per sport. Most accounts
#     have `sgm: unlimited` so this is a light touch in practice — the
#     existing flow stakes the full intended amount and ladders down on
#     stake-too-high.
#  3. Boost preference comes from yaml `boost_eligible` flag instead of
#     env SGM_BOOST_SESSIONS. Falls back to env if yaml meta missing.
#  4. Stake ladder replaces binary search on stake-too-high (matches
#     singles, simpler to reason about, fewer probe calls).
#  5. Sessions marked used regardless of outcome (no re-attempting same
#     account inside the same SGM tip).
#  6. _bookie_event translation applied per-session for AFL team-leg SGMs.
#
# Preserved unchanged:
#  - _is_team_plus_total_sgm → manual rule
#  - SGM_BLACKLIST_SESSIONS env filter (not migrating to yaml in S3)
#  - PYO normalisation
#  - per-session prop_id enrichment via _enrich_sgm_legs_with_prop_ids
#  - retry without boost on boost-related errors
#  - team-ML+total → manual rule

def _sgm_est_combined_odds(leg_odds: list) -> "float | None":
    """Estimate an SGM's combined decimal odds as the PRODUCT of the per-leg
    catalog odds (v5.38). An SGM returns no combined price until it's placed, so
    to size a LIABILITY cap (afl.sgm [400,300,200] / mlb.sgm [130,100,87]) into a
    stake we estimate the combined odds from the legs the catalog priced.

    Returns None unless EVERY leg has a usable price (>1.0): a missing leg would
    make the product incomplete and UNDER-state the combined odds, which would
    OVER-size the liability->stake conversion (stake = liab/(odds-1)) — refusing
    (None) routes the caller to its safe fallback / manual instead. NOTE the
    product OVER-states a positively-correlated SGM's TRUE combined odds (e.g.
    HRRBI 1+ & 2+ are correlated, so the real SGM price < the product), which
    makes the derived stake UNDER-size — i.e. realised liability lands UNDER the
    cap. That's the safe direction; the bookie MBL backstops the rest."""
    if not leg_odds:
        return None
    prod = 1.0
    for o in leg_odds:
        try:
            o = float(o)
        except (TypeError, ValueError):
            return None
        if o <= 1.0:
            return None
        prod *= o
    return round(prod, 4) if prod > 1.0 else None


def _sgm_ladder_steps(list_ladder: list, effective_stake: float) -> list[float]:
    """Stake steps for a list-cap (e.g. MLB [87,85,80]) SGM ladder: each rung
    clamped to what's left to fill (effective_stake), de-duped, >0. Keeps the
    explicit 87->85->80 rungs (NOT a percentage ladder) so the bot probes the
    account's per-SGM stake limit precisely, laddering down on a stake reject."""
    steps: list[float] = []
    for v in list_ladder:
        s = round(min(float(v), effective_stake), 2)
        if s > 0 and (not steps or steps[-1] != s):
            steps.append(s)
    return steps


def _sgm_outcome_record(tip, intended_stake, placed_results, remaining_stake,
                        orchestrated) -> dict:
    """Build the audit.jsonl `tip_outcome` record for a SEQUENTIAL SGM placement
    (_place_sgm_v4). v5.47: this path emitted NO outcome before — only the
    concurrent _place_sgm_fanout did — so a placed NBA SGM (sequential) was
    missing from the audit trail (2026-06-09 KAT/Brunson). Pure (no I/O) so it's
    unit-testable; the caller does the once-guarded, try/except'd _log_jsonl."""
    return {
        "type": "tip_outcome", "tipster": tip.tipster, "event": tip.event,
        "intended_stake": round(intended_stake, 2),
        "placed_stake": round(sum(r.stake or 0 for r in placed_results), 2),
        "unfilled_stake": round(max(0.0, remaining_stake), 2),
        "fanout": "sgm_sequential", "sgm": True, "orchestrated": orchestrated,
        "placements": [
            {"session_id": r.session_id, "bookie": r.bookie, "stake": r.stake,
             "fill_odds": r.odds, "bet_id": r.bet_id} for r in placed_results],
    }


def _place_sgm_v4(tip: ParsedTip, _orchestrated: bool = False) -> list[BetResult]:
    """v4.0 SGM placement. See block comment above for design notes.

    `_orchestrated=True` (set by `_place_mlb_hrrbi`): suppress the terminal
    "all sessions failed -> manual" alert, because the MLB orchestrator owns
    final alerting (it still has Alex's single to try, then decides the one
    leftover/failure notification). Per-bet placed alerts + the ambiguous aux
    alert still fire; the MBL-violation alert is SUPPRESSED when orchestrated
    (v5.34: the MLB per-account model leaves expected leftover, and a 538
    ladder-down is benign — see _should_alert_mbl_violation)."""

    # Wilson's rule: team ML/handicap + total SGMs always go to manual.
    # Different bet sizing rules apply for this combination — skip auto-place.
    if _is_team_plus_total_sgm(tip):
        log.info(
            "SGM contains team market + total: routing to manual alert "
            "(Wilson's bet-sizing rule for this combination)"
        )
        tip.alert_reason = (
            "SGM with team ML/handicap + total — different sizing rules, "
            "manual placement only"
        )
        notifier.notify_manual_alert(tip)
        return []

    # MLB: ONLY the approved HRRBI 2-leg [1+, 2+] same-player SGM auto-places.
    # Any other MLB SGM shape (a Groq misparse, an unexpected combo) routes to
    # manual — belt-and-braces on top of the flat MLB stake, so the only thing
    # that ever auto-places for MLB is the validated HRRBI correlation edge.
    if (tip.sport or "").lower() == "mlb" and not _is_mlb_hrrbi_sgm(tip):
        log.info("MLB SGM is not the approved HRRBI 2-leg shape — routing to manual")
        tip.alert_reason = "MLB SGM not the approved HRRBI 2-leg play — manual only"
        notifier.notify_manual_alert(tip)
        return [BetResult(
            success=False, tip=tip,
            error="MLB non-HRRBI SGM routes to manual",
            timestamp=datetime.now(),
            placed_leg_summary=_format_tip_placement_summary(tip),
        )]

    sport = tip.sport or "nba"

    # Confirm a priority list exists for this sport's SGMs. No list = manual
    # only (matches singles v4 + spec §"Sessions not in priority list excluded").
    configured_priority = session_priority.get_priority_for(sport, is_sgm=True)
    if not configured_priority:
        log.info(
            f"v4 SGM: no priority list configured for {sport} SGMs — routing to manual"
        )
        tip.alert_reason = f"No SGM priority list for {sport} (manual only)"
        notifier.notify_manual_alert(tip)
        return [BetResult(
            success=False, tip=tip,
            error=f"{sport} SGM routes to manual",
            timestamp=datetime.now(),
            placed_leg_summary=_format_tip_placement_summary(tip),
        )]

    raw_sessions = _v4_get_active_sessions_unfiltered(tip)
    if not raw_sessions:
        log.warning("v4 SGM: no active sessions after sport filter")
        if not tip.alert_reason:
            tip.alert_reason = (
                f"No active HyperBot sessions available for {sport.upper()} SGM"
            )
        notifier.notify_manual_alert(tip)
        return [BetResult(
            success=False, tip=tip, error="No active sessions",
            timestamp=datetime.now(),
            placed_leg_summary=_format_tip_placement_summary(tip),
        )]

    sessions = session_priority.filter_and_order_sessions(
        raw_sessions, sport, is_sgm=True,
    )
    if not sessions:
        log.warning(
            f"v4 SGM: no priority sessions for {sport} SGMs — all candidates "
            f"unlisted. Routing to manual."
        )
        if not tip.alert_reason:
            # Saiyan SGM 2026-04-30 hit this path with empty alert_reason
            # so Telegram showed "Reason:  " with nothing useful. Notifier
            # has a default fallback now too, but setting it here gives a
            # more specific reason than the generic notifier default.
            tip.alert_reason = (
                f"No SGM priority sessions for {sport.upper()} "
                f"(all candidates unlisted in {sport.upper()}_SGM_SESSION_PRIORITY)"
            )
        notifier.notify_manual_alert(tip)
        return [BetResult(
            success=False, tip=tip,
            error=f"No SGM priority sessions for {sport}",
            timestamp=datetime.now(),
            placed_leg_summary=_format_tip_placement_summary(tip),
        )]

    # SGM blacklist (env). Spec §9 keeps this out of yaml for now.
    sgm_blacklist_env = os.getenv("SGM_BLACKLIST_SESSIONS", "").strip()
    if sgm_blacklist_env:
        blacklist = {s.strip() for s in sgm_blacklist_env.split(",") if s.strip()}
        before = len(sessions)
        sessions = [s for s in sessions if str(s.get("session_id")) not in blacklist]
        if before - len(sessions):
            log.info(
                f"v4 SGM: dropped {before - len(sessions)} blacklisted session(s) "
                f"({', '.join(sorted(blacklist))})"
            )
        if not sessions:
            log.warning("v4 SGM: all priority sessions blacklisted")
            if not tip.alert_reason:
                tip.alert_reason = (
                    f"All {sport.upper()} SGM-eligible sessions are in "
                    f"SGM_BLACKLIST_SESSIONS env var"
                )
            notifier.notify_manual_alert(tip)
            return [BetResult(
                success=False, tip=tip, error="All sessions blacklisted for SGM",
                timestamp=datetime.now(),
                placed_leg_summary=_format_tip_placement_summary(tip),
            )]

    # Build legs once. Legs are bookmaker-agnostic (pre-prop_id enrichment),
    # so this work isn't repeated per-session — only the prop_id lookup is.
    hb_legs = []
    for leg in tip.legs:
        leg_is_threshold = getattr(leg, "_is_threshold", False)

        # PYO SGMs: thresholds get cast to "over" at line-0.5 so HyperBot's
        # pick_own_line market accepts them.
        if tip.is_pyo_sgm and leg_is_threshold:
            leg_is_threshold = False
            if leg.line and leg.line == int(leg.line):
                leg.line = float(leg.line) - 0.5
            if not leg.selection or leg.selection in ("", "over"):
                leg.selection = "over"

        resolved = _resolve_leg_for_hyperbot(
            leg, tip.sport, is_threshold=leg_is_threshold, for_sgm=True,
            tipster=tip.tipster,
            # v5.87: AFL SGM legs are same-game — scope the surname to BOTH event
            # teams + forbid the cross-game global roster fallback (the BUG A fix,
            # extended from singles to SGM legs). Non-AFL SGM (MLB/NBA) -> None.
            event_teams=(_afl_event_teams(tip.event)
                         if (tip.sport or "").lower() == "afl" else None),
        )
        hb_leg = {"market": resolved["market"], "selection": resolved["selection"]}
        if resolved["player"]:
            hb_leg["player"] = resolved["player"]
        if resolved["stat"]:
            hb_leg["stat"] = resolved["stat"]
        if resolved["line"] is not None:
            hb_leg["line"] = resolved["line"]
        hb_legs.append(hb_leg)
        log.info(f"v4 SGM leg: {json.dumps(hb_leg)}")

    if tip.is_pyo_sgm:
        log.info(f"v4 SGM (PYO): {len(hb_legs)} legs treated as alt/custom lines")

    # Target odds: 90% of suggested, floored at 1.01 (HyperBot rejects < 1.0)
    target_odds = None
    if tip.suggested_odds and tip.suggested_odds > 1.0:
        target_odds = _afl_target_odds(tip.sport, tip.suggested_odds)

    intended_stake = tip.stake_dollars
    tipped_odds = tip.suggested_odds

    # Errors that mean the bet itself is bad (no point retrying without boost)
    _BOOST_DONT_RETRY_PATTERNS = (
        "line moved", "selection ", "player ", "market ",
        "odds too low", "suspended", "not found", "stake",
    )

    def _should_retry_without_boost(err: str) -> bool:
        if not err:
            return False
        low = err.lower()
        return not any(pat in low for pat in _BOOST_DONT_RETRY_PATTERNS)

    # Env fallback for boost preference if yaml meta unavailable. Yaml flag
    # takes precedence when meta is loaded. Default "" so boosts are OFF unless
    # explicitly enabled (2026-05-30) — a missing env var must not re-enable.
    boost_env = os.getenv("SGM_BOOST_SESSIONS", "").strip()
    boost_env_set = {
        s.strip() for s in boost_env.split(",") if s.strip()
    }

    def _is_boost_session(sid: str) -> bool:
        meta = session_priority.get_session_meta(sid)
        if meta is not None:
            return bool(meta.boost_eligible)
        return sid in boost_env_set

    # ── Outer loop: walk sessions in priority order ────────────────
    used_session_ids: set[str] = set()
    last_error = "unknown"
    last_resp: dict = {}
    # Ladder + MBL tracking. Same shape and intent as singles v4 — see
    # _place_singles_v4 for the rationale.
    ladder_attempts: list[dict] = []
    mbl_violations: list[dict] = []
    # Slow-rejection AMBIGUOUS_OUTCOME tracking. Same shape and intent as
    # singles v4 — Erasmus class slow rejection detection. Each entry:
    # {bookie, session_id, stake, odds, elapsed_sec, error, reason}.
    ambiguous_outcomes: list[dict] = []

    # SGM spillover state. Per Wilson 2026-05-07: when the first session
    # accepts a partial fill (account-limited, MBL hit, etc.), continue
    # placing the remainder on subsequent priority sessions. Anchor the
    # subsequent target_odds at first_placed_odds*0.9 so we don't accept
    # materially worse odds. 10% tolerance per Wilson. Holmgren 2026-05-07
    # 17:50: $80 placed on 65465, $320 unfilled, would have spilled to
    # 68723 with these changes.
    placed_results: list[BetResult] = []
    remaining_stake = intended_stake
    first_placed_odds: float | None = None
    original_target_odds = target_odds  # Snapshot for first-session use

    # v5.36: end-to-end timer + consolidate the per-account placements into ONE
    # notify_tip_placed_summary (instead of a per-account notify_bet_placed each
    # — the "2 messages for a 2-account spillover" fix). The summary is emitted
    # by _emit_sgm_aux_alerts (the universal terminal hook, so it covers the
    # EARLY full-fill returns too, not just the tail), once-guarded by
    # _sgm_summary_sent. The flag makes notify_bet_placed no-op (telegram + its
    # ledger write); the summary writes the per-leg ledger rows. Orchestrated
    # (MLB) keeps its own summary, so it is NOT flagged here.
    _sgm_v4_t_start = time.time()
    _sgm_summary_sent = [False]  # once-guard for the consolidated summary
    _sgm_outcome_logged = [False]  # v5.47: once-guard for the audit.jsonl tip_outcome
    if not _orchestrated:
        tip._sgm_consolidate = True

    def _emit_sgm_aux_alerts():
        """Fire ladder/bookie-stake-cap alerts before any return path. The
        stake-cap alert takes priority — sending both would just duplicate
        the same data on the Maintenance channel (both non-critical since
        v5.52). Safe to call multiple times: callers always
        return immediately after.

        Slow-rejection ambiguous outcomes also fire from here, in addition
        to (not instead of) the MBL/ladder alert. They're a different class
        of problem (bookie may have placed the bet) and warrant their own
        critical alert.
        """
        # v5.36: ONE consolidated BET-PLACED summary, emitted at the FIRST
        # terminal reached. A full-fill (incl. a 2-account spillover that
        # completes) returns EARLY via the inner _accumulate_and_check_done
        # paths — NOT the function tail — so the summary must fire here, the
        # universal terminal hook, or a fully-filled SGM would send NOTHING
        # (per-account notify is suppressed via tip._sgm_consolidate). Once-
        # guarded; non-orchestrated only (MLB owns its own summary). The
        # summary writes the per-leg bets_placed.csv rows, so the ledger is
        # preserved. SGM is sequential spillover -> concurrent_bookies=False.
        if placed_results and not _orchestrated and not _sgm_summary_sent[0]:
            _sgm_summary_sent[0] = True
            _sgm_session_timing = [{
                "session_id": r.session_id, "bookie": r.bookie,
                "elapsed_sec": getattr(r, "elapsed_sec", None),
                "attempts": 1, "fails": 0, "succeeded": True,
            } for r in placed_results]
            try:
                notifier.notify_tip_placed_summary(
                    tip, placed_results, intended_stake, round(remaining_stake, 2),
                    total_elapsed_sec=round(time.time() - _sgm_v4_t_start, 2),
                    session_timing=_sgm_session_timing,
                    concurrent_bookies=False,
                )
            except Exception as e:
                log.error(f"v4 SGM consolidated summary notify failed: {e}")

        if _should_alert_mbl_violation(mbl_violations, remaining_stake, _orchestrated):
            try:
                notifier.notify_sports_mbl_violation(tip, mbl_violations)
            except Exception as e:
                log.error(f"notify sports MBL violation failed: {e}")
        elif mbl_violations:
            log.info(
                f"v4 SGM: {len(mbl_violations)} stake-too-high ladder-down(s) on a "
                f"{'fully-filled' if remaining_stake <= MBL_FILLED_DEADBAND else 'orchestrated'} "
                f"bet — benign (designed ladder behaviour), no MBL alert"
            )
        elif ladder_attempts:
            try:
                notifier.notify_sports_ladder_maintenance(tip, ladder_attempts)
            except Exception as e:
                log.error(f"notify sports ladder maintenance failed: {e}")

        # Slow-rejection critical alert fires independent of MBL/ladder
        # (different class of problem — bookie may have placed the bet).
        if ambiguous_outcomes:
            _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)

        # v5.47: write the SGM tip_outcome to audit.jsonl from this universal
        # terminal hook (once-guarded). _place_sgm_v4 (the SEQUENTIAL SGM path
        # NBA SGMs use) previously wrote NO tip_outcome at all — only the
        # concurrent _place_sgm_fanout did — so a placed sequential SGM was
        # missing from the audit trail (2026-06-09 KAT u20.5/Brunson o21.5 $400
        # placed + recorded in bets_placed.csv, but with no audit.jsonl row).
        # The hook is the universal terminal (it covers the early full-fill
        # returns AND the partial/exhausted post-loop paths), so logging here
        # catches every non-orchestrated exit exactly once. Orchestrated (MLB)
        # outcomes are owned by the orchestrator. Best-effort: an audit write
        # must never break a placement.
        if not _orchestrated and not _sgm_outcome_logged[0]:
            _sgm_outcome_logged[0] = True
            try:
                _log_jsonl(_audit_log_path(), _sgm_outcome_record(
                    tip, intended_stake, placed_results, remaining_stake, _orchestrated))
            except Exception as e:
                log.error(f"v4 SGM tip_outcome audit write failed: {e}")

    def _accumulate_and_check_done(result: BetResult) -> bool:
        """Record a successful SGM placement and update spillover state.

        Returns True if the tip is fully filled (caller should return
        placed_results). Returns False if there's still remaining stake
        and the caller should break the inner ladder and continue to the
        next priority session.

        First placement anchors first_placed_odds for subsequent target_odds.
        """
        nonlocal remaining_stake, first_placed_odds
        placed_results.append(result)
        # Anchor odds tolerance on the first placement only. Subsequent
        # placements may come in at higher odds (better) or within 10%
        # below (still acceptable per Wilson). Anchoring on the first
        # avoids drift if odds shift across spillover sessions.
        if first_placed_odds is None and result.odds:
            try:
                first_placed_odds = float(result.odds)
            except (TypeError, ValueError):
                first_placed_odds = None
        try:
            placed_amt = float(result.stake or 0)
        except (TypeError, ValueError):
            placed_amt = 0
        remaining_stake = max(0.0, remaining_stake - placed_amt)
        log.info(
            f"v4 SGM spillover state: placed ${placed_amt:.2f}, "
            f"remaining ${remaining_stake:.2f}"
        )
        # Done if remaining is essentially zero.  STAKE_FLOOR is 0.0 so this
        # catches genuine zero-remaining and any auto-cap residue (e.g. $0.50
        # left after Neds caps to $319.50) without abandoning real spillover
        # amounts.  Previously used < 1.0 which swallowed the residue silently.
        return remaining_stake <= STAKE_FLOOR

    for session in sessions:
        sid = str(session["session_id"])
        if sid in used_session_ids:
            continue

        bookie = session.get("bookie", "unknown")
        boost_eligible = _is_boost_session(sid)

        # Per-session catalog resolution: prop_id for O/U legs + AFL
        # line/market encoding (N+ -> over N-0.5 in the threshold ladder).
        # Team legs pass through unchanged. If any leg isn't carried, skip
        # this session (ultimately routes to manual).
        _sgm_session_odds: list = []  # v5.38: per-leg catalog odds for est combined
        session_hb_legs, enrich_err = _enrich_sgm_legs_with_prop_ids(
            hb_legs, tip, sid, bookie=bookie, odds_out=_sgm_session_odds,
        )
        if enrich_err:
            log.warning(
                f"v4 SGM prop_id lookup failed on {bookie} session {sid}: "
                f"{enrich_err}. Skipping session."
            )
            used_session_ids.add(sid)
            continue
        _sgm_est_odds = _sgm_est_combined_odds(_sgm_session_odds)

        # Resolve per-session sgm cap from yaml.
        #  - LIST cap (afl.sgm [400,300,200] / mlb.sgm [130,100,87]) = a LIABILITY
        #    ladder. v5.38 (Wilson): an SGM returns no combined price pre-place, so
        #    we ESTIMATE it as the PRODUCT of the per-leg catalog odds
        #    (_sgm_est_odds) and convert each liability bracket to a stake via
        #    resolve_stake_steps (liability/(est_odds-1)), laddering down on a
        #    reject. This RETIRES the v5.15 MLB "$100 stake rung" hack — the
        #    [130,100,87] values are now real liabilities, not raw stakes. If the
        #    estimate is unavailable (a leg lacked a catalog price), resolve_stake_
        #    steps falls back to its no-odds seeded ladder (capped at intended), so
        #    a list cap is NEVER treated as a raw stake. (The CONCURRENT fan-out,
        #    _place_sgm_fanout, is the PRIMARY path for AFL + MLB SGMs; this
        #    sequential branch is the fallback when SGM_CONCURRENT_FANOUT=false or
        #    the estimate can't be formed.)
        #  - SCALAR / unlimited cap (NBA sgm: 600) = existing behaviour
        #    (resolve_max_stake when tipped odds exist, else pass intended through).
        #    Byte-identical to before.
        sgm_cap = session_priority.lookup_liability_cap(sid, sport, "sgm")
        list_ladder = None
        if isinstance(sgm_cap, tuple) and sgm_cap:
            list_ladder = [float(v) for v in sgm_cap]
            sizing_odds = _sgm_est_odds or 0  # est combined odds for liability sizing
            _top_liab = max(list_ladder)
            if sizing_odds and sizing_odds > 1.0:
                # Top liability bracket -> its stake (the ladder's biggest try).
                max_stake = session_priority.liability_to_max_stake(_top_liab, sizing_odds)
                cap_reason = f"list liability ladder {list_ladder} @ est_odds={_sgm_est_odds}"
            else:
                # No est odds (a leg lacked a catalog price): we can't convert the
                # liability brackets to stakes, so seed the ceiling at the SMALLEST
                # bracket (the most conservative — adversarial-pass fix v5.38). The
                # old MLB path's blind first try was ~$100; min([130,100,87])=$87 is
                # tighter, not looser (MAX would have staked the $130 LIABILITY as a
                # raw stake — over-exposed). The bookie MBL is the only true backstop
                # without odds; keep it small.
                max_stake = min(intended_stake, min(list_ladder))
                cap_reason = (f"list liability ladder {list_ladder} (no est odds — "
                              f"seeded ceiling at min bracket ${min(list_ladder):.0f})")
        else:
            sizing_odds = tipped_odds if tipped_odds and tipped_odds > 1.0 else 0
            if sizing_odds:
                max_stake, cap_reason = session_priority.resolve_max_stake(
                    sid, sport, "sgm", sizing_odds, intended_stake,
                )
            else:
                # No tipped odds — pass intended through, let bookie ladder it
                max_stake = intended_stake
                cap_reason = "no-odds"
        log.info(
            f"v4 SGM cap: {sid} {sport}.sgm @ {sizing_odds} -> "
            f"max_stake=${max_stake:.0f} ({cap_reason})"
        )

        if max_stake <= 0:
            log.warning(f"v4 SGM: max_stake=0 on {sid}, skipping")
            used_session_ids.add(sid)
            continue

        # Translate event for this bookie (AFL Squiggle -> bookmaker form).
        event_for_hb = _bookie_event(tip.event, bookie, tip.sport)

        # Spillover-aware sizing. Cap with effective_stake = min(remaining,
        # max_stake) so we never try more than is left to fill. On the
        # first session this equals the original intended stake; on
        # spillovers it's whatever's left after prior placements.
        effective_stake = min(remaining_stake, max_stake)
        if effective_stake <= 0:
            log.info(f"v4 SGM: nothing to place on {sid} (remaining=0), stopping")
            break

        # Spillover target_odds: anchor at first_placed_odds * 0.9 once we
        # have a placement. Higher odds (better for us) always pass; lower
        # by more than 10% are rejected by HyperBot. On the first session
        # use the original target_odds (90% of tipped) per pre-spillover
        # behaviour.
        if first_placed_odds is not None:
            spillover_target = round(max(1.01, first_placed_odds * 0.9), 2)
            target_odds = spillover_target
            log.info(
                f"v4 SGM spillover: target_odds={target_odds} "
                f"(90% of first placed @ {first_placed_odds})"
            )
        else:
            target_odds = original_target_odds

        # Stake steps. v5.38: a LIST cap (afl.sgm [400,300,200] / mlb.sgm
        # [130,100,87]) is a LIABILITY ladder -> resolve_stake_steps converts each
        # bracket to a stake via the est combined odds (liability/(est_odds-1)),
        # capped at effective_stake; with no est odds it falls back to the seeded
        # percentage ladder (capped at effective_stake) so a list value is never a
        # raw stake. Scalar/unlimited -> the percentage ladder (NBA SGMs unchanged).
        if list_ladder is not None:
            steps, _step_reason, _ = session_priority.resolve_stake_steps(
                sid, sport, "sgm",
                sizing_odds if (sizing_odds and sizing_odds > 1.0) else 0,
                effective_stake, _v4_ladder_steps,
            )
        else:
            steps = _v4_ladder_steps(effective_stake)
        success_on_session = False

        log.info(
            f"v4 SGM on {bookie} (session {sid}): {len(session_hb_legs)} legs, "
            f"remaining ${remaining_stake} (effective ${effective_stake}) steps={steps}"
        )

        for step_stake in steps:
            # Per-attempt timing for the bet log alert. SGMs go through up
            # to three placement paths (boost, no-boost retry, no-boost
            # initial) — capture start fresh on each so the recorded
            # elapsed reflects the call that actually returned the result.
            import time as _sgm_time_mod
            # Single source of truth for the elapsed time of the LAST
            # place_sgm_bet call on this step. Used by the slow-rejection
            # AMBIGUOUS_OUTCOME check downstream. Reset per step so a slow
            # rejection on one rung doesn't contaminate the next.
            _sgm_last_elapsed: float = 0.0
            # First attempt: with boost if eligible
            if boost_eligible:
                log.info(
                    f"v4 SGM ladder ${step_stake:.2f} on {bookie} session {sid} (boost)"
                )
                _sgm_t_start = _sgm_time_mod.time()
                resp = hb.place_sgm_bet(
                    session_id=sid, sport=tip.sport, event=event_for_hb,
                    legs=session_hb_legs, stake=step_stake, target_odds=target_odds,
                    use_boost=True,
                )
                _sgm_elapsed = round(_sgm_time_mod.time() - _sgm_t_start, 2)
                _sgm_last_elapsed = _sgm_elapsed
                last_resp = resp
                if resp.get("success"):
                    try:
                        actual_stake = float(resp.get("stake", step_stake))
                    except (TypeError, ValueError):
                        actual_stake = step_stake
                    if abs(actual_stake - step_stake) > 1.0:
                        log.warning(
                            f"v4 SGM AUTO-CAP detected on SGM: requested "
                            f"${step_stake:.2f}, bookie accepted ${actual_stake:.2f} "
                            f"({bookie}:{sid})"
                        )
                    result = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bookie,
                        bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                        stake=actual_stake, timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip),
                        used_boost=True,
                        elapsed_sec=_sgm_elapsed,
                    )
                    log.info(
                        f"v4 SGM placed WITH BOOST: {result.bet_id} on {bookie} "
                        f"@ {result.odds}"
                    )
                    if not _orchestrated:
                        notifier.notify_bet_placed(result)
                    if _accumulate_and_check_done(result):
                        _emit_sgm_aux_alerts()
                        return placed_results
                    # Partial fill — continue outer loop to spill remainder
                    success_on_session = True
                    break

                boost_err = str(resp.get("error", "") or "")
                last_error = boost_err
                # v5.69 (M2): NEVER re-fire after a maybe-landed boost attempt.
                # A slow (>=5s) or ambiguous-tagged boost reject may already be
                # ON the books at the bookie; re-placing without boost on the
                # same legs/stake/session would DOUBLE-STAKE (the Erasmus
                # class). Skip the retry and let it fall through to the
                # slow-rejection/ambiguous guard below (debit-as-placed +
                # reconcile), which is the only safe handling for maybe-landed.
                _boost_maybe_landed = (
                    bool(resp.get("ambiguous"))
                    or _sgm_elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
                    or _is_ambiguous_outcome(boost_err)
                )
                # Retry once without boost on same session if error looks
                # boost-related (no tokens, promo disabled, etc) AND the boost
                # attempt was provably not landed.
                if _should_retry_without_boost(boost_err) and not _boost_maybe_landed:
                    log.warning(
                        f"v4 SGM boost failed ({boost_err[:80]}); retrying "
                        f"without boost on same session"
                    )
                    _sgm_t_retry = _sgm_time_mod.time()
                    resp = hb.place_sgm_bet(
                        session_id=sid, sport=tip.sport, event=event_for_hb,
                        legs=session_hb_legs, stake=step_stake, target_odds=target_odds,
                        use_boost=False,
                    )
                    _sgm_retry_elapsed = round(_sgm_time_mod.time() - _sgm_t_retry, 2)
                    _sgm_last_elapsed = _sgm_retry_elapsed
                    last_resp = resp
                    if resp.get("success"):
                        try:
                            actual_stake = float(resp.get("stake", step_stake))
                        except (TypeError, ValueError):
                            actual_stake = step_stake
                        if abs(actual_stake - step_stake) > 1.0:
                            log.warning(
                                f"v4 SGM AUTO-CAP detected on SGM: requested "
                                f"${step_stake:.2f}, bookie accepted "
                                f"${actual_stake:.2f} ({bookie}:{sid}, no-boost retry)"
                            )
                        result = BetResult(
                            success=True, tip=tip, session_id=sid, bookie=bookie,
                            bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                            stake=actual_stake, timestamp=datetime.now(),
                            placed_leg_summary=_format_tip_placement_summary(tip),
                            used_boost=False,
                            elapsed_sec=_sgm_retry_elapsed,
                        )
                        log.info(
                            f"v4 SGM placed (no-boost retry): {result.bet_id} on "
                            f"{bookie} @ {result.odds}"
                        )
                        if not _orchestrated:
                            notifier.notify_bet_placed(result)
                        if _accumulate_and_check_done(result):
                            _emit_sgm_aux_alerts()
                            return placed_results
                        success_on_session = True
                        break
                    last_error = str(resp.get("error", "") or "")
            else:
                log.info(
                    f"v4 SGM ladder ${step_stake:.2f} on {bookie} session {sid}"
                )
                _sgm_t_start = _sgm_time_mod.time()
                resp = hb.place_sgm_bet(
                    session_id=sid, sport=tip.sport, event=event_for_hb,
                    legs=session_hb_legs, stake=step_stake, target_odds=target_odds,
                )
                _sgm_elapsed = round(_sgm_time_mod.time() - _sgm_t_start, 2)
                _sgm_last_elapsed = _sgm_elapsed
                last_resp = resp
                if resp.get("success"):
                    try:
                        actual_stake = float(resp.get("stake", step_stake))
                    except (TypeError, ValueError):
                        actual_stake = step_stake
                    if abs(actual_stake - step_stake) > 1.0:
                        log.warning(
                            f"v4 SGM AUTO-CAP detected on SGM: requested "
                            f"${step_stake:.2f}, bookie accepted ${actual_stake:.2f} "
                            f"({bookie}:{sid})"
                        )
                    result = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bookie,
                        bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                        stake=actual_stake, timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip),
                        elapsed_sec=_sgm_elapsed,
                    )
                    log.info(
                        f"v4 SGM placed: {result.bet_id} on {bookie} @ {result.odds}"
                    )
                    if not _orchestrated:
                        notifier.notify_bet_placed(result)
                    if _accumulate_and_check_done(result):
                        _emit_sgm_aux_alerts()
                        return placed_results
                    success_on_session = True
                    break
                last_error = str(resp.get("error", "") or "")

            err_lower = last_error.lower()
            log.warning(
                f"v4 SGM fail on {bookie}:{sid} ${step_stake:.2f}: {last_error[:120]}"
            )

            # SLOW REJECTION detection — Erasmus class. Fires BEFORE any
            # downstream alt-line / blocklist handling. If the bet was
            # actually placed at the bookie but reported as failed, we
            # must NOT alt-line retry, NOT ladder down, NOT spill to
            # another session. Debit + blocklist + break ladder + fire
            # critical alert. Same shape as singles v4 + racing_placer.
            # _sgm_last_elapsed reflects the most recent place_sgm_bet
            # call on this step (boost, no-boost retry, or no-boost
            # initial — whichever produced last_error).
            _sgm_slow = _sgm_last_elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
            if (
                (
                    _sgm_slow
                    or bool((last_resp or {}).get("ambiguous"))  # C5: fast ambiguous
                )
                and not _is_definitely_pre_placement(last_error)
            ):
                # Fix B (2026-06-01, GATED reconciliation, Tier-1 only): check
                # /api/pending_bets before the blind debit. RECONCILE_AMBIGUOUS
                # off (default) -> 'conservative' -> the existing debit+blocklist
                # below runs UNCHANGED. 'placed' debits the ACTUAL stake. SGM
                # Tier-2 SPILL (recover on another bookie) is NOT wired —
                # cross-bookie SGM recovery is non-trivial — so spill_enabled=
                # False here regardless of the global flag.
                import reconcile as _recon_mod
                import time as _t_sgm
                _sgm_acct = next(
                    (s.get("account_id") for s in sessions
                     if str(s.get("session_id", "")) == sid), None)
                _sgm_recon = _recon_mod.decide_ambiguous(
                    hb, _sgm_acct, event=event_for_hb, stake=step_stake,
                    sport=tip.sport, selection="",
                    submit_ts=_t_sgm.time() - _sgm_last_elapsed,
                    reconcile_enabled=RECONCILE_AMBIGUOUS, spill_enabled=False,
                )
                # v5.69 (M1): a reconcile-CONFIRMED placed SGM must flow through
                # the normal success accumulator (placed accounting / BET PLACED
                # summary / ledger), NOT the ambiguous bucket (which fires a
                # contradictory "MAY have placed" CRITICAL with no ledger row).
                # Mirrors the v5.55 fan-out fix; event_for_hb (M3) is the
                # bookie-aliased name so pending_bets actually matches.
                if _sgm_recon["action"] == "placed":
                    try:
                        _sgm_actual = float(_sgm_recon.get("actual_stake", step_stake) or step_stake)
                    except (TypeError, ValueError):
                        _sgm_actual = step_stake
                    _sgm_match = _sgm_recon.get("match") or {}
                    _conf_result = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bookie,
                        bet_id=_sgm_match.get("bookie_bet_id") or _sgm_match.get("id"),
                        odds=_sgm_match.get("odds") or (last_resp or {}).get("odds"),
                        stake=_sgm_actual, timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip),
                        elapsed_sec=_sgm_last_elapsed,
                    )
                    try:
                        _conf_result._requested_stake = _sgm_actual
                        _conf_result._reconcile_confirmed_placed = True
                    except Exception:
                        pass
                    log.warning(
                        f"v4 SGM: reconcile CONFIRMED placed {bookie}:{sid} "
                        f"actual=${_sgm_actual:.2f} bet_id={_conf_result.bet_id} "
                        f"— recording as PLACED (not ambiguous)"
                    )
                    if not _orchestrated:
                        notifier.notify_bet_placed(_conf_result)
                    if _accumulate_and_check_done(_conf_result):
                        _emit_sgm_aux_alerts()
                        return placed_results
                    success_on_session = True
                    break
                _sgm_debit = step_stake
                _sgm_amb_reason = "slow_rejection" if _sgm_slow else "fast_ambiguous"
                log.error(
                    f"v4 SGM: AMBIGUOUS OUTCOME ({_sgm_amb_reason.replace('_', ' ')}) "
                    f"{bookie}:{sid} stake=${_sgm_debit:.2f} "
                    f"elapsed={_sgm_last_elapsed:.1f}s "
                    f"(threshold={STAKE_REJECT_LATENCY_THRESHOLD_SEC}s) "
                    f"err='{last_error[:80]}'. Debiting as placed, "
                    f"blocklisting bookie, stopping ladder."
                )
                ambiguous_outcomes.append({
                    "bookie": bookie,
                    "session_id": sid,
                    "stake": round(_sgm_debit, 2),
                    "odds": (last_resp or {}).get("odds") or sizing_odds or 0,
                    "elapsed_sec": round(_sgm_last_elapsed, 2),
                    "error": last_error[:200],
                    "reason": _sgm_amb_reason,
                })
                # Debit remaining + blocklist all sessions on this bookie.
                remaining_stake -= _sgm_debit
                for s in sessions:
                    if (s.get("bookie", "") or "") == bookie:
                        used_session_ids.add(str(s.get("session_id", "")))
                break

            # NOTE: AFL player-prop line/market encoding (the "N+ == over
            # N-0.5" threshold-ladder fix) is now done UP FRONT in
            # _enrich_sgm_legs_with_prop_ids via the live catalog, so each leg
            # is placed at the exact line/market/prop_id Sportsbet carries.
            # No in-ladder line-move retry here (an earlier version mutated
            # session_hb_legs in place across stake rungs without restore — a
            # real bug; the catalog rewrite removes the need entirely).

            # ── SGM handicap alt-line retry ──────────────────────────
            # If a line leg failed with "did not match", try the standard
            # alt-line ladder before declaring the bookie fatal. Mirrors the
            # singles Path A.5 logic. Only one shot — at the current stake
            # only — to avoid combinatorial blowup with the stake ladder.
            # Same alt order as singles: better first (line+0.5, +1.0), then
            # worse (line-0.5, -1.0). Touches the line value in
            # session_hb_legs in-place; restored after the loop on failure.
            if (
                "did not match" in err_lower
                and ("line=" in err_lower or "leg" in err_lower)
            ):
                line_leg_indices = [
                    i for i, leg in enumerate(session_hb_legs)
                    if leg.get("market") in ("line", "first_half_line")
                    and leg.get("line") is not None
                ]
                # Only attempt alt-line retry when exactly one line leg
                # exists. With multiple, we don't know from the error which
                # one HyperBot rejected — better to stay manual than guess.
                if len(line_leg_indices) == 1:
                    leg_idx = line_leg_indices[0]
                    saved_line = session_hb_legs[leg_idx]["line"]
                    try:
                        base_line = float(saved_line)
                    except (TypeError, ValueError):
                        base_line = None
                    if base_line is not None:
                        log.info(
                            f"v4 SGM HC alt-line retry: leg {leg_idx} "
                            f"line={base_line} on {bookie}:{sid}"
                        )
                        alt_offsets = [0.5, 1.0, -0.5, -1.0]
                        alt_success = False
                        _sgm_alt_ambiguous = False  # C3: slow alt may have landed
                        for offset in alt_offsets:
                            alt = round((base_line + offset) * 2) / 2
                            if alt == base_line:
                                continue
                            session_hb_legs[leg_idx]["line"] = alt
                            log.info(
                                f"v4 SGM alt try: leg {leg_idx} {base_line} -> "
                                f"{alt} (offset {offset:+.1f}) at "
                                f"${step_stake:.2f}"
                            )
                            _alt_t_start = _sgm_time_mod.time()
                            alt_resp = hb.place_sgm_bet(
                                session_id=sid, sport=tip.sport,
                                event=event_for_hb, legs=session_hb_legs,
                                stake=step_stake, target_odds=target_odds,
                                use_boost=False,
                            )
                            if alt_resp.get("success"):
                                # Time only THIS alt attempt (per-attempt reset),
                                # not since the initial place call.
                                _alt_elapsed = round(
                                    _sgm_time_mod.time() - _alt_t_start, 2
                                )
                                # Auto-cap detection (mirror the H4 branches):
                                # read the actual placed stake so a silent
                                # bookie cap debits the real amount and spillover
                                # continues, rather than over-counting step_stake.
                                try:
                                    _alt_actual = float(alt_resp.get("stake", step_stake))
                                except (TypeError, ValueError):
                                    _alt_actual = step_stake
                                if _alt_actual <= 0:
                                    _alt_actual = step_stake
                                if abs(_alt_actual - step_stake) > 1.0:
                                    log.warning(
                                        f"v4 SGM AUTO-CAP (HC alt-line) on "
                                        f"{bookie}:{sid}: requested=${step_stake:.2f} "
                                        f"actual=${_alt_actual:.2f}"
                                    )
                                result = BetResult(
                                    success=True, tip=tip, session_id=sid,
                                    bookie=bookie,
                                    bet_id=alt_resp.get("bet_id"),
                                    odds=alt_resp.get("odds"),
                                    stake=_alt_actual,
                                    timestamp=datetime.now(),
                                    placed_leg_summary=_format_tip_placement_summary(tip),
                                    used_boost=False,
                                    elapsed_sec=_alt_elapsed,
                                )
                                log.info(
                                    f"v4 SGM placed (HC alt-line {alt}): "
                                    f"{result.bet_id} on {bookie} @ "
                                    f"{result.odds}"
                                )
                                if not _orchestrated:
                                    notifier.notify_bet_placed(result)
                                if _accumulate_and_check_done(result):
                                    _emit_sgm_aux_alerts()
                                    # Restore line before returning so any
                                    # caller-side state stays sane.
                                    session_hb_legs[leg_idx]["line"] = saved_line
                                    return placed_results
                                # Partial fill — restore line, mark
                                # success_on_session, break out so the
                                # outer loop spills to the next session.
                                session_hb_legs[leg_idx]["line"] = saved_line
                                success_on_session = True
                                break
                            alt_err = str(alt_resp.get("error", "") or "")
                            alt_err_lower = alt_err.lower()
                            # C3 (2026-05-31): the pre-loop slow-rejection guard
                            # only covered the INITIAL place. A slow alt attempt
                            # (>=5s, or hyperbot-flagged ambiguous) that failed may
                            # have actually landed at the bookie — re-run the
                            # Erasmus guard PER alt attempt (timed from this attempt's
                            # start). Treat as AMBIGUOUS: debit + blocklist + abort,
                            # never spill/ladder. Mirrors the initial-place guard
                            # above. A slow "did not match" / "line moved" is
                            # definitely pre-placement, so it correctly walks on.
                            _alt_fail_elapsed = round(
                                _sgm_time_mod.time() - _alt_t_start, 2
                            )
                            _alt_slow = _alt_fail_elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
                            if (
                                (_alt_slow or bool(alt_resp.get("ambiguous")))
                                and not _is_definitely_pre_placement(alt_err)
                            ):
                                _alt_amb_reason = "slow_rejection" if _alt_slow else "fast_ambiguous"
                                log.error(
                                    f"v4 SGM: AMBIGUOUS OUTCOME (alt-line "
                                    f"{_alt_amb_reason.replace('_', ' ')}) {bookie}:{sid} "
                                    f"stake=${step_stake:.2f} "
                                    f"elapsed={_alt_fail_elapsed:.1f}s "
                                    f"(threshold={STAKE_REJECT_LATENCY_THRESHOLD_SEC}s) "
                                    f"err='{alt_err[:80]}'. Debiting as placed, "
                                    f"blocklisting bookie, stopping ladder."
                                )
                                ambiguous_outcomes.append({
                                    "bookie": bookie,
                                    "session_id": sid,
                                    "stake": round(step_stake, 2),
                                    "odds": (alt_resp or {}).get("odds") or sizing_odds or 0,
                                    "elapsed_sec": _alt_fail_elapsed,
                                    "error": alt_err[:200],
                                    "reason": _alt_amb_reason,
                                })
                                remaining_stake -= step_stake
                                for s in sessions:
                                    if (s.get("bookie", "") or "") == bookie:
                                        used_session_ids.add(str(s.get("session_id", "")))
                                _sgm_alt_ambiguous = True
                                break
                            # Different error class: stop alt walk and treat
                            # the new error as the canonical failure for
                            # downstream stake/blocklist handling.
                            if "did not match" not in alt_err_lower:
                                last_error = alt_err
                                err_lower = alt_err_lower
                                break
                        # Restore original line for any further session
                        # attempts; bookie blocklist applies regardless.
                        session_hb_legs[leg_idx]["line"] = saved_line
                        # If alt-line placed (partial spillover case), we
                        # already broke the alt-offset loop. Need to also
                        # exit the step_stake ladder so the outer session
                        # loop can spill to the next priority session.
                        if success_on_session:
                            break
                        # C3: a slow alt attempt was flagged AMBIGUOUS above —
                        # the stake was debited + bookie blocklisted; exit the
                        # step_stake ladder so we don't ladder/spill a maybe-
                        # placed bet.
                        if _sgm_alt_ambiguous:
                            break

            # Capture stake-too-high rejections + MBL violations. Same logic
            # as singles v4 — see _place_singles_v4 for the rationale.
            # `sizing_odds` is the odds we used for liability sizing on this
            # session. cap_reason "no-cap"/"no-odds" means we didn't apply a
            # cap so MBL can't be detected (no ground truth).
            if _is_stake_error(last_error):
                ladder_attempts.append({
                    "bookie": bookie,
                    "session_id": sid,
                    "stake_rejected": round(step_stake, 2),
                    "error": last_error[:120],
                })
                cap_reason_lower = (cap_reason or "").lower()
                cap_known = not cap_reason_lower.startswith(("no-cap", "no-odds"))
                if (
                    cap_known
                    and step_stake <= max_stake
                    and any(
                        kw in err_lower
                        for kw in ("limit", "max", "exceed", "too high", "restricted")
                    )
                ):
                    log.error(
                        f"v4 SGM: stake reject below our cap (bookie max-bet limit) "
                        f"{bookie}:{sid} rejected "
                        f"${step_stake:.2f} but cap allows ${max_stake:.2f} "
                        f"({cap_reason})"
                    )
                    mbl_violations.append({
                        "bookie": bookie,
                        "session_id": sid,
                        "stake_tried": round(step_stake, 2),
                        "mbl_max": round(max_stake, 2),
                        "liability_cap": round(
                            max_stake * (sizing_odds - 1) if sizing_odds else 0, 2
                        ),
                        "odds": sizing_odds,
                        "error": last_error[:200],
                        "market": "sgm",
                    })

            # Stake error: continue ladder
            if _is_stake_error(last_error):
                continue
            # Same-bookie fatal: blocklist the bookie for this tip
            if _is_same_bookie_fatal(last_error):
                log.warning(
                    f"v4 SGM same-bookie fatal on {bookie} ({last_error[:120]}); "
                    f"abandoning this session and any other on same bookie"
                )
                # Mark all sessions on this bookie as used to skip them
                for s in sessions:
                    if (s.get("bookie", "") or "") == bookie:
                        used_session_ids.add(str(s.get("session_id", "")))
                break
            # Other error: abandon this session
            log.warning(f"v4 SGM non-stake error on {sid}, abandoning ladder")
            break

        used_session_ids.add(sid)
        if not success_on_session:
            log.info(f"v4 SGM: no placement on session {sid}")

    # End of priority loop. Three terminal states:
    #  1. placed_results non-empty AND remaining_stake essentially zero
    #     -> all-good return (already handled inside the loop on full fill,
    #     but reach here if the final placement exactly emptied remaining).
    #  2. placed_results non-empty but partial fill -> still good, return
    #     what we got. The unfilled remainder is a partial fill, not a
    #     failure — Wilson's Holmgren scenario (account-limited spillover).
    #  3. placed_results empty -> manual alert as before.
    if placed_results:
        log.info(
            f"v4 SGM: completed with {len(placed_results)} placement(s), "
            f"remaining ${remaining_stake:.2f} unfilled"
        )
        # NOTE: the consolidated BET-PLACED summary is emitted by
        # _emit_sgm_aux_alerts (the universal terminal hook) so it also covers
        # the early full-fill returns; it is NOT emitted here.
        # Partial-fill alert: some stake placed but remainder could not be
        # filled across all priority sessions. Mirrors _place_singles_v4's
        # notify_tip_unfilled_with_placements path.
        #
        # MLB EXCEPTION (Wilson 2026-06-01): MLB is a PER-ACCOUNT model — each
        # account independently takes its ~$87 ladder; there is no per-play
        # "total" to fill, so a leftover after every account has placed its rung
        # is EXPECTED, not a partial failure. Suppress the unfilled→manual alert
        # for MLB (the per-account placements already returned in placed_results).
        if remaining_stake > STAKE_FLOOR and (tip.sport or "").lower() != "mlb":
            total_placed_sgm = sum(r.stake or 0 for r in placed_results)
            unfilled_sgm = round(remaining_stake, 2)
            log.warning(
                f"v4 SGM: partial fill — ${total_placed_sgm:.2f} of "
                f"${intended_stake:.2f} placed, ${unfilled_sgm:.2f} unfilled"
            )
            failed_sgm = [BetResult(
                success=False, tip=tip, error=f"SGM partial fill: {last_error}",
                timestamp=datetime.now(),
                placed_leg_summary=_format_tip_placement_summary(tip),
            )]
            notifier.notify_tip_unfilled_with_placements(
                tip, intended_stake, total_placed_sgm, unfilled_sgm,
                placed_results, failed_sgm,
            )
        elif remaining_stake > STAKE_FLOOR:
            # MLB per-account: log only, no manual alert.
            log.info(
                f"v4 SGM (mlb per-account): placed on {len(placed_results)} "
                f"account(s) at their ~$87 ladder; ${remaining_stake:.2f} of the "
                f"${intended_stake:.2f} intended left unplaced (expected — no "
                f"more MLB SGM accounts). No unfilled alert."
            )
        _emit_sgm_aux_alerts()
        return placed_results

    # All sessions exhausted — alert manual (unless an orchestrator owns
    # final alerting, e.g. the MLB HRRBI orchestrator which still has Alex's
    # single to try before deciding the one manual notification).
    log.warning("v4 SGM: auto-placement failed on all priority sessions")
    if not _orchestrated:
        tip.alert_reason = f"SGM auto-place failed: {last_error}"
        notifier.notify_manual_alert(tip)
    _emit_sgm_aux_alerts()
    return [BetResult(
        success=False, tip=tip, error=f"SGM failed: {last_error}",
        timestamp=datetime.now(),
        placed_leg_summary=_format_tip_placement_summary(tip),
    )]


def _sgm_fanout_place_account(tip, sess: dict, ladder: list,
                              session_hb_legs: list, target_odds) -> BetResult:
    """Place ONE account in the concurrent SGM fan-out, walking its liability-
    derived stake ladder via hb.place_sgm_bet: top rung first; on a stake-too-high
    reject drop to the next bracket; STOP on the first success, on a non-stake
    error, or on an AMBIGUOUS (maybe-landed) outcome — never ladder past an
    ambiguous (that could double-stake). Mirrors _fanout_place_account (the
    singles worker) but for the multi-leg SGM payload. NO boost path: every SGM
    account is boost_eligible:false and SGM_BOOST_SESSIONS is empty, so a boost
    branch would add risk for no gain. Returns the terminal BetResult; the rung
    requested is stashed as _requested_stake for the at-risk/ambiguous reporting."""
    import time as _t
    sid = str(sess.get("session_id", ""))
    bk = sess.get("bookie", "unknown")
    event_for_hb = _bookie_event(tip.event, bk, tip.sport)
    last: BetResult | None = None
    _did_sgm_max_rebet = False  # v5.9x: at most one SB max-stake rebet per account
    for i, step in enumerate(ladder):
        _t0 = _t.time()
        resp = hb.place_sgm_bet(
            session_id=sid, sport=tip.sport, event=event_for_hb,
            legs=session_hb_legs, stake=step, target_odds=target_odds,
        )
        _el = round(_t.time() - _t0, 2)
        if resp.get("success"):
            try:
                actual = float(resp.get("stake", step))
            except (TypeError, ValueError):
                actual = step
            if actual <= 0:
                actual = step
            if abs(actual - step) > 1.0:
                log.warning(
                    f"SGM fan-out AUTO-CAP on {bk}:{sid}: requested=${step:.2f} "
                    f"actual=${actual:.2f}")
            r = BetResult(
                success=True, tip=tip, session_id=sid, bookie=bk,
                bet_id=resp.get("bet_id"), odds=resp.get("odds"), stake=actual,
                timestamp=datetime.now(), elapsed_sec=_el,
                placed_leg_summary=_format_tip_placement_summary(tip))
            try:
                r._requested_stake = step
            except Exception:
                pass
            if i > 0:
                log.info(f"SGM fan-out: {bk}:{sid} laddered to rung {i + 1} "
                         f"(${step:.2f}) after {i} stake-reject(s)")
            return r
        err = str(resp.get("error", "") or "")
        # Erasmus: a slow (>=threshold) or hyperbot-ambiguous failure may have
        # LANDED at the bookie -> stop, flag ambiguous, NEVER ladder/re-bet (a
        # lower rung after a maybe-landed top rung = double-stake). A definitely-
        # pre-placement error ("did not match"/"line moved") walks on. v5.53:
        # both fan-out workers now run the Tier-1 /api/pending_bets confirm/deny
        # (_reconcile_fanout_ambiguous) — the ladder STAYS stopped either way;
        # only the accounting (actual stake vs no-debit) + alert severity change.
        _slow = _el >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
        if (_slow or bool(resp.get("ambiguous"))) and not _is_definitely_pre_placement(err):
            r = BetResult(
                success=False, tip=tip, session_id=sid, bookie=bk,
                error=f"ambiguous ({'slow_rejection' if _slow else 'fast_ambiguous'}): "
                      f"{err[:120]}",
                is_ambiguous=True, stake=step, elapsed_sec=_el,
                # v6.07 (Batch A audit): set the STRUCTURAL flag on the SGM fan-out
                # too. Guard B must not depend on the cid markers surviving inside
                # this composed/truncated error string (err[:120]) — one HB rewording
                # or a tighter slice would silently re-open the double-stake hole.
                cid_unresolved=bool(resp.get("cid_unresolved", False)),
                timestamp=datetime.now(),
                placed_leg_summary=_format_tip_placement_summary(tip))
            try:
                r._requested_stake = step
            except Exception:
                pass
            log.warning(f"SGM fan-out: {bk}:{sid} AMBIGUOUS on ${step:.2f} "
                        f"(elapsed {_el:.1f}s) — stopping ladder (may have landed)")
            return _reconcile_fanout_ambiguous(
                tip, sess, r, step, f"SGM fan-out {bk}:{sid}")
        last = BetResult(success=False, tip=tip, session_id=sid, bookie=bk,
                         error=err, elapsed_sec=_el, timestamp=datetime.now())
        try:
            last._requested_stake = step
        except Exception:
            pass
        if not _is_stake_error(err):
            # v5.68 (Wilson): ONE retry on a TRANSIENT pre-placement reject (proxy
            # 403 / auth / network) — bet never submitted, zero double-stake risk.
            # Re-issue the SAME rung once + re-classify (success / ambiguous /
            # stake-reject->ladder-down / else abandon). Mirrors the AFL fan-out.
            if AFL_FANOUT_PREPLACEMENT_RETRY and _is_definitely_pre_placement(err):
                log.info(f"SGM fan-out: {bk}:{sid} transient pre-placement reject on "
                         f"${step:.2f} ({err[:60]}) — retrying SAME rung once")
                _t.sleep(AFL_FANOUT_RETRY_DELAY_SEC)
                _t0 = _t.time()
                resp = hb.place_sgm_bet(
                    session_id=sid, sport=tip.sport, event=event_for_hb,
                    legs=session_hb_legs, stake=step, target_odds=target_odds,
                )
                _el = round(_t.time() - _t0, 2)
                if resp.get("success"):
                    try:
                        actual = float(resp.get("stake", step))
                    except (TypeError, ValueError):
                        actual = step
                    if actual <= 0:
                        actual = step
                    r = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bk,
                        bet_id=resp.get("bet_id"), odds=resp.get("odds"), stake=actual,
                        timestamp=datetime.now(), elapsed_sec=_el,
                        placed_leg_summary=_format_tip_placement_summary(tip))
                    try:
                        r._requested_stake = step
                    except Exception:
                        pass
                    log.info(f"SGM fan-out: {bk}:{sid} retry PLACED ${step:.2f} "
                             f"(first attempt was a transient pre-placement reject)")
                    return r
                err = str(resp.get("error", "") or "")
                _slow = _el >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
                if (_slow or bool(resp.get("ambiguous"))) and not _is_definitely_pre_placement(err):
                    r = BetResult(
                        success=False, tip=tip, session_id=sid, bookie=bk,
                        error=f"ambiguous (retry): {err[:120]}", is_ambiguous=True,
                        # v6.07 (Batch A audit): structural flag, not text-dependent
                        cid_unresolved=bool(resp.get("cid_unresolved", False)),
                        stake=step, elapsed_sec=_el, timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip))
                    try:
                        r._requested_stake = step
                    except Exception:
                        pass
                    return _reconcile_fanout_ambiguous(
                        tip, sess, r, step, f"SGM fan-out {bk}:{sid} (retry)")
                last = BetResult(success=False, tip=tip, session_id=sid, bookie=bk,
                                 error=err, elapsed_sec=_el, timestamp=datetime.now())
                try:
                    last._requested_stake = step
                except Exception:
                    pass
                if _is_stake_error(err):
                    log.info(f"SGM fan-out: {bk}:{sid} retry hit stake-reject "
                             f"${step:.2f}, laddering down")
                    continue
                # retry ALSO a non-stake error -> fall through to abandon.
            log.info(f"SGM fan-out: {bk}:{sid} non-stake error on ${step:.2f} — "
                     f"abandoning ladder: {err[:80]}")
            return last
        # v5.9x (#7 Part A) SGM MAX-STAKE REBET: on a FAST Sportsbet stake-too-high
        # (538) the bookie returns the allowable max. Rebet ONCE at that max (one
        # shot/account) instead of laddering down blindly. SB-only, bounded strictly
        # below `step` (can't over-stake). A SLOW / maybe-landed 538 was already
        # diverted to the ambiguous branch above (never reaches here), so this is a
        # clean fast reject. On failure -> the normal ladder-down backstop.
        # Kill-switch SPORTSBET_MAX_STAKE_REBET. Mirrors the reviewed _execute_bet
        # singles rebet (the SGM path has no _execute_bet, so it lives here).
        if not _did_sgm_max_rebet:
            _mx = _sb_max_stake_target(resp, bk, step)
            if _mx is not None:
                _did_sgm_max_rebet = True
                log.info(f"SGM fan-out: {bk}:{sid} stake-reject ${step:.2f} — SB "
                         f"stated max ${_mx:.2f}; rebetting ONCE at max")
                _t0 = _t.time()
                resp = hb.place_sgm_bet(
                    session_id=sid, sport=tip.sport, event=event_for_hb,
                    legs=session_hb_legs, stake=_mx, target_odds=target_odds,
                )
                _el = round(_t.time() - _t0, 2)
                if resp.get("success"):
                    try:
                        actual = float(resp.get("stake", _mx))
                    except (TypeError, ValueError):
                        actual = _mx
                    if actual <= 0:
                        actual = _mx
                    r = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bk,
                        bet_id=resp.get("bet_id"), odds=resp.get("odds"), stake=actual,
                        timestamp=datetime.now(), elapsed_sec=_el,
                        placed_leg_summary=_format_tip_placement_summary(tip))
                    try:
                        r._requested_stake = _mx
                    except Exception:
                        pass
                    log.info(f"SGM fan-out: {bk}:{sid} PLACED at SB max ${_mx:.2f}")
                    return r
                _err2 = str(resp.get("error", "") or "")
                if ((_el >= STAKE_REJECT_LATENCY_THRESHOLD_SEC or bool(resp.get("ambiguous")))
                        and not _is_definitely_pre_placement(_err2)):
                    r = BetResult(
                        success=False, tip=tip, session_id=sid, bookie=bk,
                        error=f"ambiguous (max-stake rebet): {_err2[:120]}",
                        is_ambiguous=True, stake=_mx, elapsed_sec=_el,
                        # v6.07 (Batch A audit): structural flag, not text-dependent
                        cid_unresolved=bool(resp.get("cid_unresolved", False)),
                        timestamp=datetime.now(),
                        placed_leg_summary=_format_tip_placement_summary(tip))
                    try:
                        r._requested_stake = _mx
                    except Exception:
                        pass
                    return _reconcile_fanout_ambiguous(
                        tip, sess, r, _mx, f"SGM fan-out {bk}:{sid} (max-stake)")
                last = BetResult(success=False, tip=tip, session_id=sid, bookie=bk,
                                 error=_err2, elapsed_sec=_el, timestamp=datetime.now())
                try:
                    last._requested_stake = _mx
                except Exception:
                    pass
                # 07-12 review C3: mirror the pre-rebet invariant — a NON-stake
                # error on the rebet abandons the ladder (a lower rung won't fix a
                # market/auth/price error); only a stake error ladders down.
                if not _is_stake_error(_err2):
                    log.info(f"SGM fan-out: {bk}:{sid} rebet non-stake error — "
                             f"abandoning ladder: {_err2[:80]}")
                    return last
                log.info(f"SGM fan-out: {bk}:{sid} rebet at SB max ${_mx:.2f} still "
                         f"failed ({_err2[:60]}) — laddering down")
        log.info(f"SGM fan-out: {bk}:{sid} stake-reject ${step:.2f}, laddering down")
    return last if last is not None else BetResult(
        success=False, tip=tip, session_id=sid, bookie=bk,
        error="SGM fan-out: empty ladder", timestamp=datetime.now())


def _place_sgm_fanout(tip: ParsedTip, _orchestrated: bool = False) -> list[BetResult]:
    """v5.38 CONCURRENT SGM fan-out (Saiyan AFL + Shook MLB HRRBI) — Wilson.

    Even-splits the intended unit across the SGM-capable accounts, then places ALL
    accounts CONCURRENTLY (ThreadPoolExecutor), each capped by its yaml `sgm`
    LIABILITY ladder (afl.sgm [400,300,200] / mlb.sgm [130,100,87]) sized off the
    ESTIMATED combined SGM odds (= PRODUCT of the per-leg catalog odds, since an
    SGM has no pre-placement price), each laddering DOWN a bracket on a stake-too-
    high reject in its own thread. The even-split's liability lands below the top
    bracket, so the cap normally doesn't bind — it only ladders down on a reject
    or caps a long-odds SGM. Mirrors _place_afl_fanout (the singles fan-out).

    `_orchestrated=True` (MLB, from _place_mlb_hrrbi): return the placement
    results WITHOUT emitting the consolidated summary / unfilled->manual (the MLB
    orchestrator owns those + adds Alex as the single-account backstop). The
    AMBIGUOUS (maybe-landed) critical alert still fires (independent of the
    summary). Non-orchestrated (AFL): emit the one consolidated summary +
    unfilled->manual + ambiguous, exactly like _place_afl_fanout.

    FALLBACK: on ANY non-happy-path (team+total SGM, no priority list, no active/
    eligible sessions, legs not carried on any bookie, or no usable combined-odds
    estimate) it DELEGATES to the sequential _place_sgm_v4(tip, _orchestrated) —
    the proven path that owns the manual-alert routing AND now sizes the same
    liability lists off est_odds too. So the fan-out only takes over the happy
    path; everything else is byte-identical to pre-v5.38."""
    import time as _time_mod
    import concurrent.futures
    sport = (tip.sport or "nba").lower()
    intended_stake = tip.stake_dollars

    def _fallback(reason: str) -> list[BetResult]:
        log.info(f"SGM fan-out: delegating to sequential _place_sgm_v4 ({reason})")
        return _place_sgm_v4(tip, _orchestrated=_orchestrated)

    # ── Gating (mirrors _place_sgm_v4; delegate on any miss) ───────────
    if _is_team_plus_total_sgm(tip):
        return _fallback("team ML/handicap + total SGM — manual rule")
    if not session_priority.get_priority_for(sport, is_sgm=True):
        return _fallback(f"no {sport} SGM priority list")
    raw_sessions = _v4_get_active_sessions_unfiltered(tip)
    if not raw_sessions:
        return _fallback("no active sessions after sport filter")
    sessions = session_priority.filter_and_order_sessions(raw_sessions, sport, is_sgm=True)
    sgm_blacklist_env = os.getenv("SGM_BLACKLIST_SESSIONS", "").strip()
    if sgm_blacklist_env:
        _blk = {s.strip() for s in sgm_blacklist_env.split(",") if s.strip()}
        sessions = [s for s in sessions if str(s.get("session_id")) not in _blk]
    if not sessions:
        return _fallback("no priority/eligible SGM sessions")
    # De-dup by session_id BEFORE the split (a duplicated priority entry would
    # inflate n_accounts + double-POST the same account).
    _seen_pre: set[str] = set()
    _ded: list[dict] = []
    for s in sessions:
        _sid = str(s.get("session_id", ""))
        if _sid and _sid in _seen_pre:
            log.warning(f"SGM fan-out: duplicate session {_sid} in priority — de-duped")
            continue
        _seen_pre.add(_sid)
        _ded.append(s)
    sessions = _ded

    # ── Build the bookmaker-agnostic legs once (mirror _place_sgm_v4) ──
    hb_legs: list[dict] = []
    for leg in tip.legs:
        leg_is_threshold = getattr(leg, "_is_threshold", False)
        if tip.is_pyo_sgm and leg_is_threshold:
            leg_is_threshold = False
            if leg.line and leg.line == int(leg.line):
                leg.line = float(leg.line) - 0.5
            if not leg.selection or leg.selection in ("", "over"):
                leg.selection = "over"
        resolved = _resolve_leg_for_hyperbot(
            leg, tip.sport, is_threshold=leg_is_threshold, for_sgm=True,
            tipster=tip.tipster,
            # v5.87: AFL SGM legs are same-game — scope the surname to BOTH event
            # teams + forbid the cross-game global roster fallback (the BUG A fix,
            # extended from singles to SGM legs). Non-AFL SGM (MLB/NBA) -> None.
            event_teams=(_afl_event_teams(tip.event)
                         if (tip.sport or "").lower() == "afl" else None),
        )
        hb_leg = {"market": resolved["market"], "selection": resolved["selection"]}
        if resolved["player"]:
            hb_leg["player"] = resolved["player"]
        if resolved["stat"]:
            hb_leg["stat"] = resolved["stat"]
        if resolved["line"] is not None:
            hb_leg["line"] = resolved["line"]
        hb_legs.append(hb_leg)

    # Target odds: 90% of suggested, floored at 1.01 — usually None for SGMs
    # (saiyan/Shook quote no combined price). est_odds is for SIZING only, NOT a
    # price floor (an SGM fills at any price when target_odds is None).
    target_odds = None
    if tip.suggested_odds and tip.suggested_odds > 1.0:
        target_odds = _afl_target_odds(tip.sport, tip.suggested_odds)

    _t_start = _time_mod.time()
    # ── Resolve ONCE per bookie: enrich legs + estimate combined odds ──
    legs_by_bookie: dict[str, list | None] = {}
    est_by_bookie: dict[str, float | None] = {}
    _pc_t0 = _time_mod.time()
    for sess in sessions:
        bk = (sess.get("bookie", "") or "").lower()
        if bk in legs_by_bookie:
            continue
        _leg_odds: list = []
        enriched, enrich_err = _enrich_sgm_legs_with_prop_ids(
            hb_legs, tip, str(sess.get("session_id", "")), bookie=bk, odds_out=_leg_odds,
        )
        if enrich_err:
            log.info(f"SGM fan-out: enrich failed on {bk}: {enrich_err}")
            legs_by_bookie[bk] = None
            continue
        legs_by_bookie[bk] = enriched
        est_by_bookie[bk] = _sgm_est_combined_odds(_leg_odds)
    _tm = getattr(tip, "_timing", None)
    if isinstance(_tm, dict):
        _tm["price_check_sec"] = round(_time_mod.time() - _pc_t0, 2)

    if not any(legs_by_bookie.values()):
        return _fallback("SGM legs not carried on any eligible bookie")
    if not any(est_by_bookie.get(bk) for bk, lg in legs_by_bookie.items() if lg):
        return _fallback("no usable combined-odds estimate (a leg lacked catalog odds)")

    # ── Size each account: even-split, capped by the liability ladder ──
    # v5.69 (m5): split over only the sessions that can ACTUALLY place these
    # legs (their bookie resolved both legs and an est combined odds). Dividing
    # by ALL priority sessions and then skipping the unplaceable ones left
    # (n-k)/n of the unit permanently unfilled -> manual, even when the k
    # working accounts had spare cap. The per-account budget cap below still
    # prevents any over-allocation.
    _placeable_sessions = []
    _seen_split: set[str] = set()
    for sess in sessions:
        _psid = str(sess.get("session_id", ""))
        if _psid in _seen_split:
            continue
        _seen_split.add(_psid)
        _pbk = (sess.get("bookie", "") or "").lower()
        if legs_by_bookie.get(_pbk) is not None and est_by_bookie.get(_pbk):
            _placeable_sessions.append(sess)
    n_accounts = len(_placeable_sessions) or len(sessions)
    per_account_target = round(intended_stake / n_accounts, 2)
    log.info(
        f"SGM fan-out: {n_accounts} placeable session(s) of {len(sessions)}, "
        f"intended ${intended_stake:.2f} -> ${per_account_target:.2f}/account "
        f"(even split over placeable), est_odds={est_by_bookie}"
    )
    jobs: list[tuple[dict, list, list]] = []  # (session, stake-ladder, enriched legs)
    seen_sids: set[str] = set()
    allocated = 0.0
    for sess in sessions:
        sid = str(sess.get("session_id", ""))
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        bk = (sess.get("bookie", "") or "").lower()
        enriched = legs_by_bookie.get(bk)
        est = est_by_bookie.get(bk)
        if enriched is None or not est:
            log.info(f"SGM fan-out: {bk} session {sid} skipped (legs/odds unresolved)")
            continue
        remaining_budget = round(intended_stake - allocated, 2)
        if remaining_budget <= 0:
            log.info(f"SGM fan-out: intended fully allocated — {bk} {sid} not needed")
            continue
        if sport == "mlb":
            # v5.74 (Wilson): MLB HRRBI uses a fixed-% STAKE ladder off the even
            # split ($100 -> 100/90/85/80% = $100/$90/$85/$80), NOT the liability
            # brackets — place the full even-split first, ladder DOWN on a bookie
            # stake-reject. Any unfilled remainder spills to Alex as a single
            # (handled by _place_mlb_hrrbi). Even split = $400 / 4 SGM accounts.
            steps = [round(per_account_target * p, 2) for p in MLB_HRRBI_LADDER_PCT]
            cap_reason = "mlb-hrrbi-pct-ladder"
        else:
            # resolve_stake_steps (list-cap mode) converts each liability bracket
            # to a stake at the est combined odds, capped at the even-split target.
            steps, cap_reason, _ = session_priority.resolve_stake_steps(
                sid, sport, "sgm", est, per_account_target, _v4_ladder_steps,
            )
        steps = [round(min(s, remaining_budget), 2) for s in steps if s and s > 0]
        ladder = [s for s in steps if s >= AFL_FANOUT_MIN_STAKE]
        if not ladder and steps:
            ladder = [round(min(AFL_FANOUT_MIN_STAKE, remaining_budget), 2)]
        _dedup: list = []
        for s in ladder:
            if s > 0 and (not _dedup or _dedup[-1] != s):
                _dedup.append(s)
        ladder = _dedup
        if not ladder:
            log.info(f"SGM fan-out: {bk} {sid} no usable stake ({cap_reason}) — skip")
            continue
        allocated = round(allocated + ladder[0], 2)
        log.info(f"SGM fan-out: {bk} {sid} -> ladder {ladder} ({cap_reason}, est_odds={est})")
        jobs.append((sess, ladder, enriched))

    if not jobs:
        return _fallback("no placeable SGM accounts after sizing")

    # ── Fire all accounts CONCURRENTLY (each ladders in its own thread) ─
    results: list[BetResult] = []
    log.info(f"SGM fan-out: firing {len(jobs)} concurrent SGM placement(s)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {
            ex.submit(_sgm_fanout_place_account, tip, sess, ladder, enriched, target_odds): sess
            for (sess, ladder, enriched) in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            sess = futures[fut]
            sid = str(sess.get("session_id", ""))
            try:
                results.append(fut.result())
            except Exception as e:
                log.error(f"SGM fan-out: placement on {sid} raised: {e}")
                results.append(BetResult(
                    success=False, tip=tip, session_id=sid,
                    bookie=sess.get("bookie", "unknown"),
                    error=f"SGM fan-out placement exception: {e}",
                    timestamp=datetime.now()))

    # ── Roll up: placed / ambiguous / unfilled (mirror _place_afl_fanout) ──
    placed_results = [r for r in results if r.success]
    ambiguous_results = [r for r in results if not r.success and _is_ambiguous_result(r)]
    failed_results = [r for r in results if not r.success and not _is_ambiguous_result(r)]

    top_by_sid = {str(s.get("session_id", "")): ladder[0] for (s, ladder, _e) in jobs}

    def _at_risk_stake(r: BetResult) -> float:
        return round((getattr(r, "_requested_stake", None) or r.stake
                      or top_by_sid.get(str(r.session_id), 0.0) or 0.0), 2)

    attempted_stake = round(sum(top_by_sid.values()), 2)
    total_placed = round(sum(r.stake or 0 for r in placed_results), 2)
    ambiguous_total = round(sum(_at_risk_stake(r) for r in ambiguous_results), 2)
    unfilled = round(max(0.0, intended_stake - total_placed - ambiguous_total), 2)
    display_intended = round(intended_stake, 2)

    session_timing = [{
        "session_id": r.session_id, "bookie": r.bookie,
        "elapsed_sec": getattr(r, "elapsed_sec", None) or 0.0,
        "attempts": 1, "fails": 0 if r.success else 1, "succeeded": r.success,
    } for r in results]
    ambiguous_outcomes = [{
        "bookie": r.bookie, "session_id": r.session_id, "stake": _at_risk_stake(r),
        "odds": (r.odds or 0), "elapsed_sec": round(getattr(r, "elapsed_sec", None) or 0.0, 2),
        "error": (r.error or "")[:200],
        "reason": ("fast_ambiguous" if getattr(r, "is_ambiguous", False) else "slow_rejection"),
        "correlation_id": getattr(r, "correlation_id", None),
    } for r in ambiguous_results]

    # v5.69 (i2): suppress this per-fan-out audit row when ORCHESTRATED (MLB
    # HRRBI) — _place_mlb_hrrbi now writes ONE consolidated tip_outcome covering
    # the SGM accounts AND Alex's single, so the audit trail matches the
    # consolidated summary instead of recording the SGM slice alone.
    if not _orchestrated:
        _log_jsonl(_audit_log_path(), {
            "type": "tip_outcome", "tipster": tip.tipster, "event": tip.event,
            "intended_stake": round(intended_stake, 2), "attempted_stake": attempted_stake,
            "placed_stake": total_placed, "ambiguous_stake": ambiguous_total,
            "unfilled_stake": unfilled, "fanout": "sgm", "sgm": True,
            "orchestrated": _orchestrated, "accounts": len(jobs),
            "placements": [
                {"session_id": r.session_id, "bookie": r.bookie, "stake": r.stake,
                 "fill_odds": r.odds, "bet_id": r.bet_id} for r in placed_results],
            "ambiguous": [
                {"session_id": r.session_id, "bookie": r.bookie, "stake": _at_risk_stake(r),
                 "error": r.error} for r in ambiguous_results],
            "failures": [
                {"session_id": r.session_id, "bookie": r.bookie, "error": r.error}
                for r in failed_results],
        })

    # ORCHESTRATED (MLB): the orchestrator owns the consolidated summary +
    # leftover->manual (it still has Alex's single to place). Only the AMBIGUOUS
    # critical fires here (maybe-landed — independent of fill state). Return.
    if _orchestrated:
        if ambiguous_outcomes:
            _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)
        log.info(
            f"SGM fan-out (orchestrated): placed ${total_placed:.2f} across "
            f"{len(placed_results)}/{len(jobs)} account(s) "
            f"({len(failed_results)} failed, {len(ambiguous_results)} ambiguous)")
        return results

    # NON-ORCHESTRATED (AFL saiyan): the one consolidated summary + unfilled.
    if placed_results:
        notifier.notify_tip_placed_summary(
            tip, placed_results, display_intended, unfilled,
            total_elapsed_sec=round(_time_mod.time() - _t_start, 2),
            session_timing=session_timing,
            concurrent_bookies=True,  # fan-out: bookie wall-clock = MAX not SUM
        )
        log.info(
            f"SGM fan-out: placed ${total_placed:.2f} across "
            f"{len(placed_results)}/{len(jobs)} account(s) "
            f"({len(failed_results)} failed, {len(ambiguous_results)} ambiguous)")

    if failed_results or unfilled > 1.0:
        log.warning(
            f"SGM fan-out: ${unfilled:.2f} unfilled ({len(failed_results)} failed)")
        notifier.notify_tip_unfilled_with_placements(
            tip, display_intended, total_placed, unfilled,
            placed_results, failed_results,
            session_timing=session_timing,
            total_elapsed_sec=round(_time_mod.time() - _t_start, 2),
            concurrent_bookies=True,
        )
        _log_jsonl(ERROR_LOG, {
            "type": "tip_unfilled", "tipster": tip.tipster, "event": tip.event,
            "intended_stake": intended_stake, "attempted_stake": attempted_stake,
            "placed_stake": total_placed, "unfilled_stake": unfilled,
            "last_error": failed_results[-1].error if failed_results else None,
            "message": tip.raw_message, "fanout": "sgm",
        })

    if ambiguous_outcomes:
        _emit_sports_ambiguous_alert(tip, ambiguous_outcomes)

    return results


def _place_mlb_alex_single(tip: ParsedTip, cap_stake: float) -> list[BetResult]:
    """Place the 2+ HRRBI as a SINGLE on the MLB single-only account(s)
    (MLB_HRRBI_SINGLE_SESSIONS — Alex Liu, who can't do multis). Per-account
    STAKE ladder from the yaml `mlb.player_stats` list (e.g. [87,85,80]), capped
    by `cap_stake` (what's left of the per-play intended after the SGM accounts).
    Catalog-resolves the prop_id via _match_mlb_player_prop (incl. the guarded
    fuzzy player match). Erasmus: place_single_sports_bet is max_attempts=1; a
    slow (>=5s) or ambiguous rejection -> stop that account + flag ambiguous
    (gated reconcile, no re-bet); a stake reject -> ladder down; a FAST
    price-change (code=535) reject -> retry ONCE per account on the same rung
    with target_odds dropped (v5.52: slip-validation reject = pre-placement, so
    retry-safe; slow/ambiguous still wins, a second 535 stops the account);
    any other pre-placement reject -> stop (no debit). Returns the BetResults
    (success/ambiguous). The caller (_place_mlb_hrrbi) owns the leftover/manual
    notification."""
    import time as _t
    single_sids = [s.strip() for s in os.getenv("MLB_HRRBI_SINGLE_SESSIONS", "").split(",") if s.strip()]
    # MINOR (Wilson 2026-06-21): a sub-$1 remainder (the $0.01 left after a full
    # 3-way SGM fill) must NOT fire a degenerate bookie-minimum single.
    if not single_sids or cap_stake < MLB_HRRBI_SINGLE_MIN_STAKE:
        return []
    player = (tip.legs[0].player if tip.legs else "") or ""
    if not player.strip():
        return []
    sessions = _v4_get_active_sessions_unfiltered(tip)
    by_id = {str(s.get("session_id", "")): s for s in sessions}
    results: list[BetResult] = []
    remaining = cap_stake
    for sid in single_sids:
        if remaining <= STAKE_FLOOR:
            break
        sess = by_id.get(sid)
        if not sess:
            log.info(f"MLB single: session {sid} not active — skipping (-> leftover/manual)")
            continue
        bookie = sess.get("bookie", "sportsbet")
        # Per-account STAKE ladder from the yaml mlb.player_stats list cap.
        cap = session_priority.lookup_liability_cap(sid, "mlb", "player_stats")
        ladder = list(cap) if isinstance(cap, tuple) else [float(cap)] if cap else [cap_stake]
        steps = _sgm_ladder_steps(ladder, min(remaining, max(ladder)))
        if not steps:
            continue
        # Catalog-resolve the 2+ single (h_r_rbi over 1.5) on this account.
        event_for_hb = _bookie_event(tip.event, bookie, "mlb")
        try:
            pc = hb.price_check_sports(
                session_id=sid, sport="mlb", event=event_for_hb,
                markets_filter=["player_props"],
            )
        except Exception as e:
            log.warning(f"MLB single price-check failed on {bookie}:{sid}: {e} — skipping")
            continue
        if not pc.get("success"):
            log.info(f"MLB single: catalog unavailable on {bookie}:{sid} "
                     f"({pc.get('error', 'unknown')}) — skipping (-> leftover/manual)")
            continue
        leg_dict = {
            "market": "player_stats", "player": player, "stat": "h_r_rbi",
            "selection": f"{player} Over", "line": 1.5,
        }
        m = _match_mlb_player_prop(leg_dict, pc.get("markets") or {})
        if not m:
            log.info(f"MLB single: 2+ HRRBI for {player} not carried on "
                     f"{bookie}:{sid} — skipping (-> leftover/manual)")
            continue
        target_odds = (_afl_target_odds(tip.sport, m["odds"])
                       if m.get("odds") else None)
        # v5.52: index-based ladder so ONE fast price-change (code=535) retry
        # can re-run the SAME rung with target_odds dropped, re-entering the
        # full classification below — the ambiguous guard keeps precedence on
        # the retry attempt itself (Erasmus). Flags are per-ACCOUNT.
        _step_i = 0
        _price_retried = False
        while _step_i < len(steps):
            step_stake = steps[_step_i]
            _t0 = _t.time()
            resp = hb.place_single_sports_bet(
                session_id=sid, sport="mlb", event=event_for_hb,
                market="player_stats", selection=m["selection"],
                player=m["selection"], stat=m["stat"], line=m["line"],
                stake=step_stake, target_odds=target_odds,
                proposition_id=m["proposition_id"],
            )
            _el = round(_t.time() - _t0, 2)
            if resp.get("success"):
                try:
                    actual = float(resp.get("stake", step_stake))
                except (TypeError, ValueError):
                    actual = step_stake
                r = BetResult(
                    success=True, tip=tip, session_id=sid, bookie=bookie,
                    bet_id=resp.get("bet_id"), odds=resp.get("odds"),
                    stake=actual, timestamp=datetime.now(), elapsed_sec=_el,
                    placed_market="player_stats", placed_player=player,
                    placed_stat="h_r_rbi", placed_line=m["line"],
                    placed_selection=m["selection"],
                    placed_leg_summary=_format_tip_placement_summary(tip),
                )
                log.info(f"MLB single placed: {r.bet_id} on {bookie}:{sid} "
                         f"@ {r.odds} ${actual} (2+ HRRBI {player})")
                # No per-placement notify — the orchestrator (_place_mlb_hrrbi)
                # sends ONE consolidated summary covering all accounts.
                results.append(r)
                remaining = round(remaining - actual, 2)
                break
            err = str(resp.get("error", "") or "")
            _slow = _el >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
            if (_slow or bool(resp.get("ambiguous"))) and not _is_definitely_pre_placement(err):
                # Erasmus: maybe-placed -> reconcile (gated), STOP this account
                # either way. Never re-bet/ladder on uncertainty.
                import reconcile as _recon
                _acct = sess.get("account_id")
                _decision = _recon.decide_ambiguous(
                    hb, _acct, event=tip.event, stake=step_stake, sport="mlb",
                    selection=m["selection"], submit_ts=_t.time() - _el,
                    reconcile_enabled=RECONCILE_AMBIGUOUS, spill_enabled=False,
                )
                _action = _decision.get("action")
                _reason = "slow_rejection" if _slow else "fast_ambiguous"
                if _action == "placed":
                    # v5.55 (audit): the bet IS on the books — record a real
                    # PLACED result at the ACTUAL stake (auto-cap: smaller
                    # counts) instead of an ambiguous debit (v5.52 flagged
                    # is_ambiguous=True even when reconcile confirmed).
                    _match = _decision.get("match") or {}
                    try:
                        _actual = float(
                            _decision.get("actual_stake", step_stake) or step_stake)
                    except (TypeError, ValueError):
                        _actual = step_stake
                    r = BetResult(
                        success=True, tip=tip, session_id=sid, bookie=bookie,
                        bet_id=_match.get("bookie_bet_id") or _match.get("id"),
                        odds=_match.get("odds") or resp.get("odds"),
                        stake=_actual, elapsed_sec=_el, timestamp=datetime.now(),
                        placed_market="player_stats", placed_player=player,
                        placed_stat="h_r_rbi", placed_line=m["line"],
                        placed_selection=m["selection"],
                        placed_leg_summary=_format_tip_placement_summary(tip),
                    )
                    log.warning(
                        f"MLB single reconcile CONFIRMED placed on {bookie}:{sid} "
                        f"${_actual:.2f} (bet_id={r.bet_id}) — recording as PLACED"
                    )
                    results.append(r)
                    remaining = round(remaining - _actual, 2)
                    break
                # v6.03 Guard B (also on the MLB single path): a HARD poll-deadline
                # ambiguous is NEVER treated as a clean not-placed here — HB never
                # confirmed the cid resolved, so a pending_bets 'not found' can still
                # land after the poll window. not_placed -> leftover -> manual re-place
                # -> LATE land = DOUBLE STAKE. Fall through to conservative
                # (debit-as-placed), mirroring _reconcile_fanout_ambiguous's Guard B.
                # v6.07 (sweep HIGH #1/#5): broadened from the single "hard poll
                # deadline" literal to EVERY unresolved-cid envelope (cid timeout /
                # poll budget exhausted / hard deadline) — those bets can still land
                # after the reconcile window, so they must stay ambiguous, never be
                # downgraded to a clean not-placed. Mirrors _reconcile_fanout_ambiguous.
                if _action == "not_placed" and not (
                    bool(resp.get("cid_unresolved", False))
                    or _err_is_cid_unresolved(err)
                ):
                    # v5.55: pending_bets POSITIVELY confirmed nothing landed —
                    # no debit, no ambiguous critical; the stake stays in the
                    # orchestrator's leftover -> manual alert. Still STOP this
                    # account (a confirmed-not-placed re-bet is Tier-2 spill,
                    # deliberately off for sports).
                    log.error(
                        f"MLB single CONFIRMED NOT placed on {bookie}:{sid} "
                        f"(reconcile, {_reason}) err='{err[:80]}' — no debit, "
                        f"stake stays leftover->manual; stopping this account"
                    )
                    break
                # Conservative (reconcile off / API down / no account_id):
                # debit-as-placed, flag, alert — unchanged behaviour.
                _debit = step_stake
                log.error(
                    f"MLB single AMBIGUOUS ({_reason}) on {bookie}:{sid} "
                    f"stake=${_debit:.2f} elapsed={_el:.1f}s err='{err[:80]}'. "
                    f"Debiting as placed, stopping this account."
                )
                r = BetResult(
                    success=False, tip=tip, session_id=sid, bookie=bookie,
                    error=f"ambiguous ({_reason}): {err[:120]}", is_ambiguous=True,
                    # v6.07 (Batch A audit): structural flag, not text-dependent
                    cid_unresolved=bool(resp.get("cid_unresolved", False)),
                    stake=_debit, elapsed_sec=_el, timestamp=datetime.now(),
                    placed_leg_summary=_format_tip_placement_summary(tip),
                )
                results.append(r)
                _emit_sports_ambiguous_alert(tip, [{
                    "bookie": bookie, "session_id": sid, "stake": round(_debit, 2),
                    "odds": resp.get("odds") or (m.get("odds") or 0),
                    "elapsed_sec": _el, "error": err[:200], "reason": _reason,
                }])
                remaining = round(remaining - _debit, 2)
                break
            if _is_stake_error(err):
                log.info(f"MLB single stake reject ${step_stake:.2f} on {bookie}:{sid} "
                         f"— laddering down")
                _step_i += 1
                continue
            # v5.52: FAST price-change (code=535) = slip validation rejected
            # BEFORE submission (definitively pre-placement) -> retry ONCE on
            # the same rung with target_odds dropped (accept current market),
            # mirroring the v4 singles retry (~L4726). The ambiguous branch
            # above already swallowed slow/flagged rejects; the explicit
            # not-_slow / not-ambiguous guards re-assert that for compound
            # error strings that also match a pre-placement pattern. A second
            # 535 (flag spent) falls through to the stop below.
            if (_is_price_change_error(err) and not _price_retried
                    and not _slow and not resp.get("ambiguous")):
                _price_retried = True
                target_odds = None
                log.info(f"MLB single price-change retry on {bookie}:{sid} "
                         f"(target_odds dropped, same rung ${step_stake:.2f})")
                continue
            log.warning(f"MLB single non-stake fail on {bookie}:{sid}: {err[:120]} "
                        f"— stopping this account")
            break
    return results


def _place_mlb_hrrbi(tip: ParsedTip) -> list[BetResult]:
    """Per-account MLB HRRBI placement. v5.77 (Wilson 2026-06-20): the 1+/2+ HRRBI
    2-leg SGM (multi) is EVEN-SPLIT across the THREE SGM-capable accounts
    (MLB_SGM_SESSION_PRIORITY: Adam 65465 / Wilson 53522 / Daniel 68723 = $400/3 ~=
    $133 each), each laddering DOWN 100/90/85/80% of its share on a bookie reject
    (MLB_HRRBI_LADDER_PCT). Ryan 102506 was DROPPED from the SGM split (Sportsbet
    returns "outcome is suspended code=540" on every HRRBI SGM for Ryan). Whatever
    is UNFILLED after the SGM SPILLS to the SINGLE-only accounts in order
    (MLB_HRRBI_SINGLE_SESSIONS = Alex 65463 then Ryan 102506) as 2+ HRRBI SINGLES
    (neither can do multis). [v5.74 history: was a 4-way SGM incl. Ryan + Alex single.]
    Anything still unfilled after Alex -> ONE consolidated manual-placement
    Telegram alert. Only the validated HRRBI shape reaches here (place_tip gates
    on _is_mlb_hrrbi_sgm). [v5.0/v5.38 history: was 3 SGM accounts on the
    [130,100,87] liability ladder + Alex single; Ryan added + %-ladder 2026-06-17.]"""
    _t_start = time.time()  # v5.37: end-to-end timing for the consolidated summary
    intended = tip.stake_dollars
    # SGM accounts first: even-split ($400/4=$100) + per-account 100/90/85/80%
    # stake ladder (v5.74), placed CONCURRENTLY across the 4 SGM accounts via
    # _place_sgm_fanout(_orchestrated=True) — it returns the placements WITHOUT a
    # summary (this function owns the ONE consolidated summary + spill->Alex +
    # leftover->manual). Falls back to the sequential _place_sgm_v4 internally on
    # any non-happy-path. SGM_CONCURRENT_FANOUT=false -> sequential.
    # _orchestrated suppresses the SGM path's own terminal manual alert.
    if SGM_CONCURRENT_FANOUT:
        sgm_results = _place_sgm_fanout(tip, _orchestrated=True)
    else:
        sgm_results = _place_sgm_v4(tip, _orchestrated=True)
    placed = sum((r.stake or 0) for r in sgm_results if r.success)
    # v5.38 (adversarial-pass fix): an AMBIGUOUS (maybe-landed) SGM rung is
    # COMMITTED — its at-risk stake MUST reduce the remainder handed to Alex, else
    # Alex backstops a slice that may already be on the books (Erasmus/Dawson
    # double-stake). Mirror the fan-out's own rollup (which excludes ambiguous
    # from unfilled). At-risk = _requested_stake (the rung fired; r.stake on an
    # ambiguous fan-out result is the step, but be defensive).
    _sgm_ambiguous = [r for r in sgm_results if not r.success and _is_ambiguous_result(r)]
    _sgm_ambiguous_total = sum(
        (getattr(r, "_requested_stake", None) or r.stake or 0) for r in _sgm_ambiguous)
    remaining = round(intended - placed - _sgm_ambiguous_total, 2)
    # Then the single-only account(s) for the next rung(s). MINOR (2026-06-21):
    # a sub-$1 rounding remainder after a full SGM fill must not spill a token
    # single (the gate inside _place_mlb_alex_single enforces the same minimum).
    alex_results = (_place_mlb_alex_single(tip, remaining)
                    if remaining >= MLB_HRRBI_SINGLE_MIN_STAKE else [])
    placed += sum((r.stake or 0) for r in alex_results if r.success)
    remaining = round(intended - placed - _sgm_ambiguous_total, 2)

    all_results = list(sgm_results) + list(alex_results)
    successes = [r for r in all_results if r.success]
    # v5.38: the 3 SGM accounts now place CONCURRENTLY (via _place_sgm_fanout's
    # ThreadPoolExecutor) when SGM_CONCURRENT_FANOUT; Alex's single runs
    # sequentially AFTER. So the bookie wall-clock is ~MAX(SGM accounts) + Alex,
    # closer to MAX than SUM -> concurrent_bookies=SGM_CONCURRENT_FANOUT (Alex's
    # tail lands in "other"; SUM would 3x-overstate the dominant concurrent SGM
    # block — the v5.35 fix this restores). Per-account timing for the summary's
    # reconciling breakdown (else all placement time lumps into "other").
    _mlb_concurrent = bool(SGM_CONCURRENT_FANOUT)
    _mlb_session_timing = [{
        "session_id": r.session_id, "bookie": r.bookie,
        "elapsed_sec": getattr(r, "elapsed_sec", None),
        "attempts": 1, "fails": 0, "succeeded": True,
    } for r in successes]
    log.info(
        f"MLB HRRBI per-account: placed ${placed:.2f} across {len(successes)} "
        f"account(s); ${remaining:.2f} of ${intended:.2f} leftover"
        + (f"; ${_sgm_ambiguous_total:.2f} ambiguous (maybe-landed, committed)"
           if _sgm_ambiguous_total else "")
    )
    if not successes and not _sgm_ambiguous:
        # Nothing landed anywhere -> manual (the play could not be auto-placed).
        if not tip.alert_reason:
            tip.alert_reason = "MLB HRRBI: no auto-placement on any account"
        notifier.notify_manual_alert(tip)
    elif not successes:
        # No CONFIRMED placement, but >=1 SGM account is AMBIGUOUS (maybe-landed).
        # The fan-out already fired the CRITICAL ambiguous alert; do NOT also
        # re-prompt a FULL manual re-place of a play that may already be (partly)
        # on the books — that's the double-stake the ambiguous handling prevents.
        log.warning(
            f"MLB HRRBI: no CONFIRMED placement but ${_sgm_ambiguous_total:.2f} "
            f"ambiguous (maybe-landed) — full manual re-place SUPPRESSED (the "
            f"ambiguous critical alert owns reconciliation)")
    else:
        # ONE consolidated success message per tip (Wilson 2026-06-02: like
        # Tip Titans / singles-v4 — not 3 separate "BET PLACED" msgs). Rolls up
        # every account's placement + shows the leftover as "Unfilled $X" (the
        # cue to place the rest manually). Per-placement notifies are suppressed
        # upstream (_place_sgm_v4 _orchestrated / _place_mlb_alex_single).
        try:
            notifier.notify_tip_placed_summary(
                tip, successes, intended, round(remaining, 2),
                total_elapsed_sec=round(time.time() - _t_start, 2),
                session_timing=_mlb_session_timing,
                concurrent_bookies=_mlb_concurrent,
            )
        except Exception as e:
            log.error(f"MLB HRRBI placed-summary notify failed: {e}")
        # v5.16 (Wilson 2026-06-05): also send the LEFTOVER to the Manual Bets
        # channel so the rest gets placed by hand — not just the inline
        # "Unfilled $X" tag in the summary above. Previously the per-account MLB
        # model suppressed this entirely (Happ's $52 went only to the tag).
        # Fires whenever a real remainder is left after every account took its
        # rung (i.e. an account couldn't fill / fewer accounts than the stake).
        # v5.18 (Wilson 2026-06-05): only fire the leftover->manual alert for a
        # GENUINE partial fill. A stale/replayed tip (e.g. an overnight Shook
        # message re-delivered on a flaky telethon reconnect, OUTSIDE the dedup
        # window) reaches here with no resolved event AND no raw message, so the
        # alert renders "Event: UNRESOLVED", empty Raw, "@ 0" odds. The sportsbot
        # fork emitted 4x duplicate Freddie Freeman "BET UNFILLED" alerts exactly
        # this way. A live Shook tip ALWAYS carries the raw Telegram text (and a
        # resolved event), so require at least one as proof-of-liveness.
        _tip_is_live = bool((tip.event or "").strip()) or bool(
            (getattr(tip, "raw_message", "") or "").strip()
        )
        if remaining > 1.0 and _tip_is_live:
            try:
                notifier.notify_tip_unfilled_with_placements(
                    tip, intended, round(placed, 2), round(remaining, 2),
                    successes, [],
                    session_timing=_mlb_session_timing,
                    total_elapsed_sec=round(time.time() - _t_start, 2),
                    concurrent_bookies=_mlb_concurrent,
                )
                log.info(
                    f"MLB HRRBI: ${remaining:.2f} leftover -> Manual Bets alert"
                )
            except Exception as e:
                log.error(f"MLB HRRBI leftover manual notify failed: {e}")
        elif remaining > 1.0:
            log.warning(
                f"MLB HRRBI: ${remaining:.2f} leftover but tip looks degraded "
                f"(event='{tip.event}', raw_empty="
                f"{not bool((getattr(tip, 'raw_message', '') or '').strip())}) "
                f"-- suppressing leftover->manual alert (likely a stale/replayed "
                f"re-delivery, not a live partial fill)"
            )

    # v5.69 (i2): ONE consolidated audit tip_outcome covering BOTH the SGM
    # accounts AND Alex's single, so the audit trail matches the consolidated
    # Telegram summary. Previously the SGM fan-out wrote an SGM-only row and
    # Alex's placements never appeared in any audit tip_outcome. Best-effort:
    # an audit write must never break a placement.
    try:
        _log_jsonl(_audit_log_path(), {
            "type": "tip_outcome", "tipster": tip.tipster, "event": tip.event,
            "intended_stake": round(intended, 2),
            "placed_stake": round(placed, 2),
            "ambiguous_stake": round(_sgm_ambiguous_total, 2),
            "unfilled_stake": round(max(0.0, remaining), 2),
            "mlb_hrrbi": True, "accounts": len(successes),
            "placements": [
                {"session_id": r.session_id, "bookie": r.bookie, "stake": r.stake,
                 "fill_odds": r.odds, "bet_id": r.bet_id,
                 "sgm": bool(r in sgm_results)}
                for r in successes],
            "ambiguous": [
                {"session_id": r.session_id, "bookie": r.bookie,
                 "stake": (getattr(r, "_requested_stake", None) or r.stake or 0),
                 "error": r.error}
                for r in _sgm_ambiguous],
        })
    except Exception as e:
        log.error(f"MLB HRRBI consolidated tip_outcome audit write failed: {e}")
    return all_results


def _afl_event_teams(event: str) -> list:
    """Split a resolved AFL fixture string ('Home v Away' / 'Home vs Away') into
    its two team names. BUG A (Wilson 2026-06-21): used to scope an AFL player
    surname to BOTH teams of the game so the resolver can never resolve to a
    wrong-GAME player (Richards -> Joe Richards/Port Adelaide). Returns [] if the
    event doesn't split cleanly (caller then keeps the single-team scope)."""
    if not event or not isinstance(event, str):
        return []
    parts = re.split(r"\s+v(?:s)?\s+", event.strip(), maxsplit=1, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()] if len(parts) == 2 else []


def _resolve_leg_for_hyperbot(
    leg, sport: str, is_threshold: bool = False, for_sgm: bool = False,
    tipster: str = "", event_teams: "list | None" = None,
) -> dict:
    """
    Resolve a parsed leg into HyperBot API payload fields.
    Shared by both single bets and SGM legs.

    `event_teams` (BUG A): when an AFL fixture is known, the player surname is
    scoped to the UNION of BOTH teams and the cross-game global roster fallback
    is forbidden — the resolver returns a player in THIS game or leaves the
    surname unchanged (the event-scoped bookie catalog then resolves it or routes
    to manual), never a wrong-GAME player.

    Returns dict with: market, selection, player, stat, line
    """
    market = leg.market
    selection = leg.selection
    line = leg.line if leg.line else None
    player = leg.player or None
    stat = leg.stat or None

    # ── AGS shorthand normalisation ─────────────────────────────────
    # AGS = anytime goalscorer = "1+ goals" in AFL. Only valid as an SGM
    # leg (Wilson confirmed Saiyan never tips AGS as a single). If we see
    # AGS outside an SGM, leave it unhandled so the bet fails loudly to
    # manual rather than placing a wrong market.
    # The selection (= resolved player name) is assigned AFTER player
    # resolution below, since AGS markets use the player name as selection.
    # Saiyan tips AGS only inside SGMs; test tipster can tip AGS as a
    # standalone single ("afl josh treacy AGS"). Removed the for_sgm gate
    # so test single AGS bets get the same normalisation treatment.
    is_ags_leg = False
    if (
        sport == "afl"
        and (
            (stat and stat.lower() == "ags")
            or (selection and "AGS" in selection.upper() and not stat)
        )
    ):
        is_ags_leg = True
        stat = "goals"
        line = 1
        is_threshold = True
        market = "goalscorer_threshold_afl"
        log.info(f"AGS normalised: player='{player}' -> goalscorer_threshold_afl line=1")
        _afl_log.info(
            f"AGS normalised: player='{player}' tipster={tipster} "
            f"for_sgm={for_sgm} -> market=goalscorer_threshold_afl "
            f"stat=goals line=1 is_threshold=True"
        )

    # ── Resolve player full name + strip accents ────────────────────
    if player:
        # Shook always sends full names - skip fuzzy matching which can
        # corrupt correct names (e.g. Jabari Smith -> Malachi Smith).
        if tipster != "shook":
            # Pass team_full as a filter so the matcher scopes to the
            # leg's team. Saiyan AFL always includes a team code per
            # leg, so this is the primary safeguard against cross-team
            # collisions like "Davis" -> "Hugh Davies" (Fremantle) when
            # the actual tip was Hamish Davis on West Coast Eagles.
            # 2026-05-02 SGM regressions: Davis/O'Sullivan/NWM all routed
            # to wrong players or failed because legs were resolved with
            # no team context.
            #
            # BUG A (Wilson 2026-06-21): for AFL, scope the surname to BOTH
            # teams of the resolved fixture (not just the tip's single team)
            # and FORBID the cross-game global fallback. 'Richards' on 'STK'
            # missed -> global fallback grabbed Joe Richards (Port Adelaide,
            # not in St Kilda v Western Bulldogs) instead of Ed Richards (WB).
            if sport == "afl" and event_teams:
                _before = player
                player = resolve_player_name(
                    player, sport, team=leg.team_full or "", teams=event_teams)
                # LATENCY (2026-06-27): resolve_player_name returns the input
                # UNCHANGED both on a true roster miss AND when the name was
                # already exactly correct (Saiyan/Eddie send full names like
                # 'Jack Crisp'/'Elliot Yeo'), so `player == _before` alone CANNOT
                # tell them apart — it fired the ~14s Claude web-search on EVERY
                # already-rostered player (06-27: ~14s x N legs, sequential, = the
                # 76s tip latency). FIX A (2026-07-05): key the skip-guard off the
                # ROSTER MATCH RESULT, not leg.team_full. Saiyan/Eddie IMAGE tips
                # arrive with BLANK leg.team_full, so the old `bool(leg.team_full)
                # and any(_roster_team_matches(...))` was ALWAYS False for them and a
                # rostered full-name player (e.g. 'Colby McKercher' matched North
                # Melbourne score=1.0) STILL fired the ~14s web-search. get_player_team
                # runs the SAME event-scoped fuzzy match as resolve_player_name above
                # (identical team=/teams= args, same _before input); because
                # teams=event_teams FORBIDS fuzzy_match_player's cross-game global
                # fallback (roster.py ~600-621), ANY non-empty team it returns is a
                # player in THIS fixture -> the backstop is redundant, skip it. A
                # genuine roster MISS (just-listed/stale player absent from
                # roster_afl.json) returns "" -> the search still fires, preserving
                # the BUG-A wrong-game backstop (the only case that ever NEEDED it).
                # Mirrors the already-correct AFL singles path (~12015-12020).
                _resolved_team = get_player_team(
                    _before, sport, team=leg.team_full or "", teams=event_teams)
                _team_in_fixture = bool(_resolved_team)
                if player == _before and len(_before.split()) >= 2 \
                        and not _team_in_fixture \
                        and tip_parser._claude_websearch_enabled():
                    # No roster hit on EITHER event team for a FULL name — the
                    # roster may be stale (just-listed player). Confirm the
                    # CURRENT club via Claude web-search; accept ONLY if it is
                    # one of the two event teams (proves the player is in THIS
                    # game). A bare surname is left to the event-scoped catalog
                    # matcher (web-search on a surname is unreliable). Never
                    # resolves to a wrong-GAME player.
                    try:
                        import claude_parser as _cp
                        _club = _cp.resolve_afl_player_team(_before)
                    except Exception as _e:
                        _club = ""
                        log.warning(f"BUG-A web-search backstop failed for {_before!r}: {_e}")
                    if _club and any(_roster_team_matches(_club, t) for t in event_teams):
                        leg.team_full = _club
                        log.warning(
                            f"BUG-A: '{_before}' absent from {event_teams} rosters; "
                            f"Claude web-search confirms current club '{_club}' IS in "
                            f"this game — proceeding (still gated on bookie catalog)")
                    elif _club:
                        log.warning(
                            f"BUG-A: '{_before}' web-search club '{_club}' NOT in event "
                            f"{event_teams} — likely wrong game; leaving to catalog gate "
                            f"(-> manual on a miss, never a wrong-game bet)")
            else:
                player = resolve_player_name(player, sport, team=leg.team_full or "")
        # Strip NBSP/narrow-NBSP/zero-width chars that the roster JSON may
        # have introduced. roster_afl.json shipped with U+00A0 between
        # first/last names for all 784 entries — every Saiyan tip ended up
        # with NBSP in the HyperBot payload. Bookies seemed to tolerate it
        # but it's the kind of thing that can silently break SGM matching
        # on a different bookie. Run BEFORE the NFD/Mn accent strip so we
        # don't normalise into a non-canonical form. This also belt-and-
        # braces against any future roster regen reintroducing NBSP.
        if player:
            player = (
                player.replace("\u00a0", " ")
                      .replace("\u202f", " ")
                      .replace("\u200b", "")
            )
            # Collapse runs of whitespace (post-strip) to single spaces.
            player = " ".join(player.split())
        player = "".join(
            c for c in unicodedata.normalize("NFD", player)
            if unicodedata.category(c) != "Mn"
        )

    # AGS legs use the resolved (full, accent-stripped) player name as the
    # HyperBot selection. Set this here so we get the canonical name from
    # the roster lookup above, not the raw "Treacy" surname. Also fires
    # defensively when Groq pre-emits market='goalscorer_threshold_afl'
    # directly (clean test-tipster output) — selection might still be
    # "over"/"under" in that case which HyperBot won't accept.
    #
    # Extends to ALL AFL threshold markets (player_disposals_threshold_afl,
    # player_marks_threshold_afl, player_tackles_threshold_afl, etc).
    # 2026-05-01 Saiyan SGM "Evans 14+ disposals/Thilthorpe AGS" failed
    # because the disposals leg was sent with selection="over" — for
    # *_threshold_afl markets, selection must be the player name, same
    # pattern as goalscorer. HyperBot returned:
    #   "Leg 1: Selection 'over' not found"
    # The Thilthorpe AGS leg was correctly shaped (selection=Thilthorpe);
    # only the threshold-disposals leg was wrong.
    if (
        is_ags_leg
        or market == "goalscorer_threshold_afl"
        or (sport == "afl" and isinstance(market, str)
            and market.endswith("_threshold_afl"))
    ):
        selection = player or ""

    # ── Stat normalization ──────────────────────────────────────────
    STAT_NORMALIZE = {
        # NBA
        "points_rebounds_assists": "points_rebounds_assists",
        "points_rebounds": "points_rebounds",
        "points_assists": "points_assists",
        "assists_rebounds": "assists_rebounds",
        # Belt-and-braces alongside groq_parser._STAT_ALIASES. Only the
        # rebounds_assists case is broken — NBA_OU_MARKETS keys it
        # alphabetically while the other combos key in input-order, so
        # tipster "R+A" is the lone case that emits non-canonical from Groq.
        # See KAT regression 2026-05-05.
        "rebounds_assists": "assists_rebounds",
        "points": "points", "p": "points", "pts": "points",
        "rebounds": "rebounds", "r": "rebounds", "reb": "rebounds", "rbd": "rebounds",
        "assists": "assists", "a": "assists", "ast": "assists",
        "threes": "threes", "3s": "threes",
        "blocks": "blocks", "blk": "blocks",
        "steals": "steals", "stl": "steals",
        "pra": "points_rebounds_assists",
        "pr": "points_rebounds", "rp": "points_rebounds",
        "pa": "points_assists", "ap": "points_assists",
        "ra": "assists_rebounds", "ar": "assists_rebounds",
        # AFL
        "disposals": "disposals", "disposal": "disposals",
        "disp": "disposals", "d": "disposals", "touches": "disposals",
        "goals": "goals", "goal": "goals", "g": "goals",
        "marks": "marks", "mark": "marks",
        "tackles": "tackles", "tackle": "tackles",
        "kicks": "kicks", "kick": "kicks", "k": "kicks",
        "handballs": "handballs", "handball": "handballs", "hb": "handballs",
        "clearances": "clearances", "hitouts": "hitouts",
        "fantasy_points": "fantasy_points",
    }
    if stat:
        stat = STAT_NORMALIZE.get(stat.lower(), stat.lower())

    # ── Market normalization ────────────────────────────────────────
    if market in ("ml", "moneyline", "money_line"):
        market = "h2h"

    # For H2H: selection must be team name
    if market == "h2h" and (not selection or selection.upper() == "ML"):
        selection = leg.team_full or ""

    # For line/handicap: selection must be team name
    if market in ("line", "first_half_line") and selection in ("over", "under", ""):
        selection = leg.team_full or ""

    # 2026-05-03 Kev "Philly" regression: kev_nba parser passed "Philly"
    # through as leg.team_full, so selection became "Philly" which the
    # bookie didn't recognise (Sportsbet had "Philadelphia 76ers"). Fix:
    # for h2h/line markets, normalise the selection through the sport's
    # team-alias map BEFORE sending to bookie. Same map already used for
    # event resolution. Catches Philly, Mavs, Sixers, Cavs, Heat, etc
    # for any tipster (not just Kev). AFL aliases already applied at
    # parse time via AFL_TEAMS so we only need to handle NBA/NBL here.
    if market in ("h2h", "line", "first_half_line") and selection:
        if sport in ("nba", "nbl"):
            try:
                from nba_resolver import NBA_TEAM_ALIASES
                canonical = NBA_TEAM_ALIASES.get(selection.lower())
                if canonical and canonical != selection:
                    log.info(
                        f"Team-name normalised: '{selection}' -> "
                        f"'{canonical}' for {market} bet"
                    )
                    selection = canonical
            except Exception as e:
                log.debug(f"Team alias lookup skipped: {e}")

    # NOTE on SGM player prop legs:
    # The HyperBot API docs show SGM legs using market="player_prop" (generic)
    # with selection="over"/"under" (raw direction). Empirically this FAILS —
    # HyperBot's bookie-side code looks up the literal market string in its
    # parsed markets dict and "player_prop" is not a valid key. Confirmed via
    # Sportsbet logs:
    #   "Leg 1: Market 'player_prop' not found.
    #    Available: [... 'player_points', 'player_pts_threshold']"
    # So SGM legs use the SAME format as singles: specific market keys
    # (player_points, player_pts_threshold, player_pra, etc.) and formatted
    # "Player Name Over/Under" selections. No SGM-specific fast-path.

    # ── Player prop routing (singles + SGM — same format) ──────────
    # NBA O/U market map (main + alt lines)
    NBA_OU_MARKETS = {
        "points": "player_points",
        "rebounds": "player_rebounds",
        "assists": "player_assists",
        "threes": "player_threes",
        "blocks": "player_blocks",
        "steals": "player_steals",
        "points_rebounds_assists": "player_pra",
        "points_rebounds": "player_pts_rebs",
        "points_assists": "player_pts_asts",
        "assists_rebounds": "player_asts_rebs",
    }

    # NBA threshold market map
    NBA_THRESH_MARKETS = {
        "points": "player_pts_threshold",
        "rebounds": "player_rebounds_threshold",
        "assists": "player_assists_threshold",
        "points_rebounds_assists": "player_pra_threshold",
        "points_assists": "player_pts_asts_threshold",
        "points_rebounds": "player_pts_rebs_threshold",
        "threes": "player_threes_threshold",
        "blocks": "player_blocks_threshold",
        "steals": "player_steals_threshold",
    }

    # AFL market map (used for both O/U and thresholds)
    AFL_STAT_MARKETS = {
        "disposals": "player_disposals",
        "goals": "goalscorer_threshold_afl",
        "marks": "player_marks",
        "tackles": "player_tackles",
        "kicks": "player_kicks",
        "handballs": "player_handballs",
        "clearances": "player_clearances",
        "hitouts": "player_hitouts",
        "fantasy_points": "player_fantasy",
    }

    # AFL threshold markets — separate from AFL_STAT_MARKETS because Sportsbet
    # offers DIFFERENT markets for O/U (player_disposals at line=X.5) vs
    # threshold (player_disposals_threshold at integer line). SGM legs like
    # "11+ Disposals" must route to the threshold market; otherwise HyperBot
    # receives an integer line on the O/U market and rejects with "line
    # moved". Confirmed 2026-04-24 on Tholstrup/Brown Saiyan SGM.
    # Goals only has the threshold market on Sportsbet — same as O/U map.
    AFL_THRESH_MARKETS = {
        "disposals": "player_disposals_threshold",
        "goals": "goalscorer_threshold_afl",
        "marks": "player_marks_threshold",
        "tackles": "player_tackles_threshold",
        "kicks": "player_kicks_threshold",
        "handballs": "player_handballs_threshold",
        "clearances": "player_clearances_threshold",
        "hitouts": "player_hitouts_threshold",
        "fantasy_points": "player_fantasy_threshold",
    }

    # ── MLB player props: ONE `player_stats` market keyed by `stat` ──────
    # All Sportsbet MLB player props (hits/total_bases/home_runs/h_r_rbi/...)
    # live in a single `player_stats` market, distinguished by a per-selection
    # `stat` field (confirmed live 2026-06-01). Route any MLB player-prop leg
    # there, canonicalise the stat via MLB_STAT_MAP, and carry the direction in
    # the selection text so the SGM enricher's _match_mlb_player_prop can
    # resolve the exact proposition_id (it overwrites `selection` with the
    # catalog's bare-name value before placing). Lines stay X.5 O/U (no AFL-
    # style threshold encoding). Only MLB SGMs auto-place (the HRRBI 2-leg
    # SGM); MLB singles have no priority list -> manual, so in practice this
    # branch runs via the SGM path. Set market BEFORE the NBA/AFL player-prop
    # block below so that block (which only handles market=="player_prop") is
    # skipped for MLB.
    if sport == "mlb" and market in ("player_prop", "player_stats") and player and stat:
        from config import MLB_STAT_MAP
        stat = MLB_STAT_MAP.get(stat.lower(), stat.lower())
        market = "player_stats"
        if selection in ("", "over", "under"):
            direction = selection or "over"
            selection = f"{player} {direction.capitalize()}"
        log.info(f"MLB player_stats leg: sel='{selection}' stat={stat} line={line}")

    if market == "player_prop" and player and stat:
        if is_threshold:
            # ── Threshold: 25+ Points ───────────────────────────────
            # selection = player name, line = whole number
            if sport in ("nba", "nbl"):
                mapped = NBA_THRESH_MARKETS.get(stat)
                if mapped:
                    market = mapped
                    selection = player
                    if line and line % 1 != 0:
                        line = int(line + 0.5)
                    elif line:
                        line = int(line)
                    log.info(f"Threshold: {market}, sel='{selection}', line={line}")
            elif sport == "afl":
                if for_sgm:
                    # SGM legs: per-player threshold markets DO work inside
                    # Sportsbet's SGM builder. Keep as-is (Wilson confirmed
                    # working 2026-05-30) — do not change the SGM path.
                    mapped = AFL_THRESH_MARKETS.get(stat)
                    if mapped:
                        market = mapped
                        selection = player
                        if line and line % 1 != 0:
                            line = int(line + 0.5)
                        elif line:
                            line = int(line)
                        log.info(f"AFL SGM threshold: {market}, sel='{selection}', line={line}")
                elif stat == "goals":
                    # Singles: goals threshold = Anytime Goal Scorer, a real
                    # standalone market. selection = player name.
                    market = "goalscorer_threshold_afl"
                    selection = player
                    if line and line % 1 != 0:
                        line = int(line + 0.5)
                    elif line:
                        line = int(line)
                    log.info(f"AFL single goals threshold (AGS): {market}, sel='{selection}', line={line}")
                else:
                    # Singles — CONFIRMED working payload (Wilson 2026-05-31):
                    #   {market: player_disposals, selection: "Marcus Bontempelli",
                    #    line: 29.0}   (a "29+ disposals" threshold)
                    # i.e. base O/U market + PLAYER NAME as the selection +
                    # INTEGER line, and NO separate `player` field and NO `stat`.
                    # (Empty player/stat are omitted from the place payload.)
                    mapped = AFL_STAT_MARKETS.get(stat)
                    if mapped:
                        market = mapped
                        if line:
                            line = float(int(round(line)))  # 29+ -> 29.0, NOT 28.5
                        selection = player   # player NAME is the selection
                        player = ""          # no separate player field
                        stat = ""            # no stat field
                        log.info(
                            f"AFL single threshold: {market}, "
                            f"selection='{selection}' (player name), line={line}, "
                            f"no player/stat field"
                        )
        else:
            # ── O/U (main + alt lines): Over 29.5 Points ───────────
            # selection = "Player Name Over/Under", line = decimal
            if sport in ("nba", "nbl"):
                mapped = NBA_OU_MARKETS.get(stat)
                if mapped:
                    market = mapped
                    log.info(f"O/U market: player_prop -> {mapped}")
            elif sport == "afl":
                mapped = AFL_STAT_MARKETS.get(stat)
                if mapped:
                    market = mapped
                    log.info(f"AFL O/U market: player_prop -> {mapped}")

            # For O/U: use "Player Name Over"/"Player Name Under" (main-line
            # format only).
            #
            # DO NOT use the line-suffixed format ("Player Name Under 33.5").
            # HyperBot appears to ignore the direction when the selection
            # contains a line suffix — tipbot sent "Jalen Brunson Under 33.5"
            # on 2026-04-24 and HyperBot matched "Jalen Brunson Over 33.5"
            # instead, placing at 4.6 when user wanted 1.16 Under. Confirmed
            # via Sportsbet HyperBot log:
            #   "Matched: Jalen Brunson Over 33.5 @ 4.6" after we sent
            #   "selection='Jalen Brunson Under 33.5'".
            #
            # Consequence: alt lines (anything that isn't HyperBot's "main"
            # line) will fail with "did not match any of 2 candidates" and
            # we route to manual. That's the less-bad failure mode until
            # Finn fixes HyperBot's alt-selection matcher.
            if selection in ("over", "under"):
                selection = f"{player} {selection.capitalize()}"
                log.info(f"Selection: '{selection}'")

    # Groq is inconsistent: it sometimes emits the AFL stat-threshold MARKET
    # NAME directly (e.g. "player_disposals_threshold_afl") instead of
    # market="player_prop" + is_threshold, which bypasses the routing above and
    # leaves the unsupported threshold market on the wire (-> market_not_carried,
    # 2026-05-31). For AFL SINGLES, normalise any "*_threshold_afl" market to the
    # CONFIRMED format: base market (player_disposals) + player NAME as selection
    # + integer line, no player/stat field. Goalscorer/AGS is a real standalone
    # market — leave it. SGM keeps its own *_threshold markets (for_sgm).
    if (
        sport == "afl" and not for_sgm and isinstance(market, str)
        and market.endswith("_threshold_afl")
        and not market.startswith("goalscorer")
        and market.startswith("player_")
    ):
        base = market[: -len("_threshold_afl")]   # player_disposals_threshold_afl -> player_disposals
        market = base
        selection = (player or selection or "")   # player name as the selection
        player = ""
        stat = ""
        if line:
            line = float(int(round(line)))
        log.info(
            f"AFL single: normalised Groq threshold market -> {market}, "
            f"selection='{selection}' (player name), line={line}, no player/stat"
        )

    return {
        "market": market,
        "selection": selection,
        "player": player,
        "stat": stat,
        "line": line,
    }


def _format_leg_human(leg) -> str:
    """
    Format a single leg as human-readable text for notifications.
    Examples:
      "Jaylen Brown OVER 25.5 points"
      "Los Angeles Lakers +4.5"
      "Boston Celtics ML"
      "OVER 210.5"
    """
    market = (leg.market or "").lower()
    if market in ("h2h", "head_to_head", "moneyline", "ml"):
        team = leg.selection or leg.team_full or ""
        return f"{team} ML".strip()
    if market in ("line", "first_half_line"):
        team = leg.selection or leg.team_full or ""
        line_str = ""
        if leg.line is not None:
            try:
                ln = float(leg.line)
                line_str = f" {ln:+g}" if ln != 0 else " PK"
            except (TypeError, ValueError):
                pass
        prefix = "1H " if market == "first_half_line" else ""
        return f"{prefix}{team}{line_str}".strip()
    if market in ("total", "total_points"):
        sel = (leg.selection or "").upper()
        return f"{sel} {leg.line}".strip()
    # Player props and everything else with a player
    if leg.player:
        sel = (leg.selection or "").upper().replace(leg.player.upper(), "").strip()
        if sel in ("", leg.player.upper()):
            sel = "OVER"  # default
        return f"{leg.player} {sel} {leg.line} {leg.stat}".strip()
    return leg.raw_text or "(bet)"


def _format_tip_placement_summary(tip) -> str:
    """
    Snapshot the current state of tip.legs as a single human-readable string.
    For SGMs, joins all legs with ' / '. For singles, just the one leg.
    Called at placement time — if the tip's leg is later mutated (alt spillover),
    this string preserves what was actually placed.
    """
    if not tip.legs:
        return "(no legs)"
    return " / ".join(_format_leg_human(l) for l in tip.legs)


def _resolve_single_for_placement(
    tip: ParsedTip, session: dict, *,
    apply_ceiling: bool = True, apply_floor: bool = True, exact_only: bool = False,
) -> "tuple[dict | None, BetResult | None]":
    """Resolve a single leg to the exact catalog {market, line, selection,
    proposition_id, target_odds} the bookie carries, for one session.

    Returns (resolved, None) when placeable, or (None, BetResult) when the leg
    must route to manual — a catalog miss, an empty selection+player, an odds
    CEILING breach (when apply_ceiling), or a price-FLOOR breach (when apply_floor).

    Extracted from _execute_bet (v5.11). The AFL fan-out resolves ONCE per bookie
    and reuses it across accounts. Both guards run off the resolve-time catalog
    odds that are already captured here — NO extra price-check call.
      - apply_ceiling: the WRONG-SELECTION guard (live > 1.25x tipped -> manual;
        catches same-surname / ±1.0 line / wrong-O/U snaps placing a wrong pick).
      - apply_floor: the price-moved guard (live < 0.9x tipped -> manual).
    The fan-out keeps the ceiling but DROPS the floor (Wilson v5.13: a shorter-
    than-tipped live price should still place). Every other caller (NBA/MLB/
    handicap/total/SGM/racing via presolved=None) keeps BOTH (the defaults).
    The catalog line resolver + empty-selection guard are ALWAYS applied — a
    blind POST of an unresolved line places $0 (the Jaxon Prior failure).

    v6.08: `exact_only` is forced ON for DisposalsModel HERE, at the choke point,
    regardless of what the caller passed. Its exact-line rule is a MONEY invariant, so
    leaving it to one call site meant any other caller could re-arm the ±1.0 snap
    silently — including _execute_bet's own presolved=None re-resolve, which uses the
    bare defaults. Making it intrinsic to the resolver closes every path at once.
    """
    if getattr(tip, "tipster", "") == DISPOSALS_MODEL_TIPSTER and not exact_only:
        exact_only = True
    leg = tip.legs[0]
    sid = str(session["session_id"])
    bookie = session.get("bookie", "unknown")

    is_threshold = getattr(tip, '_is_threshold', False)
    resolved = _resolve_leg_for_hyperbot(
        leg, tip.sport, is_threshold=is_threshold, tipster=tip.tipster,
        event_teams=(_afl_event_teams(tip.event) if (tip.sport or "").lower() == "afl" else None),
    )

    market = resolved["market"]
    selection = resolved["selection"]
    player = resolved["player"]
    stat = resolved["stat"]
    line = resolved["line"]
    # Catalog-resolved proposition_id, sent to HyperBot for markets whose line
    # is encoded in the selection (pick_own_line) so the exact rung is matched.
    _resolved_prop_id = None
    # Live (catalog/price-check) odds for the resolved selection, captured by the
    # resolution blocks below and checked once against the max-odds ceiling
    # before placing (wrong-selection guard). None => no live odds => no ceiling.
    _resolved_live_odds = None

    # ── Target odds (10% below the relevant price, floored at 1.01) ─
    # Prefer the per-bookie alt-line odds if this is an auto-alt attempt;
    # otherwise use the tipped odds. Without this, alt-line attempts on
    # different lines target the original tipped odds floor and accept
    # any fill at or above that, which is too generous when the alt line
    # is showing a much higher market price.
    # If _skip_target_odds is set on the tip (price-change retry path),
    # send no target_odds at all — let HyperBot fill at current market.
    target_odds = None
    if not getattr(tip, "_skip_target_odds", False):
        alt_odds_map = getattr(tip, "_alt_target_odds_by_bookie", None)
        basis_odds = None
        basis_label = ""
        if alt_odds_map and bookie in alt_odds_map:
            basis_odds = alt_odds_map[bookie]
            basis_label = f"alt-line bookie odds {basis_odds}"
        elif tip.suggested_odds and tip.suggested_odds > 1.0:
            basis_odds = tip.suggested_odds
            basis_label = f"tipped {tip.suggested_odds}"

        if basis_odds and basis_odds > 1.0:
            target_odds = _afl_target_odds(tip.sport, basis_odds)
            _pct = 80 if ((tip.sport or "").lower() == "afl" and basis_odds > 2.00) else 90
            log.info(f"Target odds: {target_odds} ({_pct}% of {basis_label}, floor 1.01)")
    else:
        log.info("Target odds: omitted (price-change retry, accepting current market)")

    # ── AFL player-prop singles: catalog-driven market/line resolution ──
    # Sportsbet keys "N+ disposals" as the OVER half-line (N-0.5) in the
    # player_X_threshold ladder, NOT as integer N on the player_X O/U market
    # (which only carries the main line). So a "23+" single sent to
    # player_disposals line=23.0 fails "did not match" (only 31.5 exists),
    # 2026-05-31. Resolve against the live catalog and rewrite to the exact
    # {market, over-line, selection} Sportsbet carries — same logic as the
    # SGM enricher. Uses leg.player/leg.stat because the AFL threshold
    # routing empties the resolved player/stat fields. NBA falls through to
    # the within-1.0 block below (its O/U markets carry the right lines).
    if (tip.sport or "").lower() == "afl" and (player or leg.player) and (
        _is_afl_player_prop_market(market) or leg.stat
    ):
        # Catalog-resolve against the live single price_check_sports (/v3/price)
        # catalog. A genuine miss — line not carried even after the ±1.0 nearest
        # snap, or a price-check outage — MUST route to manual; the old code only
        # LOGGED "will fail to manual" then fell through and POSTed blind, which
        # produced "did not match" rejections that LOST the bet (e.g. Jaxon Prior
        # Under 17.5 vs carried 16.5, saiyan $0-placed 2026-05-31). Wilson
        # 2026-06-01: divert to manual instead, mirroring the odds-ceiling guard.
        _afl_pp_resolved = False
        try:
            _event_pc = _bookie_event(tip.event, bookie, tip.sport)
            _pc = hb.price_check_sports(
                session_id=sid, sport=tip.sport, event=_event_pc,
                markets_filter=["player_props"],
            )
            if _pc.get("success"):
                # Use the ORIGINAL tipped line (leg.line), not the resolved
                # line: the singles threshold normaliser rounds "22.5+" via
                # round(22.5)=22 (banker's rounding) -> 22.0, which would make
                # the over-line ceil(22.0)-0.5=21.5 (not carried). leg.line is
                # the true tip (22.5 -> over 22.5). 2026-05-31.
                _tip_line = leg.line if leg.line is not None else line
                _leg_dict = {
                    "market": market, "selection": selection,
                    "player": player or leg.player,
                    "stat": stat or leg.stat, "line": _tip_line,
                }
                _m = _match_afl_player_prop(_leg_dict, _pc.get("markets") or {}, exact_only=exact_only)
                if _m:
                    _resolved_live_odds = _m.get("odds")  # ceiling-checked below
                    if _m["market"] != market or _m["line"] != line:
                        log.info(
                            f"AFL single catalog-matched: {player or leg.player} "
                            f"{stat or leg.stat} {market} line={line} -> "
                            f"market={_m['market']} line={_m['line']} "
                            f"sel='{_m['selection']}' odds={_m['odds']}"
                        )
                    market = _m["market"]
                    line = _m["line"]
                    selection = _m["selection"]
                    # v5.96 (2026-07-06): adopt the catalog's CANONICAL player
                    # spelling for the payload's `player` field. HyperBot matches
                    # AFL player props on `player`, so sending the roster short-
                    # form ('Brad Hill') while `selection` already carried the
                    # catalog name ('Bradley Hill') was rejected on EVERY account
                    # ("player='Brad Hill' did not match ... Bradley Hill",
                    # 2026-07-05 eddie disposals fan-out — the catalog match had
                    # resolved the name but only `selection` was updated). The
                    # matcher already ran _afl_canonical_catalog_player, so
                    # _m["player"] is the exact live-catalog spelling; fall back
                    # to the tip/roster name only when the catalog omits it.
                    player = _m.get("player") or player or leg.player
                    stat = stat or leg.stat
                    _afl_pp_resolved = True
        except Exception as e:
            log.debug(f"AFL single catalog match skipped: {e}")
        if not _afl_pp_resolved:
            log.info(
                f"AFL single: {player or leg.player} {stat or leg.stat} "
                f"line={line} not carried in catalog on {bookie}:{sid} "
                f"(market={market}) — routing to manual (no blind POST)"
            )
            return (None, BetResult(
                success=False, tip=tip, session_id=sid, bookie=bookie,
                error=(f"afl player-prop not carried in catalog: "
                       f"{player or leg.player} {stat or leg.stat} line={line} "
                       f"— routing to manual"),
                timestamp=datetime.now(),
                placed_market=market, placed_player=player, placed_stat=stat,
                placed_line=line, placed_selection=selection,
                placed_leg_summary=_format_tip_placement_summary(tip),
            ))

    # ── Price check + within-1.0 line auto-adjust ───────────────────
    # Player props use the per-player markets_filter and look up by
    # player name. Team handicap (`line`) and totals (`total_points`) use
    # the same price endpoint but match by selection name (team) or
    # direction. Spurs case 2026-04-29: tip was -11.0, available was
    # -11.5, gap of 0.5 well within tolerance, but the player-prop guard
    # meant team handicap never got auto-adjusted and the bookie was
    # blocklisted. AFL player props are handled by the catalog block above,
    # so this within-1.0 path is NBA/NBL (and team markets) only.
    if market.startswith("player_") and player and (tip.sport or "").lower() != "afl":
        try:
            price_resp = hb.price_check_sports(
                session_id=sid, sport=tip.sport, event=tip.event,
                markets_filter=["player_props"],
            )
            if price_resp.get("success"):
                markets_data = price_resp.get("markets", {})
                # Direction-aware catalog match for O/U props (NBA matcher,
                # 2026-06-01): exact (player, side, line) else the nearest carried
                # line within ±1.0 ON THE SAME SIDE. Replaces the old player-only
                # closest-line snap, which matched by player ALONE and so could
                # snap the line — and capture the odds — from the OPPOSITE side
                # (feeding the wrong price to the odds-ceiling guard). The NBA O/U
                # catalog carries direct lines (no AFL-style half-line encoding),
                # so this only rewrites line/selection/odds, never the market.
                # Returns None for a bare-name (threshold) or directionless
                # selection -> falls through to the legacy player-only snap below,
                # so threshold props keep their existing behaviour (no regression).
                _nm = _match_nba_player_prop(
                    {"market": market, "selection": selection,
                     "player": player, "line": line},
                    markets_data,
                )
                if _nm:
                    if _nm["line"] != line or _nm["selection"] != selection:
                        log.info(
                            f"NBA catalog-matched: {player} {market} line={line} "
                            f"-> line={_nm['line']} sel='{_nm['selection']}' "
                            f"odds={_nm['odds']}"
                        )
                    line = _nm["line"]
                    selection = _nm["selection"]
                    _resolved_live_odds = _nm.get("odds")  # ceiling-checked below
                else:
                    # Legacy fallback (threshold / no-direction selections): match
                    # by player + exact line, else nearest within ±1.0. Safe here
                    # because threshold markets are over-only (no opposite side to
                    # confuse).
                    market_data = markets_data.get(market) or {}
                    selections = market_data.get("selections", [])
                    matching = [s for s in selections
                                if s.get("player", "").lower() == player.lower()
                                and (line is None or abs(float(s.get("line", 0)) - float(line)) < 0.01)]
                    if matching:
                        _resolved_live_odds = matching[0].get("odds")  # ceiling-checked below
                    if not matching and line is not None:
                        # Find closest line for this player
                        player_sels = [s for s in selections if s.get("player", "").lower() == player.lower()]
                        if player_sels:
                            closest = min(player_sels, key=lambda s: abs(float(s.get("line", 0)) - float(line)))
                            avail_line = float(closest.get("line", 0))
                            log.info(f"Price check: line {line} not found for {player}, closest is {avail_line}")
                            # If within 1 line, auto-adjust. Otherwise let HyperBot handle it.
                            if abs(avail_line - float(line)) <= 1.0:
                                log.info(f"Auto-adjusting line {line} -> {avail_line}")
                                line = avail_line
                                _resolved_live_odds = closest.get("odds")  # ceiling-checked below
        except Exception as e:
            log.debug(f"Price check skipped: {e}")

    elif market == "line" and line is not None:
        # Team HANDICAP: catalog-driven resolution (±0.5, all sports — Wilson
        # 2026-05-31). The standard `line` market carries only the main line
        # (~-0.5); a specific handicap (e.g. +50.5) lives in pick_own_line,
        # where the line is baked into the selection ("GWS GIANTS (+50.5)") and
        # matched by proposition_id. Match `line` first (±0.5), then fall back
        # to pick_own_line (±0.5). A catalog miss routes to manual; the blind
        # sign-flip / alt-line / line-move ladders are gated off once the
        # catalog has been consulted (see _try_place_with_name_variants and
        # _place_singles_v4), which stops the dozens-of-dead-retries churn
        # observed 2026-05-31 on "giants +50.5hc".
        # C4 (2026-05-31): mark the catalog as consulted UP FRONT — before the
        # price-check call — so that even if price_check_sports returns
        # success=False or throws, the blind sign-flip / alt-line / line-move
        # ladders stay gated off and a catalog miss (or a price-check outage)
        # routes to manual rather than placing the WRONG SIDE of the spread.
        # Previously this was set only inside the success block, so a failed or
        # throwing price-check left the blind ladders armed on a handicap.
        # v5.9x (#5): an AFL PERIOD handicap (quarter 1q-4q / 1st-half 1h) carries a
        # supported period code on the leg. Resolve the EXACT proposition_id from
        # the live catalog (SB-only) and place THAT; a miss / non-SB / price-check
        # failure returns a clean MANUAL result and NEVER falls back to the
        # full-game line (which would be the WRONG market — Wilson: "don't fuck up
        # the prop_id"). NO fuzzy. Gated by AFL_PERIOD_MARKETS_ENABLED (dormant by
        # default). Full-game handicaps take the unchanged `else` path below.
        _pcode = (_afl_period_code(getattr(leg, "period", ""))
                  if AFL_PERIOD_MARKETS_ENABLED else "")
        tip._hc_catalog_consulted = True
        if _pcode:
            # 07-12 review #3: a period handicap with NO tipster odds gives NO
            # price protection (the ceiling + floor are both inert when
            # suggested_odds<=1.0), so it could place at any live price. Require a
            # tipster price > 1.0 -> else manual (mirrors the Dello no-odds rule).
            if not (tip.suggested_odds and float(tip.suggested_odds) > 1.0):
                log.info(f"AFL period {_pcode} handicap '{selection}' has no tipster "
                         f"odds (>1.0) — no price protection -> manual")
                return (None, BetResult(
                    success=False, tip=tip, session_id=sid, bookie=bookie,
                    error=(f"AFL period {_pcode} handicap with no tipster odds "
                           f"(price-guard-less) — place manually"),
                    timestamp=datetime.now(),
                    placed_market=market, placed_player=player, placed_stat=stat,
                    placed_line=line, placed_selection=selection,
                    placed_leg_summary=_format_tip_placement_summary(tip),
                ))
            _pm = None
            if "sportsbet" not in str(bookie).lower():
                log.info(f"AFL period {_pcode} handicap '{selection}' — SB-only; "
                         f"{bookie} is not Sportsbet -> manual")
            else:
                try:
                    _event_pc = _bookie_event(tip.event, bookie, tip.sport)
                    price_resp = hb.price_check_sports(
                        session_id=sid, sport=tip.sport, event=_event_pc)
                    if price_resp.get("success"):
                        _pm = _resolve_afl_period_prop(
                            price_resp.get("markets") or {}, _pcode, selection, line)
                except Exception as e:
                    log.debug(f"AFL period catalog match skipped: {e}")
            if _pm:
                log.info(
                    f"AFL period catalog-matched: '{selection}' {_pcode} line={line}"
                    f" -> market={_pm['market']} prop={_pm['proposition_id']} "
                    f"odds={_pm['odds']}")
                market = _pm["market"]
                line = _pm["line"]
                _resolved_prop_id = _pm["proposition_id"]
                _resolved_live_odds = _pm.get("odds")  # ceiling/floor-checked below
            else:
                log.info(
                    f"AFL period {_pcode} handicap '{selection}' line={line} not "
                    f"carried EXACTLY on {bookie}:{sid} (no fuzzy) -> manual")
                return (None, BetResult(
                    success=False, tip=tip, session_id=sid, bookie=bookie,
                    error=(f"AFL period {_pcode} handicap not carried exactly "
                           f"('{selection}' {line}) — place manually"),
                    timestamp=datetime.now(),
                    placed_market=market, placed_player=player, placed_stat=stat,
                    placed_line=line, placed_selection=selection,
                    placed_leg_summary=_format_tip_placement_summary(tip),
                ))
        else:
            try:
                _event_pc = _bookie_event(tip.event, bookie, tip.sport)
                price_resp = hb.price_check_sports(
                    session_id=sid, sport=tip.sport, event=_event_pc,
                )
                if price_resp.get("success"):
                    _hm = _match_handicap_in_catalog(
                        selection, line, price_resp.get("markets") or {},
                    )
                    if _hm:
                        if _hm["market"] != market or _hm["line"] != line:
                            log.info(
                                f"Handicap catalog-matched: '{selection}' line={line} "
                                f"-> market={_hm['market']} sel='{_hm['selection']}' "
                                f"line={_hm['line']} odds={_hm['odds']}"
                            )
                        market = _hm["market"]
                        selection = _hm["selection"]
                        line = _hm["line"]  # None for pick_own_line (in-selection)
                        _resolved_prop_id = _hm.get("proposition_id")
                        _resolved_live_odds = _hm.get("odds")  # ceiling-checked below
                    else:
                        log.info(
                            f"Handicap '{selection}' line={line} not carried within "
                            f"±{_HC_LINE_TOLERANCE} on {bookie}:{sid} (line / "
                            f"pick_own_line) — routing to manual"
                        )
            except Exception as e:
                log.debug(f"Handicap catalog match skipped: {e}")

    elif market == "total_points" and line is not None:
        # v6.00 (#D): an AFL PERIOD total (1h/2h/1q-4q) carries a period code on the
        # leg -> resolve the EXACT proposition_id (first_half_total /
        # second_half_total / quarter_total) and place THAT; a miss / non-SB /
        # price-check fail -> MANUAL, never a full-game total fallback. Gated by
        # AFL_PERIOD_MARKETS_ENABLED. Requires a tipster price >1.0 (no price
        # protection otherwise — mirrors the period handicap rule at 07-12 review #3).
        _pcode_to = (_afl_period_code(getattr(leg, "period", ""))
                     if AFL_PERIOD_MARKETS_ENABLED else "")
        if _pcode_to:
            if not (tip.suggested_odds and float(tip.suggested_odds) > 1.0):
                log.info(f"AFL period {_pcode_to} total '{selection}' has no tipster "
                         f"odds (>1.0) — no price protection -> manual")
                return (None, BetResult(
                    success=False, tip=tip, session_id=sid, bookie=bookie,
                    error=(f"AFL period {_pcode_to} total with no tipster odds "
                           f"(price-guard-less) — place manually"),
                    timestamp=datetime.now(),
                    placed_market=market, placed_player=player, placed_stat=stat,
                    placed_line=line, placed_selection=selection,
                    placed_leg_summary=_format_tip_placement_summary(tip),
                ))
            _ptm = None
            if "sportsbet" not in str(bookie).lower():
                log.info(f"AFL period {_pcode_to} total '{selection}' — SB-only; "
                         f"{bookie} is not Sportsbet -> manual")
            else:
                try:
                    _event_pc = _bookie_event(tip.event, bookie, tip.sport)
                    price_resp = hb.price_check_sports(
                        session_id=sid, sport=tip.sport, event=_event_pc)
                    if price_resp.get("success"):
                        _ptm = _resolve_afl_period_total(
                            price_resp.get("markets") or {}, _pcode_to, selection, line)
                except Exception as e:
                    log.debug(f"AFL period total catalog match skipped: {e}")
            if _ptm:
                log.info(
                    f"AFL period TOTAL catalog-matched: '{selection}' {_pcode_to} "
                    f"line={line} -> market={_ptm['market']} "
                    f"prop={_ptm['proposition_id']} odds={_ptm['odds']}")
                market = _ptm["market"]
                line = _ptm["line"]
                _resolved_prop_id = _ptm["proposition_id"]
                _resolved_live_odds = _ptm.get("odds")  # ceiling/floor-checked below
            else:
                log.info(
                    f"AFL period {_pcode_to} total '{selection}' line={line} not "
                    f"carried EXACTLY on {bookie}:{sid} (no fuzzy) -> manual")
                return (None, BetResult(
                    success=False, tip=tip, session_id=sid, bookie=bookie,
                    error=(f"AFL period {_pcode_to} total not carried exactly "
                           f"('{selection}' {line}) — place manually"),
                    timestamp=datetime.now(),
                    placed_market=market, placed_player=player, placed_stat=stat,
                    placed_line=line, placed_selection=selection,
                    placed_leg_summary=_format_tip_placement_summary(tip),
                ))
        else:
            # Match TOTAL against the catalog: total_points main line (±1.0), then
            # pick_own_total alt lines (±0.5; line baked into the selection ->
            # proposition_id). Mirrors the line/pick_own_line handicap path so an
            # off-main total (e.g. Eddie's 172.5 vs a 166.5 main line) places at the
            # EXACT alt line instead of snapping to the wrong line / dying. A catalog
            # miss leaves the leg as-is -> placement fails -> manual. 2026-06-03.
            try:
                price_resp = hb.price_check_sports(
                    session_id=sid, sport=tip.sport, event=tip.event,
                )
                if price_resp.get("success"):
                    _tm = _match_total_in_catalog(
                        selection, line, price_resp.get("markets") or {},
                    )
                    if _tm:
                        if _tm["market"] != market or _tm["line"] != line:
                            log.info(
                                f"Total catalog-matched: '{selection}' line={line} "
                                f"-> market={_tm['market']} sel='{_tm['selection']}' "
                                f"line={_tm['line']} odds={_tm['odds']}"
                            )
                        market = _tm["market"]
                        selection = _tm["selection"]
                        line = _tm["line"]  # None for pick_own_total (in-selection)
                        _resolved_prop_id = _tm.get("proposition_id")
                        _resolved_live_odds = _tm.get("odds")  # ceiling-checked below
                    else:
                        log.info(
                            f"Total '{selection}' line={line} not carried "
                            f"(total_points ±1.0 / pick_own_total ±0.5) on "
                            f"{bookie}:{sid} — routing to manual"
                        )
            except Exception as e:
                log.debug(f"Total catalog match skipped: {e}")

    # ── Max-odds CEILING sanity check (all sports tipsters) ─────────
    # If the live (catalog/price-check) odds for the resolved selection are far
    # above the tipped odds (default >1.25×), it's almost always a WRONG
    # selection/line — do NOT place; route to manual. Only fires when we actually
    # captured live odds above; a missing price never blocks. Pairs with the 0.9×
    # target_odds floor. Racing has its own ceiling in racing_placer.
    if apply_ceiling and _resolved_live_odds and _exceeds_odds_ceiling(
        tip.tipster, tip.suggested_odds, _resolved_live_odds
    ):
        _mult = TIPSTERS_MAX_ODDS_MULT.get(tip.tipster, MAX_ODDS_MULT)
        log.warning(
            f"ODDS-CEILING: {tip.tipster} {leg.player or leg.team_full} "
            f"{selection} {line} {stat} — live odds {_resolved_live_odds} > tipped "
            f"{tip.suggested_odds} ×{_mult} on {bookie}:{sid}. Possible WRONG "
            f"selection; NOT placing, routing to manual."
        )
        return (None, BetResult(
            success=False, tip=tip, session_id=sid, bookie=bookie,
            error=(f"odds ceiling: live {_resolved_live_odds} > tipped "
                   f"{tip.suggested_odds} ×{_mult} (possible wrong selection)"),
            timestamp=datetime.now(),
            placed_market=market, placed_player=player, placed_stat=stat,
            placed_line=line, placed_selection=selection,
            placed_leg_summary=_format_tip_placement_summary(tip),
        ))

    # ── Min-odds FLOOR sanity check (all sports tipsters) ───────────
    # Symmetric to the ceiling: if the live odds are >10% BELOW the tipped price
    # (live < tipped × 0.9), the market has moved against us / it's likely the
    # wrong selection — route to manual with a PRICE reason rather than place at
    # a much shorter price. target_odds enforces this bookie-side too, but this
    # routes cleanly to manual with a clear reason instead of a bookie reject.
    # Only fires when we captured live odds (catalog match); a missing price
    # never blocks. Wilson 2026-06-03.
    # BUG C: single source of truth for the floor — _afl_target_odds (AFL >$2.00
    # = 15%, else 10%; non-AFL = 10%, identical to the old _below_odds_floor). The
    # AFL fan-out passes apply_floor=False so this gate is the NON-AFL price-moved
    # guard, but keying it on the same helper keeps the floor consistent if an AFL
    # leg is ever routed through this path.
    _floor_odds = _afl_target_odds(tip.sport, tip.suggested_odds)
    if apply_floor and _resolved_live_odds and _floor_odds and _resolved_live_odds < _floor_odds:
        log.warning(
            f"ODDS-FLOOR: {tip.tipster} {leg.player or leg.team_full} "
            f"{selection} {line} {stat} — live odds {_resolved_live_odds} < floor "
            f"{_floor_odds} (tipped {tip.suggested_odds}) on {bookie}:{sid}. Price "
            f"moved / possible wrong selection; NOT placing, routing to manual."
        )
        return (None, BetResult(
            success=False, tip=tip, session_id=sid, bookie=bookie,
            error=(f"price floor: live {_resolved_live_odds} < floor {_floor_odds} "
                   f"(tipped {tip.suggested_odds}; price moved / possible wrong selection)"),
            timestamp=datetime.now(),
            placed_market=market, placed_player=player, placed_stat=stat,
            placed_line=line, placed_selection=selection,
            placed_leg_summary=_format_tip_placement_summary(tip),
        ))

    # ── Empty selection AND player guard (fix F, 2026-06-01) ────────
    # place_single_sports_bet OMITS empty selection/player from the payload, so
    # if BOTH resolve empty we would POST a market + line with NOTHING
    # identifying what to bet on -> a malformed bet (the 2026-05-31 selection=""
    # 400s, and a latent "market with no selection" risk). Nothing legitimate
    # has both empty (team markets carry a selection; player props carry a
    # player), so route to manual rather than POST under-specified. h2h/line
    # backfill `selection` from the team name; total_points relies on the parser
    # — but this guard is market-AGNOSTIC, so it catches ANY market where BOTH
    # selection and player end up empty.
    if not (selection or "").strip() and not (player or "").strip():
        log.warning(
            f"empty selection AND player for {tip.event} {market} line={line} "
            f"on {bookie}:{sid} — nothing to bet; routing to manual (no POST)"
        )
        return (None, BetResult(
            success=False, tip=tip, session_id=sid, bookie=bookie,
            error=(f"empty selection and player ({market} line={line}) "
                   f"— routing to manual"),
            timestamp=datetime.now(),
            placed_market=market, placed_player=player, placed_stat=stat,
            placed_line=line, placed_selection=selection,
            placed_leg_summary=_format_tip_placement_summary(tip),
        ))

    # ── Resolution complete — hand back the catalog-matched fields ──
    # `live_odds` is the catalog price captured during resolution (None for
    # markets with no catalog odds, e.g. team h2h). The AFL fan-out sizes
    # liability off it when present so the cap is honoured against a near-live
    # price — at no extra API cost, since resolution already fetched it.
    return (
        {
            "market": market, "selection": selection, "player": player,
            "stat": stat, "line": line, "target_odds": target_odds,
            "proposition_id": _resolved_prop_id,
            "live_odds": _resolved_live_odds,
        },
        None,
    )


def _execute_bet(
    tip: ParsedTip, session: dict, stake: float, presolved: dict | None = None,
) -> BetResult:
    """Execute a single bet on a specific session.

    When `presolved` is provided (catalog-resolved fields from
    _resolve_single_for_placement), the per-account price check and odds
    guards are skipped and the resolved payload is POSTed directly — the AFL
    concurrent fan-out path (v5.11), where the line was resolved ONCE per
    bookie up front. When None (the default), the leg is resolved against the
    live catalog first, exactly as before — every existing caller
    (_try_place_with_name_variants, SGM/MLB/racing paths) is unchanged.
    """
    leg = tip.legs[0]
    sid = str(session["session_id"])
    bookie = session.get("bookie", "unknown")

    log.info(
        f"Placing on {bookie} (session {sid}): "
        f"{leg.player or leg.team_full} {leg.selection} {leg.line} {leg.stat} ${stake}"
    )

    if presolved is None:
        resolved, _manual = _resolve_single_for_placement(
            tip, session,
        )
        if _manual is not None:
            return _manual
    else:
        resolved = presolved

    market = resolved["market"]
    selection = resolved["selection"]
    player = resolved["player"]
    stat = resolved["stat"]
    line = resolved["line"]
    target_odds = resolved["target_odds"]
    _resolved_prop_id = resolved["proposition_id"]

    # ── Build and log payload ───────────────────────────────────────
    # Translate Squiggle-format event to bookmaker-specific format
    # (e.g. sportsbet AFL "Greater Western Sydney" -> "GWS Giants").
    # Internal logic (audit logs, notifications) keeps the Squiggle name.
    event_for_hb = _bookie_event(tip.event, bookie, tip.sport)

    # Mirror place_single_sports_bet's actual payload EXACTLY so the log never
    # misleads — empty selection/stat are OMITTED there, not sent as "".
    payload = {
        "session_id": sid,
        "category": "sports",
        "sport": tip.sport,
        "event": event_for_hb,
        "market": market,
        "stake": stake,
    }
    if selection:
        payload["selection"] = selection
    if player:
        payload["player"] = player
    if stat:
        payload["stat"] = stat
    if line is not None:
        payload["line"] = line
    if target_odds:
        payload["target_odds"] = target_odds
    if _resolved_prop_id:
        payload["proposition_id"] = _resolved_prop_id
    log.info(f"HyperBot payload: {json.dumps(payload)}")
    _afl_log_event(
        tip,
        f"PAYLOAD bookie={bookie} sid={sid} {json.dumps(payload)}",
    )

    # Capture per-placement bookie-side elapsed time so the success
    # alert can show "(N.Ns)" per account. Same pattern as racing.
    # Wraps only the HyperBot round-trip — local prep above is fast.
    import time as _time_mod
    _t_place_start = _time_mod.time()
    # v6.07 (HB API doc 1.7.10x): send the documented over/under `direction` and the
    # bookie-side `max_odds` ceiling. `direction` is defensive (HB already honours our
    # side via selection+proposition_id — 763/763 correct in production); `max_odds`
    # is a genuine new guard: the BOOKIE rejects a fill above our ceiling, which also
    # covers price drift between the price-check and the fill (our client-side
    # _exceeds_odds_ceiling can only judge the pre-placement catalog price). Both are
    # omitted when unavailable, so behaviour is unchanged in that case.
    _dir = (getattr(tip.legs[0], "selection", "") or "").strip().lower() if tip.legs else ""
    _direction = _dir if (HB_SEND_DIRECTION and _dir in ("over", "under")) else None
    _max_odds = _bookie_max_odds(tip.tipster, tip.suggested_odds, target_odds)
    resp = hb.place_single_sports_bet(
        session_id=sid,
        sport=tip.sport,
        event=event_for_hb,
        market=market,
        selection=selection,
        stake=stake,
        player=player,
        stat=stat,
        line=line,
        target_odds=target_odds,
        proposition_id=_resolved_prop_id,
        direction=_direction,
        max_odds=_max_odds,
    )
    _elapsed = round(_time_mod.time() - _t_place_start, 2)

    # v5.9x MAX-STAKE REBET (shared chokepoint for every sports-singles caller:
    # AFL fan-out, singles_v4, spillover, name-variants). On a Sportsbet
    # stake-too-high (538) reject the bookie now tells us the allowable max
    # (HB v1.7.85). Instead of returning a failure that ladders DOWN blindly
    # upstream, rebet ONCE right here at exactly that max and continue with THAT
    # response. Bounded strictly below the rejected `stake` (a too-HIGH reject
    # always is) so it can NEVER over-stake; ONE shot (no re-entry — we call the
    # placement primitive directly). Kill-switch SPORTSBET_MAX_STAKE_REBET.
    # BUG A (max-stake review 2026-07-12): ONLY rebet when the ORIGINAL reject
    # was FAST. A SLOW "stake too high" can be the Erasmus class (2026-05-03: a
    # 33s stake-too-high that ACTUALLY LANDED on Sportsbet) — HyperBot does NOT
    # tag such a completed-cid reject `ambiguous`; its ambiguity is derived
    # DOWNSTREAM by the elapsed>=threshold slow-rejection guard. If we rebet on a
    # slow 538 we (a) risk double-staking a bet that already landed and (b)
    # overwrite `_elapsed`/`resp`, defeating that downstream guard for the
    # original. So on a slow reject we SKIP the rebet and let the original slow
    # failure flow to the maybe-landed handling. Symmetric with the racing gate.
    _mx_target = _sb_max_stake_target(resp, bookie, stake)
    if _mx_target is not None and _elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC:
        log.warning(
            f"MAX-STAKE REBET SKIPPED: {bookie}:{sid} stake-too-high on ${stake:.2f} "
            f"came back SLOW ({_elapsed:.1f}s >= {STAKE_REJECT_LATENCY_THRESHOLD_SEC}s) "
            f"— treating as maybe-landed (Erasmus guard), NOT rebetting"
        )
        _mx_target = None
    if _mx_target is not None:
        log.info(
            f"MAX-STAKE REBET: {bookie}:{sid} stake-too-high on ${stake:.2f} "
            f"(SB max ${_mx_target:.2f}) — rebetting ONCE at max"
        )
        _afl_log_event(
            tip,
            f"MAXSTAKE-REBET bookie={bookie} sid={sid} "
            f"from=${stake:.2f} to=${_mx_target:.2f}",
        )
        _t_place_start = _time_mod.time()
        resp = hb.place_single_sports_bet(
            session_id=sid,
            sport=tip.sport,
            event=event_for_hb,
            market=market,
            selection=selection,
            stake=_mx_target,
            player=player,
            stat=stat,
            line=line,
            target_odds=target_odds,
            proposition_id=_resolved_prop_id,
        )
        _elapsed = round(_time_mod.time() - _t_place_start, 2)
        # Reassign `stake` so the auto-cap check + BetResult reflect the true
        # (max) amount we requested — not a spurious "auto-cap" vs the original.
        stake = _mx_target
        if resp.get("success"):
            log.info(f"MAX-STAKE REBET PLACED ${_mx_target:.2f} on {bookie}:{sid}")
        else:
            log.info(
                f"MAX-STAKE REBET at ${_mx_target:.2f} still failed on "
                f"{bookie}:{sid}: {(resp.get('error') or '')[:80]}"
            )

    if resp.get("success"):
        # AUTO-CAP detection: bookies (Neds/Ladbrokes esp.) silently
        # auto-cap to account liability limits without rejecting. Read
        # actual placed amount from response 'stake' field so spillover
        # ladders (v4 and v3.10 _place_with_spillover) can debit the
        # right amount and continue to the next session. Failure case
        # 2026-05-12 racing: tip 61783 CHARIVARI neds req=$600
        # actual=$416.67, TipBot reported full fill, never spilled
        # remainder. Sports path has same shape, so same fix.
        try:
            actual_stake = float(resp.get("stake", stake))
        except (TypeError, ValueError):
            actual_stake = stake
        if actual_stake <= 0:
            actual_stake = stake
        if abs(actual_stake - stake) > 1.0:
            log.warning(
                f"AUTO-CAP detected on {bookie}:{sid}: requested=${stake:.2f} "
                f"actual=${actual_stake:.2f} "
                f"(${stake - actual_stake:.2f} short, will spill to next)"
            )
        _afl_log_event(
            tip,
            f"PLACED bookie={bookie} sid={sid} bet_id={resp.get('bet_id')} "
            f"odds={resp.get('odds')} stake=${actual_stake:.2f}",
        )
        return BetResult(
            success=True, tip=tip, session_id=sid, bookie=bookie,
            bet_id=resp.get("bet_id"), odds=resp.get("odds"),
            stake=actual_stake, timestamp=datetime.now(),
            placed_leg_summary=_format_tip_placement_summary(tip),
            placed_market=market, placed_player=player, placed_stat=stat,
            placed_line=line, placed_selection=selection,
            elapsed_sec=_elapsed,
            correlation_id=resp.get("correlation_id"),
        )
    else:
        _afl_log_event(
            tip,
            f"FAILED bookie={bookie} sid={sid} stake=${stake:.2f} "
            f"error={(resp.get('error') or '')[:300]}",
            level="warning",
        )
        return BetResult(
            success=False, tip=tip, session_id=sid, bookie=bookie,
            error=resp.get("error", "Unknown error"), timestamp=datetime.now(),
            # v5.9x (max-stake review 2026-07-12, BUG C): carry the ACTUAL stake
            # attempted. After a max-stake rebet `stake` was reassigned to the
            # (smaller) SB max, so an ambiguous/maybe-landed reject debits the
            # amount truly at risk — not the original ladder stake — which would
            # otherwise over-debit `remaining` and under-fill the spillover.
            # Honest value on every failure (== the amount we tried on this leg).
            stake=stake,
            placed_leg_summary=_format_tip_placement_summary(tip),
            placed_market=market, placed_player=player, placed_stat=stat,
            placed_line=line, placed_selection=selection,
            elapsed_sec=_elapsed,
            # C5 (2026-05-31): copy the ambiguous flag set by hyperbot_client
            # (_post_v3_async tags a FAST-failing /place_bet POST ambiguous=True
            # because the bet may have landed at the bookie before the connection
            # dropped — a fast ambiguous that the >=5s elapsed guard would miss).
            # The slow-rejection handlers also trigger on result.is_ambiguous.
            is_ambiguous=bool(resp.get("ambiguous", False)),
            # v6.07 (sweep HIGH #1/#5): carry the STRUCTURAL unresolved-cid marker so
            # Guard B never downgrades a maybe-landed bet to a clean not-placed.
            cid_unresolved=bool(resp.get("cid_unresolved", False)),
            # v5.9x: bookie-stated allowable max stake on a stake-too-high (538)
            # reject (Sportsbet, HB v1.7.85). Read by the max-stake rebet so a
            # capped account fills at exactly this instead of laddering blindly.
            max_stake=resp.get("max_stake"),
            # Carried so ambiguous-outcome alerts include the server-side cid
            # for manual reconciliation / future /v3/transactions lookup.
            correlation_id=resp.get("correlation_id"),
        )


# ── Audit Logging ───────────────────────────────────────────────────

def _audit_tip(tip: ParsedTip, msg_time: datetime):
    """Log tip to audit JSONL."""
    # v5.43/v5.76: redirect to a temp file under TIPBOT_TESTING (via the shared
    # _audit_log_path resolver) so the unit suite never pollutes the production
    # logs/audit.jsonl (same class as the bet_ledger X1/X2 fix).
    audit_path = _audit_log_path()
    if tip.legs and tip.legs[0].market == "player_prop":
        payload = {
            "player": tip.legs[0].player,
            "stat": tip.legs[0].stat,
            "line": tip.legs[0].line,
            "selection": tip.legs[0].selection,
            "stake": tip.stake_dollars,
            "legs": [
                {"market": l.market, "player": l.player, "stat": l.stat,
                 "line": l.line, "selection": l.selection}
                for l in tip.legs
            ],
        }
    else:
        payload = {"raw": tip.raw_message, "stake": tip.stake_dollars}

    _log_jsonl(audit_path, {
        "msg_sent_at": str(msg_time),
        "tipster": tip.tipster,
        "is_sgm": tip.is_sgm,
        "alert_only": tip.alert_only,
        "stake": tip.stake_dollars,
        "payload": payload,
        "raw_message": tip.raw_message,
    })


# ── Shook Message Buffer ────────────────────────────────────────────

# Rolling buffer: last 15 messages, max 10 min old
_shook_buffer: list[tuple[float, str]] = []
SHOOK_BUFFER_SIZE = 15
SHOOK_BUFFER_AGE = 600

# Cooldown between Shook triggers. Prevents every bet-pattern message in the
# 10-min @everyone window from firing a separate Groq task (causes duplicates
# and wastes API quota). 30s is short enough to catch genuine follow-ups
# (Shook rarely posts two distinct props within 30s) while stopping rapid-fire.
_shook_last_trigger_ts: float = 0.0
# v5.69 (m10): the normalised bet-content of the LAST trigger, so the cooldown
# can suppress only a REPEAT of the same bet (the announcement+bet double-fire)
# and NOT silently drop a genuinely distinct second prop posted within 30s.
_shook_last_trigger_bet: str = ""

# Dedupe set for the bot_id-mismatch diagnostic. One log line per
# (chat_id, sender_id) per process lifetime. Reset on restart.
_unexpected_senders_seen: set[tuple[int, int]] = set()

# Noise phrases — split into two classes by behaviour.
#
# PREAMBLE noise: always disqualifying, regardless of where it appears.
# These are announcement / recap / disclaimer phrases that change the
# whole meaning of the message. "Recap of Vassell M 12.5 P (won)" must
# never re-place even though it contains valid-looking bet content.
# Scanned across the whole message text.
SHOOK_NOISE_PREAMBLE = [
    "prop coming",
    "just got news",
    "let me check",
    "and ill give",
    "SL Futures",
    "posting early",
    "won't touch any recaps",
    "wont touch any recaps",
    "recap",
    "for people who",
]

# TAIL-OK noise: conversational filler / wait-conditions that Shook
# routinely appends to real tips. "Vassell M 12.5 P. will keep refreshing
# if other books update" is a legitimate tip with a wait-condition tail.
# Scanned only against the LEAD of the message (everything before the
# bet-content match). Appearing after bet content is fine.
# Wembanyama L 1.5 3s Made on 2026-05-21 10:07 was dropped silently
# because "will keep refreshing" appeared as a tail on a real tip and
# the old whole-text check killed it.
SHOOK_NOISE_TAIL_OK = [
    "will keep refreshing",
    "keep refreshing",
    "will monitor",
    "as always",
    "gonna go lay down",
    "but noting",
    "im fine with",
]

# Trigger: @everyone message with bet-like content
SHOOK_BET_RE = re.compile(
    r"(?:"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z\-]+)+\s+[ML]\s+\d+"  # Player M/L line
    r"|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+[+-]\d+"        # Team spread
    r"|Money\s*Line"                                       # Money Line
    r"|\bML\b.*-\d+"                                       # ML with odds
    r"|Double\s+Double"                                    # Double Double
    r"|[Oo]ver\s+\d+\.?\d*"                               # Over X.5
    r"|[Uu]nder\s+\d+\.?\d*"                              # Under X.5
    r"|\b[ML]\s+\d+\.?\d*\s+(?:P|Points?|PRA|PR|PA|Reb)"  # M 15.5 PRA
    r")",
    re.IGNORECASE,
)


def _shook_buffer_add(text: str):
    """Add message to Shook rolling buffer."""
    now = time.time()
    _shook_buffer.append((now, text))
    # Trim old/excess
    cutoff = now - SHOOK_BUFFER_AGE
    while len(_shook_buffer) > SHOOK_BUFFER_SIZE:
        _shook_buffer.pop(0)
    while _shook_buffer and _shook_buffer[0][0] < cutoff:
        _shook_buffer.pop(0)


def _shook_get_context() -> str:
    """Get recent buffer messages as context string."""
    if not _shook_buffer:
        return ""
    lines = [text for _, text in _shook_buffer[-8:]]
    return "\n---\n".join(lines)


def _shook_should_process(text: str) -> bool:
    """Check if a Shook message should trigger Groq processing.

    Shook's actual posting pattern burns the @everyone ping on the
    announcement message ("@everyone prop coming MGM, DK..."), which
    is then filtered out by the preamble noise list. The follow-up
    message with the actual prop often does NOT repeat @everyone.
    Requiring @everyone in the current text caused 2026-05-09 misses on
    Mitchell Robinson (07:51), Holmgren (14:52), and Reaves (14:52).

    Trigger when bet content is detected AND either:
    - The current message has @everyone, OR
    - A recent buffer message has @everyone (buffer is already age-
      trimmed to 10 min by SHOOK_BUFFER_AGE).

    Noise filter has two classes:
    - PREAMBLE phrases ("prop coming", "recap", ...) scanned whole-text
      because they change the meaning of the whole message regardless
      of position.
    - TAIL-OK phrases ("will keep refreshing", "im fine with", ...)
      scanned only in the LEAD (text before the bet-content match)
      because they routinely appear as tails on real tips. Failure
      case: Wembanyama L 1.5 3s Made 2026-05-21 10:07, dropped
      silently because "will keep refreshing" appeared after the bet
      content in the same message.
    """
    bet_match = SHOOK_BET_RE.search(text)
    if not bet_match:
        # No bet content — nothing to trigger on regardless of noise.
        return False

    text_lower = text.lower()

    # Preamble noise: scan whole text. "Recap of Vassell M 12.5 P (won)"
    # must fail here even though the bet regex matches it.
    for noise in SHOOK_NOISE_PREAMBLE:
        if noise.lower() in text_lower:
            return False

    # Tail-OK noise: scan only the LEAD of the message (everything before
    # the bet content match). Lets real tips with wait-condition tails
    # through while catching "as always, here's a recap" style preambles.
    lead = text[: bet_match.start()].lower()
    for noise in SHOOK_NOISE_TAIL_OK:
        if noise.lower() in lead:
            return False

    # @everyone presence: current message OR anywhere in recent buffer
    if "@everyone" in text:
        _everyone_present = True
    else:
        _everyone_present = any("@everyone" in buf_text for _, buf_text in _shook_buffer)

    if not _everyone_present:
        return False

    # Cooldown: suppress duplicate triggers within 30 seconds. Shook often
    # posts an @everyone announcement followed immediately by the bet text;
    # without this, both messages trigger separate Groq tasks and place the
    # same bet twice.
    # v5.69 (m10): the cooldown is CONTENT-AWARE — it suppresses only a REPEAT
    # of the same bet content. A genuinely DISTINCT second prop within 30s used
    # to be silently dropped (buffered but never dispatched); now it triggers.
    global _shook_last_trigger_ts, _shook_last_trigger_bet
    _bet_key = re.sub(r"\s+", " ", (bet_match.group(0) or "").strip().lower())
    if time.time() - _shook_last_trigger_ts < 30:
        if _bet_key and _bet_key == _shook_last_trigger_bet:
            # Same bet content within the window — the announcement+bet
            # double-trigger this guard exists for. Suppress.
            return False
        log.info(
            f"Shook: distinct bet content within 30s cooldown "
            f"('{_bet_key[:40]}' vs last '{_shook_last_trigger_bet[:40]}') — "
            f"processing as a new prop, not suppressing"
        )

    _shook_last_trigger_ts = time.time()
    _shook_last_trigger_bet = _bet_key
    return True


# ── Tip Processor ───────────────────────────────────────────────────

async def _process_tip(text: str, tipster: str, sport: str,
                       unit_size: float, default_units: float,
                       msg_time, channel_name: str, skip_dedup: bool = False):
    """Process a single tip message through full pipeline with timing.

    skip_dedup (v5.9x, self-bet channel): bypass the 10-min duplicate guard so
    Wilson can intentionally re-bet the same selection from his own bet channel
    as many times as he likes. Default False (every real tipster keeps dedup)."""
    pipeline_start = time.time()

    try:
        tips, timing = route_message(text, tipster, sport, unit_size, default_units)
    except Exception as e:
        log.exception(f"Parser error: {e}")
        _log_jsonl(ERROR_LOG, {
            "type": "parse_exception", "tipster": channel_name,
            "message": text, "error": str(e),
        })
        notifier.notify_parse_error(channel_name, text, str(e))
        return 0

    if not tips:
        log.debug("No tips found in message")
        return 0

    log.info(
        f"Parsed {len(tips)} tip(s) from {channel_name} via {timing.get('parser', '?')} "
        f"[groq={timing.get('groq_parse', 0):.2f}s, regex={timing.get('regex_parse', 0):.2f}s]"
    )

    # Recover from Groq misparsing a "/" SGM as multiple singles. Catches
    # the case where same-batch same-player different-stat tips arrive,
    # promotes them back into a single SGM tip BEFORE alt-merge runs (so
    # the alt-merge doesn't collapse them into primary+alt instead).
    tips = _promote_misparsed_sgms(tips)

    # Shook-specific: a single message often lists multiple option lines
    # for the same player ("Holmgren M 17.5 P+A. 16.5 P > 25.5 P+R > 27.5
    # PRA"). The first-mentioned is the preferred play; the rest are
    # alternatives Wilson does NOT want auto-placed. Drop the rest.
    # Different stats so _merge_batch_alts won't catch them. 2026-05-07
    # Holmgren regression: 3 separate plays became a 3-leg SGM at $80
    # instead of one $400 single on the preferred line.
    tips = _dedupe_shook_same_player(tips)

    # Merge same-player repeats from the same tipster into primary + alts.
    # This catches the Shook-style "main prop + mentioned alts" where Groq
    # parses each mention as a separate tip.
    tips = _merge_batch_alts(tips)

    # MLB HRRBI 2+ -> 2-leg same-player SGM (verified correlation edge). Runs
    # LAST so it operates on the final tip list and produces a clean SGM with
    # no alt fallback (Wilson 2026-06-01: bet only the actual HRRBI play). Only
    # the 2+ HRRBI line is transformed; other MLB tips stay singles -> manual.
    tips = _mlb_hrrbi_to_sgm(tips)

    # Force-bookie tipsters (EasyMoneyAFL = sportsbet): stamp suggested_bookie so
    # the placement alert shows the bookie and the soft/legacy filters agree with
    # the HARD lock in _v4_get_active_sessions_unfiltered. The capper's own
    # bookmaker (e.g. Tabtouch) is intentionally overridden — Wilson places these
    # on Sportsbet only.
    _forced_bookie = TIPSTERS_FORCE_BOOKIE.get(tipster)
    if _forced_bookie:
        for _t in tips:
            _t.suggested_bookie = _forced_bookie

    # v5.9x (self-bet single-ticket guard, 07-12 review #2/#3): a self-bet message
    # is exactly ONE ticket. If the parser split it into >1 tip (a compound
    # "X and Y" selection, or an SGM the LLM broke into separate singles), placing
    # N bets each at the full capped stake would blow past the $1500 total cap ->
    # route to manual instead. Wilson re-sends a clean single-line self-bet.
    if tipster == "self_bet" and len(tips) > 1:
        log.warning(
            f"[self-bet] parsed into {len(tips)} tips -> manual (one message = one "
            f"ticket; would exceed the ${SELF_BET_MAX_STAKE:.0f} total cap): {text[:160]}")
        try:
            notifier.notify_image_alert(
                channel_name,
                f"SELF-BET parsed into {len(tips)} bets (expected 1) -> place "
                f"manually: {text[:200]}")
        except Exception:
            pass
        return 0

    for tip in tips:
        tip.timestamp = msg_time

        # v5.37: per-step timing carrier. t0 = the exact moment this message
        # arrived at the handler (pipeline_start), parse_sec = the Groq+regex
        # parse phase. place_tip ADDS resolve_sec; the AFL/ETR fan-outs ADD
        # price_check_sec. The notifier renders one reconciling end-to-end line
        # (parse / resolve / price-check / bookies / other) that SUMS to
        # time.time()-t0. All tips on this path share the one parse phase.
        tip._timing = {
            "t0": pipeline_start,
            "parse_sec": round(
                (timing.get("groq_parse", 0) or 0)
                + (timing.get("regex_parse", 0) or 0), 3),
        }

        # Cap at max units
        if tip.units > MAX_UNITS:
            notifier.notify_info(
                f"Unit cap: {tip.tipster} tipped {tip.units}u, capped to {MAX_UNITS}u\n"
                f"Raw: {tip.raw_message[:200]}"
            )
            tip.units = MAX_UNITS

        # MLB flat stake (ignore the recommended unit; $1 while gated, prod $).
        _apply_mlb_flat_stake(tip)
        _apply_saiyan_sgm_unit(tip)   # Saiyan SGM -> 750/u (250 ea x3); no-op otherwise
        _apply_dello_flat_stake(tip)  # Dello -> flat $ (test $1 / prod DELLO_UNIT_SIZE); no-op otherwise
        # v5.9x (07-12 review #2): self-bet -> flat 1u x (capped) $ so a stray unit
        # token in the free-text selection can't multiply the total past the cap.
        _apply_self_bet_flat_stake(tip)

        # Image-tip channels: pin to $1/unit while IMAGE_TIPS_TEST_MODE is on.
        # No-op for every non-image tipster; a safety belt in case an image
        # channel's text ever reaches the text pipeline (images route via
        # _process_image_tip, which also applies this).
        _apply_image_test_stake(tip)

        # Dupe detection: same tipster + same bet fingerprint within 10 min.
        # Capture the fp BEFORE place_tip — placement mutates legs (catalog match
        # rewrites a leg selection), so a post-place fp wouldn't match a re-send's
        # pre-place fp and the dedup would be defeated (v5.49 James Wood HRRBI).
        _dupe_fp = f"{tip.tipster}::{_tip_fingerprint(tip)}"
        if not skip_dedup and _is_duplicate(tip):
            log.info(f"DUPE detected, skipping: {tip.tipster} {_tip_fingerprint(tip)}")
            continue
        if skip_dedup:
            log.info(f"[{tip.tipster}] self-bet: dedup bypassed (intentional re-bet allowed)")

        # No-unit gate: aus/kev tips MUST carry an explicit unit to be a bet.
        # Without one we'd default the stake and place a bet the capper never
        # actually sized — route to manual instead (Wilson 2026-06-04).
        # v5.52 belt: units_explicit alone is NOT trusted any more — Groq
        # invented a unit on 2026-06-11 ("nothin today ... none quite get to
        # my price threshold" still placed Spurs ML $400). The RAW message
        # must also contain a literal unit token ("1U -", "- 1 unit").
        if tip.tipster in UNITS_REQUIRED_TIPSTERS and (
            not getattr(tip, "units_explicit", True)
            or not _raw_has_unit_token(tip.raw_message)
        ):
            log.info(
                f"No-unit gate: {tip.tipster} tip has no explicit unit/stake "
                f"in the raw message -> manual (not placing). "
                f"Raw: {tip.raw_message[:120]}"
            )
            tip.alert_only = True
            tip.alert_reason = "no unit/stake specified by the tipster — place manually"

        # v5.52 braces: explicit no-bet framing routes ALL tips parsed from the
        # message to manual ("nothin today", "no bets today", "none quite",
        # "close to bets but no" — on 2026-06-11 these were near-miss lines,
        # explicitly NOT bets, yet one placed $400). Runs AFTER the belt so its
        # specific alert_reason wins if both fire.
        # v5.69 (i4): applied to ALL UNITS_REQUIRED_TIPSTERS (was ausbets-only),
        # since kev_nba can frame a message the same way.
        if tip.tipster in UNITS_REQUIRED_TIPSTERS and _is_no_bet_framing(tip.raw_message):
            log.info(
                f"No-bet framing: {tip.tipster} message says these are NOT "
                f"bets -> manual. Raw: {tip.raw_message[:120]}"
            )
            tip.alert_only = True
            tip.alert_reason = (
                "tipster framed this message as NO bet ('nothin today' / "
                "'none quite' phrasing) — review manually, do not place"
            )

        # Time the resolve + place step
        resolve_start = time.time()
        _audit_tip(tip, msg_time)

        # v6.07 (sweep #15): False until place_tip is about to run, so the except
        # handler can tell a pre-placement crash (safe to re-open for a re-send) from a
        # post-placement one (bets may be ON -> keep the claim, never double-stake).
        _place_attempted = False
        try:
            # v6.03: CLAIM the dedup fingerprint BEFORE we yield to the executor.
            # Dedup lives on the loop thread only; offloading place_tip introduces a
            # real await (yield) between the _is_duplicate check above and here, so
            # two rapid identical re-sends could BOTH pass the check before either
            # registered -> double fan-out. Claiming now (no await between the check
            # and this point) closes that race. This is EXACTLY equivalent to the old
            # register-on-landed: we RELEASE below if nothing landed. Mirrors the
            # racing image path's claim-before (_route_image_racing_tips) + tiptitans
            # _claim_bet. self-bet (skip_dedup) never claims (Wilson re-bets on
            # purpose). A CancelledError during the await is BaseException (NOT caught
            # by `except Exception` below), so the claim is deliberately RETAINED on
            # cancellation: the offloaded place_tip may still land, and retaining
            # blocks a double-stake re-send.
            if not skip_dedup:
                _register_tip_fingerprint(tip, fp=_dupe_fp)
                _inflight_fps.add(_dupe_fp)  # v6.03: block re-send for the WHOLE flight
            # v6.07 (sweep #15): from here on a crash may follow a LANDED bet, so the
            # except below must NOT release the re-send claim. See it for the detail.
            _place_attempted = True
            if PLACEMENT_OFFLOAD_ENABLED:
                results = await _run_in_placement_executor(place_tip, tip)
            else:
                results = place_tip(tip)
            if not skip_dedup:
                # placement finished -> no longer in-flight; the _recent_tips claim
                # (refreshed/popped below) now governs the post-completion 10-min window.
                _inflight_fps.discard(_dupe_fp)
            resolve_time = time.time() - resolve_start

            any_success = any(r.success for r in results)
            if not skip_dedup:
                if any_success or any(_is_ambiguous_result(r) for r in results):
                    # landed/ambiguous: REFRESH the claim to COMPLETION time (mirrors the
                    # image PASS-3 refresh). A >600s flight (the HB-meltdown this bundle
                    # targets) could have aged the claim-time stamp past DUPE_WINDOW_SECS;
                    # without this refresh a re-send arriving JUST after completion would be
                    # in neither _inflight_fps (discarded above) nor _recent_tips (purged)
                    # -> double fan-out onto accounts where attempt #1 may have landed.
                    _register_tip_fingerprint(tip, fp=_dupe_fp)
                else:
                    # clean total failure: RELEASE the claim (re-sendable), preserves the
                    # v5.13 "re-send after a clean fail" semantics.
                    _recent_tips.pop(_dupe_fp, None)

            for r in results:
                if r.success:
                    total_time = time.time() - pipeline_start
                    log.info(
                        f"PLACED: {r.bet_id} on {r.bookie} @ {r.odds} "
                        f"[total={total_time:.2f}s, parse={timing.get('groq_parse', 0):.2f}s, "
                        f"resolve+place={resolve_time:.2f}s]"
                    )
                elif getattr(r, "is_intermediate", False):
                    # Suppress: ladder-step or retry failure that wasn't the
                    # final outcome on its session. The session either
                    # ultimately succeeded (so this rejection is noise) or
                    # already has a "final" failure logged below. Sicily AFL
                    # 2026-04-30 produced 11 spurious "FAILED: stake too high"
                    # warnings before this filter.
                    log.debug(f"Intermediate ladder failure (suppressed): {r.error}")
                else:
                    log.warning(f"FAILED: {r.error}")
        except Exception as e:
            # v6.03: non-cancellation crash -> RELEASE the claim to preserve today's
            # "re-send allowed after a clean crash" behaviour (a place_tip exception
            # is almost always a pre-placement resolve/parse crash, nothing fired;
            # per-account errors inside the fan-out come back as failed BetResults,
            # not exceptions). A CancelledError is NOT caught here, so a cancelled
            # placement keeps BOTH claims (see the claim-before comment above).
            #
            # v6.07 (sweep #15): that "nothing fired" rationale holds only for a crash
            # BEFORE place_tip. This try also spans the POST-placement bookkeeping
            # (ledger write, notifier calls, jsonl), so a disk-full / locked-file /
            # notifier raise AFTER the bets are on used to release the claim too, and a
            # re-send inside DUPE_WINDOW_SECS would then re-fan-out onto accounts that
            # already hold the bet = DOUBLE STAKE. _place_attempted splits the two: a
            # provably pre-placement crash still re-opens the tip; a post-placement one
            # KEEPS the claim (mirroring the image PASS-3 rule).
            if not skip_dedup:
                _inflight_fps.discard(_dupe_fp)
                if not _place_attempted:
                    _recent_tips.pop(_dupe_fp, None)
                else:
                    log.warning(
                        "Crash occurred AFTER placement was attempted: KEEPING the "
                        "dedup claim so a re-send cannot double-stake a landed bet"
                    )
            log.exception(f"Bet placement error: {e}")
            _log_jsonl(ERROR_LOG, {
                "type": "placement_exception", "tipster": tip.tipster,
                "message": tip.raw_message, "error": str(e),
            })
            notifier.notify_parse_error(tip.tipster, tip.raw_message, str(e))

    # v5.9x: number of tips this message produced (0 on parse-fail/empty). The
    # Eddie caption fallback reads this to decide whether the caption was handled
    # or should still ping manual (07-12 review #5 — no silent drop).
    return len(tips)


# ── Image-tip processing (vision-parsed channels) ───────────────────
# Eddie's Bets AFL, Zak Trussell SA Racing, The Trial Sniper post tips as
# IMAGES. The handler downloads the image and calls _process_image_tip, which
# runs groq_parser.parse_tip_image (Scout vision) then routes each extracted
# tip: racing -> the racing pipeline (process_image_racing_tip ->
# place_racing_tip), AFL -> the sports pipeline (place_tip, Sportsbet-locked).
# $1/unit while IMAGE_TIPS_TEST_MODE. Anything unparseable / unresolvable /
# unplaceable -> manual (image alert or place_tip's own manual routing).

_AFL_IMAGE_STAT_ALIASES = {
    "disposal": "disposals", "disposals": "disposals", "disp": "disposals",
    "goal": "goals", "goals": "goals",
    "mark": "marks", "marks": "marks",
    "tackle": "tackles", "tackles": "tackles",
    "kick": "kicks", "kicks": "kicks",
    "handball": "handballs", "handballs": "handballs",
    "clearance": "clearances", "clearances": "clearances",
    "hitout": "hitouts", "hitouts": "hitouts", "hit out": "hitouts",
    "fantasy": "fantasy_points", "fantasy_points": "fantasy_points",
    "fantasy point": "fantasy_points", "fantasy points": "fantasy_points",
    "afl fantasy": "fantasy_points",
}


def _normalise_afl_image_stat(stat) -> str:
    s = (stat or "").strip().lower()
    return _AFL_IMAGE_STAT_ALIASES.get(s, s)


# Track-name aliases: the name a tipster/image uses -> the name bookies carry.
# Some SA venues are written differently by tipsters vs bookies — Zak writes
# "Morphettville Parks" / "Morphettville Park" for the venue bookies list as
# "Morphettville". Keyed lowercased. Runner-matching is the safety net if a
# venue genuinely splits courses on the same day (wrong course -> the tipped
# runner isn't in that race -> no bet -> manual). 2026-06-03.
RACING_TRACK_ALIASES = {
    "morphettville parks": "Morphettville",
    "morphettville park": "Morphettville",
}


def _normalise_racing_track(track):
    """Map a tipster/image track name to the bookie-carried name via
    RACING_TRACK_ALIASES (e.g. 'Morphettville Parks' -> 'Morphettville').
    Unknown tracks pass through unchanged."""
    if not track:
        return track
    return RACING_TRACK_ALIASES.get(track.strip().lower(), track)


def _img_coerce_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _img_coerce_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


# Word-boundary keywords that mark a text-only post in an IMAGE channel as
# actionable (a bet or an instruction worth a manual ping) rather than chatter.
# Kept deliberately generous on INSTRUCTION words (scratch/late-mail/etc.) since
# the cost of dropping one is the operator leaving a bet they should've pulled;
# the cost of keeping chatter is just a ping. Bet TYPES + race/odds/stake
# patterns are handled separately below.
_IMAGE_ACTIONABLE_KEYWORDS = (
    # instructions / changes (NEVER drop a pull-the-bet / line-change message —
    # the cost of missing a scratching is a bet you should've pulled).
    "scratch", "scratched", "scratching", "non runner", "non-runner",
    "late mail", "mail", "update", "remove", "removed", "cancel", "cancelled",
    "off the", "adding", "added",
    # bet TYPES — a concrete bet STRUCTURE, not betting vocabulary.
    "each way", "e/w", "multi", "double", "treble", "quaddie", "quadrella",
    "trifecta", "quinella", "exacta", "exotic", "first 4", "first four",
    "sgm", "srm", "same race",
    # v5.77 (Wilson 2026-06-20): REMOVED the soft betting-VOCABULARY words that
    # fire on commentary, NOT a tip — "odds"/"tip"/"tips"/"back"/"lay"/"selection"/
    # "selections"/"runner"/"runners"/"to win"/"the win"/"the place"/"add". They
    # pinged manual on Eddie chatter ("Just running the ODDS for the last bets",
    # "Not sure many would've picked X last night", 06-19). A REAL text tip still
    # pings via a concrete selection pattern (race/$/units/decimal-odds/X+) or a
    # bet-type/instruction above — so no real tip is missed (tips carry structure;
    # the actual play is posted as an IMAGE anyway). "add" dropped too ("add me");
    # "adding"/"added" kept (leg changes). The image channel is the primary path.
)
# Matched as whole words (\b...\b) so "multi" doesn't fire on "multiple",
# "tip" doesn't fire on inflections we didn't mean, etc.
_IMAGE_ACTIONABLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _IMAGE_ACTIONABLE_KEYWORDS) + r")\b"
)

# v5.52 announcement-guard (2026-06-12): text-only posts that merely ANNOUNCE
# that tips are coming ("Going to send out match plays soon", "x2 bets coming")
# kept slipping through as actionable (Eddie 2026-06-11 17:39 manual ping)
# because announcements often brush a keyword ("tips"/"update"/"double"/"odds")
# or a stray decimal. Checked BEFORE the keyword list in
# _image_text_is_actionable: an announcement phrase classifies the post as
# chatter UNLESS it also carries an URGENT instruction (scratchings /
# cancellations - never drop a pull-the-bet message) or a CONCRETE selection
# pattern (R4 / $50 / 2u / 2.50 / "race 5"), which must still ping (Eddie) or
# parse-and-place (Zak/Trial racing text tips).
_IMAGE_ANNOUNCEMENT_PHRASES = (
    "going to send", "gonna send", "about to send", "will be sending",
    "sending out", "send out", "sending through", "sending a couple",
    "plays soon", "plays shortly", "plays coming", "play coming",
    "picks coming", "pick coming", "bets coming", "bet coming",
    "more coming", "coming soon", "coming shortly",
    "coming through soon", "coming through shortly",
)
_IMAGE_ANNOUNCEMENT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _IMAGE_ANNOUNCEMENT_PHRASES) + r")\b"
)
# Urgent instructions that must ALWAYS surface, even inside an announcement
# post ("plays coming soon - and scratch the R3 runner" must still ping).
_IMAGE_URGENT_INSTRUCTION_RE = re.compile(
    r"\b(?:scratch|scratched|scratching|non runner|non-runner|late mail|"
    r"cancel|cancelled|remove|removed)\b"
)

# v5.58 (Wilson 2026-06-13): summary/recap posts in image channels (Zak's
# end-of-day results image + "GL all / next meet ..." wrap-ups) parse to 0
# tips BY DESIGN — suppress the 0-tip manual ping when the caption says so.
# The ping STAYS for unexplained 0-tip parses (the safety net for a real tip
# image the vision model fumbled).
_IMAGE_SUMMARY_RE = re.compile(
    r"\b(?:summary|recap|results?|resulted|wrap(?:ped)?|gl all|"
    r"good luck all|next meet|that'?s it for|done for the (?:day|night))\b"
)

# BUG B (Wilson 2026-06-21): post-game RECAP / COMMENTARY about bets already
# taken — distinct multi-word signatures so a forward tip ("R4 Lingani 2u") is
# never suppressed. These posts brush a stray score/odds/number and used to
# FALSE-PING manual ("Not the best weekend on the cores, but have copped some
# tough beats..."; "Have taken these combos as SGMs. Could be better odds
# elsewhere."). Eddie's real tips arrive as IMAGES; text is supplementary.
_IMAGE_RECAP_RE = re.compile(
    r"\b(?:not the best|tough beat|bad beat|rough (?:night|day|one|trot)|"
    r"have (?:taken|copped)|i'?ve taken|taken these|took these|"
    r"already (?:taken|on these)|copped (?:a|some|the)|"
    r"better odds elsewhere|could be better odds|better elsewhere|"
    # v5.92 (2026-07-03): weekend P&L wrap-up signatures. The 06-28 "Meh weekend,
    # some losses involved (-5.8u by my calculations)" + 07-02 "2-2 night with the
    # late add cooking us" fell through to the units pattern (the -5.8u P&L read as
    # a stake) -> false manual ping. These phrases never appear in a forward tip.
    r"meh (?:weekend|round|week)|by my calculation|stunk it up|"
    # v5.92 review: require a DASH between the score digits (so "2-2 night" matches
    # but a real "30 night ..." does not) + dropped the "cooking us" alt (it
    # collided with the forward idiom "cooking us a multi"; "2-2 night" already
    # catches the 07-02 recap).
    r"\d\s?[-–]\s?\d\s+night)\b",
    re.IGNORECASE,
)


def _doc_mime_is_image(mime: str) -> bool:
    """True when a Telegram DOCUMENT post is actually an image sent as a file
    (mime image/*) and therefore vision-parseable. PDFs / videos / anything
    else are NOT — Zak posts a PDF DUPE of his already-posted tip images
    (Wilson 2026-06-13; the 10:24 Groq 400 was a PDF hitting the vision API),
    so those drop with a log line instead of a manual ping."""
    return (mime or "").lower().startswith("image/")


def _image_text_selection_pattern(t: str) -> bool:
    """Racing/odds/stake patterns that imply a CONCRETE selection (not just
    talk about betting). Shared by _image_text_is_actionable's tail check and
    the v5.52 announcement-guard (a 'coming soon' post that names R4 @ 2.50
    is a real tip, not an announcement). Expects pre-lowercased text."""
    if re.search(r"\br\d{1,2}\b", t):            # race code R1..R12
        return True
    if re.search(r"\$\s*\d", t):                 # $50
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s*u\b", t):   # 0.5u / 2u units
        return True
    # v5.77: X+ threshold (a strong tip signal — "25+ disposals", "2+ goals").
    if re.search(r"\b\d{1,2}\+", t):
        return True
    # v5.77: an OVER/UNDER LINE ("over 30", "under 23.5", "o30", "u23") — a concrete
    # player-prop selection even with no unit/decimal-odds. Keeps a real text tip
    # like "Back Bont o30 disposals" pinging after 'back'/'over'/'under' were
    # dropped from the keyword list, WITHOUT re-pinging chatter ("u18s" / "go30"
    # don't match — the trailing word char / leading non-boundary fails \b).
    if re.search(r"\b(?:over|under)\s+\d{1,3}(?:\.\d)?\b", t):
        return True
    if re.search(r"\b[ou]\d{1,2}(?:\.\d)?\b", t):
        return True
    # decimal odds e.g. 2.50 — but NOT a CLOCK TIME ("8.30 am", "8-8.30 AM SA
    # time" — 06-19 "eyeing a goal scorer pick for around 8-8.30 AM" pinged manual
    # because 8.30 matched as odds). A decimal followed by am/pm is a time, skip it.
    for _m in re.finditer(r"\b\d{1,3}\.\d{1,2}\b", t):
        if re.match(r"\s*(?:am|pm)\b", t[_m.end():_m.end() + 6]):
            continue
        return True
    if re.search(r"\brace\s*\d", t):             # "race 5"
        return True
    return False


_IMG_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_IMG_MONTHS = {}
for _mi, _mn in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1):
    _IMG_MONTHS[_mn] = _mi
    _IMG_MONTHS[_mn[:3]] = _mi


def _au_east_day(dt) -> "_date":
    """The post's AUSTRALIAN-EASTERN calendar date, DST-correct.

    v6.07 (sweep #25). Telethon's `event.date` is tz-aware UTC, so the local calendar
    day must be derived before it can be used to pick a race MEETING. Three sites did
    this by hand and one of them didn't do it at all:
      * `_img_parse_racing_date` and the Leroy path added a hardcoded `+10h`
      * the trackless Zak/Trial meeting resolver used the RAW UTC date, so a tip posted
        00:00-10:00 AEST (= 14:00-24:00 UTC the previous day) asked the resolver about
        YESTERDAY's meetings -> wrong or empty track. That is a 10-hour window every
        day, and it covers exactly the morning slot these tipsters post in.
    The `+10h` hardcode is also wrong under AEDT (UTC+11, roughly Oct-Apr), which
    mis-dates the 00:00-00:59 local hour for about half the year.

    A NAIVE datetime is already server-local, so it is returned as-is. Falls back to
    the old +10h behaviour if the tz database is unavailable (Windows needs `tzdata`),
    so this can never be worse than what it replaces."""
    if dt is None:
        return _date.today()
    try:
        if getattr(dt, "tzinfo", None) is None:
            return dt.date()
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo("Australia/Sydney")).date()
        except Exception:
            return (dt + _timedelta(hours=10)).date()
    except Exception:
        return _date.today()


def _img_parse_racing_date(raw_date, msg_time) -> "str|None":
    """Convert a vision-extracted race date/day to ISO YYYY-MM-DD, anchored on
    the post time. Returns None when absent/unparseable — the caller's
    price-check then defaults to today (pre-existing behaviour).

    WHY: ante-post image tips (Zak posts Saturday's SA card mid-week) were
    price-checked against TODAY because the date wasn't carried — wrong day ->
    'race not in catalog' -> manual, and worse, a same-numbered race on the
    wrong day could match and place on the WRONG race. Handles ISO, D/M[/Y]
    (AU order), 'June 6'/'6 June', weekday names ('Saturday'/'Sat' -> the next
    such day on/after the post), and today/tomorrow. 2026-06-03."""
    s = (raw_date or "").strip().lower()
    if not s:
        return None
    # Anchor on the post's Australian-Eastern calendar date (event.date is UTC).
    # v6.07: via _au_east_day, which is DST-correct (the old hardcoded +10h mis-dated
    # the 00:00-00:59 local hour throughout AEDT).
    base = _au_east_day(msg_time)
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    if "today" in s:
        return base.isoformat()
    if "tomorrow" in s:
        return (base + _timedelta(days=1)).isoformat()
    for name, wd in _IMG_WEEKDAYS.items():
        if re.search(rf"\b{name}\b", s):
            return (base + _timedelta(days=(wd - base.weekday()) % 7)).isoformat()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else base.year
        if yr < 100:
            yr += 2000
        try:
            return _date(yr, mo, d).isoformat()
        except ValueError:
            return None
    m = (re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\b", s)
         or re.search(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", s))
    if m:
        g1, g2 = m.group(1), m.group(2)
        d, mon_name = (int(g1), g2) if g1.isdigit() else (int(g2), g1)
        mo = _IMG_MONTHS.get(mon_name) or _IMG_MONTHS.get(mon_name[:3])
        if mo:
            try:
                return _date(base.year, mo, d).isoformat()
            except ValueError:
                return None
    return None


def _image_text_is_actionable(text: str, sport: str = "") -> bool:
    """True if a text-only post in an image-tip channel looks like a bet or an
    instruction (keep -> manual ping / parse); False for plain chatter (drop).

    These channels post the actual TIP as an image (always processed); text
    posts are supplementary, so we only surface the ones that carry betting
    intent. 2026-06-03: previously EVERY text post pinged manual, flooding it
    with 'thanks lads' / 'good luck' / emoji chatter from Zak & Trial.

    `sport`: the channel's sport. The BUG B recap guard (which drops a recap even
    when it brushes a stray score/odds) applies ONLY to NON-racing channels
    (Eddie AFL — text only pings manual, so dropping a numbered recap is the
    whole point). For RACING (Zak/Trial) the text path PARSES+PLACES real money
    and has its OWN results guard (_text_looks_like_result) + a runner-not-found
    drop downstream, so the recap guard is SKIPPED here to avoid silently
    vanishing a real forward tip whose text happens to contain a recap word
    ('R4 7 Lingani 2u for the next meet'). v5.86 review BLOCKER."""
    t = (text or "").strip().lower()
    if not t:
        return False
    # v5.52 announcement-guard: "tips are coming soon" posts are chatter even
    # when they brush an actionable keyword - unless they also carry an urgent
    # instruction or a concrete selection (see _IMAGE_ANNOUNCEMENT_PHRASES).
    if (_IMAGE_ANNOUNCEMENT_RE.search(t)
            and not _IMAGE_URGENT_INSTRUCTION_RE.search(t)
            and not _image_text_selection_pattern(t)):
        return False
    # BUG B (Wilson 2026-06-21): a RECAP / results / post-game commentary post
    # (which brushes a stray score/odds and used to false-ping manual) is
    # chatter — drop it UNLESS it ALSO carries an urgent instruction (never drop
    # a "scratch the R3 leg" message). Unlike the announcement guard, there is
    # NO concrete-selection exception: recaps routinely contain stray
    # numbers/odds, which is exactly why they false-fired. v5.86 review BLOCKER:
    # SKIP this for RACING — that text path PARSES+PLACES real money and already
    # has its own results guard, so a recap word in a real forward tip
    # ('R4 7 Lingani 2u for the next meet') must NOT be silently dropped here.
    if ((sport or "").lower() != "racing"
            and (_IMAGE_SUMMARY_RE.search(t) or _IMAGE_RECAP_RE.search(t))
            and not _IMAGE_URGENT_INSTRUCTION_RE.search(t)):
        return False
    if _IMAGE_ACTIONABLE_RE.search(t):
        return True
    # Racing/odds/stake patterns that imply a concrete selection.
    return _image_text_selection_pattern(t)


# v5.59 (Wilson 2026-06-13): per-tipster track memory for forward-fill. Zak
# posts his card as an IMAGE (track named) then follow-up tips as TEXT that
# just say "R1 8. My Zephyr 0.1u" — no track. Today (08:50) those parsed fine
# (2 tips) but routed to MANUAL as "? R1"/"? R5" because the track was
# unknown. Remember the last track each racing tipster's tips carried (image
# or text, fresh within the TTL) and forward-fill a missing track. A WRONG
# fill is caught downstream: the runner/saddle won't match the wrong track's
# card -> price-check fails -> manual, i.e. never worse than today. Filled
# tips are flagged in event_title + a WARNING log. Event-loop only (both the
# image and text racing paths build dicts on the asyncio loop) — no lock,
# same as _racing_recent_fps.
_tipster_last_track: dict = {}  # tipster -> (track, epoch_seen)
_TRACK_FILL_TTL_SEC = 12 * 3600  # same race day; stale memory never fills


def _build_racing_tip_dict(raw: dict, tipster: str, default_units: float, idx: int,
                           msg_time=None) -> dict:
    """Build a racing parsed_tip dict (place_racing_tip shape) from one raw
    vision-extracted racing dict. Adds synthetic id/titan/event_title/title
    for logging + Tip-Titans-style notifications."""
    track = _normalise_racing_track(raw.get("track")) or None
    track_inferred = False
    track_claude_resolved = False  # v5.80/81: track came from the Claude web-search resolver
    if track:
        _tipster_last_track[tipster] = (track, time.time())
    else:
        _prev = _tipster_last_track.get(tipster)
        if _prev and (time.time() - _prev[1]) < _TRACK_FILL_TTL_SEC:
            track = _prev[0]
            track_inferred = True
            log.warning(
                f"[{tipster}] tip has NO track — forward-filled '{track}' from "
                f"this tipster's last tip ({(time.time() - _prev[1]) / 60:.0f}m "
                f"ago). A wrong fill fails the card match downstream -> manual."
            )
    race_num = _img_coerce_int(raw.get("race"))
    saddle = _img_coerce_int(raw.get("saddle"))
    runner = (raw.get("runner") or "").strip()
    odds = _img_coerce_float(raw.get("odds")) or 0.0
    units = _img_coerce_float(raw.get("units"))
    if units is None or units <= 0:
        units = float(default_units)
    market = (raw.get("market") or "win").strip().lower()
    if market not in ("win", "place"):
        market = "win"
    titan = {"zak_racing": "ZAK", "trial_sniper": "TRIAL"}.get(
        tipster, (tipster or "IMG").upper()[:5]
    )
    # Zak & Trial Sniper tip THOROUGHBREDS (a distinct discipline from the
    # harness tracks in sessions.yaml — same-named tracks like Bunbury have
    # both). Tag the discipline so racing_placer caps these against the flat
    # per-account `racing.thoroughbreds` cap (win 1000 / place 500) and BYPASSES
    # the per-track harness caps. Day-before tips have no MBL.
    discipline = "thoroughbred" if tipster in ("zak_racing", "trial_sniper") else ""
    # Race-type code (R=thoroughbred, H=harness, G=greyhound). Vision rarely
    # prints it. For Zak/Trial THOROUGHBRED tipsters, default an empty type to
    # "(R)" so bookies can disambiguate MULTI-DISCIPLINE venues: Geelong has BOTH
    # a thoroughbred and a greyhound track, and an empty type made 5/6 bookies
    # miss Geelong R1 (sportsbet 'race_type_mismatch', others 'not in catalog');
    # only tabtouch matched (2026-06-04). 2026-06-04.
    race_type = (raw.get("race_type") or "").strip()
    if not race_type and discipline == "thoroughbred":
        race_type = "(R)"

    # v5.80/v5.81 RECOVERY (track=None): the THOROUGHBRED image tipsters (Zak =
    # SA-only; Trial Sniper = any AU track) can give a runner+race with NO track
    # (forward-fill empty). Ask Claude (web-search, Sonnet) which meeting runs
    # that runner today. ONLY the track is taken -> the existing price-shop still
    # matches the runner NUMBER+NAME on that card + applies the odds floor, and
    # the resolved-track flag DISABLES the saddle-only fallback (NAME-or-manual),
    # so a wrong/hallucinated track fails the card match -> manual (never a
    # wrong-track bet). Zak uses the SA-scoped lookup; Trial uses the general
    # runner-name lookup (the OC-harness / racenet style — name is the anchor,
    # corrects the race#).
    if (track is None and tipster in ("zak_racing", "trial_sniper")
            and runner and race_num is not None
            and tip_parser._claude_websearch_enabled()):
        try:
            import claude_parser
            import datetime as _dt
            # v6.07 (sweep #25A): was msg_time.strftime(...) on the RAW UTC datetime, so
            # a tip posted 00:00-10:00 AEST asked the resolver about YESTERDAY's
            # meetings -> wrong/empty track -> a recoverable tip lost to manual (worst
            # for trial_sniper, which has no Zak-style STAGE-1B catalog re-probe).
            _date_str = _au_east_day(msg_time).isoformat()
            _resolved = None
            if tipster == "zak_racing":
                # Zak is SA-only -> pass the SA thoroughbred candidate tracks so the
                # web-search is a fast CONSTRAINED lookup (not the broad open-web
                # search that timed out on 06-27 'Pure Bliss'), and RETRY once if the
                # first call returns empty (one slow turn shouldn't doom a trackless
                # SA tip). The list is a HINT only (no hard exclusion); a wrong
                # resolved track still fails the NAME match downstream
                # (track_claude_resolved disables saddle-only) + the odds floor ->
                # manual, never a wrong-track bet.
                # v6.06: shared SA candidate list (config) so the upstream resolver and
                # the racing_placer live-catalog probe (STAGE 1B) stay in sync.
                _sa_tracks = SA_THOROUGHBRED_TRACKS
                _resolved = claude_parser.resolve_sa_track_today(
                    race_num, runner, _date_str, candidate_tracks=_sa_tracks)
                if not _resolved:
                    _resolved = claude_parser.resolve_sa_track_today(
                        race_num, runner, _date_str, candidate_tracks=_sa_tracks)
            else:  # trial_sniper — general runner-name -> track (+ name-anchored race#)
                _info = claude_parser.resolve_racing_runner(runner, race_num, _date_str)
                if _info.get("track"):
                    _resolved = _info["track"]
                    if _info.get("race_num"):
                        race_num = _info["race_num"]
            if _resolved:
                track = _normalise_racing_track(_resolved) or _resolved
                track_inferred = True
                track_claude_resolved = True  # forces NAME-or-manual in racing_placer (no saddle-only)
                log.warning(
                    f"[{tipster}] track=None -> resolved track '{track}' R{race_num} for "
                    f"{runner} (web-search OR SA-calendar fallback; see claude_parser log "
                    f"for which). NAME match REQUIRED downstream (saddle-only disabled); a "
                    f"Zak SA miss is re-probed against the LIVE catalog in racing_placer "
                    f"(STAGE 1B) -> wrong track = manual, never a wrong-track bet."
                )
        except Exception as e:
            log.error(f"Claude track resolve failed for {tipster}: {e}")

    return {
        "id": f"img-{tipster}-{race_num}-{saddle}-{idx}",
        "titan": titan,
        "discipline": discipline,
        "track": track,
        "track_inferred": track_inferred,  # v5.59: forward-filled from memory
        "track_claude_resolved": track_claude_resolved,  # v5.80/81: NAME-or-manual gate
        "race_num": race_num,
        "race_type": race_type,
        "runner": runner,
        "saddle": saddle,
        "market": market,
        "units": units,
        "tipster_odds": odds,
        # Carry the meeting date from the image (ante-post tips are for a future
        # day; null defaults to today downstream). 2026-06-03.
        "date": _img_parse_racing_date(raw.get("date"), msg_time),
        # Synthetic fields the racing notifier reads off the raw tip:
        "event_title": (
            f"{track or '?'}"
            f"{' (track inferred)' if track_inferred else ''} "
            f"R{race_num if race_num is not None else '?'}"
        ),
        "title": f"{saddle if saddle is not None else '?'}. {runner or '?'}",
    }


async def _route_image_racing_tips(raw_tips: list, tipster: str,
                                   channel_name: str, unit_size: float,
                                   default_units: float, msg_time,
                                   pipeline_start: float = None,
                                   parse_sec: float = None) -> None:
    """Route vision-extracted RACING tips (Zak/Trial) to the racing pipeline.

    Stake = units (capped at IMAGE_RACING_MAX_UNITS=3u) × unit_size, unless
    IMAGE_RACING_TEST_MODE is on (then $1/u). v5.15: previously the production
    branch multiplied by default_units (the channel's 1.0 field) instead of
    unit_size, so it never actually staked at the configured size — fixed.

    v5.37: pipeline_start (image/text arrival t0) + parse_sec (vision/text parse)
    are threaded into process_image_racing_tip -> notify_tiptitans_placed so the
    auto-place racing summary shows a TRUE end-to-end (arrival -> placement) with
    the parse split — same reconciling line as the sports paths.
    """
    from tiptitans_processor import process_image_racing_tip
    _phase_timing = None
    if pipeline_start is not None:
        _phase_timing = {"t0": pipeline_start, "parse_sec": round(parse_sec or 0.0, 3)}

    # ── PASS 1 (SEQUENTIAL, on the event loop): guards + forward-fill + dedup ──
    # FIX C (2026-07-05): the forward-fill (last_race_num) and the _racing_recent_fps
    # check-then-register are ORDER-DEPENDENT and have NO await between the check and
    # the write, so they MUST stay single-threaded here. Only the slow placement
    # (PASS 2) is parallelised. Mirrors _route_image_afl_tips (v5.91).
    jobs = []  # [(idx, parsed, intended_stake)]
    last_race_num = None  # forward-fill across selections grouped under a race
    for idx, raw in enumerate(raw_tips):
        try:
            saddle = _img_coerce_int(raw.get("saddle"))
            runner = (raw.get("runner") or "").strip()
            odds = _img_coerce_float(raw.get("odds"))
            race_num = _img_coerce_int(raw.get("race"))

            # Guard 1: phantom header row (no selection at all) -> skip (don't
            # let it touch the forward-fill state).
            if saddle is None and not runner and not odds:
                log.info(f"[{channel_name}] skipping non-tip row {idx} (no saddle/runner/odds)")
                continue

            # Forward-fill the race number: Zak/Trial list 2+ selections under
            # ONE race heading and the 2nd+ row often has a null race# (Jofra in
            # Morphettville R7, 2026-06-03 — was dropped to manual). Inherit the
            # last-seen race# so the extra selection still places. Safe: if the
            # inherit is wrong the tipped runner won't be in that race -> no
            # price -> manual (runner-match guard).
            if race_num is None and last_race_num is not None:
                race_num = last_race_num
                raw = {**raw, "race": race_num}
                log.info(f"[{channel_name}] racing tip {idx} missing race# -> "
                         f"inherited R{race_num} from the preceding selection")
            if race_num is not None:
                last_race_num = race_num

            parsed = _build_racing_tip_dict(raw, tipster, default_units, idx, msg_time)

            # Guard 2: no race number (and none to inherit) -> can't price-check
            # the right race safely. Route to manual rather than risk the wrong race.
            if race_num is None:
                log.info(f"[{channel_name}] racing tip {idx} missing race# -> manual")
                notifier.notify_image_alert(
                    channel_name,
                    f"(manual: missing race number) {parsed['title']} @ "
                    f"{parsed['tipster_odds'] or '?'} {parsed['track'] or ''}".strip(),
                )
                continue

            # Guard 2b (v6.07, sweep #22): the row has NO runner NAME. Guard 1 only
            # skips a row with no saddle AND no runner AND no odds, so a cropped horse
            # name on an otherwise-complete row ({"saddle":7,"runner":null,"odds":4.2})
            # sailed through to racing_placer as runner="" and BOUND A HORSE ANYWAY:
            # Pass 2's substring test matches every runner ("" in rn is always True)
            # and Pass 3 binds saddle #N with no name cross-check. The TEXT racing
            # parsers strip these rows; the IMAGE parsers do not. racing_placer now
            # refuses to price a nameless tip as well (defence in depth) — this arm
            # makes it a LOUD manual instead of a silent no-price.
            if not runner:
                log.warning(f"[{channel_name}] racing tip {idx} has NO runner name -> manual")
                notifier.notify_image_alert(
                    channel_name,
                    f"(manual: no runner NAME read) #{parsed.get('saddle') or '?'} "
                    f"{parsed['track'] or '?'} R{parsed['race_num']} @ "
                    f"{parsed['tipster_odds'] or '?'}".strip(),
                )
                continue

            # v5.22 DEDUP (image + text racing): a reposted / re-delivered tip must
            # NOT double-place real money — the racing path previously had NO dedup.
            # Fingerprint the selection; skip a repeat within DUPE_WINDOW_SECS.
            # Registered BEFORE placing so a rapid re-delivery during the ~seconds of
            # placement is also caught (a failed tip won't auto-retry within the
            # window — acceptable, it routes to manual anyway). FIX C: this
            # check-then-register runs on the event loop with NO await between the
            # scan and the write, so two identical selections in ONE post cannot
            # both build a job even though PASS 2 places concurrently.
            _rfp = (tipster, (parsed.get("track") or "").lower(), parsed.get("race_num"),
                    (parsed.get("runner") or "").lower(), parsed.get("saddle"),
                    parsed.get("market"), parsed.get("date"))
            _rnow = datetime.now()
            for _rk in [k for k, t in _racing_recent_fps.items()
                        if (_rnow - t).total_seconds() > DUPE_WINDOW_SECS]:
                del _racing_recent_fps[_rk]
            if _rfp in _racing_recent_fps:
                log.info(f"[{channel_name}] DUPLICATE racing tip (seen <{DUPE_WINDOW_SECS}s "
                         f"ago) -> skipped (no double-bet): {parsed.get('runner')} "
                         f"{parsed.get('track')} R{parsed.get('race_num')}")
                continue
            _racing_recent_fps[_rfp] = _rnow

            # Stake = units × unit_size, capped at the DEDICATED racing-image
            # cap (Zak/Trial max 3u — survives a global MAX_UNITS bump) AND the
            # global MAX_UNITS — the per-runner typo/wrong-parse guard. While
            # IMAGE_RACING_TEST_MODE is on, fall back to the $1/u test stake
            # (Zak/Trial-specific gate; does NOT affect Eddie AFL). Enforced
            # here in the placing process by design (the $600 lesson). v5.15:
            # use unit_size, not default_units (the old bug capped prod to ~$1).
            units_capped = min(parsed["units"], MAX_UNITS, IMAGE_RACING_MAX_UNITS)
            if IMAGE_RACING_TEST_MODE:
                intended_stake = round(units_capped * IMAGE_TIPS_TEST_UNIT_SIZE, 2)
            else:
                intended_stake = round(units_capped * float(unit_size), 2)

            jobs.append((idx, parsed, intended_stake))
        except Exception as e:
            log.exception(f"[{channel_name}] racing image tip {idx} build crashed: {e}")
            try:
                notifier.notify_image_alert(
                    channel_name, f"(racing tip {idx} error: {e}) — place manually"
                )
            except Exception:
                pass

    if not jobs:
        log.info(f"[{channel_name}] no placeable racing tips after guards")
        return

    # ── PASS 2: PLACE. Concurrent when >1 job + flag on. Each job is a DISTINCT
    # selection; all downstream shared placement state is thread/async-safe:
    #   * racing_placer._pace_* keyed per session_id (SAME-account bets still
    #     stagger 5-10s; DIFFERENT accounts overlap) — _pace_lock;
    #   * circuit-breaker _cb_* — _cb_lock; tiptitans _claim_bet — _fps_lock;
    #   * place_racing_tip's used_session_ids is a LOCAL set per call.
    # Bounded by IMAGE_RACING_MAX_CONCURRENCY; one tip crashing never kills siblings.
    # IMAGE_RACING_CONCURRENT=false reverts to the serial loop (kill-switch).
    sem = asyncio.Semaphore(max(1, IMAGE_RACING_MAX_CONCURRENCY))

    async def _place_racing_job(job):
        _idx, _parsed, _stake = job
        async with sem:
            try:
                await process_image_racing_tip(
                    _parsed, _stake, hb, notifier,
                    source=channel_name, test_mode=IMAGE_RACING_TEST_MODE,
                    phase_timing=_phase_timing,
                )
            except Exception as e:
                log.exception(f"[{channel_name}] racing image tip {_idx} crashed: {e}")
                try:
                    notifier.notify_image_alert(
                        channel_name, f"(racing tip {_idx} error: {e}) — place manually"
                    )
                except Exception:
                    pass

    if IMAGE_RACING_CONCURRENT and len(jobs) > 1:
        log.info(f"[{channel_name}] placing {len(jobs)} racing image tips CONCURRENTLY "
                 f"(<= {max(1, IMAGE_RACING_MAX_CONCURRENCY)} at once)")
        await asyncio.gather(*(_place_racing_job(j) for j in jobs))
    else:
        for j in jobs:
            await _place_racing_job(j)


def _afl_disambiguate_surname_by_odds(scoped: list, game_labels: list, token: str,
                                      stat: str, line, side, tip_odds,
                                      odds_tol: float = 0.20):
    """Break a same-surname collision using the live catalog + the tip's odds.

    `scoped` is the list of same-surname candidates ({"name","team"}) on the
    in-play teams (len >= 2). For each candidate we look up their catalog
    proposition for the tipped stat+line+side (via _match_afl_player_prop on a
    single-session price_check_sports catalog) and compare the catalog odds to
    the tip's quoted odds (`tip_odds`). The candidate whose catalog price is
    within `odds_tol` (fractional, default 20%) of the tip price is the bet.

    Resolution rule (v5.77, Wilson 2026-06-20):
      - resolve to the IN-RANGE (within odds_tol of tip) candidate whose catalog
        odds is CLOSEST to the tip price; ZERO in range -> None (manual); a genuine
        EQUIDISTANT tie between the top two -> None (manual). WAS: require EVERY
        candidate priced AND exactly one in range -> bailed to manual when a
        same-surname sibling simply had no market for the prop (06-19 Noah Anderson
        o28.5 disposals priced while defender Cody Anderson had no such line).
    Also returns None on any missing context (no tip odds / stat / line / side),
    no owned sportsbet session, or a price-check failure. Returns (name, team)
    or None.

    NOTE: this is the SECONDARY tie-break only — it runs AFTER game-scoping has
    already collapsed league-wide ambiguity, so the pool is the same-surname
    players on the two teams playing now (typically exactly 2)."""
    # Require full tip context + a usable tip price; otherwise we cannot do an
    # odds tie-break -> manual (the conservative default).
    try:
        tip_odds_f = float(tip_odds or 0)
    except (TypeError, ValueError):
        tip_odds_f = 0.0
    if tip_odds_f <= 1.0 or not stat or line is None or not side:
        log.info(
            f"Eddie surname '{token}': collision, no odds tie-break possible "
            f"(stat={stat!r} line={line!r} side={side!r} tip_odds={tip_odds}) -> manual"
        )
        return None
    side_l = (side or "").strip().lower()
    if side_l not in ("over", "under"):
        log.info(f"Eddie surname '{token}': collision, side {side!r} not over/under -> manual")
        return None
    # An owned SPORTSBET session for the catalog probe (Eddie is sportsbet-locked).
    try:
        sb_sids = [
            str(s.get("session_id", "")) for s in (hb.get_sessions() or [])
            if (s.get("bookie", "") or "").lower() == "sportsbet"
            and _is_owned_session(s.get("session_id", ""))
        ]
    except Exception as e:
        log.warning(f"Eddie surname collision: get_sessions failed: {e}")
        return None
    if not sb_sids:
        log.info(f"Eddie surname '{token}': collision, no owned sportsbet session for catalog -> manual")
        return None
    sid = sb_sids[0]
    in_range = []   # (name, team, catalog_odds)
    probed = 0      # candidates we actually got a catalog price for
    for cand in scoped:
        name, team = cand.get("name"), cand.get("team", "")
        event = resolve_afl_event(team) or ""
        if not event:
            continue
        try:
            event_pc = _bookie_event(event, "sportsbet", "afl")
            pc = hb.price_check_sports(
                session_id=sid, sport="afl", event=event_pc,
                markets_filter=["player_props"],
            )
        except Exception as e:
            log.debug(f"Eddie surname collision: price_check_sports failed for {name}: {e}")
            continue
        if not pc.get("success"):
            continue
        leg_dict = {
            "market": _AFL_OU_MARKET_BY_STAT.get((stat or "").lower(), ""),
            "selection": side_l, "player": name,
            "stat": (stat or "").lower(), "line": line,
        }
        m = _match_afl_player_prop(leg_dict, pc.get("markets") or {})
        if not m:
            continue
        try:
            cat_odds = float(m.get("odds") or 0)
        except (TypeError, ValueError):
            cat_odds = 0.0
        if cat_odds <= 1.0:
            continue
        probed += 1
        if abs(cat_odds - tip_odds_f) / tip_odds_f <= odds_tol:
            in_range.append((name, team, cat_odds))
            log.info(
                f"Eddie surname '{token}' collision: candidate '{name}' ({team}) "
                f"catalog {side_l} {line} {stat} @ {cat_odds} within {odds_tol:.0%} "
                f"of tip {tip_odds_f}"
            )
        else:
            log.info(
                f"Eddie surname '{token}' collision: candidate '{name}' ({team}) "
                f"catalog @ {cat_odds} OUTSIDE {odds_tol:.0%} of tip {tip_odds_f} -> excluded"
            )
    # v5.77 (Wilson 2026-06-20): resolve to the candidate whose catalog odds is
    # CLOSEST to the tipped odds among those in range. PRIOR rule required EVERY
    # same-surname candidate to be priced AND exactly one in range — so it bailed
    # to manual when a same-surname sibling simply had no market for the tipped
    # prop (06-19 Noah Anderson o28.5 disposals @1.84 priced — the obvious bet for
    # 28.5 disposals — while defender Cody Anderson had no such line; tie-break
    # "inconclusive" -> manual). Now: 0 in range -> manual; else pick the in-range
    # candidate nearest the tip price; a genuine TIE (two equidistant) -> manual.
    # The 20% odds_tol still gates which candidates are even eligible, and the
    # placement odds-guard (target = 0.9x tipped) backstops a bad price.
    if not in_range:
        log.info(
            f"Eddie surname '{token}' collision odds tie-break inconclusive "
            f"(0 of {len(scoped)} candidates priced within {odds_tol:.0%} of "
            f"tip {tip_odds_f}; priced {probed}/{len(scoped)}) -> manual"
        )
        return None
    in_range.sort(key=lambda t: abs(t[2] - tip_odds_f))
    if (len(in_range) >= 2
            and abs(abs(in_range[0][2] - tip_odds_f)
                    - abs(in_range[1][2] - tip_odds_f)) < 0.01):
        log.info(
            f"Eddie surname '{token}' collision: top 2 candidates EQUIDISTANT "
            f"from tip {tip_odds_f} -> manual"
        )
        return None
    name, team, cat_odds = in_range[0]
    log.info(
        f"Eddie surname-collision RESOLVED '{token}' -> '{name}' ({team}) via odds "
        f"tie-break (catalog @ {cat_odds} CLOSEST to tip {tip_odds_f}; "
        f"{len(in_range)}/{len(scoped)} in range, priced {probed}/{len(scoped)}) "
        f"[{'; '.join(game_labels)}]"
    )
    return name, team


# Eddie posts bare-surname props anywhere from ~30 min to ~2 h before bounce, not
# reliably at the 45-min mark. v5.32 (Wilson): widen the game-scan LOOK-AHEAD to 2
# HOURS so the surname still scopes to the right team when the game is up to 2 h
# out — the 2026-06-07 14:24 batch (Mills / Wilson / McInerney / Owens) all went
# to manual with "no AFL game in the start window" because the games were >45 min
# away. In-progress games (the behind window) are unchanged. A wider window can
# surface MORE same-surname candidates, but the odds tie-break + the
# manual-on-any-ambiguity safety below still apply (never guess a $400 bet).
EDDIE_GAME_LOOKAHEAD_SEC = 7200  # 2 hours (was the resolver default 2700 = 45 min)

# v5.77 (Wilson 2026-06-20): when the EXACT bare-surname match misses on the
# in-play teams (a vision typo — 06-19 "D'Ambrossio" vs roster "D'Ambrosio",
# which scores ~0.95), fall back to a FUZZY surname match SCOPED to just that
# game's ~44 players, resolving ONLY if a SINGLE player is within threshold
# (else manual). 0.85 (not 0.95) because D'Ambrossio scores 0.947 — 0.95 would
# miss it; uniqueness within the tiny game-scoped pool prevents a false match.
EDDIE_SURNAME_FUZZY_THRESHOLD = float(os.getenv("EDDIE_SURNAME_FUZZY_THRESHOLD", "0.85"))

# Eddie AFL image tips: when the vision parse can't read the unit sizing off the
# image (units null/0 — e.g. Eddie put it in a SEPARATE follow-up message we don't
# merge), fall back to 2.5u (Eddie's typical play) instead of 1u, but CAP the TOTAL
# stake so a misread can't balloon. Wilson 2026-06-08 (the Collingwood v Melbourne
# under 180.5 read 1u -> placed $400 instead of the intended 2.5u -> $1000). At the
# live $400/u this is exactly 2.5u = $1000; a larger unit_size scales the fallback
# units down so units * unit_size never exceeds the cap.
EDDIE_IMAGE_NO_UNITS_FALLBACK_UNITS = 2.5
EDDIE_IMAGE_NO_UNITS_MAX_STAKE = 1000.0
# ...AND cap the bookie LIABILITY (winnings = stake*(odds-1)) at $1000 too, so a
# high-odds bet can't put $1000 down (Wilson: "don't accidentally put $1000 on a
# $10 odder"). At odds o the stake is capped at 1000/(o-1): e.g. o=10 -> $111 stake
# ($1000 to-win), o=3 -> $500, o<=2 -> the $1000 stake cap binds first.
EDDIE_IMAGE_NO_UNITS_MAX_LIABILITY = 1000.0

# v6.05 (Wilson 2026-07-21): Eddie often posts a betslip PHOTO with the units in
# the photo's CAPTION (same Telegram message) rather than on the slip — the vision
# parse then reads "NO unit sizing" and the 2.5u fallback fired instead of his real
# size. Extract "Xu" / "X units" from the caption and use it (treated as a REAL
# size, like an in-image units read). Sanity-capped so a caption misparse can't
# stake an absurd size; a value outside (0, MAX] is ignored -> the 2.5u fallback
# still applies. Scoped at the CALL SITE to a LONE no-units tip (single betslip),
# so a multi-tip image can never get N x the caption units.
EDDIE_CAPTION_UNITS_MAX = 5.0
_CAPTION_UNITS_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:u\b|units?\b)", re.IGNORECASE)
# v6.05 audit hardening: a RESULTS / BRAG / commentary caption ('won 3u yesterday',
# 'up 4 units this month', 'on a 4-unit heater') carries a units token that is NOT
# THIS slip's stake. If any of these markers appear, refuse to read units from the
# caption (-> the normal 2.5u fallback applies instead of a misparsed size).
_CAPTION_NONBET_RE = re.compile(
    r"\b(won|banked|bankroll|profit|lost|losses|winner|heater|yesterday|"
    r"last\s+week|this\s+(?:week|month|round|season)|so\s+far|on\s+the\s+year|"
    r"record|streak|up\s+\d|down\s+\d)\b",
    re.IGNORECASE,
)


def _extract_units_from_caption(caption: str):
    """Return the units float from an Eddie photo caption ('2.5u', '3 units'),
    or None if the caption is absent, reads like a RESULTS/brag caption, is
    AMBIGUOUS (zero or >1 units tokens — e.g. 'won 4u, play 2u'), or the value is
    out of the sane (0, EDDIE_CAPTION_UNITS_MAX] range. v6.05 audit: this is a
    HEURISTIC read (lower confidence than an on-slip units read), so the caller
    still applies the no-units $1000 stake/liability caps to whatever this returns.
    The negative-lookbehind + digit-anchor means 'under'/'disposals' never match
    (only a NUMBER immediately followed by u/units)."""
    if not caption:
        return None
    if _CAPTION_NONBET_RE.search(caption):
        return None  # brag/results caption — a units token here is not this bet's size
    matches = _CAPTION_UNITS_RE.findall(caption)
    if len(matches) != 1:
        return None  # 0 tokens (no size) or >1 (ambiguous which is the stake) -> fallback
    try:
        u = float(matches[0])
    except (TypeError, ValueError):
        return None
    if u <= 0 or u > EDDIE_CAPTION_UNITS_MAX:
        return None
    return u


def _resolve_eddie_surname_to_player(token: str, msg_time, stat: str = None,
                                     line=None, side: str = None, tip_odds=None):
    """Resolve an Eddie BARE-SURNAME player prop to a unique full-name player on
    a team in the AFL game about to start.

    Eddie posts last-name-only player props right at game time ("Daniel 25+
    disposals"). This finds the AFL game(s) about to start / in progress near
    the post time (Squiggle `afl_games_in_play`), then surname-anchors the token
    to a UNIQUE player on those teams (roster `afl_surname_candidates` scoped via
    `team_key`). Game-scoping is what makes it safe: league-wide a surname can be
    ambiguous (and 'Daniel'/'Bailey' are common FIRST names), but inside the ~44
    players on the two teams playing now, the surname is almost always unique.

    Same-surname COLLISION on the in-play teams (e.g. Harley Reid + Archer Reid,
    both West Coast): when the optional tip context (stat/line/side/tip_odds) is
    supplied, attempt a secondary odds tie-break against the live Sportsbet
    catalog (_afl_disambiguate_surname_by_odds) — resolve only when EXACTLY ONE
    candidate's catalog price is within tolerance of the tip's quoted odds.
    Without that context, or if the tie-break is inconclusive, the collision
    still routes to manual.

    Returns (full_name, team) on a unique hit, else None (caller -> manual).
    Routes to manual (None) on ANY ambiguity: no game in the window, surname not
    in the roster for those teams, or the surname matching >1 player across the
    in-play teams that the odds tie-break can't uniquely resolve. Never guesses a
    $400 bet."""
    token = (token or "").strip()
    if not token or len(token.split()) != 1:
        return None  # only the bare-single-token (surname) case
    try:
        ref_ts = msg_time.timestamp() if hasattr(msg_time, "timestamp") else time.time()
    except Exception:
        ref_ts = time.time()
    try:
        cands = afl_surname_candidates(token)
    except Exception as e:
        log.warning(f"Eddie surname resolve: afl_surname_candidates failed: {e}")
        return None

    def _scope_resolve(games: list, via: str):
        """Resolve `token` to a UNIQUE player among the teams playing in `games`.
        Returns (name, team) or None. A collision (>1 distinct full name) attempts
        the catalog-odds tie-break before giving up. Shared by the 2h-window scope
        (Tier 1) and the today's-fixtures fallback (Tier 2)."""
        if not games:
            return None
        keys, labels = set(), []
        for g in games:
            h, a = g.get("hteam", ""), g.get("ateam", "")
            if h:
                keys.add(team_key(h))
            if a:
                keys.add(team_key(a))
            labels.append(f"{h} v {a}")
        scoped = [c for c in cands if team_key(c.get("team", "")) in keys]
        names = {c["name"] for c in scoped}
        if len(names) == 1:
            hit = scoped[0]
            log.info(f"Eddie surname-anchored '{token}' -> '{hit['name']}' "
                     f"({hit['team']}) via {via} [{'; '.join(labels)}]")
            return hit["name"], hit["team"]
        if len(names) >= 2:
            _by_name: dict = {}
            for c in scoped:
                _by_name.setdefault(c["name"], c)
            tie = _afl_disambiguate_surname_by_odds(
                list(_by_name.values()), labels, token,
                stat=stat, line=line, side=side, tip_odds=tip_odds,
            )
            if tie:
                return tie
            log.info(f"Eddie surname '{token}' collision via {via} "
                     f"(hits: {sorted(names)}) — odds tie-break inconclusive")
        if not names:
            # v5.77 (Wilson): EXACT surname found NO player on the in-play teams —
            # usually a vision typo (06-19 "D'Ambrossio" -> roster "D'Ambrosio").
            # Fuzzy-match the token against ONLY these ~44 players; resolve solely
            # when a SINGLE player is within threshold (else manual — never guess).
            try:
                fz = afl_fuzzy_surname_candidates(token, EDDIE_SURNAME_FUZZY_THRESHOLD)
            except Exception as e:
                log.warning(f"Eddie surname fuzzy fallback failed: {e}")
                fz = []
            fz_scoped = [c for c in fz if team_key(c.get("team", "")) in keys]
            fz_names = {c["name"] for c in fz_scoped}
            if len(fz_names) == 1:
                hit = fz_scoped[0]
                log.info(f"Eddie surname '{token}' -> '{hit['name']}' ({hit['team']}) "
                         f"via {via} FUZZY surname (score {hit.get('score')}; "
                         f"exact missed — likely a typo) [{'; '.join(labels)}]")
                return hit["name"], hit["team"]
            if len(fz_names) >= 2:
                log.info(f"Eddie surname '{token}' FUZZY fallback ambiguous via {via} "
                         f"(hits: {sorted(fz_names)}) -> manual")
        return None

    # Tier 1: teams in a game within the 2h window / in progress — the precise,
    # safe scope (Eddie usually posts surnames at game time).
    try:
        games_2h = afl_games_in_play(ref_ts, ahead_sec=EDDIE_GAME_LOOKAHEAD_SEC)
    except Exception as e:
        log.warning(f"Eddie surname resolve: afl_games_in_play failed: {e}")
        games_2h = []
    hit = _scope_resolve(games_2h, "game about to start")
    if hit:
        return hit

    # Tier 2 (v5.67, Wilson): no 2h-window resolution -> fall back to ALL of
    # TODAY'S fixtures' rosters, regardless of time (e.g. Greene tipped 10:13 for
    # a game >2h away). SAME safety: resolves ONLY if the surname is unique across
    # every team playing today; any ambiguity -> manual.
    try:
        games_today = afl_games_on_date(ref_ts)
    except Exception as e:
        log.warning(f"Eddie surname resolve: afl_games_on_date failed: {e}")
        games_today = []
    hit = _scope_resolve(games_today, "today's fixtures (no game within 2h)")
    if hit:
        return hit

    if not games_2h and not games_today:
        log.info(f"Eddie surname '{token}': no AFL game today / within "
                 f"{EDDIE_GAME_LOOKAHEAD_SEC // 3600}h -> manual")
    else:
        log.info(f"Eddie surname '{token}' did NOT uniquely resolve -> manual "
                 f"(tried 2h-window + today's fixtures)")
    return None


def _build_afl_tip_from_image(raw: dict, tipster: str, unit_size: float,
                              default_units: float, msg_time) -> ParsedTip:
    """Build a ParsedTip from one raw vision-extracted AFL dict. Player props
    resolve the team from the roster (so place_tip can resolve the event);
    non-player-prop markets (margin/team line/total/other) are flagged
    alert_only so place_tip routes them straight to manual."""
    market_type = (raw.get("market_type") or "").strip().lower()
    player = (raw.get("player") or "").strip()
    team = (raw.get("team") or "").strip()
    # PERIOD guard (v5.17): Eddie sometimes tips a HALF/QUARTER market (e.g.
    # "Hawthorn -5.5 2nd Half Line"). The AFL catalog the bot places against is
    # FULL-GAME only, so placing a full-game line/total for a half/quarter tip is
    # the WRONG market. Detect a non-full-game period (from the vision `period`
    # field, or a 'half'/'quarter' marker left in any text field) and force the
    # tip to MANUAL. Wilson 2026-06-05 (the Hawthorn -5.5 2nd-half misparse).
    _period = (raw.get("period") or "").strip().lower()
    # v6.07 (sweep #21): this free-text arm was 100% DEAD. IMAGE_PROMPT_AFL emits
    # exactly player/team/stat/side/line/odds/bookie/units/period/market_type/
    # description, so `market`, `market_detail`, `title` and `selection` are PHANTOM
    # keys here (`market_detail` has ZERO assignments repo-wide; `market`/`selection`
    # belong to the TEXT prompt, `title` to racing). _label therefore collapsed to the
    # `period` string that arm 1 already checks, contributing no coverage at all.
    # The ONE free-text field that can carry a dropped qualifier is `description`, so
    # a vision read that puts the period ONLY there ("Hawthorn -5.5 2nd Half Line",
    # period=null) used to place a FULL-GAME -5.5 (measured: alert_only=False,
    # market='line', leg.period=''). Same phantom-field class as the v6.01 margin fix.
    _label = " ".join(str(raw.get(k) or "") for k in ("period", "description")).lower()
    _partial_period = (
        (_period not in ("", "full", "match", "fulltime", "full time", "game", "full game")
         and any(w in _period for w in ("half", "quarter", "1st", "2nd", "3rd", "4th",
                                        "1h", "2h", "q1", "q2", "q3", "q4", "h1", "h2",
                                        # 07-13 re-verify: digit-first codes the
                                        # period map accepts (1q..4q) were missed,
                                        # so a period leg could slip to full-game.
                                        "1q", "2q", "3q", "4q")))
        or "half" in _label or "quarter" in _label
    )
    stat = _normalise_afl_image_stat(raw.get("stat"))
    side = (raw.get("side") or "").strip().lower()
    line = _img_coerce_float(raw.get("line")) or 0.0
    odds = _img_coerce_float(raw.get("odds")) or 0.0
    bookie = (raw.get("bookie") or "").strip()
    units = _img_coerce_float(raw.get("units"))
    no_units_fallback = False
    if units is None or units <= 0:
        # v5.42: no unit sizing in the image -> fall back to 2.5u (Eddie's typical),
        # capped so the TOTAL stake can't exceed $1000 (Wilson). At $400/u that is
        # exactly 2.5u = $1000; a larger unit_size scales the fallback units down so
        # units * unit_size <= the cap. Only fires when units are unreadable — a
        # correctly-read 1u / 3u tip places its real size.
        no_units_fallback = True
        # v6.05: a photo CAPTION units hint (_caption_units) is used as the sizing
        # BASE instead of the 2.5u default — but it STILL flows through the $1000
        # stake + $1000 liability caps below (audit: a caption read is a heuristic,
        # lower confidence than an on-slip read, so it must NOT bypass the caps).
        _cap_hint = _img_coerce_float(raw.get("_caption_units"))
        _units_src = "caption" if (_cap_hint and _cap_hint > 0) else "fallback-2.5u"
        fb_units = _cap_hint if (_cap_hint and _cap_hint > 0) else EDDIE_IMAGE_NO_UNITS_FALLBACK_UNITS
        if unit_size and unit_size > 0:
            # cap 1: total STAKE <= $1000
            fb_units = min(fb_units, EDDIE_IMAGE_NO_UNITS_MAX_STAKE / unit_size)
            # cap 2: total bookie LIABILITY (winnings = stake*(odds-1)) <= $1000, so
            # a high-odds bet can't stake the full $1000 (the $10-odder case). Uses
            # the tipped odds; the >1.25x ceiling guard backstops a higher fill.
            if odds and odds > 1.0:
                fb_units = min(
                    fb_units,
                    EDDIE_IMAGE_NO_UNITS_MAX_LIABILITY / (unit_size * (odds - 1.0)),
                )
        units = fb_units
        log.info(
            f"[{tipster}] image tip NO on-slip units (source={_units_src}) -> {units:.3f}u "
            f"x ${unit_size:.0f} = ${round(units * unit_size, 2)} stake "
            f"(odds={odds or '?'}; caps: stake<=${EDDIE_IMAGE_NO_UNITS_MAX_STAKE:.0f}, "
            f"liability<=${EDDIE_IMAGE_NO_UNITS_MAX_LIABILITY:.0f})"
        )

    raw_msg = (
        f"[Eddie image] {player or team} {side} {line} {stat} "
        f"@ {odds or '?'} {('(' + bookie + ')') if bookie else ''} "
        f"[{market_type or '?'}]"
    ).strip()

    alert_only = False
    alert_reason = ""
    leg_team = team or player
    if market_type == "total":
        # Match TOTAL (combined points O/U) -> total_points. team_full (either
        # competing team) resolves the AFL fixture; selection over/under + line
        # drive the catalog match (_execute_bet total_points branch, ±1.0).
        # $1-gated like all image tips; a catalog miss routes to manual.
        leg = ParsedLeg(market="total_points", team_full=leg_team, player="",
                        stat="", line=line, selection=side)
        # v6.00 (#D): a supported PERIOD total (1h/2h/1q-4q) carries the SB period
        # code so the placement resolver maps it to first_half_total /
        # second_half_total / quarter_total via the EXACT prop-id (SB-only, gated by
        # AFL_PERIOD_MARKETS_ENABLED). Full-game totals leave period="" and take the
        # normal total_points / pick_own_total path. A period miss -> manual.
        if AFL_PERIOD_MARKETS_ENABLED:
            _pcode_to = _afl_period_code(_period)
            if _pcode_to:
                leg.period = _pcode_to
        if not leg_team or side not in ("over", "under") or not line:
            alert_only = True
            alert_reason = "total market missing team/side/line — place manually"
    elif market_type == "team_line":
        # Team HANDICAP -> line. The resolver backfills selection from team_full;
        # the SIGNED line is matched in the catalog ±0.5 by
        # _match_handicap_in_catalog, which never places the wrong side — a miss
        # (or a wrong/absent sign) routes to manual.
        leg = ParsedLeg(market="line", team_full=leg_team, player="",
                        stat="", line=line, selection=leg_team)
        # v5.9x (#5): a supported PERIOD (quarter 1q-4q / 1st-half) team handicap
        # carries the SB period code so the placement resolver maps it to
        # quarter_line / quarter_time_line via the EXACT prop-id (SB-only, gated by
        # AFL_PERIOD_MARKETS_ENABLED). Full-game handicaps leave period="" and take
        # the normal line/pick_own_line path. Player-prop periods are NOT set here,
        # so they still route to manual via the period gate below.
        if AFL_PERIOD_MARKETS_ENABLED:
            _pcode_tl = _afl_period_code(_period)
            # v6.00 (Wilson 2026-07-17): UN-GATE quarters (1q-4q) + 2nd-half (2h) —
            # was 1st-half only. The resolver targets the EXACT per-period
            # proposition_id from the LIVE catalog (07-17 probe confirmed:
            # quarter_line carries period=1q..4q with distinct prop_ids;
            # quarter_time_line carries 1h; there is NO SB 2h-LINE market so a 2h
            # handicap -> exact-or-manual = MANUAL). Wilson ACCEPTS the quarter
            # wrong-digit risk (identical line+odds across quarters, only the vision
            # digit distinguishes) — a placed-quarter confirmation is logged at
            # placement. Any period miss / non-SB / price-check fail -> manual;
            # NEVER a full-game or wrong-period fallback ("don't fuck up the prop_id").
            if _pcode_tl:
                leg.period = _pcode_tl
        if not leg_team or not line:
            alert_only = True
            alert_reason = "team line missing team/line — place manually"
    elif market_type == "margin":
        # v6.00 (E safety guard, Wilson's "St Kilda 1-39 must NOT become -38.5 HC"):
        # a winning-margin RANGE BAND ("Team 1-39" / "20-39" / "40-59") is NOT a
        # handicap and must NEVER be converted to a -(N-0.5) LINE (which pick_own_line
        # could carry -> a WRONG bet). Detect a lo-hi range in any parsed text field
        # and route to MANUAL. SB carries these as the `winning_margin_spread` band
        # market (matched by EXACT selection text); auto-placing them needs a confirmed
        # real Eddie tri-bet parse first (deferred), so a band -> manual for now,
        # NEVER a wrong handicap. A bare "N+" (open-ended, no range) keeps the
        # placeable -(N-0.5) alt-line conversion below (unchanged, v5.75).
        _margin_txt = " ".join(str(raw.get(k) or "") for k in
                               ("selection", "description", "market_detail", "title", "line"))
        # v6.05 (Wilson 2026-07-21): EITHER-TEAM / TRIBET band — a margin band with
        # NO team ('TriBet', 'Either Team 1-24': the match margin regardless of
        # winner). It is NEVER a one-team handicap, so it must NEVER reach the
        # -(N-0.5) conversion below. Route to manual up front (defensive net beyond
        # the range regex). SB carries these as winning_margin_spread; auto-placement
        # is a gated follow-up (needs a live-catalog probe to map the exact band
        # selection/prop_id — see WEEKEND notes). This DESCRIBES the tri-bet cleanly
        # instead of the old generic "unschematic/multi" alert.
        if not leg_team:
            alert_only = True
            alert_reason = (
                "EITHER-TEAM / TRIBET winning-margin band (e.g. 'Either Team 1-24') "
                "— no single team, so NOT a handicap. SB carries this as "
                "winning_margin_spread; auto-place pending a catalog probe — place by hand"
            )
            leg = ParsedLeg(market="other", team_full="", player="",
                            stat="", line=line, selection=side)
        elif re.search(r"\b\d{1,3}\s*-\s*\d{1,3}\b", _margin_txt):
            alert_only = True
            alert_reason = (
                "winning-margin BAND (range e.g. '1-39') — SB carries this as "
                "winning_margin_spread (exact band); auto-place pending a confirmed "
                "Eddie tri-bet parse — place by hand (NOT converted to a handicap)"
            )
            leg = ParsedLeg(market="other", team_full=leg_team, player="",
                            stat="", line=line, selection=side)
        else:
            # Winning margin "Team N+" (e.g. 'Adelaide 40+') == that team on the
            # -(N-0.5) LINE handicap (Wilson 2026-06-18: "40+ winning = -39.5 alt
            # line / handicap"). Convert to a placeable line bet so the bet is
            # ATTEMPTED rather than misparsed to a goals prop or dropped to manual.
            # The handicap matcher (_match_handicap_in_catalog, ±0.5) places the alt
            # line only if the bookie carries it; a miss routes to manual (never a
            # blind/wrong line). `line` from the vision parse is the whole margin N.
            margin_n = abs(line)
            hc_line = -(margin_n - 0.5) if margin_n else 0.0
            leg = ParsedLeg(market="line", team_full=leg_team, player="",
                            stat="", line=hc_line, selection=leg_team)
            if not leg_team or not margin_n:
                alert_only = True
                alert_reason = "winning-margin tip missing team/number — place manually"
            else:
                raw_msg = (
                    f"[Eddie image] {leg_team} {margin_n:g}+ winning margin "
                    f"({hc_line:g} line) @ {odds or '?'} "
                    f"{('(' + bookie + ')') if bookie else ''}"
                ).strip()
    elif market_type and market_type != "player_prop":
        # Other (non-margin, non-total, non-line): no clean catalog mapping -> manual.
        alert_only = True
        alert_reason = f"{market_type} market (image tip) — place manually"
        leg = ParsedLeg(market="other", team_full=leg_team, player=player,
                        stat=stat, line=line, selection=side)
    else:
        # Player prop: infer team from the roster so resolve_afl_event works.
        inferred = team
        if not inferred and player:
            if len(player.split()) == 1:
                # Eddie posts LAST-NAME-ONLY props at game time. Resolve the
                # bare surname to a UNIQUE player on a team in the game about to
                # start (game-scoped surname anchor) — NOT get_player_team's
                # fuzzy match, which can wrongly hit a FIRST-name 'Daniel' ->
                # Daniel Turner. None (no game / not unique) stays manual; we
                # deliberately do NOT fuzzy-fall-back (never guess a $400 bet).
                # Pass the tip's stat/line/side/odds so a same-surname COLLISION
                # (Reid case) can attempt the catalog-odds tie-break (v5.30).
                hit = _resolve_eddie_surname_to_player(
                    player, msg_time, stat=stat, line=line, side=side, tip_odds=odds,
                )
                if hit:
                    player, inferred = hit
            else:
                try:
                    inferred = get_player_team(player, "afl") or ""
                except Exception as e:
                    log.warning(f"get_player_team failed for {player!r}: {e}")
                    inferred = ""
        if (not inferred and len(player.split()) >= 2
                and tip_parser._claude_websearch_enabled()):
            # v5.80 RECOVERY (Hugo Hall-Kahan, 2026-06-20): the stale roster
            # can miss a just-listed player (mid-season rookie draftee). Ask
            # Claude (web-search) for the player's CURRENT club. This only
            # supplies the TEAM -> the existing event resolve + bookie
            # catalog price-check + odds floor still gate the bet, so a wrong
            # resolve just fails to price -> manual (never a blind bet).
            try:
                import claude_parser
                _team = claude_parser.resolve_afl_player_team(player)
                if _team:
                    inferred = _team
                    log.warning(
                        f"CLAUDE WEB-SEARCH resolved AFL player '{player}' -> "
                        f"'{inferred}' (roster miss; still gated on bookie catalog + odds)"
                    )
            except Exception as e:
                log.error(f"Claude player resolve failed for {player!r}: {e}")
        if not inferred:
            alert_only = True
            alert_reason = (
                f"could not infer AFL team for '{player}' (not in roster / no "
                f"unique surname match in the AFL game about to start) — "
                f"place manually"
            )
        leg = ParsedLeg(market="player_prop", team_full=inferred, player=player,
                        stat=stat, line=line, selection=side)

    # v5.17: a half/quarter period overrides everything -> manual (full-game
    # catalog only). Applies even to player props, since the bot can't place a
    # 2nd-half disposals line either.
    # v5.9x (#5): EXCEPTION — a supported PERIOD TEAM HANDICAP carries leg.period
    # (set ONLY in the team_line branch above, when AFL_PERIOD_MARKETS_ENABLED and
    # the period maps to 1q-4q/1h). That is now PLACEABLE via the exact-prop-id
    # resolver (SB-only, unmatched->manual), so it does NOT get force-routed here.
    # Player-prop periods, unsupported periods (2nd-half etc.), and the flag-off
    # case never set leg.period, so they still route to manual as before.
    if _partial_period and not alert_only and not getattr(leg, "period", ""):
        alert_only = True
        alert_reason = (
            f"AFL {_period or 'half/quarter'} market (image tip) — bot places "
            f"FULL-GAME markets only; place this period bet manually"
        )

    tip = ParsedTip(
        tipster=tipster, sport="afl", is_sgm=False, legs=[leg],
        units=units, unit_size=unit_size,
        raw_message=raw_msg, timestamp=msg_time,
        suggested_bookie="sportsbet", suggested_odds=odds,
        alert_only=alert_only, alert_reason=alert_reason,
    )
    # v5.42: mark a no-units fallback so the route can (a) guard a per-image
    # over-parse [>1 fallback tip from one image -> manual] and (b) flag the
    # guessed size in the alert.
    tip._units_fallback = no_units_fallback
    return tip


def _image_afl_conflicting_indices(raw_tips: list) -> set:
    """Indices of AFL image tips that are opposite sides (over AND under) of the
    SAME market+team+line — the signature of the vision model over-reading a
    background odds grid (one image -> 'West Coast over 172.5' AND 'under
    172.5'). Both can't be the tip and we can't disambiguate, so these route to
    manual rather than auto-place BOTH sides (which v5.2 now could, since totals
    /lines place). 2026-06-03 Eddie image. The prompt is the primary fix; this
    is the safety net."""
    # v5.69-r2 (round-2 #1): the conflict key components come from the RAW vision
    # dicts, which the model labels inconsistently. Normalise the two that bite:
    #  - period: every full-game synonym ('', 'full', 'match', 'fulltime', ...)
    #    collapses to one bucket, so an over labelled 'full' and an under labelled
    #    null/'' of the SAME full-game market still hash to the same key (else the
    #    over+under pair ESCAPES the guard and both sides auto-place — the exact
    #    bug this detector exists to catch, re-opened by the v5.69 period addition).
    #  - stat: routed through _normalise_afl_image_stat for the same class of
    #    label drift on player props.
    _FULLGAME = {"", "full", "match", "fulltime", "full time", "game", "full game", "ft"}

    def _norm_period(raw_period) -> str:
        p = (raw_period or "").strip().lower()
        return "full" if p in _FULLGAME else p

    groups: dict = {}
    for i, raw in enumerate(raw_tips):
        mkt = (raw.get("market_type") or "").strip().lower()
        ln = _img_coerce_float(raw.get("line"))
        side = (raw.get("side") or "").strip().lower()
        if ln is None:
            continue
        if side not in ("over", "under"):
            # v6.07 (sweep #16): a HANDICAP's two sides are the SIGN of the line on the
            # two competing teams ("West Coast -7.5" / "Port Adelaide +7.5"), NEVER
            # over/under. IMAGE_PROMPT_AFL only defines `side` for over/under markets
            # and mandates the signed form for handicaps, so EVERY signed team_line row
            # was dropped right here and the pair escaped the detector -> both sides of
            # ONE fixture auto-placed at up to $1000 each, a guaranteed loss of the
            # overround on a bet the tipster never made. (Measured: a -7.5/+7.5 pair
            # returned set(); the totals over/under control correctly returned {0,1}.)
            # An over/under-LABELLED team_line row keeps its explicit side, so the
            # 2026-06-03 grid-over-read shape stays caught.
            if mkt != "team_line" or ln == 0:
                continue
            side = "over" if ln > 0 else "under"
        period = _norm_period(raw.get("period"))
        if mkt == "player_prop":
            # v5.69 (m11): include stat + period so two DISTINCT props on the
            # SAME player at the same numeric line but DIFFERENT stats (e.g.
            # 'Bont over 20.5 disposals' + 'Bont under 20.5 tackles') are NOT
            # falsely treated as an over/under conflict and force-routed manual.
            who = (raw.get("player") or raw.get("team") or "").strip().lower()
            stat = (_normalise_afl_image_stat(raw.get("stat")) or "").strip().lower()
            key = (mkt, who, stat, period, round(abs(ln), 1))
        elif mkt in ("total", "alternate_total"):
            # v5.69 (m12): key totals on the RESOLVED EVENT, not the raw team
            # label. The vision model can write a different competing team on
            # the over vs the under row of the SAME game's total, which let both
            # sides escape the guard and auto-place. Resolving both team labels
            # to the event collapses them to one key. Falls back to the raw team
            # string if resolution fails.
            team_raw = (raw.get("team") or raw.get("player") or "").strip()
            try:
                key_event = (resolve_afl_event(team_raw) or team_raw).lower()
            except Exception:
                key_event = team_raw.lower()
            key = (mkt, key_event, period, round(abs(ln), 1))
        elif mkt == "team_line":
            # v6.07 (sweep #16): same event-resolution trick as totals above. The model
            # writes the FAVOURITE on one row and the UNDERDOG on the other, so both
            # team labels must resolve to the fixture to collapse into one key.
            # round(abs(ln),1) is already sign-free, so -7.5 and +7.5 hash together.
            # A resolve failure falls back to the raw team string = fail-OPEN (exactly
            # today's behaviour), so this can never invent a false manual.
            team_raw = (raw.get("team") or raw.get("player") or "").strip()
            try:
                key_event = (resolve_afl_event(team_raw) or team_raw).lower()
            except Exception:
                key_event = team_raw.lower()
            key = (mkt, key_event, period, round(abs(ln), 1))
        else:
            who = (raw.get("team") or raw.get("player") or "").strip().lower()
            key = (mkt, who, period, round(abs(ln), 1))
        groups.setdefault(key, []).append((i, side))
    conflicted: set = set()
    for items in groups.values():
        if {s for _, s in items} >= {"over", "under"}:
            conflicted.update(i for i, _ in items)
    return conflicted


async def _route_image_afl_tips(raw_tips: list, tipster: str, unit_size: float,
                          default_units: float, msg_time, channel_name: str,
                          pipeline_start: float = None, parse_sec: float = None,
                          raw_caption: str = "") -> None:
    """Route vision-extracted AFL tips through the sports pipeline (place_tip,
    Sportsbet-locked via TIPSTERS_FORCE_BOOKIE). Mirrors the per-tip steps of
    _process_tip (cap units, flat/test-stake clamp, dupe-check, place_tip).

    v5.37: pipeline_start (image arrival t0) + parse_sec (vision parse) are
    stamped onto each tip._timing so the BET PLACED summary shows the true
    end-to-end + the parse/resolve/price-check breakdown, same as the text path."""
    conflicted = _image_afl_conflicting_indices(raw_tips)
    # v5.42: per-image over-parse guard for the no-units FALLBACK. The vision can
    # over-read a betslip grid into several tips; with the 2.5u/$1000 fallback,
    # MULTIPLE no-units tips from ONE image would each auto-stake $1000. A single
    # no-units tip is Wilson's intended case (place 2.5u); >1 is the over-parse
    # signature -> route the guessed-size tips to manual instead of N x $1000.
    no_units_count = sum(
        1 for r in raw_tips if (_img_coerce_float(r.get("units")) or 0) <= 0
    )
    # v6.05 (Wilson 2026-07-21): CAPTION UNITS. Eddie posts a betslip PHOTO with the
    # units in the photo's CAPTION ("3u") rather than on the slip; the vision parse
    # reads "NO unit sizing" and the 2.5u fallback fired instead of his real size.
    # Attach the caption units as a HINT (_caption_units) on the tip; the builder
    # uses it as the fallback BASE but STILL applies the no-units $1000 stake/liability
    # caps (audit: a caption read is a heuristic, not an on-slip read, so it must not
    # bypass the safety caps). SCOPED to a SINGLE-tip image (len==1) with no units:
    #   - len==1 => the caption units can never be mis-attributed across a multi-slip
    #     image, and a multi/over-parsed image never gets N x the caption units.
    #   - _extract_units_from_caption already refuses brag/results + ambiguous captions.
    # eddie_afl only.
    if tipster == "eddie_afl" and len(raw_tips) == 1 and no_units_count == 1 and raw_caption:
        _cap_units = _extract_units_from_caption(raw_caption)
        if _cap_units is not None:
            raw_tips[0]["_caption_units"] = _cap_units
            log.info(
                f"[{channel_name}] CAPTION UNITS: '{_cap_units:g}u' read from the photo "
                f"caption for the lone no-units betslip (caption={raw_caption[:60]!r}) "
                f"— used as the sizing base, STILL $1000 stake/liability-capped"
            )
    # v6.00 (Wilson 2026-07-17): count no-odds PLAYER-PROP legs in THIS image.
    # A LONE one (==1) is a standalone single -> auto-place at market (fan-out,
    # exactly like an odds-bearing disposals prop). >=2 no-odds player legs in ONE
    # image = Eddie's legs-only SGM combo signature -> ONE MANUAL alert (never a
    # silent drop, never split into separate singles). Fixes the 07-16 Marshall
    # o22.5 / Wanganeen-Milera under drop (each arrived alone in its own image).
    n_player_noodds = sum(
        1 for r in raw_tips
        if tipster == "eddie_afl" and r.get("player")
        and (_img_coerce_float(r.get("odds")) or 0.0) <= 1.0
    )
    _noodds_multi_alerted = False

    # ── PASS 1 (SEQUENTIAL, main thread): guards + build + dedupe -> jobs ──
    # v5.91: everything that touches the shared dedup map (_is_duplicate) or is
    # cheap runs HERE so PASS 2 only parallelises the SLOW place_tip. Batch-dedupe
    # within the image (_batch_fps) preserves the old check-then-register-
    # sequentially semantics under concurrency (place_tip never touches
    # _recent_tips, so the dedup map is only read/written from this main thread).
    jobs = []          # [(idx, tip, dupe_fp)]
    _batch_fps = set()
    for idx, raw in enumerate(raw_tips):
        try:
            # E (2026-06-28): an unschematic / moneyline / multi-parlay selection the
            # vision parser couldn't fit a market to comes back as market_type
            # "other" + a plain-English `description`. Route it to MANUAL WITH the
            # bet text (we never auto-place an Eddie multi) — BEFORE the no-odds drop
            # below so the parlay is DESCRIBED, not dropped quietly. The 06-27 Eddie
            # 09:49 moneyline parlay had only pinged the generic "(no bettable tips)"
            # with no bet detail.
            _desc = raw.get("description")
            _desc = _desc.strip() if isinstance(_desc, str) else ""
            _mt = (raw.get("market_type") or "").strip().lower()
            # v5.91 review (#3): gate on market_type "other"/empty ONLY. The earlier
            # "or no player/team" arm over-fired on a real total/team_line/margin tip
            # that arrived with a stray description + null team, wrongly diverting it
            # to manual. A real market_type now flows to _build_afl_tip_from_image
            # (which routes an unplaceable one to manual itself); a multi still can't
            # auto-place (alert_only backstops in _build + place_tip).
            if _desc and _mt in ("other", ""):
                log.info(f"[{channel_name}] AFL image tip {idx} is an unschematic/"
                         f"multi selection -> manual (not auto-placed): {_desc}")
                notifier.notify_image_alert(
                    channel_name,
                    f"(manual: multi / non-standard bet — review and place by "
                    f"hand) {_desc}",
                )
                continue
            # BUG B (Wilson 2026-06-21): an Eddie AFL PLAYER-PROP image tip with NO
            # odds is a MULTI/SGM leg — Eddie posts SGM combos as a legs-only image
            # with no per-leg price, and the user does NOT take Eddie multis. DROP
            # it QUIETLY (no "N tips in one image" MANUAL BET ALERT, no blind $1000
            # guess). Singles WITH odds still place (incl. the lone no-units 2.5u
            # fallback). v5.86 review: SCOPED to player-prop legs (raw.player) — a
            # no-odds TEAM bet (margin 'Adelaide 40+' / total / line / h2h) is NOT
            # the multi signature and is left to the normal path (the v5.75 margin
            # placement + the no-units 2.5u fallback), so we never suppress one.
            # Gated to eddie_afl; other AFL image channels keep prior behaviour.
            if (tipster == "eddie_afl" and raw.get("player")
                    and (_img_coerce_float(raw.get("odds")) or 0.0) <= 1.0):
                if n_player_noodds >= 2:
                    # v6.00: >=2 no-odds player legs in ONE image = SGM combo (Eddie
                    # posts legs-only, no per-leg price) -> ONE MANUAL alert, never a
                    # silent drop and never auto-split into singles. Alert once per
                    # image (listing all legs), then skip each leg.
                    if not _noodds_multi_alerted:
                        _noodds_multi_alerted = True
                        _legs = "; ".join(
                            f"{r.get('player')} {r.get('side') or ''} "
                            f"{r.get('line') or ''}".strip()
                            for r in raw_tips
                            if r.get("player")
                            and (_img_coerce_float(r.get("odds")) or 0.0) <= 1.0
                        )
                        log.info(
                            f"[{channel_name}] Eddie AFL image: {n_player_noodds} "
                            f"no-odds player legs (SGM combo signature) -> MANUAL "
                            f"alert (not auto-placed): {_legs}"
                        )
                        notifier.notify_image_alert(
                            channel_name,
                            f"(manual: {n_player_noodds}-leg SGM / multi — no per-leg "
                            f"odds, place by hand) {_legs}",
                        )
                    continue
                # v6.00: a LONE no-odds player prop (==1 in this image) is a standalone
                # single -> DO NOT drop; fall through to build + auto-place at market
                # via the fan-out (odds-less like every Eddie disposals prop; the
                # fan-out sizes off the live catalog + liability caps, not tipster
                # odds). Was a silent drop pre-v6.00 (the 07-16 Marshall/Nasiah miss).
                log.info(
                    f"[{channel_name}] Eddie AFL image LONE no-odds player prop "
                    f"-> auto-place as single at market: {raw.get('player')} "
                    f"{raw.get('side') or ''} {raw.get('line') or ''}".rstrip()
                )
            # Over+under of the same line parsed from one image = background-grid
            # over-parse. Can't tell which is the tip -> manual, never auto-place
            # both sides.
            if idx in conflicted:
                desc = (f"{raw.get('team') or raw.get('player') or '?'} "
                        f"{raw.get('side') or ''} {raw.get('line') or ''} "
                        f"[{raw.get('market_type') or '?'}]").strip()
                log.info(f"[{channel_name}] AFL image tip {idx} ambiguous "
                         f"(over+under of same line parsed) -> manual: {desc}")
                notifier.notify_image_alert(
                    channel_name,
                    f"(manual: ambiguous — both sides of the same line parsed "
                    f"from the image) {desc}",
                )
                continue

            tip = _build_afl_tip_from_image(
                raw, tipster, unit_size, default_units, msg_time
            )
            # Forced bookie stamp (Sportsbet) for clean alerts; the hard lock
            # in the v4 session filter enforces it regardless.
            forced = TIPSTERS_FORCE_BOOKIE.get(tipster)
            if forced:
                tip.suggested_bookie = forced

            # v5.42: >1 no-units tip from one image = likely vision over-parse —
            # don't auto-stake N x the $1000 fallback; route the guessed-size tips
            # to manual. A lone no-units tip still auto-places at 2.5u (intended).
            if getattr(tip, "_units_fallback", False) and no_units_count > 1:
                tip.alert_only = True
                tip.alert_reason = (
                    f"{no_units_count} tips in one image had NO unit sizing "
                    f"(likely vision over-parse) — place by hand to avoid an "
                    f"over-staked $1000 guess on each"
                )

            if tip.units > MAX_UNITS:
                tip.units = MAX_UNITS
            _apply_mlb_flat_stake(tip)       # no-op for AFL
            _apply_image_test_stake(tip)      # $1/unit while test mode
            _apply_saiyan_sgm_unit(tip)       # Saiyan SGM -> 750/u (250 ea x3); after test-stake

            _dupe_fp = f"{tip.tipster}::{_tip_fingerprint(tip)}"  # capture pre-place (v5.49)
            if _is_duplicate(tip) or _dupe_fp in _batch_fps:
                log.info(f"[{channel_name}] DUPE image tip skipped: {_tip_fingerprint(tip)}")
                continue
            _batch_fps.add(_dupe_fp)

            # v5.37: stamp arrival t0 + vision parse so the summary reconciles a
            # true end-to-end. place_tip ADDS resolve_sec; the fan-out price_check_sec.
            if pipeline_start is not None:
                tip._timing = {"t0": pipeline_start,
                               "parse_sec": round(parse_sec or 0.0, 3)}

            _audit_tip(tip, msg_time)
            jobs.append((idx, tip, _dupe_fp))
            # v6.03: CLAIM the cross-message dedup fp only AFTER the tip is committed
            # as a job (a build/_audit_tip crash ABOVE must NOT leak a claim -> that
            # would block a legit re-send for ~10 min). Before PASS 2 is offloaded/
            # awaited, so a second identical image during the await is still caught.
            # _recent_tips = post-completion window; _inflight_fps = whole flight (never
            # time-purged). Released in PASS 3 if this tip doesn't land.
            _register_tip_fingerprint(tip, fp=_dupe_fp)
            _inflight_fps.add(_dupe_fp)
        except Exception as e:
            log.exception(f"[{channel_name}] AFL image tip {idx} build crashed: {e}")
            try:
                notifier.notify_image_alert(
                    channel_name, f"(AFL tip {idx} error: {e}) — place manually"
                )
            except Exception:
                pass

    if not jobs:
        return

    # ── PASS 2: PLACE. Concurrent when >1 job + flag on (the Saiyan/Eddie
    # multi-tip image: each tip's slow resolve+fan-out runs in PARALLEL instead of
    # summing — 06-27 Crisp/De Goey/Trezise were 76s SEQUENTIAL). Each place_tip is
    # independent (a distinct selection); the shared writes it makes are
    # thread-safe (bet_ledger _lock, _log_jsonl _AUDIT_LOCK) and _recent_tips is
    # only touched in PASS 1/3 on this main thread. Bounded (IMAGE_TIP_MAX_
    # CONCURRENCY) so tips x accounts threads don't explode; one tip crashing is
    # captured and handled in PASS 3 (never kills its siblings). ──
    def _place_one(job):
        _i, _t, _f = job
        try:
            return (_i, _t, _f, place_tip(_t), None)
        except Exception as e:  # noqa: BLE001 — isolate one tip's crash
            return (_i, _t, _f, None, e)

    def _place_all_jobs():
        if IMAGE_TIP_CONCURRENT and len(jobs) > 1:
            import concurrent.futures
            _workers = min(len(jobs), max(1, IMAGE_TIP_MAX_CONCURRENCY))
            log.info(f"[{channel_name}] placing {len(jobs)} image tips CONCURRENTLY "
                     f"(<= {_workers} at once)")
            with concurrent.futures.ThreadPoolExecutor(max_workers=_workers) as ex:
                return list(ex.map(_place_one, jobs))
        return [_place_one(j) for j in jobs]

    # v6.03: offload the whole blocking PASS 2 (place_tip fan-outs) OFF the event
    # loop so an image slate can't freeze telethon / the freeze-watchdog ticker.
    # PASS 1 (claim) + PASS 3 (register/release) stay on the loop thread so
    # _recent_tips is NEVER mutated from a worker (that would race the watchdog
    # cleanup). The inner ThreadPoolExecutor still gives per-tip concurrency inside
    # the single offloaded worker; each place_tip is bounded by #2's poll ceiling.
    if PLACEMENT_OFFLOAD_ENABLED:
        outcomes = await _run_in_placement_executor(_place_all_jobs)
    else:
        outcomes = _place_all_jobs()

    # ── PASS 3 (SEQUENTIAL, main thread): register fingerprint + log per result.
    # v5.13: lock the fingerprint on success OR an ambiguous (maybe-landed) outcome
    # so a re-post can't double-stake an account that may have already placed.
    for _idx, _tip, _fp, results, err in sorted(outcomes, key=lambda o: o[0]):
        try:
            if err is not None:
                # place_tip itself raised in PASS 2 — the bet most likely never
                # placed, but a mid-flight crash isn't 100% certain, so VERIFY.
                # v6.03: RELEASE the PASS-1 claim so a re-send is allowed (preserves
                # the prior "not registered on a PASS-2 crash" behaviour).
                _inflight_fps.discard(_fp)
                _recent_tips.pop(_fp, None)
                log.error(f"[{channel_name}] AFL image tip {_idx} placement crashed: {err!r}")
                notifier.notify_image_alert(
                    channel_name,
                    f"(AFL tip {_idx} placement error: {err} — VERIFY before "
                    f"re-placing) place by hand"
                )
                continue
            if any(r.success for r in results) or any(_is_ambiguous_result(r) for r in results):
                # landed / maybe-landed: KEEP the PASS-1 claim (refresh its timestamp)
                # so a re-post can't double-stake; placement is done -> clear in-flight.
                _register_tip_fingerprint(_tip, fp=_fp)
                _inflight_fps.discard(_fp)
                for r in results:
                    if r.success:
                        log.info(f"[{channel_name}] PLACED image tip: {r.bet_id} on {r.bookie} @ {r.odds}")
                # v5.43: the no-units FALLBACK flag (tip._units_fallback) rides on the
                # tip and is surfaced in the BET PLACED summary itself (no extra ping).
            else:
                # clean total failure: RELEASE the PASS-1 claim (re-sendable), matching
                # the old net "register only on landed" behaviour.
                _inflight_fps.discard(_fp)
                _recent_tips.pop(_fp, None)
                log.info(f"[{channel_name}] AFL image tip {_idx} not placed (routed to manual): {_tip.raw_message}")
        except Exception as _e3:
            # v5.91 review (#5): a crash HERE means PASS 2 already placed the bet(s)
            # but post-processing (register/log) failed — the dedup fingerprint may
            # be unregistered. ISOLATE it (don't drop later outcomes) + ping to
            # VERIFY; NEVER silently re-place (the bet is likely already on).
            # v6.03: placement is done -> clear in-flight, but KEEP the _recent_tips
            # claim (PASS-1 registered it) so a re-send is blocked (bet likely on).
            _inflight_fps.discard(_fp)
            log.error(f"[{channel_name}] AFL image tip {_idx} post-place crashed: {_e3!r}")
            try:
                notifier.notify_image_alert(
                    channel_name,
                    f"(AFL tip {_idx}: bet likely PLACED but post-processing failed: "
                    f"{_e3} — VERIFY before any re-place)"
                )
            except Exception:
                pass


async def _process_image_tip(image_bytes: bytes, tipster: str, sport: str,
                             unit_size: float, default_units: float,
                             msg_time, channel_name: str, raw_caption: str = ""):
    """Vision-parse an image-tip post and route to the right pipeline."""
    sport_l = (sport or "").lower()
    # v5.37: t0 = the moment the image arrived (before the vision parse), so the
    # BET PLACED summary's end-to-end spans arrival -> last placement, with the
    # vision parse as its own phase. Threaded into the AFL + racing routers.
    _t0 = time.time()
    try:
        loop = asyncio.get_event_loop()
        # v5.83 CLAUDE PRIMARY: parse the image with Claude up front (skip Groq
        # vision). Same downstream routing. Falls back to Groq if Claude unusable.
        if tip_parser._claude_primary_enabled():
            raw_tips, elapsed = await loop.run_in_executor(
                None, tip_parser.parse_image_fallback, image_bytes, tipster, sport_l
            )
        else:
            raw_tips, elapsed = await loop.run_in_executor(
                None, parse_tip_image, image_bytes, tipster, sport_l
            )
    except Exception as e:
        log.exception(f"[{channel_name}] image vision parse crashed: {e}")
        notifier.notify_image_alert(channel_name, f"(vision parse error: {e})")
        return

    if not raw_tips:
        # v5.58 (Wilson): a summary/recap post (results image captioned
        # "summary / GL all / next meet ...") parses to 0 tips by design —
        # drop it silently instead of pinging manual. The ping remains for
        # an UNEXPLAINED 0-tip parse (a real tip image the model fumbled).
        # v5.69 (m18) + r2 (#3): only suppress the ping when the caption is a
        # summary AND carries no CONCRETE selection. A real tip image whose
        # caption mentions "results" but ALSO names a play ("results aside,
        # here's the play R4 ...") must still ping. NB: gate on
        # _image_text_selection_pattern (race-code / $ / units / decimal odds) +
        # an urgent instruction — NOT _image_text_is_actionable, whose keyword
        # list ('tips'/'odds'/'selections'/'update') ROUTINELY appears in genuine
        # recap captions and would re-spam the very ping v5.58 added (round-2 #3).
        _cap_l = (raw_caption or "").lower()
        _cap_has_play = (_image_text_selection_pattern(_cap_l)
                         or _IMAGE_URGENT_INSTRUCTION_RE.search(_cap_l))
        if (raw_caption and _IMAGE_SUMMARY_RE.search(_cap_l)
                and not _cap_has_play):
            log.info(
                f"[{channel_name}] vision parse returned 0 tips and the "
                f"caption reads as a summary/recap (non-actionable) -> dropped "
                f"(no manual ping): {raw_caption[:100]}"
            )
            return
        # v5.80 RECOVERY: a GENUINE parse failure on a real tip image (we are
        # past the v5.58 summary/recap suppress above, so this is NOT a no-bet
        # image). Retry the VISION parse with Claude (Opus 4.8) BEFORE routing to
        # manual. 2026-06-20: 5 Eddie evening disposal images lost to a Groq
        # vision gibberish regression here. Claude tips re-enter the identical
        # routing/roster/floor gates below.
        if tip_parser._claude_fallback_enabled() and not tip_parser._claude_primary_enabled():
            try:
                c_tips, c_elapsed = await loop.run_in_executor(
                    None, tip_parser.parse_image_fallback, image_bytes, tipster, sport_l
                )
                if c_tips:
                    log.warning(
                        f"[{channel_name}] CLAUDE VISION FALLBACK recovered "
                        f"{len(c_tips)} tip(s) after Groq returned 0"
                    )
                    try:
                        notifier.notify_info(
                            f"\U0001f7e2 CLAUDE VISION FALLBACK: recovered {len(c_tips)} "
                            f"'{channel_name}' tip(s) after a Groq vision failure"
                        )
                    except Exception:
                        pass
                    raw_tips, elapsed = c_tips, c_elapsed
            except Exception as e:
                log.error(f"[{channel_name}] Claude vision fallback failed: {e}")
        if not raw_tips:
            log.info(f"[{channel_name}] vision parse returned 0 tips")
            # v5.9x (Wilson 2026-07-12): CAPTION FALLBACK. Some AFL image tips put
            # the reasoning IN the image and the actual BET in the Telegram caption
            # (e.g. "Crows -19.5 hc"). When vision yields 0 tips, optionally parse
            # the caption through the AFL TEXT pipeline. Ships DORMANT
            # (EDDIE_CAPTION_FALLBACK_ENABLED default False) — the 07-12 review found
            # a recap/scratch/no-bet caption could auto-place a LIVE bet. STRICT gate:
            # AFL image only, caption must carry a real selection pattern AND must NOT
            # be an urgent/scratch instruction, a recap, a summary, or no-bet framing
            # — those route to the manual ping, never to placement. If the caption
            # parses to 0 tips we still ping manual (never silently drop). On any
            # error -> manual ping. Only fires on the vision-empty path, so it can
            # never double-place a vision tip.
            _cap_fallback_ok = (
                EDDIE_CAPTION_FALLBACK_ENABLED
                and sport_l == "afl" and bool(raw_caption)
                and _image_text_selection_pattern(_cap_l)
                and not _IMAGE_URGENT_INSTRUCTION_RE.search(_cap_l)
                and not _IMAGE_RECAP_RE.search(_cap_l)
                and not _IMAGE_SUMMARY_RE.search(_cap_l)
                and not _is_no_bet_framing(raw_caption)
            )
            if _cap_fallback_ok:
                log.warning(
                    f"[{channel_name}] CAPTION FALLBACK: vision 0 tips + actionable "
                    f"non-recap caption -> AFL text pipeline: {raw_caption[:100]}"
                )
                try:
                    _n_handled = await _process_tip(
                        raw_caption, tipster, "afl", unit_size, default_units,
                        msg_time, channel_name)
                    if _n_handled:
                        return  # caption produced >=1 tip (placed or its own manual)
                    log.info(f"[{channel_name}] caption fallback parsed 0 tips -> manual ping")
                except Exception as e:
                    log.error(
                        f"[{channel_name}] caption-fallback text parse failed: {e} "
                        f"— falling back to manual ping"
                    )
            # v5.91 (2026-06-28): a no-tip image is NOT a 'bet detected' -> use the
            # dedicated no-tip notifier (notify_image_alert's footer falsely said
            # 'Image bet detected. Check and place manually.' — the 23:33 Eddie recap
            # case). Still PINGS (a real tip the model fumbled must not be silently
            # dropped — esp. under Claude-primary, where there's no 2nd model).
            notifier.notify_image_no_tip(channel_name, raw_caption or "")
            return
        # else: Claude recovered tips -> fall through to normal routing below.

    log.info(
        f"[{channel_name}] vision extracted {len(raw_tips)} tip(s) in "
        f"{elapsed:.2f}s; routing as {sport_l}"
    )

    if sport_l == "racing":
        await _route_image_racing_tips(
            raw_tips, tipster, channel_name, unit_size, default_units, msg_time,
            pipeline_start=_t0, parse_sec=round(elapsed, 3),
        )
    else:
        await _route_image_afl_tips(
            raw_tips, tipster, unit_size, default_units, msg_time, channel_name,
            pipeline_start=_t0, parse_sec=round(elapsed, 3),
            raw_caption=raw_caption,
        )


_RESULT_MARKERS_RE = re.compile(
    r"(\bwon\b|\bresults?\b|\bsaluted\b|\bgot up\b|\bbagged\b|\bcollected\b|"
    r"\bcashed\b|\bbanked\b|\b(placed|ran|finished|came)\s+(2nd|3rd|second|third)\b|"
    r"\bpaid\b\s*\$?\d)",
    re.IGNORECASE,
)


def _text_looks_like_result(text: str) -> bool:
    """True if a text post reads like a RESULT/recap of a settled race rather than
    a forward tip (v5.22). Conservative: 'X to win' does NOT match; 'X won' does.
    Keeps a results post off the auto-place text racing path (it can carry a race
    code + runner + price and would otherwise place on an already-run race)."""
    return bool(_RESULT_MARKERS_RE.search(text or ""))


async def _process_self_bet(text: str, msg_time, channel_name: str):
    """v5.9x (Wilson 2026-07-12): Wilson's OWN bet channel.

    6-field pipe format: `sport / teams-or-event / single|sgm / selection / stake / odds`
      e.g. `afl / St Kilda v Port Adelaide / single / Nasiah Wanganeen-Milera under 24.5 disposals / 400 / 1.82`
           `nba / Celtics v Knicks / sgm / Tatum 25+ pts and Brown 20+ pts / 300 / 3.5`

    AFL/NBA route through the normal per-sport pipeline (`_process_tip`), which
    fuzzy-matches the fixture + selection, fans out across ALL that sport's accounts,
    ladders on a limit and rebets at the Sportsbet max. RACING and MLB -> manual
    alert (an explicit-dollar racing self-bet doesn't fit the image-racing test/cap
    path; MLB's flat-stake/HRRBI machinery can't honour an arbitrary dollar). HARD
    $SELF_BET_MAX_STAKE cap enforced BOTH here and via _apply_self_bet_flat_stake
    (units forced to 1 so a stray unit token can't multiply the total). Requires
    valid odds > 1.0 (so the price guards apply). NO content dedup — Wilson re-bets
    the same selection on purpose (skip_dedup=True). A message that parses into >1
    tip, is malformed, has an unknown sport, or lacks odds -> manual (never a
    mis-placed or mis-staked bet)."""
    parts = [p.strip() for p in (text or "").split("/")]
    if len(parts) < 6 or not parts[0]:
        notifier.notify_image_alert(
            channel_name,
            "SELF-BET format (6 fields):\n"
            "sport / teams-or-event / single|sgm / selection / stake / odds\n"
            f"got {len(parts)} field(s) -> place manually: {text[:200]}")
        return
    # v5.9x (07-12 review #10): pin the fixed fields from BOTH ends so a "/" inside
    # the selection over-splits gracefully — sport/event/type from the left,
    # stake/odds from the right, selection = everything in between rejoined.
    sport_raw, event_raw, bet_type = parts[0], parts[1], parts[2]
    stake_raw, odds_raw = parts[-2], parts[-1]
    selection = "/".join(parts[3:-2]).strip()
    sport = (sport_raw or "").strip().lower()
    stake = _img_coerce_float(stake_raw) or 0.0
    odds = _img_coerce_float(odds_raw) or 0.0
    is_sgm = "sgm" in (bet_type or "").lower()

    if stake <= 0 or not selection.strip() or not event_raw.strip():
        notifier.notify_image_alert(
            channel_name,
            f"SELF-BET: missing/invalid event, selection or stake -> place manually: {text[:200]}")
        return

    # HARD cap — accidental extra-0 guard (Wilson: "$1500 max in case I add a 0").
    if stake > SELF_BET_MAX_STAKE:
        log.warning(
            f"[self-bet] stake ${stake:.2f} exceeds the ${SELF_BET_MAX_STAKE:.0f} cap "
            f"-> capping to ${SELF_BET_MAX_STAKE:.0f} (accidental extra-0 guard)")
        try:
            notifier.notify_info(
                f"⚠️ SELF-BET stake ${stake:.0f} exceeds the "
                f"${SELF_BET_MAX_STAKE:.0f} cap -> capped to ${SELF_BET_MAX_STAKE:.0f}")
        except Exception:
            pass
        stake = SELF_BET_MAX_STAKE

    _sport_map = {"afl": "afl", "nba": "nba", "nbl": "nbl", "mlb": "mlb",
                  "racing": "racing", "horse": "racing", "horses": "racing",
                  "gallops": "racing", "thoroughbred": "racing"}
    sport_key = _sport_map.get(sport)
    if sport_key is None:
        notifier.notify_image_alert(
            channel_name,
            f"SELF-BET: unknown sport '{sport_raw}' (use afl / nba / mlb / racing) "
            f"-> place manually: {text[:200]}")
        return

    if sport_key in ("racing", "mlb"):
        # RACING: an explicit-dollar racing self-bet doesn't fit the image-racing
        # test-mode / unit-cap / dedup pipeline. MLB (07-12 review #4): the MLB
        # flat-stake + HRRBI-reshape machinery is hardcoded to Shook sizing and
        # can't honour an arbitrary dollar figure. Surface BOTH for a hand-place so
        # we never mis-stake them. (Direct wiring for each is a follow-up.)
        _lbl = "RACING" if sport_key == "racing" else "MLB"
        notifier.notify_image_alert(
            channel_name,
            f"SELF-BET {_lbl} (place manually): {event_raw} / {selection} / "
            f"${stake:.0f} @ {odds or '?'}")
        log.info(f"[self-bet] {sport_key} -> manual: {event_raw} / {selection} / ${stake:.0f}")
        return

    # v5.9x (07-12 review #7): AFL/NBA require valid odds (>1.0). A blank/omitted
    # odds field would leave suggested_odds=0, which disables BOTH the odds ceiling
    # AND the floor and lets a ±1.0 nearest-line snap place a wrong line at any
    # price. Missing odds -> manual (mirrors the Dello no-odds rule).
    if odds <= 1.0:
        notifier.notify_image_alert(
            channel_name,
            f"SELF-BET: missing/invalid odds '{odds_raw}' (need > 1.0 so the price "
            f"guards apply) -> place manually: {text[:200]}")
        log.info(f"[self-bet] {sport_key} no valid odds -> manual: {text[:160]}")
        return

    # AFL / NBA: reconstruct a natural-language tip and route through the normal
    # per-sport pipeline. Stake is carried as unit_size; _apply_self_bet_flat_stake
    # forces units=1 so the placed total is EXACTLY the capped dollar figure even
    # if the LLM reads a stray unit token. skip_dedup=True (Wilson re-bets).
    recon = f"{event_raw}: {selection}"
    if is_sgm and "sgm" not in recon.lower():
        recon += " (SGM)"
    recon += f" @ {odds}"
    log.info(
        f"[self-bet] {sport_key} {'SGM' if is_sgm else 'single'} ${stake:.2f} "
        f"(cap ${SELF_BET_MAX_STAKE:.0f}) -> routing as text: {recon[:160]}")
    await _process_tip(recon, "self_bet", sport_key, stake, 1.0,
                       msg_time, channel_name, skip_dedup=True)


def _disposals_in_quiet_window(now=None) -> bool:
    """True inside the scheduled HyperBot blackout (default 23:00-07:00 local).

    Bookie sessions are turned off overnight to conserve data, but this feed posts
    hourly THROUGH the night, so ~8 messages a night arrive with no session to place
    on. Inside the window the manual ping is suppressed to a log breadcrumb: the feed
    re-offers hourly and no AFL game starts before 07:00, so nothing is lost and the
    alternative is eight false 'place by hand' pages every night.

    Handles a window that wraps midnight (start > end)."""
    now = now or datetime.now()
    start = int(DISPOSALS_MODEL_QUIET_START_HOUR)
    end = int(DISPOSALS_MODEL_QUIET_END_HOUR)
    if start == end:
        return False
    h = now.hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def _disposals_parse_start_utc(raw: str):
    """Parse the BET| `start` field to an AWARE UTC datetime, or None.

    main.py works in NAIVE local datetimes almost everywhere and does not import
    `timezone`, so comparing an aware ISO timestamp against datetime.now() raises
    TypeError — inside the crash-guarded dispatcher that would turn 100% of the feed
    into manual alerts (resolver.py:220-226 documents this exact class biting
    before). So parse explicitly and return an AWARE value the caller compares
    against an AWARE now."""
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
        # REFUSE a naive value rather than guess. The contract says UTC with a
        # trailing Z, so naive is a contract breach — and guessing is dangerous in
        # BOTH directions here: if the emitter actually sent local AEST, reading it
        # as UTC puts the bounce 10-11 HOURS LATER than reality, which is the
        # PERMISSIVE direction and would wave an already-started game through the
        # in-play gate. Returning None routes it to a loud refusal instead.
        return None
    return dt.astimezone(_dt_timezone.utc)


def _build_disposals_tip(row: dict, msg_time) -> ParsedTip:
    """Build a ParsedTip DIRECTLY from a validated BET| row — no LLM round-trip.

    Deliberately NOT routed through _process_tip/route_message like the self-bet
    channel is: that path reconstructs natural language and re-parses it with an LLM,
    which is exactly the re-interpretation risk this feed exists to avoid. For this
    strategy the LINE IS THE TRIGGER.

    Note `market` on the leg is the literal "player_prop", NOT the BET| line's
    `market=player_disposals`. That field is a schema ASSERTION; the HyperBot
    resolver only maps AFL player props under market == "player_prop" and derives
    "player_disposals" itself from the stat. Copying it through would route every
    tip to manual."""
    leg = ParsedLeg(
        market="player_prop",
        team_full=row["team"],
        player=row["player"],
        stat="disposals",
        line=float(row["line"]),
        selection="under",
        raw_text=row["raw"],
    )
    tip = ParsedTip(
        tipster=DISPOSALS_MODEL_TIPSTER,
        sport="afl",
        is_sgm=False,
        legs=[leg],
        # Stake arrives as DOLLARS. units=1.0 x unit_size=<dollars> makes
        # stake_dollars exactly the posted figure; _apply_disposals_model_stake
        # then re-clamps it in the placing process.
        units=1.0,
        unit_size=float(row["stake"]),
        raw_message=row["raw"],
        timestamp=msg_time,
        suggested_bookie="sportsbet",
        suggested_odds=float(row["price"]),
        units_explicit=True,
    )
    # Carried for the placement gates + the fills record; not part of ParsedTip's
    # schema, so attached dynamically the way other paths attach _timing.
    tip._disposals_row = row
    return tip


async def _process_disposals_model_tip(text: str, msg_time, channel_name: str):
    """v6.08: ingest one DisposalsModel message (contract DISPOSALS_MODEL_CONTRACT_v2.md).

    Regex-only. A message with no BET| line is chatter and drops silently; a message
    WITH one that fails validation is REFUSED LOUDLY (never silently dropped, and
    never handed to an LLM to reinterpret). Each valid row is gated on start time and
    the exposure ledger, then placed through the normal AFL fan-out so it inherits
    the audited ladder, the 538 max-stake rebet, ambiguous/Guard-B handling, the
    Telegram summary and the bets_placed.csv ledger row."""
    if disposals_model_parser is None:
        log.error(f"[{channel_name}] disposals parser module missing — cannot process")
        return
    try:
        parsed = disposals_model_parser.parse(text)
    except Exception as e:
        log.exception(f"[{channel_name}] disposals parse crashed: {e}")
        notifier.notify_image_alert(
            channel_name, f"DisposalsModel parse CRASHED — place by hand:\n{text[:400]}")
        return
    if parsed is None:
        log.debug(f"[{channel_name}] no BET| line (chatter) -> dropped")
        return

    for raw_line, reason in parsed.refusals:
        log.warning(f"[{channel_name}] REFUSED BET| line: {reason} :: {raw_line}")
        try:
            notifier.notify_critical(
                f"⚠️ DisposalsModel REFUSED a bet line\n\n<b>{reason}</b>\n\n"
                f"<code>{raw_line}</code>\n\nNothing was placed. This is a feed/schema "
                f"problem, not a market one."
            )
        except Exception:
            pass

    for row in parsed.rows:
        try:
            await _place_one_disposals_row(row, msg_time, channel_name)
        except Exception as e:
            log.exception(f"[{channel_name}] disposals row failed: {e}")
            try:
                notifier.notify_critical(
                    f"⚠️ DisposalsModel row CRASHED after parse — check whether it "
                    f"placed before re-sending\n\n<code>{row.get('key', '?')}</code>\n{e}"
                )
            except Exception:
                pass


async def _place_one_disposals_row(row: dict, msg_time, channel_name: str):
    """Gate + place a single validated BET| row."""
    key = row["key"]
    sel_id = _disposals_selection_id(row)

    # 1. START-TIME GATE. resolve_afl_event deliberately keeps a game for six hours
    #    past bounce (resolver.py:263-265), so a delayed or replayed message would
    #    otherwise place a real IN-PLAY bet at full stake.
    start_utc = _disposals_parse_start_utc(row["start"])
    if start_utc is None:
        log.warning(f"[{channel_name}] {key}: unparseable start {row['start']!r} -> refused")
        # Every PROCESSED message writes exactly one fill record, including the ones
        # that place nothing. The model needs "a record exists saying nothing was
        # placed" to be distinguishable from "no record at all / the file is broken":
        # absence of feedback must mean "assume it is on", so a silent gap here would
        # freeze that tranche forever.
        _disposals_write_fill(row, requested=float(row["stake"]), placed=0.0,
                              ambiguous=0.0, manual=False,
                              committed=_disposals_committed(row),
                              accounts=[], outcome="refused")
        notifier.notify_critical(
            f"⚠️ DisposalsModel: unparseable start time <code>{row['start']}</code> "
            f"for {row['player']} — refused, nothing placed.")
        return
    now_utc = datetime.now(_dt_timezone.utc)
    if now_utc.timestamp() > start_utc.timestamp() + float(DISPOSALS_MODEL_START_SLACK_SEC):
        log.warning(
            f"[{channel_name}] {key}: start {start_utc.isoformat()} already passed "
            f"(now {now_utc.isoformat()}) -> REFUSED (never place in-play)")
        _disposals_write_fill(row, requested=float(row["stake"]), placed=0.0,
                              ambiguous=0.0, manual=False,
                              committed=_disposals_committed(row),
                              accounts=[], outcome="refused")
        notifier.notify_image_alert(
            channel_name,
            f"DisposalsModel: {row['player']} UNDER {row['line']} — bounce time has "
            f"PASSED, refused (tipbot never places this feed in-play).")
        return

    # 2. OVERNIGHT BLACKOUT. Bookie sessions are scheduled off 23:00-07:00 to
    #    conserve data, but this feed posts hourly through the night, so ~8 messages
    #    arrive with nothing to place on. Breadcrumb and stop BEFORE claiming the key
    #    (a claim here would be wasted, and the alert would be eight false "place by
    #    hand" pages a night). Nothing is lost: the feed re-offers hourly with a fresh
    #    key and no AFL game starts before 07:00. Set the two QUIET_*_HOUR knobs equal
    #    to disable the window if sessions are left on overnight.
    if _disposals_in_quiet_window():
        log.info(
            f"[{channel_name}] {key}: inside the {DISPOSALS_MODEL_QUIET_START_HOUR}:00-"
            f"{DISPOSALS_MODEL_QUIET_END_HOUR}:00 HyperBot blackout — not placing "
            f"{row['player']} UNDER {row['line']} (the feed re-offers after "
            f"{DISPOSALS_MODEL_QUIET_END_HOUR}:00; no alert raised)"
        )
        _disposals_write_fill(row, requested=float(row["stake"]), placed=0.0,
                              ambiguous=0.0, manual=False,
                              committed=_disposals_committed(row),
                              accounts=[], outcome="none")
        return

    # 3. BOOK EXPOSURE SWEEP (v6.08b), done OUTSIDE the state lock because it makes
    #    network calls. Reads what is ACTUALLY on this selection from /api/pending_bets,
    #    including money from other tipsters and hand-placed bets that the ledger is
    #    structurally blind to. Swept at claim time rather than on a background poll so
    #    the number is fresh exactly when it matters and there is no staleness to reason
    #    about.
    outside, outside_accounts, outside_status = await _run_in_placement_executor(
        _disposals_outside_exposure, row)
    if outside > 0 or outside_status == "unknown":
        log.warning(
            f"[{channel_name}] {key}: book sweep says ${outside:.2f} already ON this "
            f"selection from outside this tipster (status={outside_status}, "
            f"{len(outside_accounts)} account(s)) — mode={DISPOSALS_MODEL_RECONCILE_MODE}")
        try:
            notifier.notify_info(
                f"ℹ️ DisposalsModel: {row['player']} {row['side'].upper()} {row['line']} "
                f"already has <b>${outside:.2f}</b> on it from outside this tipster "
                f"(status {outside_status}). Mode={DISPOSALS_MODEL_RECONCILE_MODE}.")
        except Exception:
            pass

    # 4. EXPOSURE LEDGER. Claims the key permanently BEFORE placing, and returns how
    #    much of the request may actually go on given what this selection already
    #    carries. This is what makes kind=retry safe.
    allowed, refuse_reason = _disposals_claim(row, outside=outside,
                                             outside_status=outside_status)
    if allowed <= 0:
        log.warning(f"[{channel_name}] {key}: not placing — {refuse_reason}")
        _disposals_write_fill(row, requested=float(row["stake"]), placed=0.0,
                              ambiguous=0.0, manual=False,
                              committed=_disposals_committed(row),
                              accounts=[], outcome="refused")
        # "Nothing left to place" is the ledger working as designed on a fully-filled
        # selection — it will fire on EVERY tick until bounce, so paging CRITICAL for
        # it would bury the alerts that matter (this repo has already had a tipster
        # page ~1200 CRITICALs/day, v6.07 #30). Only genuinely anomalous refusals —
        # a repeat key, a corrupt ledger, an unpersisted claim, a breached
        # per-fixture cap — are critical.
        _benign = "nothing left to place" in refuse_reason
        try:
            if _benign:
                log.info(f"[{channel_name}] {key}: benign refusal, no alert")
            else:
                notifier.notify_critical(
                    f"⚠️ DisposalsModel refused an offer\n\n{refuse_reason}\n\n"
                    f"<code>{key}</code>\nNothing placed.")
        except Exception:
            pass
        return

    row = dict(row)
    row["stake"] = allowed

    tip = _build_disposals_tip(row, msg_time)
    _apply_disposals_model_stake(tip)

    log.info(
        f"[{channel_name}] DisposalsModel PLACING {row['player']} ({row['team']}) "
        f"UNDER {row['line']} disposals @ posted {row['price']} — ${tip.stake_dollars} "
        f"[kind={row['kind']} key={key} sel={sel_id}]"
    )

    # The reservation MUST be released whatever happens below, including a crash
    # inside place_tip — otherwise `inflight` leaks and permanently blocks any
    # further top-up of this selection. Fail-safe (it blocks rather than
    # double-places) but it would silently stop the strategy filling.
    results = []
    placed = ambiguous = 0.0
    manual = False
    accounts = []
    committed = 0.0
    requested = float(tip.stake_dollars)
    try:
        # Use the DEDICATED placement pool (v6.03), not the default executor: the
        # default pool is shared with every other run_in_executor caller, so a hung
        # bookie placement could starve unrelated work.
        results = await _run_in_placement_executor(place_tip, tip) or []
        placed = round(sum(float(r.stake or 0) for r in results
                           if getattr(r, "success", False)), 2)
        # An AMBIGUOUS or cid_unresolved outcome may still land at the bookie after we
        # stopped waiting, so it counts as exposure. Guard B (v6.03) exists because
        # treating these as "not placed" and re-staking is a double stake.
        #
        # Classify with the SHARED _is_ambiguous_result, not the is_ambiguous FLAG.
        # A SLOW REJECTION (elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC, e.g. a 538
        # at 33s — the documented Erasmus case, which HAD landed at the bookie) is
        # maybe-landed but carries NO flag: `is_ambiguous` is only set by the reconcile
        # path, and RECONCILE_AMBIGUOUS defaults false. Checking the flags alone
        # recorded that money as $0 exposure, so the next re-offer would place over a
        # bet that may already be on. The fan-out itself buckets these correctly; this
        # now uses the same predicate.
        #
        # r.stake can also be None/0 on an ambiguous result, which is why the fan-out
        # has its own _at_risk_stake falling back to _requested_stake. Mirror that,
        # then FAIL CONSERVATIVE if it still measures $0.
        _amb = [r for r in results
                if _is_ambiguous_result(r)
                or getattr(r, "is_ambiguous", False)
                or getattr(r, "cid_unresolved", False)]
        ambiguous = round(sum(float(r.stake or getattr(r, "_requested_stake", 0) or 0)
                              for r in _amb), 2)
        if _amb and ambiguous <= 0:
            ambiguous = round(max(0.0, float(tip.stake_dollars) - placed), 2)
            log.warning(
                f"[disposals_model] {len(_amb)} ambiguous result(s) reported no "
                f"measurable stake — booking the full ${ambiguous:.2f} remainder as "
                f"at-risk so nothing re-offers against it")
        # MANUAL is STICKY: it means a human may place this by hand, so any further
        # auto-offer would double-stake. It must therefore mean exactly "we told a
        # human to place it", and nothing broader — a clean no-market or price-gate
        # miss must NOT stick, or the selection is frozen and the model's later
        # re-offer (once the market appears) is refused forever.
        #
        # For this tipster the ONLY thing that tells a human is place_tip's
        # alert_only route: the fan-out's "unfilled -> place by hand" alert is
        # SUPPRESSED for disposals_model (see the unfilled block in _place_afl_fanout),
        # because the model re-offers the shortfall itself and a hand-place on top of
        # that is its own double stake.
        manual = bool(getattr(tip, "alert_only", False))
        accounts = [
            {"session_id": str(getattr(r, "session_id", "") or ""),
             "stake": round(float(r.stake or 0), 2),
             "odds": getattr(r, "odds", None),
             "bet_id": str(getattr(r, "bet_id", "") or "")}
            for r in results if getattr(r, "success", False)
        ]
    except BaseException:
        # Crash or cancellation mid-placement: the bet MAY have landed. Book the
        # amount that was actually ATTEMPTED (tip.stake_dollars, i.e. post-clamp —
        # under TEST_MODE that is $7, not the $500 the ledger reserved) as ambiguous
        # so nothing ever re-offers against it, then re-raise for the caller's alert.
        ambiguous = round(float(requested), 2)
        manual = False
        _disposals_commit(row, 0.0, ambiguous, manual, reserved=float(allowed))
        _disposals_write_fill(row, requested=requested, placed=0.0,
                              ambiguous=ambiguous, manual=False,
                              committed=_disposals_committed(row), accounts=[],
                              outcome="none",
                              event=getattr(tip, "event", "") or "")
        raise
    committed = _disposals_commit(row, placed, ambiguous, manual,
                                  reserved=float(allowed))

    if placed >= requested - 0.01:
        outcome = "filled"
    elif placed > 0:
        outcome = "partial"
    elif manual:
        outcome = "manual"
    else:
        outcome = "none"
    _disposals_write_fill(row, requested=requested, placed=placed,
                          ambiguous=ambiguous, manual=manual, committed=committed,
                          accounts=accounts, outcome=outcome,
                          event=getattr(tip, "event", "") or "")
    log.info(
        f"[{channel_name}] DisposalsModel {key}: outcome={outcome} "
        f"placed=${placed:.2f}/{requested:.2f} ambiguous=${ambiguous:.2f} "
        f"selection_committed=${committed:.2f}"
    )


async def _process_text_racing_tip(text: str, tipster: str, unit_size: float,
                                   default_units: float, msg_time,
                                   channel_name: str):
    """Parse a free-TEXT racing post (Zak/Trial) and route it through the SAME
    racing pipeline as an image tip. v5.21 (Wilson 2026-06-06): Zak & Trial post
    some real tips as TEXT, not images ('Adding Lingani for tomorrow'). The text
    is parsed with the same racing schema the vision path emits, so the relative-
    date helper (_img_parse_racing_date: 'tomorrow'/'6/6'/weekday), runner-match,
    price floor/ceiling, the dedicated 3u image-racing cap, and the runner-only
    -> manual fallback (missing-race# Guard 2 in _route_image_racing_tips) ALL
    apply unchanged. If the parser finds NO real runner the post was chatter that
    slipped past _image_text_is_actionable -> drop silently (no manual ping). On a
    parse ERROR, fall back to a manual alert so a genuine tip is never lost."""
    _t0 = time.time()  # v5.37: arrival t0 for the end-to-end timing breakdown
    # v5.22 RESULTS GUARD: a results/recap post ('R7 Lingani WON at 4.50') carries
    # a race code + runner + price and would otherwise auto-place on a settled race.
    # Route obvious results/past-tense posts to manual (never auto-place) BEFORE the
    # Groq call — a deterministic backstop on top of the parser's own results rule.
    if _text_looks_like_result(text):
        log.info(f"[{channel_name}] text looks like a RESULT/recap -> manual "
                 f"(not auto-placed): {text[:80]}")
        try:
            notifier.notify_image_alert(channel_name, text)
        except Exception:
            pass
        return
    try:
        from groq_parser import parse_racing_text
        loop = asyncio.get_event_loop()
        # v5.83 CLAUDE PRIMARY: parse racing text with Claude up front (skip Groq).
        if tip_parser._claude_primary_enabled():
            raw_tips, elapsed = await loop.run_in_executor(
                None, tip_parser.parse_racing_text_fallback, text, tipster
            )
        else:
            raw_tips, elapsed = await loop.run_in_executor(
                None, parse_racing_text, text, tipster
            )
    except Exception as e:
        log.exception(f"[{channel_name}] text racing parse crashed: {e}")
        # v5.80 RECOVERY: a Groq CRASH on a racing text post (NOT the by-design
        # "0 tips = chatter" drop below) -> retry with Claude before manual.
        # Gated to the crash path only so it can never fire on chatter (which
        # would risk inventing a bet — the AusBets hole).
        if tip_parser._claude_fallback_enabled() and not tip_parser._claude_primary_enabled():
            try:
                _loop = asyncio.get_event_loop()
                c_tips, c_elapsed = await _loop.run_in_executor(
                    None, tip_parser.parse_racing_text_fallback, text, tipster
                )
                if c_tips:
                    log.warning(
                        f"[{channel_name}] CLAUDE RACING-TEXT FALLBACK recovered "
                        f"{len(c_tips)} tip(s) after Groq crashed"
                    )
                    await _route_image_racing_tips(
                        c_tips, tipster, channel_name, unit_size, default_units, msg_time,
                        pipeline_start=_t0, parse_sec=round(c_elapsed, 3),
                    )
                    return
            except Exception as ce:
                log.error(f"[{channel_name}] Claude racing-text fallback failed: {ce}")
        try:
            notifier.notify_image_alert(channel_name, text)  # never lose a real tip
        except Exception:
            pass
        return

    if not raw_tips:
        log.info(
            f"[{channel_name}] text post parsed to 0 racing tips (chatter) -> "
            f"dropped: {text[:80]}"
        )
        return

    log.info(
        f"[{channel_name}] text extracted {len(raw_tips)} racing tip(s) in "
        f"{elapsed:.2f}s; routing as racing"
    )
    await _route_image_racing_tips(
        raw_tips, tipster, channel_name, unit_size, default_units, msg_time,
        pipeline_start=_t0, parse_sec=round(elapsed, 3),
    )


async def _process_leroy_betfair_tip(text, cfg, msg_time, channel_name):
    """v5.93 crash-guard + dispatcher for a Late Mail Leroy post. A create_task'd
    coroutine's exception is otherwise swallowed by asyncio (logged late, never
    surfaced) — for a real-money path a crash must ping manual, NEVER silently drop a
    tip. Parses (MULTI-RACE aware), anchors the date on the post's AEST day (Leroy is
    DAY-OF ONLY — never roll a date forward), and places each race independently."""
    try:
        if parse_leroy_message is None:
            log.warning(f"[{channel_name}] parsers.leroy missing — Leroy tip -> manual: {text[:80]}")
            notifier.notify_image_alert(
                channel_name, f"(Leroy parser unavailable — place manually) {text[:200]}")
            return
        parsed = parse_leroy_message(text)
        if parsed is None:
            log.info(f"[{channel_name}] Leroy: no racing header (noise) -> dropped: {text[:80]}")
            return
        races = parsed.get("races") or []
        if not races:
            log.info(f"[{channel_name}] Leroy: no bettable race (noise) -> dropped: {text[:80]}")
            return
        # Date = the post's AEST calendar day. event.date is UTC (tz-aware); +10h
        # anchors to AEST exactly like _img_parse_racing_date — a raw-UTC date on a
        # morning tip would be the PREVIOUS AEST day -> the wrong day's card -> a
        # wrong-numbered runner. Leroy is DAY-OF ONLY, so this same-day date is the
        # bet date; we NEVER probe a next meeting (contrast _next_meeting_date).
        # v6.07: _au_east_day is DST-correct (was a hardcoded +10h).
        date_str = _au_east_day(msg_time or datetime.now()).strftime("%Y-%m-%d")
        # Per-race isolation: a crash in one race must NOT skip the others, and its
        # manual alert must name ONLY that race (an already-PLACED race's legs must not
        # be re-surfaced for a hand-back — the dedup fp only blocks the automated path).
        for race in races:
            if not race.get("legs"):
                # A "#<digit>" runner line was seen for THIS race but no units parsed
                # -> a mis-formatted real race must NOT vanish silently ('every post is
                # a tip' — Wilson). Per-race so a malformed race sharing a post with a
                # well-formed one still surfaces (the good race still auto-places).
                log.info(f"[{channel_name}] Leroy: {race.get('track')} "
                         f"R{race.get('race_num')} parsed 0 bettable legs -> manual")
                try:
                    notifier.notify_image_alert(
                        channel_name,
                        f"(Leroy — couldn't parse {race.get('track')} "
                        f"R{race.get('race_num')}, place manually on Betfair BSP)")
                except Exception:
                    pass
                continue
            try:
                await _leroy_place_race(race, date_str, channel_name)
            except Exception as e:
                log.exception(f"[{channel_name}] Leroy race place crashed: {e}")
                try:
                    _legs = ", ".join(
                        f"#{l['saddle']} {l['win_units']:g}u W/{l['place_units']:g}u P"
                        for l in race.get("legs", []))
                    notifier.notify_image_alert(
                        channel_name,
                        f"(Leroy race crashed — place manually on Betfair BSP) "
                        f"{race.get('track')} R{race.get('race_num')}: {_legs} | error: {e}")
                except Exception:
                    pass
    except Exception as e:
        log.exception(f"[{channel_name}] Leroy Betfair pipeline crashed: {e}")
        try:
            notifier.notify_image_alert(
                channel_name,
                f"(Leroy tip crashed — place manually on Betfair BSP) {text[:200]} | error: {e}")
        except Exception:
            pass


async def _leroy_place_race(parsed_race, date_str, channel_name):
    """Place ONE Leroy race via Betfair BSP (BACK) on LEROY_BETFAIR_SESSION.
    $LEROY_UNIT_SIZE/unit, FULL stake, NO splitting across accounts, capped at
    LEROY_MAX_UNITS PER BET. A W/Place line places on BOTH the WIN and PLACE BSP
    markets. Saddle# -> runner NAME off a price_check_racing card (same-day, exact
    track+race+date; the card's saddle numbers are correct for THIS race). Money-safety:
    (1) per-(track,race,date,saddle,market) DEDUP registered BEFORE the place so a
    reconnect re-delivery / duplicate line can't double-back; (2) an AMBIGUOUS place
    result (POST/cid timeout — the back MAY have matched at SP) fires a verify-the-
    account CRITICAL and is NEVER told to 'place manually' (that would double-stake);
    (3) an unresolvable saddle or a clean failure routes THAT leg to a manual alert."""
    # Alias-normalise the track (e.g. 'Morphettville Parks' -> 'Morphettville'); an
    # unknown/vague track passes through and is resolved best-effort by trying the card
    # across up to 4 bookies below (if ANY recognises it we get the field), else manual.
    track = _normalise_racing_track(parsed_race["track"]) or parsed_race["track"]
    race_num = parsed_race["race_num"]
    rtype = parsed_race["race_type"]
    sess = str(LEROY_BETFAIR_SESSION)
    loop = asyncio.get_event_loop()
    log.info(f"[{channel_name}] Leroy: {track} R{race_num} ({date_str}) — "
             f"{len(parsed_race['legs'])} runner(s) -> Betfair BSP (session {sess}, "
             f"${LEROY_UNIT_SIZE:g}/u, cap {LEROY_MAX_UNITS:g}u)")

    # Card fetch for saddle# -> runner NAME (Betfair BSP is placed by NAME; saddle
    # numbers are identical across bookies for a race). Try the Betfair session first;
    # if it returns no numbered card (a Betfair session may not serve a fixed-odds
    # racing card), fall back through the FIRST FEW racing-priority sessions — bounded
    # so a run of dead sessions can't blow the latency (each price-check is a ~30s cid).
    def _fetch_runners(_sid):
        try:
            c = hb.price_check_racing(_sid, track, race_num, rtype, date_str)
            return (c or {}).get("runners", []) or []
        except Exception as e:
            log.warning(f"[{channel_name}] Leroy card fetch on {_sid} failed: {e}")
            return []

    def _has_numbers(rs):
        return any(r.get("number") is not None and (r.get("runner") or "").strip()
                   for r in rs)

    card_sessions = [sess]
    try:
        card_sessions += [s for s in session_priority.get_priority_for("racing")
                          if s != sess][:3]
    except Exception:
        pass
    runners = []
    for _sid in card_sessions:
        runners = await loop.run_in_executor(None, lambda s=_sid: _fetch_runners(s))
        if _has_numbers(runners):
            if _sid != sess:
                log.info(f"[{channel_name}] Leroy: resolved {track} R{race_num} card via "
                         f"fallback session {_sid} (Betfair {sess} had no numbered card)")
            break

    def _num_eq(a, b):
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return str(a).strip() == str(b).strip()

    def _name_for(saddle):
        for r in runners:
            if _num_eq(r.get("number"), saddle):
                return (r.get("runner") or "").strip()
        return ""

    placed, manual = [], []
    for leg in parsed_race["legs"]:
        saddle = leg["saddle"]
        name = _name_for(saddle)
        if not name:
            manual.append(
                f"#{saddle} ({leg['win_units']:g}u W / {leg['place_units']:g}u P) — "
                f"couldn't resolve saddle->name on {track} R{race_num}")
            continue
        for market, units in (("win", leg["win_units"]), ("place", leg["place_units"])):
            if units <= 0:
                continue
            # DEDUP registered BEFORE the place. A Leroy race resolves HOURS after the
            # tip, so the 10-min racing window is too short — use a day-long TTL so an
            # hour-later re-post (or a reconnect catch-up re-delivery) still can't
            # re-stake. Also collapses any within-post duplicate (saddle,market) that
            # slipped past the parser.
            fp = ("LEROY", track.lower(), race_num, date_str, saddle, market)
            _now = datetime.now()
            for _k in [k for k, t in _leroy_recent_fps.items()
                       if (_now - t).total_seconds() > LEROY_DEDUP_TTL_SEC]:
                del _leroy_recent_fps[_k]
            if fp in _leroy_recent_fps:
                log.info(f"[{channel_name}] Leroy DUPLICATE {track} R{race_num} #{saddle} "
                         f"{market} (already placed/attempted) -> skipped (no double-bet)")
                continue
            _leroy_recent_fps[fp] = _now

            capped = min(units, LEROY_MAX_UNITS)   # 4u CAP per bet (Wilson)
            size = round(capped * LEROY_UNIT_SIZE, 2)
            _t0 = datetime.now()
            try:
                res = await loop.run_in_executor(
                    None, lambda m=market, s=size: hb.place_betfair_bsp(
                        sess, track, race_num, name, s, market=m,
                        race_type=rtype, date=date_str))
            except Exception as e:
                res = {"success": False, "error": str(e)}
            _elapsed = (datetime.now() - _t0).total_seconds()
            _err = res.get("error") or ""
            # MAYBE-LANDED = the API flagged it ambiguous OR the reject was SLOW and not
            # a provably pre-placement reject. A slow non-success CAN have landed
            # off-ledger (the Erasmus $125 lesson) — mirror the slow-rejection guard
            # used on every other placement path (STAKE_REJECT_LATENCY_THRESHOLD_SEC /
            # _is_definitely_pre_placement). A maybe-landed BSP back is NEVER routed to
            # 'place manually' (a hand re-back would DOUBLE-STAKE at the SP).
            _maybe_landed = bool(res.get("ambiguous")) or (
                not res.get("success")
                and _elapsed >= STAKE_REJECT_LATENCY_THRESHOLD_SEC
                and not _is_definitely_pre_placement(_err)
            )
            if res.get("success"):
                bet_id = res.get("bet_id") or res.get("betId") or ""
                if capped < units:
                    log.info(f"[{channel_name}] Leroy #{saddle} {name} {market}: capped "
                             f"{units:g}u -> {LEROY_MAX_UNITS:g}u (${size:.2f})")
                placed.append({"saddle": saddle, "runner": name, "market": market,
                               "size": size, "units": capped, "bet_id": bet_id})
                log.info(f"[{channel_name}] Leroy BSP PLACED: #{saddle} {name} {market} "
                         f"${size:.2f} [{bet_id or 'no-id'}]")
                try:
                    import bet_ledger
                    # Synthesize a stable ledger key when HB returns no bet_id — a BSP
                    # back is UNMATCHED until the jump, so success-with-no-id is normal;
                    # bet_ledger._write_row drops a blank-bet_id row, so the placed bet
                    # would silently miss the ledger + Bet Record feed without this.
                    _lid = bet_id or f"BSP-{track}-{race_num}-{saddle}-{market}-{date_str}"
                    bet_ledger.log_racing_bet(
                        {"track": track, "race_num": race_num, "saddle": saddle,
                         "runner": name, "market": market, "date": date_str,
                         "titan": "LEROY", "units": capped},
                        {"session_id": sess, "bookie": "betfair", "stake": size,
                         "odds": None, "bet_id": _lid},
                    )
                except Exception as _le:
                    log.error(f"[{channel_name}] Leroy ledger write failed: {_le}")
            elif _maybe_landed:
                # The back MAY have matched at the SP (ambiguous flag, or a slow reject
                # that can land off-ledger). NEVER tell Wilson to 'place manually' (a
                # hand re-back = DOUBLE STAKE) — fire a verify-the-account CRITICAL,
                # mirroring racing_placer's ambiguous handling. The dedup fp is already
                # registered, so a re-post of this leg is also blocked.
                log.error(f"[{channel_name}] Leroy AMBIGUOUS: #{saddle} {name} {market} "
                          f"${size:.2f} on {track} R{race_num} (elapsed={_elapsed:.1f}s) "
                          f"— may have matched at SP")
                try:
                    notifier.notify_critical(
                        f"Leroy Betfair BSP AMBIGUOUS: {track} R{race_num} #{saddle} {name} "
                        f"{market.upper()} ${size:.2f} — the back MAY have matched at the "
                        f"Starting Price. VERIFY the Betfair account before ANY manual "
                        f"placement (do NOT double-back).")
                except Exception:
                    pass
            else:
                err = _err[:80] or "place failed"
                log.warning(f"[{channel_name}] Leroy BSP FAILED: #{saddle} {name} "
                            f"{market} ${size:.2f} — {err}")
                manual.append(f"#{saddle} {name} {market} {capped:g}u (${size:.2f}) — {err}")
    if placed:
        try:
            notifier.notify_betfair_bsp_placed(channel_name, f"{track} R{race_num}", placed)
        except Exception:
            pass
    if manual:
        try:
            notifier.notify_image_alert(
                channel_name,
                f"(Leroy — place manually on Betfair BSP) {track} R{race_num}: "
                + "; ".join(manual))
        except Exception:
            pass


# ── Roster Freshness ────────────────────────────────────────────────

ROSTER_STALE_DAYS = 1


def _check_and_refresh_roster():
    """Check if roster_nba.json is older than ROSTER_STALE_DAYS, refresh in
    background thread if so. Non-blocking - bot continues startup."""
    try:
        roster_path = Path("roster_nba.json")
        if not roster_path.exists():
            log.warning("roster_nba.json not found - skipping freshness check")
            return
        age_days = (time.time() - roster_path.stat().st_mtime) / 86400
        if age_days > ROSTER_STALE_DAYS:
            log.info(f"Roster is {age_days:.1f} days old (>{ROSTER_STALE_DAYS}), refreshing in background...")
            import threading
            from roster import update_roster_from_api
            def _refresh():
                try:
                    update_roster_from_api()
                    notifier.notify_info("NBA roster auto-refreshed")
                    log.info("Background roster refresh complete")
                except Exception as e:
                    log.error(f"Background roster refresh failed: {e}")
            threading.Thread(target=_refresh, daemon=True).start()
        else:
            log.info(f"Roster is {age_days:.1f} days old (fresh)")
    except Exception as e:
        log.warning(f"Roster freshness check failed: {e}")


# ── Session Watchdog ────────────────────────────────────────────────
#
# Mass-drop events (HyperBot.exe restarting) can take 9-13 sessions offline
# at once and most recover within 5-15 min. Sending one Critical per dropped
# session created spam — see 2026-05-04 19:55 (9 alerts) and 2026-05-06
# 01:53 (9 alerts) which all auto-recovered within minutes.
#
# Flow now:
#   1. Drop detected -> Maintenance alert (1 message, all dropped sessions
#      listed) and a one-shot recheck scheduled for +60s.
#   2. +60s recheck -> Maintenance alert listing recovered vs still-down.
#   3. On each subsequent 5-min watchdog cycle, any session that's been
#      down >=15 min and hasn't been escalated yet rolls into a single
#      Critical batch alert.
# Recoveries between 60s and 15min surface as a Maintenance message on the
# next watchdog cycle.

WATCHDOG_INTERVAL_SEC = 300  # 5 minutes
WATCHDOG_RECHECK_DELAY_SEC = 60  # +1 min follow-up after a drop
WATCHDOG_CRITICAL_AFTER_SEC = 900  # 15 min -> Critical


def _in_session_blackout(now=None) -> bool:
    """True inside the SCHEDULED nightly HyperBot shutdown (default 23:00-07:00 local).

    Wilson scheduled every bookie session off overnight (2026-08-01) to conserve data.
    The session monitors predate that and cannot tell a scheduled shutdown from a crash,
    so they page CRITICAL for it: on the night of 2026-08-02 that was 5 criticals, on the
    MONEY chat -- the one carrying the "MAY have LANDED, VERIFY" pages. Training yourself
    to ignore that chat is a far worse outcome than the noise itself, and it is exactly
    the v6.07 #30 failure (a tipster burying the alerts that mattered under ~1200/day).

    So inside the window a confirmed drop is reported as INFO instead. It is still
    detected, still logged, still visible -- only the paging is suppressed. A session that
    is genuinely dead overnight escalates to CRITICAL on the first cycle after the window
    ends, so the worst case is a delay to 07:00, and no AFL match starts before then.
    Daytime drops are untouched and still page (2026-08-03 11:20 and 12:46 were real).

    Handles a window that wraps midnight. Set the two hours equal to disable.
    """
    now = now or datetime.now()
    start = int(SESSION_BLACKOUT_START_HOUR)
    end = int(SESSION_BLACKOUT_END_HOUR)
    if not SESSION_BLACKOUT_ALERTS_QUIET or start == end:
        return False
    h = now.hour
    return (h >= start or h < end) if start > end else (start <= h < end)

# v5.61 (Wilson 2026-06-14): this in-process watchdog stamps a liveness
# heartbeat every cycle (and at startup). check_session_health.py — the
# SEPARATE scheduled BACKUP monitor — reads it and STAYS SILENT while the
# heartbeat is fresh, because this watchdog already owns session-drop
# alerting (batched, foreign-filtered, 15-min Critical). That kills the
# double-alerting Wilson saw: a real drop used to page BOTH monitors
# (~15 min here + ~20 min there). If the heartbeat goes stale (main.py dead
# OR this loop stalled/crashed), the backup takes over within ~13 min. See
# check_session_health._main_watchdog_alive.
_MAIN_WATCHDOG_HEARTBEAT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "main_watchdog_heartbeat.txt"
)


def _write_watchdog_heartbeat() -> None:
    """Stamp the watchdog liveness heartbeat (epoch seconds), atomically.
    Best-effort — a write failure must never disturb the watchdog itself."""
    try:
        tmp = _MAIN_WATCHDOG_HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        os.replace(tmp, _MAIN_WATCHDOG_HEARTBEAT_PATH)
    except Exception:
        pass


# Sessions tipbot considers active and tracked. On drop, the entry moves
# to _pending_drops. On recovery, it moves back here.
_initial_session_state: dict = {}

# Sessions in a drop window. Each entry:
#   {
#     "first_seen_down": datetime,
#     "info": {...original session info...},
#     "alerted_critical": bool,  # 15-min Critical fired? prevents re-alert
#   }
_pending_drops: dict = {}


def _dead_at_startup_sids(sessions: list) -> list:
    """PURE (v6.07, sweep #29): priority-listed AND yaml-owned session ids that are NOT
    active in `sessions`.

    Excludes any id missing from sessions.yaml, via _is_owned_session -- measured on the
    live config there are 23 priority-listed ids but only 22 in the yaml, the odd one
    being the yaml-commented 100004. Without that filter it would page forever about an
    account that is deliberately disabled.

    Returns [] for an empty/failed poll: absence of data is NOT proof a session is down.
    """
    if not sessions:
        return []
    active = {str(s.get("session_id", "")) for s in sessions if s.get("active")}
    try:
        listed = session_priority.all_priority_session_ids()
    except Exception as e:
        log.warning(f"dead-at-startup: could not read the priority lists: {e}")
        return []
    return sorted(sid for sid in listed
                  if str(sid) not in active and _is_owned_session(str(sid)))


async def _startup_dead_session_check():
    """v6.07 (sweep #29): after a grace window (HyperBot may still be logging sessions in
    while tipbot boots), re-poll and hand any still-dead priority session to the
    watchdog's pending-drop set, so the existing Step 2/3 logic emits the recovery INFO
    or the 15-min Critical with the usual placeable-vs-inert severity split.

    Alert-only. Nothing is started, placed or re-placed."""
    try:
        await asyncio.sleep(max(0, STARTUP_DEAD_SESSION_GRACE_SEC))
        cur = await asyncio.to_thread(hb.get_sessions_or_none)
        if cur is None:
            log.info("Startup dead-session check: HB API unavailable, skipping")
            return
        dead = _dead_at_startup_sids(cur)[:max(0, STARTUP_DEAD_SESSION_MAX)]
        if not dead:
            log.info("Startup dead-session check: all priority sessions are active")
            return
        now = datetime.now()
        for sid in dead:
            _pending_drops.setdefault(str(sid), {
                "first_seen_down": now,
                "info": {"session_id": sid},
                "alerted_critical": False,
            })
        log.warning(
            f"Startup dead-session check: {len(dead)} priority session(s) were DEAD at "
            f"startup {dead} - handed to the watchdog (Critical in "
            f"{WATCHDOG_CRITICAL_AFTER_SEC // 60}m if still down)"
        )
    except Exception as e:
        log.warning(f"Startup dead-session check failed: {e}")


def _drop_label(sid: str, info: dict) -> str:
    """Friendly one-line label for a session in drop alert text.

    Prefers the yaml name+bookmaker (e.g. 'Lonely Punter Sportsbet (s53522)')
    so drop alerts read the same as bet placement alerts. Falls back to
    the HyperBot session info (bookie + username) when the session isn't
    in sessions.yaml — typically only happens during USE_LEGACY_PLACEMENT
    rollback or for foreign sessions that slip past the watchdog filter.
    2026-05-17 v4.2: was previously bookie+username only, which made it
    hard to correlate dropped sessions against the accounts we'd been
    placing on.
    """
    sid_str = str(sid) if sid is not None else ""
    try:
        import session_priority
        meta = session_priority.get_session_meta(sid_str)
    except Exception:
        meta = None
    if meta and meta.name:
        bookie = (meta.bookmaker or info.get("bookie", "") or "").strip()
        bookie_pretty = bookie.capitalize() if bookie else ""
        if bookie_pretty:
            return f"{meta.name} {bookie_pretty} (s{sid_str})"
        return f"{meta.name} (s{sid_str})"
    bookie = info.get("bookie", "?")
    user = info.get("username", "?")
    return f"{bookie}:s{sid_str} ({user})"


def _is_owned_session(sid: str) -> bool:
    """True if this session_id belongs to us (i.e. is in sessions.yaml).

    HyperBot's /api/session_ids returns every active session on the API,
    including ones from other PCs sharing the same HyperBot key. Those
    foreign sessions appeared as 21 spurious drops in 2026-05-07 19:48
    when another PC running its own bookie sessions had a restart cycle.
    sessions.yaml is the source of truth for "accounts I own and care
    about" — anything not in there is invisible to the watchdog.

    Bypassed entirely when USE_LEGACY_PLACEMENT=true (yaml never loaded).
    """
    if os.getenv("USE_LEGACY_PLACEMENT", "false").lower() == "true":
        return True
    return session_priority.get_session_meta(str(sid)) is not None


def _partition_crashed_alerts(crashed: list, active_count: int, now=None):
    """FIX 4 (2026-06-12): split a confirmed-crashed batch into the CRITICAL
    message (placeable sessions, inert ones footnoted) and/or the INFO
    message (inert-only batch). PURE — no sends, unit-tested; the watchdog
    loop just delivers whatever comes back.

    Placeable = in ANY per-sport priority list AT ALERT TIME
    (session_priority.is_placeable_session, fail-open on empty config), so
    racing accounts appended to RACING_SESSION_PRIORITY stay CRITICAL while
    staged-inert ones (no priority assignment — tipbot never places on them)
    stop paging the critical channel. Evidence: 2026-06-11 Wilson Unibet
    s99998 (inert; HyperBot-side it never even logged in) dropped 11:47 and
    fired a CRITICAL at 12:02.

    `crashed` entries are (sid, info, mins_down) — the shape the watchdog
    builds. Returns (critical_msg, info_msg); either may be None."""
    placeable: list[tuple[str, dict, float]] = []
    inert: list[tuple[str, dict, float]] = []
    for sid, info, mins in crashed:
        if session_priority.is_placeable_session(sid):
            placeable.append((sid, info, mins))
        else:
            inert.append((sid, info, mins))

    def _lines(entries):
        return "\n".join(
            f"  {_drop_label(sid, info)} (down {mins:.0f}m)"
            for sid, info, mins in entries
        )

    critical_msg = None
    info_msg = None
    # v6.08f: inside the SCHEDULED nightly shutdown, a confirmed drop is expected, not a
    # crash. Report it as INFO so the money chat is not paged for something Wilson turned
    # off himself. Everything is still detected and logged, and a session still down after
    # the window escalates to CRITICAL on the next cycle.
    # `now` is an explicit parameter, not a bare datetime.now(), because v6.08f made this
    # function read the WALL CLOCK and thereby made an existing test CLOCK-DEPENDENT: it
    # passed 07:00-23:00 and failed overnight. That shipped unnoticed because the deploy
    # happened at 15:16, and it would have blocked the pre-commit gate on any night-time
    # commit. A severity decision that varies by time of day has to be injectable.
    if placeable and _in_session_blackout(now):
        info_msg = (
            f"{len(placeable) + len(inert)} session(s) down during the scheduled "
            f"{SESSION_BLACKOUT_START_HOUR}:00-{SESSION_BLACKOUT_END_HOUR}:00 HyperBot "
            f"shutdown — EXPECTED, not paging. Active remaining: {active_count}. "
            f"Anything still down after {SESSION_BLACKOUT_END_HOUR}:00 will page.\n"
            + _lines(placeable + inert)
        )
        return None, info_msg
    if placeable:
        critical_msg = (
            f"{len(placeable)} session(s) confirmed crashed "
            f"(>={WATCHDOG_CRITICAL_AFTER_SEC // 60}m offline). "
            f"Active remaining: {active_count}.\n" + _lines(placeable)
        )
        if inert:
            critical_msg += (
                "\nAlso down (INERT — no priority assignment, no "
                "auto-placement impact):\n" + _lines(inert)
            )
    elif inert:
        info_msg = (
            f"{len(inert)} INERT session(s) confirmed crashed "
            f"(>={WATCHDOG_CRITICAL_AFTER_SEC // 60}m offline) — in NO "
            f"priority list, tipbot never places on these (no betting "
            f"impact). Active remaining: {active_count}.\n" + _lines(inert)
        )
    return critical_msg, info_msg


async def _watchdog_recheck_after(sids: set, parent_first_seen):
    """One-shot follow-up ~60s after a drop batch is detected.

    Reports recoveries vs still-down for that specific batch. Scoped via
    `parent_first_seen` so a second drop wave that overlaps doesn't get
    folded in. Fire-and-forget task spawned from the main watchdog loop.
    """
    await asyncio.sleep(WATCHDOG_RECHECK_DELAY_SEC)
    try:
        current = hb.get_sessions_or_none()
        if current is None:
            log.warning("Watchdog recheck: API unreachable, skipping recheck alert")
            return
        active_sids = {
            str(s["session_id"]) for s in current
            if s.get("active") and _is_owned_session(s["session_id"])
        }
        recovered: list[str] = []
        still_down: list[tuple[str, dict]] = []
        for sid in sids:
            entry = _pending_drops.get(sid)
            if entry is None:
                continue  # already handled by another path
            if entry["first_seen_down"] != parent_first_seen:
                continue  # belongs to a later drop wave
            if sid in active_sids:
                recovered.append(sid)
                _initial_session_state[sid] = entry["info"]
                _pending_drops.pop(sid, None)
            else:
                still_down.append((sid, entry["info"]))

        # v5.61 (Wilson 2026-06-14): only speak up when something RECOVERED in
        # the recheck window (partial good news). An all-still-down recheck is
        # pure noise — the initial "disconnected" alert already named them and
        # the 15-min Critical escalates if they stay down. Still-down sessions
        # remain in _pending_drops either way. (Cuts a message per drop wave.)
        if not recovered:
            return

        rec_count = len(recovered)
        down_count = len(still_down)
        msg_lines = [
            f"Watchdog recheck (+{WATCHDOG_RECHECK_DELAY_SEC}s): "
            f"{rec_count} recovered, {down_count} still down. "
            f"Active: {len(active_sids)}."
        ]
        if still_down:
            msg_lines.append("Still down:")
            msg_lines.extend(f"  {_drop_label(sid, info)}" for sid, info in still_down)
        notifier.notify_info("\n".join(msg_lines))
    except Exception as e:
        log.warning(f"Watchdog recheck failed: {e}")


async def _session_watchdog():
    """Poll active sessions every 5 min. Batch alerts; escalate at 15 min."""
    global _initial_session_state
    consecutive_failures = 0
    _write_watchdog_heartbeat()  # mark alive immediately on (re)start
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
        # v5.61: stamp liveness BEFORE any work (even if the API is
        # unreachable below) so the backup monitor knows main.py is alive.
        _write_watchdog_heartbeat()
        try:
            current = hb.get_sessions_or_none()
            if current is None:
                # API unreachable. DO NOT touch state — a transient HyperBot
                # outage must not look like every session dropped. 2026-05-01
                # 16:38 outage fired 9 spurious "session DROPPED" Critical
                # alerts because get_sessions() returned [] on a timeout. The
                # retry layer in _post burns 3 attempts before we get here so
                # this really is a sustained failure, not a single blip.
                consecutive_failures += 1
                # Surface persistent failure once after roughly 15 minutes
                # (3 cycles at 300s). Reset on next success.
                if consecutive_failures == 3:
                    notifier.notify_critical(
                        "Watchdog: /api/session_ids has failed 3 polls in "
                        "a row (~15 min). HyperBot may be down. Existing "
                        "session-tracking state preserved; will alert on "
                        "real drops once API is reachable again."
                    )
                continue
            consecutive_failures = 0

            # Periodic dedup-cache cleanup. _recent_tips is normally cleaned
            # on each new tip arrival, but long quiet periods can leave stale
            # entries for hours. Piggyback on the watchdog cycle (every
            # WATCHDOG_INTERVAL_SEC = 300s) to purge entries older than
            # DUPE_WINDOW_SECS.
            _now_ts = datetime.now()
            _expired_fps = [
                k for k, t in list(_recent_tips.items())
                if (_now_ts - t).total_seconds() > DUPE_WINDOW_SECS
            ]
            for k in _expired_fps:
                _recent_tips.pop(k, None)
            if _expired_fps:
                log.debug(
                    f"Watchdog: purged {len(_expired_fps)} expired dedup "
                    f"fingerprint(s) from _recent_tips"
                )

            # Filter to owned sessions only — drop foreign-PC sessions
            # before they enter watchdog state. See _is_owned_session.
            current_active = {
                str(s["session_id"]): s
                for s in current
                if s.get("active") and _is_owned_session(s["session_id"])
            }
            now = datetime.now()

            # Step 1: New drops — in tracked, not in current_active. Move
            # to _pending_drops with first_seen_down=now.
            new_drops: list[tuple[str, dict]] = []
            # Snapshot keys before iterating: we mutate via .pop() inside
            # the loop and a live iterator throws "dictionary changed size
            # during iteration". Bug seen in tipbot.log on 2026-04-29 04:56
            # and 07:01 (legacy watchdog).
            for sid, info in list(_initial_session_state.items()):
                if sid not in current_active:
                    new_drops.append((sid, info))
                    _pending_drops[sid] = {
                        "first_seen_down": now,
                        "info": info,
                        "alerted_critical": False,
                    }
                    _initial_session_state.pop(sid, None)

            # Step 2: Recoveries — in pending AND back in current_active.
            recovered: list[tuple[str, dict, float]] = []  # (sid, info, mins_down)
            for sid, entry in list(_pending_drops.items()):
                if sid in current_active:
                    mins_down = (now - entry["first_seen_down"]).total_seconds() / 60
                    recovered.append((sid, entry["info"], mins_down))
                    _initial_session_state[sid] = entry["info"]
                    _pending_drops.pop(sid, None)

            # Step 3: 15-min escalations — pending, age >=15m, not yet
            # critical-alerted. Batch into one Critical.
            crashed: list[tuple[str, dict, float]] = []
            for sid, entry in _pending_drops.items():
                if entry["alerted_critical"]:
                    continue
                age_sec = (now - entry["first_seen_down"]).total_seconds()
                if age_sec >= WATCHDOG_CRITICAL_AFTER_SEC:
                    crashed.append((sid, entry["info"], age_sec / 60))

            # ── Emit alerts ──────────────────────────────────────────
            if new_drops:
                lines = [f"  {_drop_label(sid, info)}" for sid, info in new_drops]
                msg = (
                    f"{len(new_drops)} session(s) disconnected. "
                    f"Active remaining: {len(current_active)}. "
                    f"Rechecking in {WATCHDOG_RECHECK_DELAY_SEC}s; Critical at "
                    f"{WATCHDOG_CRITICAL_AFTER_SEC // 60} min if still down.\n"
                    + "\n".join(lines)
                )
                # 2026-05-17 v4.2: WARNING line previously just said
                # "Session DROPPED batch: 1 session(s)" with no IDs, so
                # finding which account dropped required cross-referencing
                # the Telegram alert. Append the per-session labels inline.
                drop_summary = ", ".join(
                    _drop_label(sid, info) for sid, info in new_drops
                )
                log.warning(
                    f"Session DROPPED batch: {len(new_drops)} session(s) "
                    f"[{drop_summary}]"
                )
                notifier.notify_info(msg)
                # Spawn one-shot recheck. Use `now` as the parent marker so
                # later drop waves don't fold into this recheck's report.
                sids_set = {sid for sid, _ in new_drops}
                asyncio.create_task(_watchdog_recheck_after(sids_set, now))

            if recovered:
                lines = [
                    f"  {_drop_label(sid, info)} (down {mins:.0f}m)"
                    for sid, info, mins in recovered
                ]
                msg = (
                    f"{len(recovered)} session(s) recovered.\n"
                    + "\n".join(lines)
                )
                log.info(f"Session RECOVERED batch: {len(recovered)} session(s)")
                notifier.notify_info(msg)

            if crashed:
                # FIX 4 (2026-06-12): inert-only batches (no priority
                # assignment -> tipbot never places on them) downgrade to
                # INFO on the maintenance chat; placeable crashes stay
                # CRITICAL with inert ones footnoted. Partition is computed
                # from the LIVE priority lists at alert time — see
                # _partition_crashed_alerts.
                critical_msg, info_msg = _partition_crashed_alerts(
                    crashed, len(current_active)
                )
                if critical_msg:
                    notifier.notify_critical(critical_msg)
                if info_msg:
                    notifier.notify_info(info_msg)
                # Mark so the same crash doesn't fire again next cycle.
                # Stays in _pending_drops; recovery still moves it back.
                for sid, _, _ in crashed:
                    if sid in _pending_drops:
                        _pending_drops[sid]["alerted_critical"] = True

            # Step 4: pick up newly-added active sessions silently (so we'll
            # alert if they drop later). Skip ones already in _pending_drops
            # — they're handled by recovery logic above.
            for sid, s in current_active.items():
                if (
                    sid not in _initial_session_state
                    and sid not in _pending_drops
                ):
                    _initial_session_state[sid] = s
                    log.info(f"Watchdog: tracking new active session {sid}")
        except Exception as e:
            log.warning(f"Session watchdog poll failed: {e}")


# ── Telethon Listener ──────────────────────────────────────────────

def _code_fingerprint() -> str:
    """SHA-256 (first 10 hex) over the core source files, logged at startup so
    it's unambiguous which code is actually running. Catches partial/stale
    deploys even when TIPBOT_VERSION wasn't bumped — _readme.md section 14
    warns about file-overwrite mistakes during long edit sessions. At startup
    disk == loaded code, so this faithfully fingerprints the running build."""
    import hashlib
    files = [
        "main.py", "racing_placer.py", "hyperbot_client.py",
        "session_priority.py", "config.py", "models.py", "groq_parser.py",
        "resolver.py", "nba_resolver.py", "notifier.py", "racing_parser.py",
        "tiptitans_processor.py",
    ]
    h = hashlib.sha256()
    here = Path(__file__).parent
    for fn in sorted(files):
        try:
            h.update((here / fn).read_bytes())
        except FileNotFoundError:
            h.update(b"MISSING:" + fn.encode())
    return h.hexdigest()[:10]


# Computed once at import so the banner and the Telegram startup message agree.
CODE_FINGERPRINT = _code_fingerprint()


# ── Telethon connection-liveness watchdog (incident 2026-07-06) ─────
# Wall-clock (time.time()) of the last update RECEIVED on any monitored chat,
# stamped in the NewMessage handler (even for ignored senders). Read by the
# watchdog ONLY as an asymmetric veto (proof the socket is alive) — a passive
# stamp must never TRIGGER a reconnect (a quiet night != a dead socket).
_last_telethon_update_ts: float = 0.0


async def _telethon_probe(client):
    """Single ACTIVE liveness probe over the MTProto socket -> (healthy, reason).
    A half-open socket makes get_me() HANG -> asyncio timeout -> unhealthy.
    get_me() returns None (does NOT raise) on a revoked/invalidated session, so a
    falsy result is treated unhealthy (2026-07-06 review blind-spot fix)."""
    try:
        if not client.is_connected():
            return False, "is_connected() is False"
        me = await asyncio.wait_for(client.get_me(), timeout=TELETHON_WATCHDOG_PROBE_TIMEOUT_SEC)
        if not me:
            return False, "get_me() returned None (unauthorized/revoked session)"
        return True, ""
    except asyncio.TimeoutError:
        return False, f"get_me() timed out (>{TELETHON_WATCHDOG_PROBE_TIMEOUT_SEC}s)"
    except Exception as e:
        return False, f"probe error: {type(e).__name__}: {e}"


def _telethon_socket_recently_alive(now: float = None) -> bool:
    """True if ANY update arrived within the last probe INTERVAL -> socket provably
    alive. ASYMMETRIC VETO: a probe failure while updates still flow (e.g. a sync
    place_tip fan-out briefly blocked the event loop, tripping the get_me timeout)
    is a FALSE positive and must NOT force a reconnect. Staleness alone NEVER
    triggers a reconnect; it only vetoes a false one."""
    now = time.time() if now is None else now
    return _last_telethon_update_ts > 0 and (now - _last_telethon_update_ts) < TELETHON_WATCHDOG_INTERVAL_SEC


async def _telethon_liveness_watchdog(client, reconnect_event):
    """Force a fast reconnect when the telethon socket goes DEAD/half-open.

    Incident 2026-07-06: main.py stayed alive but the socket went half-open
    ~01:15-10:53 (~9.5h), received ZERO updates, and telethon's own ping/reconnect
    did not notice -> every morning tip missed. This probes every INTERVAL sec; on
    FAIL_THRESHOLD consecutive genuinely-unhealthy probes it sets reconnect_event +
    client.disconnect() -> run_until_disconnected() returns -> main() raises
    ConnectionError -> __main__ restarts main() with a fresh client and RESUMES LIVE.
    NOTE: the client runs catch_up=False, so the dead-window tips are DROPPED (not
    replayed onto already-run races) — money-safe; the win is ~9.5h deaf -> ~2-4 min."""
    consecutive = 0
    while True:
        await asyncio.sleep(TELETHON_WATCHDOG_INTERVAL_SEC)
        healthy, reason = await _telethon_probe(client)
        if not healthy and _telethon_socket_recently_alive():
            log.info(f"telethon watchdog: probe unhealthy ({reason}) but an update arrived "
                     f"<{TELETHON_WATCHDOG_INTERVAL_SEC}s ago -> socket alive, vetoing reconnect")
            healthy = True
        if healthy:
            consecutive = 0
            continue
        consecutive += 1
        log.warning(f"telethon watchdog: UNHEALTHY probe {consecutive}/{TELETHON_WATCHDOG_FAIL_THRESHOLD} ({reason})")
        if consecutive >= TELETHON_WATCHDOG_FAIL_THRESHOLD:
            log.error(f"telethon watchdog: dead socket ({reason}) -> forcing listener reconnect "
                      f"(dropping the gap, resuming live)")
            try:
                notifier.notify_critical(
                    f"Telethon socket dead ({reason}) - forcing listener reconnect. "
                    f"Was silent-deaf; gap dropped, resuming live.")
            except Exception:
                pass
            reconnect_event.set()
            try:
                await client.disconnect()
            except Exception as e:
                log.error(f"telethon watchdog: disconnect() failed: {e}")
            return


# ── Event-loop FREEZE watchdog (incident 2026-07-17) ───────────────────
# Wall-clock stamp proving the asyncio EVENT LOOP is still spinning, updated by
# _loop_liveness_ticker every LOOP_FREEZE_TICK_SEC and read by the OUT-OF-LOOP
# _loop_freeze_watchdog_thread. 0.0 == DISARMED (loop intentionally not running:
# process startup / reconnect backoff) so the thread never false-fires while the
# loop is legitimately down. This is a SEPARATE mechanism from the telethon socket
# watchdog above: that one catches a dead SOCKET (loop alive); this one catches a
# frozen LOOP (a hung synchronous call blocking the single loop thread), which the
# in-loop telethon prober cannot catch because it freezes too. See config.py.
_loop_heartbeat_ts: float = 0.0


async def _loop_liveness_ticker():
    """Stamp _loop_heartbeat_ts every LOOP_FREEZE_TICK_SEC to prove the asyncio event
    loop is still spinning. Runs regardless of tip traffic (unlike the PASSIVE
    _last_telethon_update_ts stamp), so a quiet night still looks alive. If a
    synchronous call blocks the loop, this coroutine cannot resume -> the stamp goes
    stale -> the out-of-loop freeze watchdog thread restarts the process."""
    global _loop_heartbeat_ts
    while True:
        _loop_heartbeat_ts = time.time()
        await asyncio.sleep(LOOP_FREEZE_TICK_SEC)


def _loop_is_frozen(ts: float, now: float) -> bool:
    """Pure decision fn (testable): True iff the loop-liveness stamp is ARMED (>0)
    and older than LOOP_FREEZE_MAX_SILENCE_SEC. A disarmed (0.0) stamp is never
    'frozen' — the loop is intentionally down (startup / reconnect gap)."""
    return ts > 0 and (now - ts) > LOOP_FREEZE_MAX_SILENCE_SEC


def _work_is_stalled(ts: float, now: float) -> bool:
    """v6.07 (incident 2026-07-29): pure decision fn — True iff the WORK-liveness
    stamp is ARMED (>0) and older than WORK_STALL_MAX_SILENCE_SEC.

    WHY THIS EXISTS (the gap that cost 14.5h of tips on 2026-07-29):
    _loop_is_frozen only answers "is the asyncio loop spinning?". On 07-29 the loop
    WAS spinning — the process was alive (23 threads), the ticker kept stamping, so
    the freeze watchdog correctly stayed quiet — but every unit of WORK had stopped:
    the Tip Titans poller's last login was 01:36, its next (04:06) never happened, no
    telethon updates after 01:43, and nothing was ingested or placed until a manual
    restart at 16:19. A loop-liveness probe can never catch that.

    So this checks PROGRESS instead of aliveness: tiptitans_processor.LAST_POLL_TS is
    refreshed every ~10s by the poll loop REGARDLESS of whether any tips exist, so a
    stale stamp means the poller task is dead/stalled. Disarmed (0.0) until the first
    successful poll, so a fork with no Tip Titans credentials never false-fires.
    Threshold is deliberately generous (default 30 min vs a 10s poll) so a slow
    fetch, a 60s auth backoff, or a burst of concurrent placements can never trip it,
    while still turning a 14.5h blackout into ~30 min."""
    return ts > 0 and (now - ts) > WORK_STALL_MAX_SILENCE_SEC


def _sync_freeze_alert(msg: str) -> None:
    """Best-effort SYNCHRONOUS Telegram alert from the watchdog thread. The async
    notifier is on the FROZEN loop and unusable, so this posts directly via requests
    (a plain blocking HTTP call, fine on this thread). Hard 10s timeout so it can
    NEVER delay the restart; every failure is swallowed."""
    try:
        from config import NOTIFY_BOT_TOKEN, NOTIFY_CHAT_ID
        token = NOTIFY_BOT_TOKEN
        chat = os.getenv("NOTIFY_CRITICAL_CHAT_ID", "") or NOTIFY_CHAT_ID
        if not token or not chat:
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": f"[FREEZE-WATCHDOG] {msg}"},
            timeout=10,
        )
    except Exception:
        pass


def _loop_freeze_tick(now: float) -> bool:
    """ONE freeze-check iteration (split from the thread loop for testability). If the
    loop-liveness stamp is frozen (armed AND older than MAX_SILENCE) fire a bounded
    synchronous alert then os._exit(1). os._exit skips atexit/buffer-flush and
    terminates immediately -> tipbot.bat's :start loop relaunches clean. Returns False
    when the loop looks alive; returns True only in tests that patch os._exit (in
    production os._exit terminates before the return)."""
    ts = _loop_heartbeat_ts
    _frozen = _loop_is_frozen(ts, now)
    # v6.07 (incident 2026-07-29): ALSO restart on a WORK stall. The loop can be
    # perfectly alive while every unit of work has died — that is exactly what
    # happened on 07-29 (14.5h with no ingestion or placement, watchdog correctly
    # quiet because the loop was spinning). Read the poller's progress stamp lazily so
    # a fork without Tip Titans (stamp stays 0.0 = disarmed) never false-fires.
    _work_ts = 0.0
    _stalled = False
    if WORK_STALL_MAX_SILENCE_SEC and WORK_STALL_MAX_SILENCE_SEC > 0:
        try:
            import tiptitans_processor as _ttp
            _work_ts = float(getattr(_ttp, "LAST_POLL_TS", 0.0) or 0.0)
            _stalled = _work_is_stalled(_work_ts, now)
        except Exception:
            _stalled = False
    if not _frozen and not _stalled:
        return False
    if _frozen:
        age = now - ts
        msg = (f"TipBot event loop FROZEN for {age:.0f}s "
               f"(>{LOOP_FREEZE_MAX_SILENCE_SEC}s no tick) - force-restarting the "
               f"process; tipbot.bat will relaunch. (incident-2026-07-17 self-recovery)")
    else:
        age = now - _work_ts
        msg = (f"TipBot WORK STALLED: no Tip Titans poll for {age:.0f}s "
               f"(>{WORK_STALL_MAX_SILENCE_SEC}s) while the event loop was still "
               f"alive - force-restarting the process; tipbot.bat will relaunch. "
               f"(incident-2026-07-29: 14.5h of tips lost to exactly this)")
    # v6.07: logging and the Telegram alert BOTH run in a bounded worker thread.
    # log.critical() used to be called inline here, which is unsafe for the exact
    # failure this watchdog must survive: if the process is blocked on a CONSOLE write
    # (Windows QuickEdit text-selection - the root cause of the 07-06/07-08/07-29
    # silent outages) then log.critical BLOCKS rather than raises, so the try/except
    # does not help and os._exit is never reached. A blocked logger must never be able
    # to stop the restart. If the worker hasn't finished in 12s we abandon it (daemon;
    # killed by os._exit) and exit anyway.
    def _notify_then_ignore():
        try:
            log.critical(msg)
        except Exception:
            pass
        _sync_freeze_alert(msg)

    try:
        _t = threading.Thread(target=_notify_then_ignore,
                              name="freeze-alert", daemon=True)
        _t.start()
        _t.join(timeout=12)
    except Exception:
        pass
    os._exit(1)
    return True  # unreachable in production (os._exit terminated); test-only


def _loop_freeze_watchdog_thread():
    """OS-THREAD (NOT asyncio) hard-freeze watchdog. Incident 2026-07-17: the whole
    event loop froze ~6.5h on a hung synchronous HyperBot call, taking every in-loop
    watchdog down with it (no probe, no heartbeat, no auto-recovery). This thread
    lives OUTSIDE the loop — a blocking network call releases the GIL, so it keeps
    running while the loop is wedged. It polls the loop-liveness stamp; on a frozen
    stamp _loop_freeze_tick force-restarts the process. Idle while the stamp is 0.0
    (disarmed). Wrapped so a freak error can NEVER kill the watchdog itself."""
    while True:
        time.sleep(LOOP_FREEZE_CHECK_SEC)
        try:
            _loop_freeze_tick(time.time())
        except Exception:
            try:
                log.exception("loop-freeze watchdog: unexpected error in check (continuing)")
            except Exception:
                pass


# ── Startup pending-bets reconciliation (incident 2026-07-17, v6.03 bug #2) ──
# ALERT-ONLY sweep: on restart, surface pending bets that were in-flight when the
# process died and aren't in the ledger. NEVER places (an in-flight bet may have
# landed; re-placing = double stake). Gated by STARTUP_PENDING_RECONCILE_ENABLED
# (default OFF). See reconcile.find_orphan_pending.
_STARTUP_RECONCILE_STATE_PATH = os.getenv(
    "STARTUP_RECONCILE_STATE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pending_reconcile_seen.json"),
)


def _read_ledger_rows_for_reconcile():
    """Read bets_placed.csv DIRECTLY into light rows for the orphan sweep:
    [{bet_id, correlation_id, event, selection, stake, placed_at(epoch|None)}].
    Best-effort; missing/garbage ledger -> []. Does NOT rely on bet_ledger's
    lazily-seeded in-memory set."""
    import csv as _csv
    import bet_ledger as _bl
    rows = []
    path = getattr(_bl, "_LEDGER_PATH", None)
    if not path or not os.path.exists(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for r in _csv.DictReader(f):
                pa_epoch = None
                pa = r.get("placed_at")
                if pa:
                    try:
                        pa_epoch = datetime.fromisoformat(str(pa)).timestamp()
                    except (ValueError, TypeError):
                        pa_epoch = None
                rows.append({
                    "bet_id": r.get("bet_id"), "correlation_id": r.get("correlation_id"),
                    "event": r.get("event"), "selection": r.get("selection"),
                    "stake": r.get("stake"), "placed_at": pa_epoch,
                })
    except Exception as e:
        log.warning(f"startup-reconcile: could not read ledger {path}: {e}")
    return rows


def _startup_pending_reconcile_blocking(owned_sessions):
    """BLOCKING (worker thread): read ledger + seen-state, query /api/pending_bets
    for owned sessions, return (orphans, seen_ids). Reads + alerts only; NEVER places."""
    import reconcile as _recon
    seen = _recon.load_seen_orphans(_STARTUP_RECONCILE_STATE_PATH)
    ledger_rows = _read_ledger_rows_for_reconcile()
    orphans = _recon.find_orphan_pending(
        hb, owned_sessions, ledger_rows=ledger_rows, seen_ids=seen,
        lookback_sec=STARTUP_PENDING_RECONCILE_LOOKBACK_SEC,
    )
    return orphans, seen


async def _startup_pending_reconcile(owned_sessions):
    """ALERT-ONLY startup reconcile. Surfaces pending bets in-flight at the last
    shutdown that aren't in the ledger. Runs the blocking HB/CSV I/O OFF the loop so
    startup / the freeze ticker are never blocked. NEVER re-places (an in-flight bet
    MAY have landed -> re-placing would double-stake): Wilson verifies + records."""
    import reconcile as _recon
    try:
        orphans, seen = await asyncio.get_running_loop().run_in_executor(
            None, _startup_pending_reconcile_blocking, owned_sessions
        )
    except Exception as e:
        log.exception(f"startup-reconcile failed (non-fatal): {e}")
        return
    if not orphans:
        log.info("startup-reconcile: no orphaned pending bets found")
        return
    lines = [
        f"- {o.get('bookie','?')} (s{o.get('session_id')}): {o.get('event','?')} - "
        f"{o.get('bet','?')} ${o.get('stake')} @ {o.get('odds')} [{o.get('dt')}] "
        f"bookie_bet_id={o.get('bookie_bet_id')}"
        for o in orphans
    ]
    try:
        notifier.notify_critical(
            f"STARTUP RECONCILE: {len(orphans)} pending bet(s) were on the account at "
            f"restart and are NOT in the ledger. They MAY have LANDED - VERIFY on the "
            f"bookie and record manually. TipBot will NOT re-place them.\n" + "\n".join(lines)
        )
    except Exception as e:
        log.error(f"startup-reconcile: alert send failed: {e}")
    # Persist so a flapping restart doesn't re-alert the same orphans every cycle.
    try:
        seen = set(seen) | {str(o.get("id")) for o in orphans if o.get("id") is not None}
        _recon.save_seen_orphans(_STARTUP_RECONCILE_STATE_PATH, seen)
    except Exception as e:
        log.warning(f"startup-reconcile: persist seen failed: {e}")


async def main():
    global _loop_heartbeat_ts
    _loop_heartbeat_ts = time.time()  # ARM the freeze watchdog (loop is now live)
    if LOOP_FREEZE_WATCHDOG_ENABLED:
        # Ticker keeps the stamp fresh; created FIRST so a hang anywhere in startup
        # (below) is also covered by the out-of-loop thread.
        asyncio.create_task(_loop_liveness_ticker())
    log.info("Starting TipBot...")
    # Banner: version NUMBER + fingerprint only (v5.39, Wilson) — the full
    # TIPBOT_VERSION is a multi-KB accumulated changelog; it stays in config.py +
    # CHANGELOG.md as the audit record, no need to dump it into every startup log.
    log.info(f"=== TipBot {TIPBOT_VERSION.split(' (', 1)[0]} | "
             f"code fingerprint {CODE_FINGERPRINT} ===")

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        log.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        sys.exit(1)

    client = TelegramClient("tipbot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
    monitored_chats = list(TIPSTER_CHANNELS.keys())

    @client.on(events.NewMessage(chats=monitored_chats))
    async def handler(event):
        # v5.9x: stamp EVERY received update (even ones ignored for a wrong sender)
        # BEFORE the sender filter — any inbound update proves the MTProto socket is
        # alive; read by the liveness watchdog as an asymmetric veto (never a trigger).
        global _last_telethon_update_ts
        _last_telethon_update_ts = time.time()
        text = event.raw_text or ""
        chat_id = event.chat_id
        sender_id = event.sender_id or 0
        msg_time = event.date or datetime.now()

        channel_cfg = TIPSTER_CHANNELS.get(chat_id, {})
        channel_name = channel_cfg.get("name", "unknown")

        # Filter by bot_id. Previously this returned silently when sender
        # didn't match. Caused 2026-05-16 Shook outage to be invisible in
        # logs (group was promoted basic -> supergroup, chat_id changed,
        # listener got nothing). Log first occurrence per (chat, sender)
        # pair at INFO so a bot_id flip is grep-able without spamming on
        # every human message in chats that have casual chatter.
        expected_bot = channel_cfg.get("bot_id")
        if expected_bot and sender_id != expected_bot:
            key = (chat_id, sender_id)
            if key not in _unexpected_senders_seen:
                _unexpected_senders_seen.add(key)
                log.info(
                    f"[{channel_name}] ignoring message from unexpected "
                    f"sender {sender_id} (expected bot_id={expected_bot}). "
                    f"If this is the tipster's new bot, update config.py."
                )
            return

        # v5.93: Betfair-BSP CHANNELS (Leroy). A TEXT channel where EVERY post is a
        # tip; parse (saddle#+units) + place each runner via Betfair BSP (back) on the
        # configured Betfair session — NOT the fixed-odds racing pipeline. Noise is
        # dropped by the parser (returns None). LEROY_ENABLED is the kill-switch.
        if channel_cfg.get("betfair_bsp"):
            if not LEROY_ENABLED:
                log.info(f"[{channel_name}] LEROY_ENABLED=false — Betfair BSP tip skipped: {text[:80]}")
            elif text:
                asyncio.create_task(
                    _process_leroy_betfair_tip(text, channel_cfg, msg_time, channel_name)
                )
            else:
                # Leroy is a TEXT tipster; a no-text (image/sticker/empty) post isn't a
                # tip we can parse. Log so it's visible rather than a silent no-op.
                log.info(f"[{channel_name}] Betfair BSP post with no text -> ignored")
            return

        # v6.08 DisposalsModel MACHINE FEED. The post IS the bet, as a fixed 18-field
        # `BET|v2|...` line. Handled here and RETURNED so it never reaches the LLM
        # text path below: for this strategy the line IS the trigger, and an LLM
        # re-reading the human-readable summary above it is exactly the
        # re-interpretation risk the feed exists to avoid. Chatter (no BET| line)
        # drops silently inside the processor; a malformed BET| line alerts loudly.
        # The bot_id sender filter above has already run, so a message from any other
        # group member never gets here.
        if channel_cfg.get("machine_feed"):
            # v6.08e LIVENESS STAMP. Every message from this channel's locked sender is
            # proof the model is alive AND that telethon is delivering — a bet, a watch
            # list, a NOOP heartbeat or chatter, with or without text. Stamped HERE,
            # where a real message is already in hand, and never on a timer: the v6.07
            # X-watcher heartbeat was stamped before the work it claimed to measure and
            # stayed fresh for 14 hours while the watcher was blind.
            if DISPOSALS_MODEL_LIVENESS_ENABLED:
                try:
                    _live_alerts = _disposals_note_feed_message(text or "")
                    if _live_alerts:
                        # Fire-and-forget on the DEFAULT pool. notifier._send blocks for
                        # up to ~45s (20s read timeout, one retry), and this runs inline
                        # in the telethon handler — sending here would stall every tip in
                        # flight behind a diagnostic. Not awaited: the send is best-effort
                        # and _disposals_send_liveness_alerts swallows its own failures.
                        asyncio.get_running_loop().run_in_executor(
                            None, _disposals_send_liveness_alerts, _live_alerts)
                except Exception as e:
                    # Liveness is diagnostics. It must never be able to stop a bet.
                    log.error(f"[{channel_name}] liveness stamp failed: {e}")
            if text:
                asyncio.create_task(
                    _process_disposals_model_tip(text, msg_time, channel_name)
                )
            else:
                log.info(f"[{channel_name}] machine-feed post with no text -> ignored")
            return

        # v5.9x (Wilson 2026-07-12): Wilson's OWN bet channel. 6-field structured
        # text; route AFL/NBA/MLB through the normal per-sport fan-out (split-stake
        # + max-stake rebet, NO dedup, $1500 cap), racing + MLB -> manual alert.
        if channel_cfg.get("self_bet"):
            if text:
                asyncio.create_task(_process_self_bet(text, msg_time, channel_name))
            else:
                log.info(f"[{channel_name}] self-bet post with no text -> ignored")
            return

        # Image-tip CHANNELS (Eddie AFL, Zak/Trial racing): the post's image
        # IS the tip. Download it, vision-parse, and route by sport. These
        # channels own the whole post — an image is parsed+placed ($1/u while
        # gated), a text-only post is surfaced as a manual alert (never fed to
        # the text parser, which doesn't understand racing/image formats).
        if channel_cfg.get("image_tips"):
            img_tipster = channel_cfg.get("parser", "unknown")
            img_sport = channel_cfg.get("sport", "")
            img_unit_size = channel_cfg.get("unit_size", 1.0)
            img_default_units = channel_cfg.get("default_units", 1.0)
            # v5.58 (Wilson): gate the vision path on ACTUAL image posts.
            # `event.media` is ALSO truthy for a plain TEXT message with a
            # link preview (MessageMediaWebPage), polls, etc. — those were
            # being downloaded + vision-parsed "as images" (slow Groq call,
            # 0 tips, manual-ping noise; the 2026-06-13 10:24 Groq 400). Only
            # a real PHOTO or an image-sent-as-FILE (mime image/*) is
            # vision-parseable. A PDF/doc (Zak posts a PDF DUPE of his
            # already-placed tip images) DROPS with a log line, never a
            # manual ping. Any other media on a text post falls through to
            # the normal TEXT handling below.
            _doc = getattr(event, "document", None)
            _is_image_post = bool(getattr(event, "photo", None)) or (
                _doc is not None
                and _doc_mime_is_image(getattr(_doc, "mime_type", ""))
            )
            if event.media and not _is_image_post:
                if _doc is not None:
                    log.info(
                        f"[{channel_name}] non-image media post (mime="
                        f"{getattr(_doc, 'mime_type', '?')}) -> dropped "
                        f"(PDF/doc dupe of the image tips; not vision-parseable)"
                    )
                    return
                log.info(
                    f"[{channel_name}] non-image media (web preview/poll/etc.) "
                    f"on a text post -> routing to TEXT handling"
                )
            if _is_image_post:
                log.info(f"[{channel_name}] image-tip post from {sender_id}; downloading for vision parse")
                try:
                    img_bytes = await event.download_media(file=bytes)
                except Exception as e:
                    log.error(f"[{channel_name}] image download failed: {e}")
                    notifier.notify_image_alert(channel_name, f"(image download failed: {e})")
                    return
                if not img_bytes:
                    log.warning(f"[{channel_name}] download_media returned no bytes")
                    notifier.notify_image_alert(channel_name, "(image could not be downloaded)")
                    return
                asyncio.create_task(
                    _process_image_tip(
                        img_bytes, img_tipster, img_sport, img_unit_size,
                        img_default_units, msg_time, channel_name, text,
                    )
                )
            elif text:
                # Only surface text posts that look like a bet/instruction;
                # drop plain chatter (logged, never lost) to stop flooding
                # manual with 'thanks'/'good luck'/emoji noise (2026-06-03).
                # Pass img_sport so the BUG B recap guard is SKIPPED for racing
                # (the racing text path places real money + has its own results
                # guard) but applied for Eddie AFL (text only pings manual).
                if _image_text_is_actionable(text, img_sport):
                    if (img_sport or "").lower() == "racing":
                        # v5.21 (Wilson 2026-06-06): Zak/Trial post some real tips
                        # as TEXT, not images ('Adding Lingani for tomorrow').
                        # PARSE the text + route through the racing pipeline so a
                        # genuine text tip PLACES — a full tip (track+race#) places;
                        # runner-only / no-race# -> manual (Guard 2); chatter that
                        # slipped the keyword filter -> parser finds no runner ->
                        # dropped (no manual ping). AFL image channels (Eddie) keep
                        # the old manual-alert behaviour.
                        log.info(f"[{channel_name}] actionable racing TEXT post -> parsing for placement")
                        asyncio.create_task(
                            _process_text_racing_tip(
                                text, img_tipster, img_unit_size,
                                img_default_units, msg_time, channel_name,
                            )
                        )
                    elif (img_sport or "").lower() == "afl" and EDDIE_TEXT_PLACE_ENABLED:
                        # v6.06 (Wilson 2026-07-25): Eddie posts some COMPLETE, placeable
                        # bets as TEXT follow-ups, not betslip images (e.g. "2.5u -
                        # Richmond +41.5 $1.91 - 365", "Ross over 22.5 at $1.87"). Parse +
                        # place them through the SAME AFL text pipeline Saiyan uses
                        # (_process_tip -> Claude AFL parse -> place_tip fan-out,
                        # Sportsbet-locked via TIPSTERS_FORCE_BOOKIE, EXACT-OR-MANUAL): a
                        # clean single places; a matchup/parlay/unresolvable market fails
                        # to resolve in place_tip -> manual (never a wrong bet); CHATTER
                        # ("Adding one more") parses to 0 tips -> dropped, no ping. units
                        # REQUIRED (eddie_afl in UNITS_REQUIRED_TIPSTERS) so a no-stake
                        # text -> manual. dedup + caps applied by _process_tip. Gated by
                        # EDDIE_TEXT_PLACE_ENABLED (ships OFF). AFL image betslip path
                        # unchanged.
                        # AUDIT FIX (HIGH): a HALF/QUARTER PERIOD handicap posted as TEXT
                        # ("Hawthorn -5.5 2nd Half") loses its period in the text parser
                        # and would place FULL-GAME (wrong market) — the period-placement
                        # path only runs for the IMAGE schema. Route any period-qualified
                        # text to MANUAL up front (belt), mirroring the image
                        # _partial_period + Saiyan _SAIYAN_PERIOD_HC_RE guards; the odds
                        # floor/ceiling is the suspenders for anything that slips.
                        if _SAIYAN_PERIOD_HC_RE.search(text or ""):
                            log.info(
                                f"[{channel_name}] Eddie AFL text has a HALF/QUARTER period "
                                f"marker -> manual (text path places full-game only, wrong "
                                f"market otherwise): {text[:120]}")
                            notifier.notify_image_alert(channel_name, text)
                        else:
                            log.info(f"[{channel_name}] actionable AFL TEXT post -> parsing for placement")
                            asyncio.create_task(
                                _process_tip(
                                    text, img_tipster, img_sport,
                                    img_unit_size, img_default_units, msg_time, channel_name,
                                )
                            )
                    else:
                        log.info(f"[{channel_name}] text-only post on image channel (actionable) -> manual alert: {text[:120]}")
                        notifier.notify_image_alert(channel_name, text)
                else:
                    log.info(f"[{channel_name}] text-only chatter on image channel -> dropped (not bet-like): {text[:80]}")
            return

        # Detect image/media - alert only.
        # Saiyan AFL exempted: their channel posts an image of the same tip
        # text every time (literally a screenshot of what's already in the
        # message), so the image alert is always a duplicate of the text
        # tip that follows seconds later. Suppressing avoids double-pings.
        # Every other tipster still gets image alerts since they may post
        # standalone images (e.g. infographics, slate previews) that warrant
        # manual attention.
        is_saiyan = channel_cfg.get("parser") == "saiyan_afl"
        if event.media and not text:
            log.info(f"[{channel_name}] Image/media from {sender_id}")
            if not is_saiyan:
                notifier.notify_image_alert(channel_name, "(image with no text)")
            return
        if event.media and text:
            log.info(f"[{channel_name}] Image+text from {sender_id}: {text[:80]}...")
            if not is_saiyan:
                notifier.notify_image_alert(channel_name, text)

        if not text:
            return

        log.info(f"[{channel_name}] Message from {sender_id}: {text[:100]}...")

        tipster = channel_cfg.get("parser", "unknown")
        sport = channel_cfg.get("sport", "nba")
        unit_size = channel_cfg.get("unit_size", 1.0)
        default_units = channel_cfg.get("default_units", 1.0)

        # Shook: buffer messages, only process on bet trigger
        if channel_cfg.get("buffer_messages"):
            # Snapshot context BEFORE adding the current message so the
            # context reflects the buffer state at the moment the trigger
            # was evaluated. Adding first then snapshotting risks a race
            # where a concurrent Shook task mutates _shook_buffer between
            # the buffer-add and the context read.
            context = _shook_get_context()
            _shook_buffer_add(text)

            if _shook_should_process(text):
                combined = f"RECENT CONTEXT:\n{context}\n\nCURRENT MESSAGE:\n{text}"
                log.info(f"[{channel_name}] Shook trigger detected, sending to Groq with {len(_shook_buffer)} context msgs")
                asyncio.create_task(
                    _process_tip(combined, tipster, sport, unit_size, default_units, msg_time, channel_name)
                )
            else:
                log.debug(f"[{channel_name}] Buffered (no trigger): {text[:60]}...")
            return

        # All other channels: process immediately in background
        asyncio.create_task(
            _process_tip(text, tipster, sport, unit_size, default_units, msg_time, channel_name)
        )

    await client.start(phone=TELEGRAM_PHONE)
    log.info("Telethon client started")
    notifier.notify_startup()

    # ── v4.0 — load session metadata + per-sport priority lists ──
    # sessions.yaml is required for v4.0 placement paths (sessions 2 + 3).
    # Loading here so any schema problems fail loudly at startup rather than
    # mid-bet. Priority env vars are best-effort: empty lists just mean no
    # auto-placement for that (sport, kind) combo.
    #
    # When USE_LEGACY_PLACEMENT=true, skip all v4.0 startup work entirely
    # (no yaml load, no priority module init). True full rollback to v3.10:
    # session_priority module sits dormant, _place_with_spillover handles
    # singles via legacy SESSION_PRIORITY env var. Lets Wilson isolate any
    # v4-specific bugs by toggling the flag.
    if not USE_LEGACY_PLACEMENT:
        try:
            session_priority.load_sessions_yaml(SESSIONS_YAML_PATH)
        except FileNotFoundError as e:
            log.error(f"sessions.yaml load failed: {e}")
            notifier.notify_critical(
                f"sessions.yaml not found at {SESSIONS_YAML_PATH}. v4.0 routing "
                f"will fall back to legacy SESSION_PRIORITY behaviour."
            )
        except Exception as e:
            log.error(f"sessions.yaml load failed: {e}")
            notifier.notify_critical(f"sessions.yaml load failed: {e}")

        session_priority.load_priority_from_env()
        session_priority.log_startup_summary()

        # Stat-level fallback for tipsters that don't supply explicit alts
        # (Kev, AusBets). Missing yaml is non-fatal: returns empty config
        # and placement carries on as before.
        global _stat_fallback_cfg
        _stat_fallback_cfg = stat_fallback.load_stat_fallback_config(
            os.getenv("STAT_FALLBACK_YAML_PATH", "stat_fallbacks.yaml")
        )
    else:
        log.info(
            "USE_LEGACY_PLACEMENT=true — skipping v4.0 startup hooks "
            "(yaml + per-sport priority). Running v3.10 placement."
        )

    # Log active sessions at startup. Foreign sessions (other PCs sharing
    # the HyperBot key) are visible in the API but not in our sessions.yaml
    # — flag them in the log for visibility but do NOT seed them into
    # _initial_session_state, otherwise the watchdog will alert when those
    # other PCs restart their bots.
    sessions = hb.get_sessions()
    owned_sessions = [s for s in sessions if _is_owned_session(s.get("session_id", ""))]
    foreign_sessions = [s for s in sessions if not _is_owned_session(s.get("session_id", ""))]
    log.info(
        f"Startup sessions: {len(sessions)} total ({len(owned_sessions)} owned, "
        f"{len(foreign_sessions)} foreign — ignored by watchdog)"
    )
    session_lines = []
    for s in owned_sessions:
        line = f"{s.get('bookie','')} - session {s.get('session_id','')} - {s.get('username', '')}"
        log.info(f"  {line}")
        session_lines.append(line)
        if s.get("active"):
            _initial_session_state[str(s["session_id"])] = s
    for s in foreign_sessions:
        log.info(
            f"  [foreign] {s.get('bookie','')} - session "
            f"{s.get('session_id','')} - {s.get('username', '')}"
        )

    # Send startup summary to Telegram. Owned-session count only — the
    # Telegram message is the operator-facing view, foreign noise stays in
    # logs. Version NUMBER only (not the full changelog): TIPBOT_VERSION is a
    # multi-paragraph changelog now and overflowed Telegram's 4096-char limit
    # (HTTP 400 "message is too long", v5.18 deploy). v5.39: the file-log startup
    # banner is ALSO trimmed to the version number — the full TIPBOT_VERSION lives
    # in config.py + CHANGELOG.md as the audit record.
    _ver_short = TIPBOT_VERSION.split(" (", 1)[0]
    if owned_sessions:
        notifier.notify_info(
            f"TipBot {_ver_short}\n"
            f"code fingerprint {CODE_FINGERPRINT}\n"
            f"Started with {len(owned_sessions)} owned session(s):\n" +
            "\n".join(f"• {l}" for l in session_lines)
        )
    else:
        notifier.notify_critical(
            f"TipBot {_ver_short} (fingerprint {CODE_FINGERPRINT}) "
            f"started but NO owned HyperBot sessions are active!"
        )

    # v6.03 bug #2: STARTUP pending-bets reconcile (ALERT-ONLY, gated OFF by default).
    # On a restart (freeze-watchdog os._exit / crash) a bet that was in-flight at death
    # is invisible to the ledger; this sweeps /api/pending_bets for owned accounts and
    # ALERTS Wilson to verify+record. NEVER re-places. Background task off the loop so
    # it can't block the listener / freeze ticker.
    if STARTUP_PENDING_RECONCILE_ENABLED:
        asyncio.create_task(_startup_pending_reconcile(owned_sessions))
        log.info("Startup pending-bets reconcile scheduled (alert-only, background)")
    else:
        log.info("Startup pending-bets reconcile DISABLED (STARTUP_PENDING_RECONCILE_ENABLED=false)")

    # Refresh roster if stale (background, non-blocking)
    _check_and_refresh_roster()

    # Start session watchdog
    asyncio.create_task(_session_watchdog())
    log.info(f"Session watchdog started (poll every {WATCHDOG_INTERVAL_SEC}s)")

    # v6.08b: hourly book-reconciliation sweep + export for the disposals model.
    # Gated on DISPOSALS_MODEL_RECONCILE_ENABLED and deliberately NOT on
    # DISPOSALS_MODEL_ENABLED — the tipster ships dormant, but the model has live
    # tranches and hand-placed money to reconcile now, so the export must flow while
    # the placement side is still switched off.
    if DISPOSALS_MODEL_RECONCILE_ENABLED:
        asyncio.create_task(_disposals_reconciliation_loop())

    # v6.08e: NOOP liveness detector. Gated on DISPOSALS_MODEL_ENABLED as well as its
    # own switch, and that is the opposite of the reconciliation loop above ON PURPOSE:
    # reconciliation reads the BOOK, which holds live money whether or not this tipster
    # is on, while liveness reads the CHANNEL, which is only registered when the tipster
    # is enabled. Running it while dormant would observe permanent silence and page
    # forever about a feed nobody has switched on.
    if DISPOSALS_MODEL_LIVENESS_ENABLED and DISPOSALS_MODEL_ENABLED:
        asyncio.create_task(_disposals_liveness_loop())
    elif DISPOSALS_MODEL_LIVENESS_ENABLED:
        log.info("[disposals_model] liveness detector idle (DISPOSALS_MODEL_ENABLED=false, "
                 "so the channel is not registered and there is nothing to watch)")

    # v6.07 (sweep #29): report priority sessions that were DEAD at startup. The
    # watchdog above can only see TRANSITIONS out of the already-active set, so a
    # session logged out before we booted is invisible to it. Alert-only, grace-delayed.
    if STARTUP_DEAD_SESSION_ALERT:
        asyncio.create_task(_startup_dead_session_check())
        log.info(f"Startup dead-session check scheduled "
                 f"(in {STARTUP_DEAD_SESSION_GRACE_SEC}s, alert-only)")
    else:
        log.info("Startup dead-session check DISABLED (STARTUP_DEAD_SESSION_ALERT=false)")

    # v5.9x: telethon connection-liveness watchdog (2026-07-06 incident: the socket
    # went half-open ~9.5h, silent-deaf, no auto-reconnect). Gated by a kill-switch.
    _telethon_reconnect_event = asyncio.Event()
    if TELETHON_WATCHDOG_ENABLED:
        asyncio.create_task(_telethon_liveness_watchdog(client, _telethon_reconnect_event))
        log.info(
            f"Telethon liveness watchdog started (probe every {TELETHON_WATCHDOG_INTERVAL_SEC}s, "
            f"timeout {TELETHON_WATCHDOG_PROBE_TIMEOUT_SEC}s, threshold {TELETHON_WATCHDOG_FAIL_THRESHOLD})"
        )
    else:
        log.info("Telethon liveness watchdog DISABLED (TELETHON_WATCHDOG_ENABLED=false)")

    # v6.02: event-loop FREEZE watchdog (2026-07-17 incident: the whole asyncio loop
    # froze ~6.5h on a hung sync HB call; every in-loop watchdog froze with it). The
    # ticker was created at main() top; the OS-thread half is started in __main__.
    if LOOP_FREEZE_WATCHDOG_ENABLED:
        log.info(
            f"Loop-freeze watchdog active (tick {LOOP_FREEZE_TICK_SEC}s, check "
            f"{LOOP_FREEZE_CHECK_SEC}s, force-restart after {LOOP_FREEZE_MAX_SILENCE_SEC}s frozen)"
        )
    else:
        log.info("Loop-freeze watchdog DISABLED (LOOP_FREEZE_WATCHDOG_ENABLED=false)")

    # Start Tip Titans poller (optional - only if credentials set)
    if os.getenv("TIPTITANS_EMAIL") and os.getenv("TIPTITANS_PASSWORD"):
        try:
            from tiptitans_processor import poll_loop as tiptitans_poll_loop
            asyncio.create_task(tiptitans_poll_loop(hb, notifier))
            log.info("Tip Titans poller started")
        except Exception as e:
            log.error(f"Failed to start Tip Titans poller: {e}")
            notifier.notify_critical(f"Tip Titans poller failed to start: {e}")
    else:
        log.info("Tip Titans poller disabled (TIPTITANS_EMAIL/PASSWORD not set)")

    for chat_id, cfg in TIPSTER_CHANNELS.items():
        log.info(f"Monitoring: {cfg['name']} (chat_id={chat_id})")

    log.info("Listening for tips... (Ctrl+C to stop)")
    await client.run_until_disconnected()

    # v5.9x: if the liveness watchdog forced this disconnect (dead/half-open socket),
    # route into the __main__ reconnect loop (fresh client, resume live) instead of a
    # clean process exit. A genuine clean shutdown leaves the event unset -> normal exit.
    if _telethon_reconnect_event.is_set():
        raise ConnectionError("telethon liveness watchdog forced reconnect (dead/half-open socket)")


if __name__ == "__main__":
    # Outer reconnect loop. Telethon's run_until_disconnected raises
    # ConnectionError after 5 failed reconnect attempts; without an outer
    # loop that kills the whole process. Most common trigger is a local
    # network blip (wifi drop, DNS flake, router restart) that
    # simultaneously breaks Telegram, HyperBot, and Tip Titans access.
    # Observed crashes: 2026-05-27 15:25, 2026-05-28 11:01. Both DNS
    # getaddrinfo failures across all three services at once.
    #
    # Backoff caps at 5 min so we don't flog a dead network. Reset
    # backoff if main() stayed up for more than 5 minutes. A long uptime
    # followed by a crash is a fresh blip, not a tight crash loop.
    _BACKOFF_INITIAL_SEC = 10
    _BACKOFF_MAX_SEC = 300
    _STABLE_UPTIME_SEC = 300

    # v6.02: OS-thread half of the event-loop FREEZE watchdog. Started ONCE here
    # (outside asyncio) so it SURVIVES a reconnect (which tears down & rebuilds the
    # loop). It reads _loop_heartbeat_ts, which the in-loop ticker keeps fresh while
    # a loop is running and which we DISARM (=0.0) during the reconnect backoff below
    # so it never false-fires while the loop is intentionally down. daemon=True so it
    # can't block interpreter shutdown. See _loop_freeze_watchdog_thread.
    if LOOP_FREEZE_WATCHDOG_ENABLED:
        threading.Thread(
            target=_loop_freeze_watchdog_thread,
            name="loop-freeze-watchdog",
            daemon=True,
        ).start()

    _backoff = _BACKOFF_INITIAL_SEC
    while True:
        _run_started = time.time()
        try:
            asyncio.run(main())
            log.info("main() returned cleanly, exiting")
            break
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, shutting down")
            break
        except ConnectionError as e:
            # Telethon's 5-fail ConnectionError lands here. Network-level
            # outage on the local machine, retry with backoff.
            _uptime = time.time() - _run_started
            if _uptime > _STABLE_UPTIME_SEC:
                _backoff = _BACKOFF_INITIAL_SEC
            log.warning(
                f"Connection lost after {_uptime:.0f}s uptime: {e}. "
                f"Restarting in {_backoff}s"
            )
            _loop_heartbeat_ts = 0.0  # DISARM freeze watchdog (loop down for backoff)
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, _BACKOFF_MAX_SEC)
        except Exception:
            # Anything else: log full traceback and restart. Better to
            # ride out an unknown crash than die silently waiting for
            # manual intervention.
            _uptime = time.time() - _run_started
            if _uptime > _STABLE_UPTIME_SEC:
                _backoff = _BACKOFF_INITIAL_SEC
            log.exception(
                f"Unhandled exception after {_uptime:.0f}s uptime, "
                f"restarting in {_backoff}s"
            )
            _loop_heartbeat_ts = 0.0  # DISARM freeze watchdog (loop down for backoff)
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, _BACKOFF_MAX_SEC)
