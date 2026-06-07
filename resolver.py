"""
AFL event resolver via Squiggle API.
"""

import requests
import logging
import time
from datetime import datetime
from typing import Optional
from config import AFL_TEAMS

log = logging.getLogger(__name__)

_fixture_cache: dict = {}
SQUIGGLE_URL = "https://api.squiggle.com.au/"


def _fetch_afl_fixtures_by_year() -> list[dict]:
    """Fetch all upcoming AFL fixtures for the current year."""
    year = datetime.now().year
    cache_key = f"year_{year}"
    if cache_key in _fixture_cache:
        return _fixture_cache[cache_key]

    try:
        # Build URL manually - Squiggle uses semicolons as param separators
        # requests.get(params=...) would URL-encode the semicolons and break it
        url = f"{SQUIGGLE_URL}?q=games;year={year};complete=!100"
        resp = requests.get(
            url,
            headers={"User-Agent": "tipbot/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        games = data.get("games", [])
        # H38: only cache non-empty results — an empty response may be a
        # transient Squiggle glitch. Caching [] would poison every subsequent
        # call until restart.
        if games:
            _fixture_cache[cache_key] = games
        log.info(f"Fetched {len(games)} upcoming AFL fixtures for {year}")
        return games
    except Exception as e:
        log.warning(f"Squiggle API unavailable: {e}")
        return []


def afl_games_in_play(ref_ts: float = None, ahead_sec: int = 2700,
                      behind_sec: int = 10800) -> list:
    """AFL games about to start / in progress near ref_ts (default now).

    A game qualifies if its start (Squiggle `unixtime`, UTC epoch) is within
    [ref_ts - behind_sec, ref_ts + ahead_sec] — default: started up to 3h ago
    (in-progress) through starting in 45 min (about to jump). The Eddie surname
    pipeline overrides ahead_sec to 2h (EDDIE_GAME_LOOKAHEAD_SEC) so a bare
    surname still scopes to the right team when Eddie posts up to 2h pre-bounce.
    unixtime is used
    so there is NO timezone reasoning (the `date` field is naive local venue
    time; unixtime is UTC epoch). Source is _fetch_afl_fixtures_by_year
    (complete=!100, so finished games are already excluded). Returns the
    qualifying game dicts (each has hteam/ateam/unixtime).

    Used by the Eddie image pipeline to scope a bare-surname player prop to the
    teams playing right now (Eddie posts last-name-only props at game time)."""
    ref = ref_ts if ref_ts is not None else time.time()
    out = []
    for g in _fetch_afl_fixtures_by_year():
        ut = g.get("unixtime")
        if not ut:
            continue
        try:
            delta = float(ut) - ref
        except (ValueError, TypeError):
            continue
        if -behind_sec <= delta <= ahead_sec:
            out.append(g)
    return out


def team_key(name: str) -> str:
    """Normalised team key for cross-source matching (Squiggle hteam/ateam vs
    roster_afl.json team names). Lowercases, strips punctuation, and drops a
    trailing AFL nickname suffix so 'Adelaide Crows' == Squiggle 'Adelaide' and
    'Sydney Swans' == 'Sydney'. 'Greater Western Sydney' / 'Western Bulldogs'
    have no strippable nickname and stay distinct (no false collision)."""
    return _strip_nickname(_normalise_team(name))


def _normalise_team(name: str) -> str:
    if not name:
        return ""
    return name.strip().lower().replace(".", "")


# AFL nickname suffixes that Squiggle sometimes uses on hteam/ateam and
# sometimes doesn't (and the roster scrape uses consistently). Order is
# longest-first so "gold coast suns" strips correctly. "Bulldogs" is
# intentionally absent. It's part of the team name "Western Bulldogs",
# not a strippable nickname like "Crows" or "Swans".
_AFL_NICKNAME_SUFFIXES = (
    " swans", " crows", " lions", " cats", " suns", " eagles",
    " hawks", " demons", " tigers", " magpies", " dockers",
    " saints", " bombers", " blues", " power", " kangaroos",
)


def _strip_nickname(name_norm: str) -> str:
    """Strip a known AFL nickname suffix from a normalised team name.

    'sydney swans' -> 'sydney'; 'adelaide crows' -> 'adelaide'.
    'greater western sydney' -> unchanged (no nickname suffix to strip).
    'western bulldogs' -> unchanged (Bulldogs is the team, not a nickname).
    """
    for suf in _AFL_NICKNAME_SUFFIXES:
        if name_norm.endswith(suf):
            return name_norm[: -len(suf)]
    return name_norm


def _team_event_matches(target_norm: str, hteam: str, ateam: str) -> bool:
    """Match target team to a Squiggle event, exact-preferred.

    Rule: exact match (after normalisation) is the gold standard. If no
    exact match, fall back to substring match in ONE direction only:
    target_norm IN team_name (i.e. team_name extends target with extra
    words). Reverse direction (team_name IN target_norm) is rejected
    because it caused 'Greater Western Sydney' to match 'Sydney' as a
    substring (Lachie Ash regression 2026-05-03).

    Then a final pass strips AFL nickname suffixes from both sides and
    compares for equality. This handles the inverse case the Lachie Ash
    fix did not cover: roster has 'Sydney Swans' (with suffix) but
    Squiggle hteam is 'Sydney' (no suffix). Substring would need to walk
    in the rejected direction; suffix-strip equality is safer because it
    only succeeds when the difference between the two strings is a known
    nickname word. Bice/Maynard SGM 2026-05-15 was the regression that
    motivated this.

    Examples:
      target='Greater Western Sydney', hteam='Sydney' -> NO MATCH (Lachie Ash)
      target='Greater Western Sydney', hteam='Greater Western Sydney' -> MATCH (exact)
      target='Sydney', hteam='Sydney Swans' -> MATCH (target IN team)
      target='Sydney Swans', hteam='Sydney' -> MATCH (suffix-strip equality)
      target='Adelaide', hteam='Adelaide Crows' -> MATCH
      target='Adelaide Crows', hteam='Adelaide' -> MATCH (suffix-strip equality)
    """
    if not target_norm:
        return False
    h = _normalise_team(hteam)
    a = _normalise_team(ateam)
    # Exact match preferred (either side of fixture)
    if target_norm == h or target_norm == a:
        return True
    # One-direction substring fallback: target IN team_name, not the reverse
    if h and target_norm in h:
        return True
    if a and target_norm in a:
        return True
    # Suffix-strip equality. Only matches when the only difference between
    # the two strings is a known AFL nickname suffix word. Doesn't widen
    # the Lachie Ash guard because 'Sydney' is not in _AFL_NICKNAME_SUFFIXES.
    target_stripped = _strip_nickname(target_norm)
    if target_stripped and (target_stripped == _strip_nickname(h) or
                            target_stripped == _strip_nickname(a)):
        return True
    return False


def resolve_afl_event(team_full: str) -> Optional[str]:
    """Resolve an AFL team name to 'Home v Away' event string."""
    if not team_full:
        log.warning("No team provided for AFL resolution")
        return None

    # Map a code/nickname to the full Squiggle team name. AFL_TEAMS keys are
    # all abbreviations/nicknames (ADE, GWS, GIANTS, SAINTS, TIGERS, EAGLES,
    # BULLDOGS, ...) — no full team name uppercases to a key — so this is safe
    # to apply at any length. The old `len <= 4` guard silently skipped the
    # 5+ char nicknames, so "Giants" (6) never mapped and the handicap tip
    # "giants +50.5hc" failed event resolution (2026-05-31).
    if team_full.upper() in AFL_TEAMS:
        mapped = AFL_TEAMS[team_full.upper()]
        if mapped != team_full:
            log.info(f"Mapped AFL code to: '{mapped}'")
        team_full = mapped

    games = _fetch_afl_fixtures_by_year()
    target = _normalise_team(team_full)
    # H37 (corrected 2026-05-30): compare on Squiggle's `unixtime` epoch field
    # vs time.time(), both UTC epoch seconds. The earlier fix tried to parse
    # the `date` string as UTC, but Squiggle's `date` is NAIVE LOCAL venue time
    # (e.g. "2026-05-31 15:15:00" with a separate tz="+10:00" field) — it has no
    # embedded offset, so fromisoformat produced a naive datetime and crashed
    # against the aware `now` (TypeError: can't subtract naive and aware). Even
    # coerced to UTC-aware it would be ~10h off and silently skip evening games.
    # unixtime sidesteps all timezone reasoning permanently.
    now_ts = time.time()

    # Find the nearest upcoming game for this team
    best_game = None
    best_delta = None

    for game in games:
        hteam = game.get("hteam", "")
        ateam = game.get("ateam", "")

        # 2026-05-03 Lachie Ash regression: original logic was bidirectional
        # substring (target in team OR team in target). With target=
        # 'Greater Western Sydney', "sydney" (Squiggle's hteam for Sydney
        # Swans game) was a substring of "greater western sydney" so the
        # Sydney v Melbourne game matched. Both that game AND the actual
        # GWS game matched; closest-in-time picked Sydney v Melbourne.
        # Lachie Ash bet routed to the wrong game and Sportsbet rejected.
        #
        # New rule: exact match preferred; substring fallback ONLY in the
        # direction target IN team_name (not team_name IN target). So
        # 'Sydney' still matches 'Sydney Swans' (target shorter than full
        # team name), but 'Greater Western Sydney' no longer matches
        # 'Sydney' alone.
        if not _team_event_matches(target, hteam, ateam):
            continue

        # Seconds until game start from Squiggle's unixtime (UTC epoch).
        # unixtime is always present on games-query results; skip if absent
        # or non-numeric rather than risk a bad comparison.
        game_unixtime = game.get("unixtime")
        if not game_unixtime:
            continue
        try:
            delta_sec = float(game_unixtime) - now_ts
        except (ValueError, TypeError):
            continue

        # Only future games (or games within last 6 hours for in-progress)
        if delta_sec < -21600:
            continue

        if best_delta is None or abs(delta_sec) < abs(best_delta):
            best_delta = delta_sec
            best_game = game

    if best_game:
        event = f"{best_game['hteam']} v {best_game['ateam']}"
        log.info(f"Resolved '{team_full}' -> '{event}'")
        return event

    log.warning(f"Could not resolve AFL fixture for '{team_full}'")
    return None


def resolve_event_for_tip(tip) -> Optional[str]:
    """Resolve event name for a ParsedTip. AFL only."""
    if not tip.legs:
        return None
    if tip.sport != "afl":
        return None
    return resolve_afl_event(tip.primary_team)
