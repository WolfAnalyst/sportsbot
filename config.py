"""
TipBot configuration.
Loads secrets from .env and defines constants for tipster channels,
team mappings, stat mappings, and staking rules.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Version ──────────────────────────────────────────────────────────
# Bump on every meaningful change. Logged + Telegram'd at startup so it's
# always clear which build is running. The code fingerprint computed in
# main.py (_code_fingerprint) complements this — it catches partial/stale
# deploys even when this string wasn't bumped.
TIPBOT_VERSION = "v5.14 (2026-06-05: AFL fan-out re-enables the WRONG-SELECTION ceiling. After the v5.13 scrutiny flagged that the odds ceiling was also a wrong-selection guard (a catalog-valid-but-wrong pick — same-surname / ±1.0 line / wrong-O/U snap — was placing across ALL accounts), Wilson chose 'ceiling only'. _resolve_single_for_placement's apply_odds_guards flag is split into apply_ceiling + apply_floor; the fan-out now resolves with apply_ceiling=True, apply_floor=False — so a live price > 1.25x tipped routes the whole tip to manual (off the resolve-time catalog odds, no extra call), while a shorter-than-tipped live price still places. All other paths (NBA/MLB/handicap/total/SGM/racing via presolved=None) keep BOTH guards (defaults). Residual (accepted): longer-fill liability overshoot + h2h-no-odds uncapped sizing still rely on bookie MBL. Builds on v5.13.)"

# ── Telegram ─────────────────────────────────────────────────────────
_raw_api_id = os.getenv("TELEGRAM_API_ID", "0")
try:
    TELEGRAM_API_ID = int(_raw_api_id)
except ValueError:
    raise ValueError(f"TELEGRAM_API_ID must be an integer, got: {_raw_api_id!r}") from None
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")


def _env_float(name: str, default: str) -> float:
    """float(os.getenv(...)) with a clear startup error on a bad value.

    C7 (2026-05-31): a non-numeric LINE_TOLERANCE / MAX_UNITS / *_UNIT_SIZE
    env var previously crashed startup with a bare ValueError deep in module
    import. Mirror the TELEGRAM_API_ID guard above: fail fast with a message
    that names the offending variable so the misconfig is obvious."""
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got: {raw!r}") from None


def _env_int(name: str, default: str) -> int:
    """int(os.getenv(...)) with a clear startup error on a bad value. Same
    intent as _env_float. Used by the X-watcher (poll interval, bot id)."""
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var (1/true/yes => True)."""
    raw = os.getenv(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes")

# ── Sports-only fork switch (sportsbot) ──────────────────────────────
# Set true ONLY in the sportsbot fork's .env. When true, the channel filter at
# the BOTTOM of this file trims TIPSTER_CHANNELS down to the two sports tipsters
# the fork supports (Saiyan AFL + Shook NBA/MLB) plus an optional user test
# channel, and the fork simply doesn't ship the racing / X / image / other-NBA
# modules (main.py tolerates their absence). Defaults FALSE, so this is a pure
# no-op in tipbot -- nothing about the live bot changes. See memory
# `sportsbot-fork` + REBUILD_SPORTSBOT.md.
SPORTSBOT_MODE = _env_bool("SPORTSBOT_MODE", False)

# ── Notifications ────────────────────────────────────────────────────
NOTIFY_BOT_TOKEN = os.getenv("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "")
NOTIFY_SUCCESS_CHAT_ID = os.getenv("NOTIFY_SUCCESS_CHAT_ID", "")

# ── HyperBot ─────────────────────────────────────────────────────────
HYPERBOT_API_KEY = os.getenv("HYPERBOT_API_KEY", "")
HYPERBOT_BASE_URL = os.getenv(
    "HYPERBOT_BASE_URL",
    "https://api.hyperbot.imperialwealth.com",
)

# ── Groq ─────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Tip parser provider (LLM backend that parses tipster messages) ───
# Which LLM backend `tip_parser` uses: "groq" (default, LIVE) or "claude"
# (Anthropic Sonnet — SCAFFOLDED but INERT). The Claude path activates ONLY
# when ALL of these hold: TIP_PARSER_PROVIDER=claude, ANTHROPIC_API_KEY set,
# `anthropic` installed, and main.py's parser import pointed at tip_parser
# (one line). Until then this is a pure no-op — Groq stays the parser. If
# "claude" is selected but unusable (no key / SDK), tip_parser FAILS SAFE back
# to Groq rather than dropping tips. Full plan: REBUILD-note CLAUDE_PARSER.md.
TIP_PARSER_PROVIDER = os.getenv("TIP_PARSER_PROVIDER", "groq").strip().lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Sonnet 4.6 (Wilson's chosen model for the Claude parser path). Exact ID, no
# date suffix. Override via .env only if migrating models.
CLAUDE_PARSER_MODEL = os.getenv("CLAUDE_PARSER_MODEL", "claude-sonnet-4-6")

# ── Line Tolerance ───────────────────────────────────────────────────
LINE_TOLERANCE = _env_float("LINE_TOLERANCE", "1.0")

# ── Max Units ────────────────────────────────────────────────────────
MAX_UNITS = _env_float("MAX_UNITS", "3.0")

# ── MLB flat stake ───────────────────────────────────────────────────
# MLB is bet at a FLAT dollar stake, ignoring Shook's recommended unit
# (Wilson 2026-06-01: "flat 400 stakes no matter what the recommended unit is
# for shook mlb"). The SAME knob is the validation gate: it's set to $1 for the
# gated live test, then raised to the production value (e.g. 400) once the
# pipeline is validated. The clamp lives in the PLACING process
# (main._apply_mlb_flat_stake, called from _process_tip, loaded on restart) —
# NOT just here — so an .env edit alone can't change the live stake without a
# restart (the $600 lesson, 2026-06-01: a "$1-capped" test placed a real $600
# bet because main.py wasn't restarted). Set 0 to disable the flat override
# (MLB would then size like any Shook tip).
MLB_FLAT_STAKE = _env_float("MLB_FLAT_STAKE", "1")

# ── Max-odds CEILING (wrong-selection sanity guard, ALL sports tipsters) ──
# If the live (catalog/price-check) odds for the resolved selection exceed the
# tipped odds × this multiplier, the bet is NOT auto-placed — it routes to
# manual. A too-good-to-be-true price almost always means a WRONG selection or
# line. Applies to every sports tipster (Wilson 2026-05-31); pairs with the
# global 0.9× floor → auto-place band [tipped×0.9, tipped×1.25]. Per-tipster
# overrides exist (e.g. the X/EasyMoney tipster's X_MAX_ODDS_MULT). Set <=1 to
# disable. NOTE: racing has its own separate ceiling (racing_placer
# ODDS_DRIFT_CEILING = 1.5×); this one is for sports placement.
MAX_ODDS_MULT = _env_float("MAX_ODDS_MULT", "1.25")

# ── Tipster Channel Configs ──────────────────────────────────────────
# Unit sizes loaded from .env: SAIYAN_UNIT_SIZE, KEV_UNIT_SIZE, AUSBETS_UNIT_SIZE, SHOOK_UNIT_SIZE, TEST_UNIT_SIZE
SAIYAN_UNIT_SIZE = _env_float("SAIYAN_UNIT_SIZE", "150")
KEV_UNIT_SIZE = _env_float("KEV_UNIT_SIZE", "100")
AUSBETS_UNIT_SIZE = _env_float("AUSBETS_UNIT_SIZE", "100")
SHOOK_UNIT_SIZE = _env_float("SHOOK_UNIT_SIZE", "300")
TEST_UNIT_SIZE = _env_float("TEST_UNIT_SIZE", "1")

# ── Image-tip channels (vision-parsed via Groq Scout) ───────────────
# Three Telegram CHANNELS post tips as images: Eddie's Bets AFL (sports),
# Zak Trussell SA Racing + The Trial Sniper (racing). Only the channel
# admin/tipster can post in a broadcast channel, so these channels carry NO
# bot_id — the handler's sender filter is skipped and every post is treated
# as the tipster's. The image is downloaded (Telethon download_media works
# even on no-forward/no-save channels for a subscribed account) and run
# through groq_parser.parse_tip_image (Llama-4 Scout vision).
#
# TEST GATE: while IMAGE_TIPS_TEST_MODE is true (default), every image tip
# is staked at IMAGE_TIPS_TEST_UNIT_SIZE per recommended unit (default $1/u,
# so a 2.5u tip = $2.50, a 1u tip = $1). Enforced in the PLACING process
# (main._apply_image_test_stake for sports; the racing image orchestrator
# for racing) — NOT just here — so an .env edit alone can't change the live
# stake without a restart (the $600 lesson, 2026-06-01). Flip
# IMAGE_TIPS_TEST_MODE=false and set real *_UNIT_SIZE values for production.
IMAGE_TIPS_TEST_MODE = _env_bool("IMAGE_TIPS_TEST_MODE", True)
IMAGE_TIPS_TEST_UNIT_SIZE = _env_float("IMAGE_TIPS_TEST_UNIT_SIZE", "1")

# Production per-channel unit sizes (used ONLY when IMAGE_TIPS_TEST_MODE is
# false). Kept small until each tipster is validated.
EDDIE_UNIT_SIZE = _env_float("EDDIE_UNIT_SIZE", "10")
ZAK_UNIT_SIZE = _env_float("ZAK_UNIT_SIZE", "10")
TRIAL_SNIPER_UNIT_SIZE = _env_float("TRIAL_SNIPER_UNIT_SIZE", "10")

# Hard max units per racing-image play (Zak / Trial). DEDICATED cap, independent
# of the global MAX_UNITS — so raising MAX_UNITS for other tipsters never
# un-caps these. Their unit size will be $400 in production, so 3u = $1,200
# intended (then liability-capped to the $1000 win / $500 place thoroughbred
# cap per account); $1/u in test = $3. 2026-06-03.
IMAGE_RACING_MAX_UNITS = _env_float("IMAGE_RACING_MAX_UNITS", "3.0")

# Parser keys whose channels deliver tips as IMAGES (vision path). Maps the
# parser key -> sport so the handler routes racing tips to the racing
# pipeline (tiptitans_processor.process_image_racing_tip) and AFL tips to
# the sports pipeline (place_tip). Also gates _apply_image_test_stake.
IMAGE_TIP_PARSERS = {
    "eddie_afl": "afl",
    "zak_racing": "racing",
    "trial_sniper": "racing",
}

TIPSTER_CHANNELS = {
    -1003201095340: {
        "name": "Saiyan AFL",
        "parser": "saiyan_afl",
        "bot_id": 7869219767,
        "default_units": 1.0,
        "unit_size": SAIYAN_UNIT_SIZE,
        "sport": "afl",
    },
    -4925672658: {
        "name": "AusBets NBA",
        "parser": "ausbets_nba",
        "bot_id": 7964646743,
        "default_units": 1.0,
        "unit_size": AUSBETS_UNIT_SIZE,
        "sport": "nba",
    },
    -4634143283: {
        "name": "Kev NBA",
        "parser": "kev_nba",
        "bot_id": 8085856336,
        "default_units": 1.0,
        "unit_size": KEV_UNIT_SIZE,
        "sport": "nba",
    },
    -5130133556: {
        "name": "TipBot Test",
        "parser": "test",
        "bot_id": 1821631216,
        "default_units": 1.0,
        "unit_size": TEST_UNIT_SIZE,
        "sport": "nba",
    },
    # 2026-05-16: Shook's group was promoted from basic group to supergroup,
    # which flipped the chat_id from -4825377525 to -1003761736978. Telethon
    # silently stopped seeing messages because it was subscribed to the dead
    # ID. Bot_id is unchanged. Future regression to watch for: any tipster
    # group hitting supergroup conversion (Telegram auto-promotes on member
    # growth) will silently break the listener with no log signal. The
    # diagnostic log in main.py handler is the early warning for next time.
    -1003761736978: {
        "name": "Shook Plays",
        "parser": "shook",
        "bot_id": 7687523872,
        "default_units": 1.0,
        "unit_size": SHOOK_UNIT_SIZE,
        "sport": "auto",
        "buffer_messages": True,
    },
    # ── Image-tip CHANNELS (vision-parsed). NO bot_id by design: a broadcast
    #    channel's posts come from the channel (not a user sender_id), and
    #    only the tipster can post, so the sender filter is intentionally
    #    skipped (see IMAGE_TIPS_TEST_MODE above). image_tips=True routes the
    #    post's media through parse_tip_image. The tipbot's Telethon account
    #    MUST be subscribed to each channel to receive posts. ──
    -1003719024597: {
        "name": "Eddie's Bets AFL",
        "parser": "eddie_afl",
        "default_units": 1.0,
        "unit_size": EDDIE_UNIT_SIZE,
        "sport": "afl",
        "image_tips": True,
    },
    -1003155675019: {
        "name": "Zak Trussell SA Racing",
        "parser": "zak_racing",
        "default_units": 1.0,
        "unit_size": ZAK_UNIT_SIZE,
        "sport": "racing",
        "image_tips": True,
    },
    -1002980787986: {
        "name": "The Trial Sniper",
        "parser": "trial_sniper",
        "default_units": 1.0,
        "unit_size": TRIAL_SNIPER_UNIT_SIZE,
        "sport": "racing",
        "image_tips": True,
    },
}

# ── AFL Mappings ─────────────────────────────────────────────────────
AFL_TEAMS = {
    "ADE": "Adelaide", "ADEL": "Adelaide",
    "BRI": "Brisbane Lions", "BR": "Brisbane Lions", "BL": "Brisbane Lions", "BRIS": "Brisbane Lions",
    "CAR": "Carlton", "CARL": "Carlton",
    "COL": "Collingwood", "COLL": "Collingwood",
    "ESS": "Essendon", "ESSE": "Essendon",
    "FRE": "Fremantle", "FREM": "Fremantle", "FREO": "Fremantle",
    "GEE": "Geelong", "GEEL": "Geelong", "CATS": "Geelong",
    "GC": "Gold Coast", "GCS": "Gold Coast", "GCFC": "Gold Coast", "SUNS": "Gold Coast",
    "GWS": "Greater Western Sydney", "GIANTS": "Greater Western Sydney",
    "HAW": "Hawthorn", "HAWI": "Hawthorn", "HAWTH": "Hawthorn",
    "MEL": "Melbourne", "MELB": "Melbourne", "DEES": "Melbourne",
    # NWM is intentionally NOT mapped here — it collides with the AFL player
    # nickname "NWM" = Nasiah Wanganeen-Milera (St Kilda). NM is the
    # canonical North Melbourne code; Saiyan only uses NM in his messages.
    "NM": "North Melbourne", "NMFC": "North Melbourne", "KANGAS": "North Melbourne",
    "PA": "Port Adelaide", "PAFC": "Port Adelaide", "PORT": "Port Adelaide",
    "RIC": "Richmond", "RICH": "Richmond", "TIGERS": "Richmond",
    "STK": "St Kilda", "STKI": "St Kilda", "STKFC": "St Kilda", "SAINTS": "St Kilda",
    "SYD": "Sydney", "SWANS": "Sydney",
    "WCE": "West Coast", "WEAG": "West Coast", "WEST": "West Coast", "EAGLES": "West Coast",
    "WBD": "Western Bulldogs", "WB": "Western Bulldogs", "BULLDOGS": "Western Bulldogs", "DOGS": "Western Bulldogs",
}

AFL_TEAM_NAMES = {}
for abbr, full in AFL_TEAMS.items():
    AFL_TEAM_NAMES.setdefault(full, []).append(abbr)

# ── Bookmaker-specific AFL team aliases ───────────────────────────
# Squiggle's team names don't always match how bookmakers index their
# event listings. HyperBot's resolver tries case/order variants but
# doesn't alias team names. Translate at the placement boundary only.
# Add new entries when HyperBot returns "Could not find event" for an
# event that's actually live on the bookmaker (just under a different
# name).
BOOKIE_AFL_ALIASES = {
    "sportsbet": {
        "Greater Western Sydney": "GWS Giants",
    },
    # bet365 currently broken for AFL sports anyway
    # "bet365": {},
    # "tab": {},
    # "ladbrokes": {},
}

AFL_STAT_MAP = {
    "disposals": "disposals", "disposal": "disposals",
    "goals": "goals", "marks": "marks", "tackles": "tackles",
    "kicks": "kicks", "handballs": "handballs",
    "clearances": "clearances", "hitouts": "hitouts",
    "fantasy points": "fantasy_points",
    "fantasy_points": "fantasy_points", "fp": "fantasy_points", "fantasy": "fantasy_points",
}

# ── NBA / NBL Stat Mappings ──────────────────────────────────────────
NBA_STAT_MAP = {
    "p": "points", "pts": "points", "points": "points",
    "r": "rebounds", "reb": "rebounds", "rbd": "rebounds", "rebounds": "rebounds",
    "a": "assists", "ast": "assists", "assists": "assists",
    "pr": "points_rebounds", "rp": "points_rebounds",
    "pa": "points_assists", "ap": "points_assists",
    "ra": "assists_rebounds", "ar": "assists_rebounds",
    "pra": "points_rebounds_assists",
    "threes": "threes", "3s": "threes",
    "blocks": "blocks", "blk": "blocks",
    "steals": "steals", "stl": "steals",
}

# ── MLB Stat Mappings (2026-06-01) ───────────────────────────────────
# Maps tip phrasings to the `stat` values Sportsbet uses INSIDE the single
# 'player_stats' MLB market (confirmed live 2026-06-01: rbis, total_bases,
# runs, hits, home_runs, strikeouts, singles, doubles, triples, stolen_bases,
# h_r_rbi). Used by _match_mlb_player_prop.
MLB_STAT_MAP = {
    "hits": "hits", "hit": "hits", "h": "hits",
    "total bases": "total_bases", "total_bases": "total_bases", "tb": "total_bases", "bases": "total_bases",
    "rbi": "rbis", "rbis": "rbis", "runs batted in": "rbis",
    "runs": "runs", "run": "runs", "runs scored": "runs",
    "home run": "home_runs", "home runs": "home_runs", "hr": "home_runs",
    "homer": "home_runs", "homers": "home_runs", "home_runs": "home_runs",
    "strikeout": "strikeouts", "strikeouts": "strikeouts", "k": "strikeouts", "ks": "strikeouts", "so": "strikeouts",
    "single": "singles", "singles": "singles",
    "double": "doubles", "doubles": "doubles", "2b": "doubles",
    "triple": "triples", "triples": "triples", "3b": "triples",
    "stolen base": "stolen_bases", "stolen bases": "stolen_bases", "stolen_bases": "stolen_bases",
    "sb": "stolen_bases", "steal": "stolen_bases", "steals": "stolen_bases",
    "h+r+rbi": "h_r_rbi", "hits runs rbis": "h_r_rbi", "hrr": "h_r_rbi", "h_r_rbi": "h_r_rbi",
    # Defensive: older Groq prompt wording emitted "hits_runs_rbis"; the live
    # catalog stat is `h_r_rbi`, so alias it so a stale parse still matches.
    "hits_runs_rbis": "h_r_rbi", "hrrbi": "h_r_rbi",
}

# ── Kev Deobfuscation ────────────────────────────────────────────────
KEV_CHAR_MAP = {
    "!": "i", "@": "a", "0": "o", "3": "e", "1": "l", "$": "s", "5": "s",
}

# ── v4.0 — Sessions YAML + per-sport priority ───────────────────────
# Path to sessions.yaml (per-session metadata: bookmaker, liability caps,
# boost eligibility). Wilson maintains this file manually; restart tipbot
# to reload changes.
SESSIONS_YAML_PATH = os.getenv("SESSIONS_YAML_PATH", "sessions.yaml")

# Per-sport priority lists. Comma-separated session IDs in priority order.
# Sessions not in the relevant list are excluded from auto-placement for
# that (sport, kind) combo and routed to manual instead.
#
# These are intentionally exposed both as raw env strings (for legacy
# callers) and parsed lists via session_priority.load_priority_from_env().
NBA_SESSION_PRIORITY = os.getenv("NBA_SESSION_PRIORITY", "")
NBA_SGM_SESSION_PRIORITY = os.getenv("NBA_SGM_SESSION_PRIORITY", "")
AFL_SESSION_PRIORITY = os.getenv("AFL_SESSION_PRIORITY", "")
AFL_SGM_SESSION_PRIORITY = os.getenv("AFL_SGM_SESSION_PRIORITY", "")
RACING_SESSION_PRIORITY = os.getenv("RACING_SESSION_PRIORITY", "")

# v4.0 placement rollback flag. When true, all placement code uses the
# v3.10 path (legacy SESSION_PRIORITY env var, no liability caps, no
# multi-bookmaker price comparison). Session 1 startup hooks (yaml load,
# priority module init) are also skipped — the flag is a true full rollback.
# Default false: ship v4.0 logic active.
USE_LEGACY_PLACEMENT = os.getenv("USE_LEGACY_PLACEMENT", "false").strip().lower() in ("1", "true", "yes")

# ── AFL concurrent fan-out placement (v5.11/v5.12, 2026-06-05) ──────
# Saiyan + Eddie AFL singles place on ALL eligible Sportsbet sessions
# CONCURRENTLY (fired in parallel via a thread pool) instead of the sequential
# one-account-at-a-time spillover of _place_singles_v4. The intended unit stake
# is split EVENLY across the eligible accounts; each account then walks its OWN
# liability ladder (top bracket from sessions.yaml first, dropping a bracket on
# a stake-too-high / MBL reject — v5.12). The exact catalog line is resolved
# ONCE per bookie (the line resolver is kept — a catalog miss still routes to
# manual) and that resolved payload is fanned out; there is NO per-account price
# check. v5.14: the WRONG-SELECTION ceiling (live > 1.25x tipped -> manual) is
# KEPT (it runs off the resolve-time catalog odds already in hand — no extra
# call); the price-FLOOR is dropped (a shorter-than-tipped live price still
# places). Per rung initial_post_max_attempts=1,
# and the ladder STOPS on an ambiguous/maybe-landed rung (no double-stake).
# Default ON (Wilson 2026-06-05: "build it live now"). Set
# AFL_CONCURRENT_FANOUT=false to revert AFL to the sequential _place_singles_v4
# path. Only affects sport == "afl" singles; NBA/MLB/racing/SGM untouched.
# Restart tipbot to apply.
AFL_CONCURRENT_FANOUT = os.getenv("AFL_CONCURRENT_FANOUT", "true").strip().lower() in ("1", "true", "yes")

# Minimum per-account stake floor for the fan-out. When the even split
# (intended / num_accounts) rounds below this, the account places this floor
# instead so the bet still reaches the bookie. Primarily matters for Eddie in
# IMAGE_TIPS_TEST_MODE ($1/unit), where a $1 tip split across 4 accounts would
# otherwise be ~$0.25/account (below the bookie minimum). For live Saiyan
# ($600/unit) the floor never binds. Set to 0 to disable (sub-min splits then
# just fail at the bookie). Liability caps still bound the stake from above.
AFL_FANOUT_MIN_STAKE = float(os.getenv("AFL_FANOUT_MIN_STAKE", "1.0"))

# ── Ambiguous-outcome reconciliation (v4.5, 2026-05-31) ─────────────
# When a placement gets a slow rejection (Erasmus class), query
# /api/pending_bets to CHECK whether the bet actually landed instead of
# guessing from latency. Two tiers, both default OFF:
#   RECONCILE_AMBIGUOUS — master switch. When on, a confirmed-landed bet is
#     recorded with its real bet_id + actual stake (Tier 1, safe regardless of
#     feed lag — finding it = it's there). A confirmed-NOT-found or an API
#     failure still falls back to today's conservative debit-as-placed + alert.
#   RECONCILE_SPILL — Tier 2. Only when BOTH flags on: a confirmed-not-found
#     slow rejection is treated as a genuine reject and the stake is
#     laddered/spilled to recover it. DANGER if the pending_bets feed lags
#     beyond the poll window (a landed-but-not-yet-shown bet would be re-bet) —
#     enable ONLY after validating feed latency in the logs. Applies to the
#     SLOW-REJECTION class only; text-pattern (Pointsbet "intercepted") never
#     spills (it lands after MBL/trader review).
RECONCILE_AMBIGUOUS = os.getenv("RECONCILE_AMBIGUOUS", "false").strip().lower() in ("1", "true", "yes")
RECONCILE_SPILL = os.getenv("RECONCILE_SPILL", "false").strip().lower() in ("1", "true", "yes")

# ── Handicap-SGM safety (2026-05-31) ─────────────────────────────────
# Route any SGM that contains a handicap (line / first_half_line) leg to MANUAL
# instead of attempting placement. Handicap legs inside SGMs (pick_own_line
# resolution) have been unreliable; until that's hardened, alert rather than
# risk a mis-placed leg. Set false to re-enable auto-placement of handicap SGMs.
AUTO_MANUAL_HANDICAP_SGM = os.getenv("AUTO_MANUAL_HANDICAP_SGM", "true").strip().lower() in ("1", "true", "yes")

# ── X (Twitter) watcher (2026-05-31) ─────────────────────────────────
# Watches an X account, filters for one capper, and FORWARDS matching posts
# into a Telegram group that this bot already auto-places from. Runs as a
# SEPARATE process (x_watcher.py); it never places bets itself. See
# X_WATCHER_HANDOFF.md. All values are env-driven and the channel below is
# only registered when X_FORWARD_CHANNEL_ID is set, so this is INERT by
# default — nothing changes until you deliberately configure it.
X_WATCH_ACCOUNT = os.getenv("X_WATCH_ACCOUNT", "AFLCapperLeague")
X_FILTER_CAPPER = os.getenv("X_FILTER_CAPPER", "EasyMoneyAFL")
X_POLL_SEC = _env_int("X_POLL_SEC", "60")              # be gentle: >=60s avoids lockout
# Ingestion method: "twikit" (cookie API, fragile vs X anti-bot) or "playwright"
# (real headless browser intercepting the UserTweets JSON — robust against the
# transaction-id wall twikit hits). See X_WATCHER_HANDOFF.md.
X_FETCH_METHOD = os.getenv("X_FETCH_METHOD", "twikit").strip().lower()
X_BROWSER_PROFILE_DIR = os.getenv("X_BROWSER_PROFILE_DIR", "x_browser_profile")
X_BROWSER_HEADLESS = _env_bool("X_BROWSER_HEADLESS", True)
# Anti-detection: X flags the AUTOMATED LOGIN hardest. Best practice is to log in
# as a HUMAN in a normal browser, copy the session cookies (DevTools -> Application
# -> Cookies -> x.com -> auth_token + ct0), and set them here. The watcher then
# only READS with that human session — no automated login, no login challenge.
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN", "").strip()
X_CT0 = os.getenv("X_CT0", "").strip()
# Use a real installed browser ("chrome" / "msedge") instead of bundled Chromium
# (less detectable). Empty = bundled chromium.
X_BROWSER_CHANNEL = os.getenv("X_BROWSER_CHANNEL", "").strip()
X_COOKIES_PATH = os.getenv("X_COOKIES_PATH", "x_cookies.json")
X_STATE_PATH = os.getenv("X_STATE_PATH", "x_watcher_state.json")
X_FORWARD_BOT_TOKEN = os.getenv("X_FORWARD_BOT_TOKEN", "")   # Telegram bot that posts the tips
X_FORWARD_CHANNEL_ID = os.getenv("X_FORWARD_CHANNEL_ID", "") # the Telegram group id to post into
X_FORWARD_BOT_ID = _env_int("X_FORWARD_BOT_ID", "0")        # the forward bot's user id (0 = skip sender check)
X_TIPSTER = os.getenv("X_TIPSTER", "easymoney_afl")        # Groq-only tipster name (no regex parser)
# Force these tips onto ONE bookie (HARD: if that bookie has no active session,
# the tip routes to manual — it is NEVER placed on a different bookie). Empty =
# price-shop across all AFL bookies like Saiyan. EasyMoneyAFL = sportsbet per Wilson.
X_FORCE_BOOKIE = os.getenv("X_FORCE_BOOKIE", "").strip().lower()
# Max-odds sanity CEILING for the X tipster (EasyMoneyAFL). If Sportsbet's live
# odds for the resolved selection exceed tipped x this, refuse to auto-place
# (a too-good-to-be-true price usually means a WRONG selection/line) and route
# to manual instead. Pairs with the global 0.9x floor → auto-place band
# [tipped x0.9, tipped x1.25]. Set 0 (or <=1) to disable.
X_MAX_ODDS_MULT = _env_float("X_MAX_ODDS_MULT", "1.25")
# Filter knobs — by default forward only top-level original posts.
X_DROP_REPLIES = _env_bool("X_DROP_REPLIES", True)
X_DROP_RETWEETS = _env_bool("X_DROP_RETWEETS", True)
X_DROP_QUOTES = _env_bool("X_DROP_QUOTES", True)
# AFLCapperLeague posts an automated, labeled format ("Bet: ...", "Odds: ...").
# When True, the watcher extracts the Bet + Odds into a clean "afl <bet> @ <odds>"
# line (prefix triggers the deterministic sport override; strips the capper/chrome
# noise). Falls back to forwarding the RAW tweet if no "Bet:" label is present.
X_EXTRACT_TIP = _env_bool("X_EXTRACT_TIP", True)
# Unit size for EasyMoneyAFL tips. Defaults to the $1 TEST size on purpose —
# fail-safe so an un-set go-live UNDER-stakes ($1) rather than OVER-stakes.
# SET EASYMONEY_UNIT_SIZE explicitly (e.g. to SAIYAN_UNIT_SIZE) before real-size
# placement. (Open question for Wilson — see X_WATCHER_HANDOFF.md.)
EASYMONEY_UNIT_SIZE = _env_float("EASYMONEY_UNIT_SIZE", str(TEST_UNIT_SIZE))

# Gated registration: only when a forward channel is configured. Until then
# the X watcher is fully inert and tipbot's channel set is unchanged. The
# tipster name X_TIPSTER has NO regex parser, so it's a "Groq-only tipster"
# (generic AFL parse, alerts on parse failure) — never misrouted to Saiyan's
# emoji preprocessing. The forward GROUP must have the forward bot posting and
# Wilson's Telethon account as a member (see X_WATCHER_HANDOFF.md).
if X_FORWARD_CHANNEL_ID:
    try:
        TIPSTER_CHANNELS[int(X_FORWARD_CHANNEL_ID)] = {
            "name": "EasyMoneyAFL (via X)",
            "parser": X_TIPSTER,
            "bot_id": X_FORWARD_BOT_ID or None,
            "default_units": 1.0,
            "unit_size": EASYMONEY_UNIT_SIZE,
            "sport": "afl",
        }
    except ValueError:
        # Malformed X_FORWARD_CHANNEL_ID -> do NOT register (fail safe; the
        # watcher simply won't have a place to forward to / auto-place from).
        pass

# ── Sports-only fork (sportsbot) channel filter ─────────────────────
# Applied LAST so it overrides every registration above. When SPORTSBOT_MODE is
# true (sportsbot fork only), keep just the two sports tipsters this fork runs --
# Saiyan (AFL) and Shook (NBA + MLB, sport=auto, incl. the HRRBI->2-leg-SGM
# rule) -- and drop everything else (other NBA tipsters, the internal test
# channel, and the racing / image / X channels). NO-OP in tipbot, where
# SPORTSBOT_MODE is false, so the live channel set is untouched.
if SPORTSBOT_MODE:
    _SB_KEEP_PARSERS = {"saiyan_afl", "shook"}
    TIPSTER_CHANNELS = {
        cid: cfg for cid, cfg in TIPSTER_CHANNELS.items()
        if cfg.get("parser") in _SB_KEEP_PARSERS
    }
    # Optional: the fork user's OWN test channel/group. Post any tip there and
    # it places at the $1 TEST_UNIT_SIZE gate (sport=auto -> Groq decides
    # AFL/NBA/MLB). bot_id None = no sender check, so the user's own posts count
    # as tips. Inert until SPORTSBOT_TEST_CHANNEL_ID is set in .env.
    _sb_test_id = os.getenv("SPORTSBOT_TEST_CHANNEL_ID", "").strip()
    if _sb_test_id:
        try:
            TIPSTER_CHANNELS[int(_sb_test_id)] = {
                "name": "My Test Channel",
                "parser": "test",
                "bot_id": None,
                "default_units": 1.0,
                "unit_size": TEST_UNIT_SIZE,
                "sport": "auto",
            }
        except ValueError:
            pass
