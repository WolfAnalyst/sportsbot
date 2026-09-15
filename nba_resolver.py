"""
NBA/NBL event resolver.

Resolves player/team names to "TeamA v TeamB" event strings for HyperBot.
Uses ESPN's free scoreboard API for daily schedules and the roster module
for player -> team mapping.
"""

import requests
import logging
from datetime import datetime, timedelta
from typing import Optional

from roster import resolve_player_name, get_player_team

log = logging.getLogger(__name__)

# Cache: {key: {"games": [...], "fetched_at": datetime}}
_schedule_cache: dict = {}
CACHE_EXPIRY_MINUTES = 30

ESPN_NBA_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_NBL_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nbl/scoreboard"
ESPN_MLB_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
ESPN_NFL_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
_ESPN_URLS = {"nba": ESPN_NBA_URL, "nbl": ESPN_NBL_URL, "mlb": ESPN_MLB_URL, "nfl": ESPN_NFL_URL}

# Common team name aliases for matching. All 30 NBA teams covered.
# Keys are lowercased — match via .lower() lookup. Includes:
#   - Mascot ("knicks", "lakers")
#   - City ("new york", "los angeles")
#   - Three-letter abbreviation ("nyk", "lal")
#   - Common nicknames ("dubs", "sixers", "wolves", "cavs")
# This is the source of truth for team-token recognition. If a tipster's
# token isn't here, it falls through to fuzzy player matching, which can
# misfire (2026-05-07 Knicks → Nick Smith Jr. → wrong event).
NBA_TEAM_ALIASES = {
    # Atlantic
    "celtics": "Boston Celtics", "boston": "Boston Celtics", "bos": "Boston Celtics",
    "nets": "Brooklyn Nets", "brooklyn": "Brooklyn Nets", "bkn": "Brooklyn Nets", "brk": "Brooklyn Nets",
    "knicks": "New York Knicks", "new york": "New York Knicks", "ny": "New York Knicks", "nyk": "New York Knicks", "ny knicks": "New York Knicks",
    "76ers": "Philadelphia 76ers", "sixers": "Philadelphia 76ers", "philadelphia": "Philadelphia 76ers", "philly": "Philadelphia 76ers", "phi": "Philadelphia 76ers", "phl": "Philadelphia 76ers",
    "raptors": "Toronto Raptors", "raps": "Toronto Raptors", "toronto": "Toronto Raptors", "tor": "Toronto Raptors",
    # Central
    "bulls": "Chicago Bulls", "chicago": "Chicago Bulls", "chi": "Chicago Bulls",
    "cavaliers": "Cleveland Cavaliers", "cavs": "Cleveland Cavaliers", "cleveland": "Cleveland Cavaliers", "cle": "Cleveland Cavaliers",
    "pistons": "Detroit Pistons", "detroit": "Detroit Pistons", "det": "Detroit Pistons",
    "pacers": "Indiana Pacers", "indiana": "Indiana Pacers", "ind": "Indiana Pacers",
    "bucks": "Milwaukee Bucks", "milwaukee": "Milwaukee Bucks", "mil": "Milwaukee Bucks",
    # Southeast
    "hawks": "Atlanta Hawks", "atlanta": "Atlanta Hawks", "atl": "Atlanta Hawks",
    "hornets": "Charlotte Hornets", "charlotte": "Charlotte Hornets", "cha": "Charlotte Hornets", "cho": "Charlotte Hornets",
    "heat": "Miami Heat", "miami": "Miami Heat", "mia": "Miami Heat",
    "magic": "Orlando Magic", "orlando": "Orlando Magic", "orl": "Orlando Magic",
    "wizards": "Washington Wizards", "washington": "Washington Wizards", "was": "Washington Wizards", "wsh": "Washington Wizards", "wiz": "Washington Wizards",
    # Northwest
    "nuggets": "Denver Nuggets", "nugs": "Denver Nuggets", "denver": "Denver Nuggets", "den": "Denver Nuggets",
    "timberwolves": "Minnesota Timberwolves", "wolves": "Minnesota Timberwolves", "minnesota": "Minnesota Timberwolves", "min": "Minnesota Timberwolves", "twolves": "Minnesota Timberwolves", "t-wolves": "Minnesota Timberwolves",
    "thunder": "Oklahoma City Thunder", "okc": "Oklahoma City Thunder", "oklahoma city": "Oklahoma City Thunder", "oklahoma": "Oklahoma City Thunder",
    "blazers": "Portland Trail Blazers", "trail blazers": "Portland Trail Blazers", "portland": "Portland Trail Blazers", "por": "Portland Trail Blazers",
    "jazz": "Utah Jazz", "utah": "Utah Jazz", "uta": "Utah Jazz", "utah jazz": "Utah Jazz",
    # Pacific
    "warriors": "Golden State Warriors", "gsw": "Golden State Warriors", "golden state": "Golden State Warriors", "dubs": "Golden State Warriors", "gs": "Golden State Warriors",
    "clippers": "LA Clippers", "la clippers": "LA Clippers", "lac": "LA Clippers", "los angeles clippers": "LA Clippers",
    "lakers": "Los Angeles Lakers", "la lakers": "Los Angeles Lakers", "lal": "Los Angeles Lakers", "la": "Los Angeles Lakers", "los angeles": "Los Angeles Lakers",
    "suns": "Phoenix Suns", "phoenix": "Phoenix Suns", "phx": "Phoenix Suns", "phn": "Phoenix Suns",
    "kings": "Sacramento Kings", "sacramento": "Sacramento Kings", "sac": "Sacramento Kings",
    # Southwest
    "mavericks": "Dallas Mavericks", "mavs": "Dallas Mavericks", "dallas": "Dallas Mavericks", "dal": "Dallas Mavericks",
    "rockets": "Houston Rockets", "rox": "Houston Rockets", "houston": "Houston Rockets", "hou": "Houston Rockets",
    "grizzlies": "Memphis Grizzlies", "grizz": "Memphis Grizzlies", "memphis": "Memphis Grizzlies", "mem": "Memphis Grizzlies",
    "pelicans": "New Orleans Pelicans", "pels": "New Orleans Pelicans", "new orleans": "New Orleans Pelicans", "nop": "New Orleans Pelicans", "no": "New Orleans Pelicans",
    "spurs": "San Antonio Spurs", "san antonio": "San Antonio Spurs", "sa": "San Antonio Spurs", "sas": "San Antonio Spurs",
}

# MLB team aliases — values MUST match ESPN's scoreboard displayName exactly
# (the alias path matches on ==, not substring). Covers all 30 teams via
# mascot, the standard 2-3 letter abbreviations tipsters use (Shook posts bare
# codes like "OAK", "LAD", "NYY"), and common nicknames. WHY this exists:
# substring matching alone can't bridge an abbreviation to a name with no text
# overlap — "OAK" is not a substring of "Athletics" (the A's dropped the city),
# so a 2026-06-03 Shook tip "OAK / Nick Kurtz 2+ HRRBI" resolved to nothing and
# went to manual. Ambiguous bare tokens are deliberately OMITTED to avoid
# wrong-game bets: "chi" (Cubs vs White Sox), "ny" (Mets vs Yankees), "la"
# (Dodgers vs Angels), "sox" (Red vs White) — those must arrive as the distinct
# code (CHC/CWS, NYM/NYY, LAD/LAA) or full name, else they fall through to the
# substring path / manual.
MLB_TEAM_ALIASES = {
    # AL East
    "orioles": "Baltimore Orioles", "baltimore": "Baltimore Orioles", "bal": "Baltimore Orioles",
    "red sox": "Boston Red Sox", "redsox": "Boston Red Sox", "bosox": "Boston Red Sox", "boston": "Boston Red Sox", "bos": "Boston Red Sox",
    "yankees": "New York Yankees", "yanks": "New York Yankees", "nyy": "New York Yankees", "bombers": "New York Yankees",
    "rays": "Tampa Bay Rays", "tampa bay": "Tampa Bay Rays", "tampa": "Tampa Bay Rays", "tb": "Tampa Bay Rays", "tbr": "Tampa Bay Rays",
    "blue jays": "Toronto Blue Jays", "bluejays": "Toronto Blue Jays", "jays": "Toronto Blue Jays", "toronto": "Toronto Blue Jays", "tor": "Toronto Blue Jays",
    # AL Central
    "white sox": "Chicago White Sox", "whitesox": "Chicago White Sox", "chisox": "Chicago White Sox", "cws": "Chicago White Sox", "chw": "Chicago White Sox",
    "guardians": "Cleveland Guardians", "guards": "Cleveland Guardians", "cleveland": "Cleveland Guardians", "cle": "Cleveland Guardians",
    "tigers": "Detroit Tigers", "detroit": "Detroit Tigers", "det": "Detroit Tigers",
    "royals": "Kansas City Royals", "kansas city": "Kansas City Royals", "kc": "Kansas City Royals", "kcr": "Kansas City Royals",
    "twins": "Minnesota Twins", "minnesota": "Minnesota Twins", "min": "Minnesota Twins",
    # AL West
    "astros": "Houston Astros", "stros": "Houston Astros", "houston": "Houston Astros", "hou": "Houston Astros",
    "angels": "Los Angeles Angels", "halos": "Los Angeles Angels", "laa": "Los Angeles Angels", "anaheim": "Los Angeles Angels",
    "athletics": "Athletics", "oakland": "Athletics", "oakland athletics": "Athletics", "oak": "Athletics", "ath": "Athletics", "a's": "Athletics", "as": "Athletics",
    "mariners": "Seattle Mariners", "seattle": "Seattle Mariners", "sea": "Seattle Mariners",
    "rangers": "Texas Rangers", "texas": "Texas Rangers", "tex": "Texas Rangers",
    # NL East
    "braves": "Atlanta Braves", "atlanta": "Atlanta Braves", "atl": "Atlanta Braves",
    "marlins": "Miami Marlins", "miami": "Miami Marlins", "mia": "Miami Marlins",
    "mets": "New York Mets", "nym": "New York Mets",
    "phillies": "Philadelphia Phillies", "phils": "Philadelphia Phillies", "philadelphia": "Philadelphia Phillies", "philly": "Philadelphia Phillies", "phi": "Philadelphia Phillies",
    "nationals": "Washington Nationals", "nats": "Washington Nationals", "washington": "Washington Nationals", "wsh": "Washington Nationals", "wsn": "Washington Nationals", "was": "Washington Nationals",
    # NL Central
    "cubs": "Chicago Cubs", "cubbies": "Chicago Cubs", "chc": "Chicago Cubs",
    "reds": "Cincinnati Reds", "cincinnati": "Cincinnati Reds", "cin": "Cincinnati Reds",
    "brewers": "Milwaukee Brewers", "crew": "Milwaukee Brewers", "milwaukee": "Milwaukee Brewers", "mil": "Milwaukee Brewers",
    "pirates": "Pittsburgh Pirates", "bucs": "Pittsburgh Pirates", "buccos": "Pittsburgh Pirates", "pittsburgh": "Pittsburgh Pirates", "pit": "Pittsburgh Pirates",
    "cardinals": "St. Louis Cardinals", "cards": "St. Louis Cardinals", "redbirds": "St. Louis Cardinals", "st louis": "St. Louis Cardinals", "st. louis": "St. Louis Cardinals", "stl": "St. Louis Cardinals",
    # NL West
    "diamondbacks": "Arizona Diamondbacks", "dbacks": "Arizona Diamondbacks", "d-backs": "Arizona Diamondbacks", "snakes": "Arizona Diamondbacks", "arizona": "Arizona Diamondbacks", "ari": "Arizona Diamondbacks", "az": "Arizona Diamondbacks",
    "rockies": "Colorado Rockies", "rox": "Colorado Rockies", "colorado": "Colorado Rockies", "col": "Colorado Rockies",
    "dodgers": "Los Angeles Dodgers", "lad": "Los Angeles Dodgers",
    "padres": "San Diego Padres", "pads": "San Diego Padres", "friars": "San Diego Padres", "san diego": "San Diego Padres", "sd": "San Diego Padres", "sdp": "San Diego Padres",
    "giants": "San Francisco Giants", "san francisco": "San Francisco Giants", "sf": "San Francisco Giants", "sfg": "San Francisco Giants",
}

# NFL: built from roster.NFL_TEAM_ABBR_TO_FULL (single source of truth,
# avoids a hand-typed 32-team list drifting out of sync with the roster
# module) plus each full name's own mascot word as an extra key (so
# "49ers"/"Cowboys"/"Rams" alone resolve, not just the abbreviation or the
# full "City Mascot" string) and a handful of common nicknames ESPN/tipsters
# use that aren't a plain substring of the full name.
def _build_nfl_team_aliases() -> dict:
    import roster as _roster
    aliases: dict = {}
    for abbr, full in _roster.NFL_TEAM_ABBR_TO_FULL.items():
        aliases[abbr.lower()] = full
        mascot = full.rsplit(" ", 1)[-1].lower()
        aliases.setdefault(mascot, full)
    aliases.update({
        "niners": "San Francisco 49ers", "9ers": "San Francisco 49ers",
        "pats": "New England Patriots", "skins": "Washington Commanders",
        "bucs": "Tampa Bay Buccaneers", "cards": "Arizona Cardinals",
        "hawks": "Seattle Seahawks", "big blue": "New York Giants",
    })
    return aliases


NFL_TEAM_ALIASES = _build_nfl_team_aliases()


def _fetch_schedule(sport: str, date: str) -> list[dict]:
    """Fetch games for a date. Returns list of {"home": name, "away": name}."""
    cache_key = f"{sport}_{date}"
    cached = _schedule_cache.get(cache_key)

    # Use cache if it has games and isn't expired
    if cached and cached["games"]:
        age = (datetime.now() - cached["fetched_at"]).total_seconds() / 60
        if age < CACHE_EXPIRY_MINUTES:
            return cached["games"]
        else:
            log.info(f"Cache expired for {cache_key} ({age:.0f}min old), refetching")

    # Don't use cached empty results - always retry
    url = _ESPN_URLS.get(sport, ESPN_NBA_URL)

    try:
        # v6.22 (2026-09-10): Akamai (ESPN's CDN) started 403-blocking the bare
        # "tipbot/1.0" UA specifically -- verified live, back-to-back same
        # request: "tipbot/1.0" -> 403, a browser-style UA -> 200. Zero prior
        # occurrences in tipbot.log (this path is rarely exercised: MLB has no
        # priority list, NBA is out of season in September), so it was dormant
        # rather than actively broken, but it would have silently degraded
        # MLB/NBA/NFL event resolution the next time any of them needed it.
        resp = requests.get(
            url,
            params={"dates": date.replace("-", "")},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0.0.0 Safari/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        games = []
        for event in data.get("events", []):
            competitors = event.get("competitions", [{}])[0].get("competitors", [])
            home = away = ""
            for c in competitors:
                name = c.get("team", {}).get("displayName", "")
                if c.get("homeAway") == "home":
                    home = name
                else:
                    away = name
            if home and away:
                # Skip TBD placeholder games (e.g. "76ers/Magic v Charlotte Hornets")
                if "/" in home or "/" in away:
                    log.info(f"  Skipping TBD game: {home} v {away}")
                    continue
                games.append({"home": home, "away": away})

        # Only cache non-empty results
        if games:
            _schedule_cache[cache_key] = {
                "games": games,
                "fetched_at": datetime.now(),
            }

        # Log actual game names for debugging
        log.info(f"Fetched {len(games)} {sport.upper()} games for {date}")
        for g in games:
            log.info(f"  Game: {g['home']} v {g['away']}")

        return games

    except Exception as e:
        log.warning(f"ESPN schedule fetch failed for {sport} {date}: {e}")
        return []


def _match_team(query: str, games: list[dict], aliases: dict = NBA_TEAM_ALIASES) -> Optional[dict]:
    """Find a game involving the queried team. `aliases` is the sport-specific
    abbreviation/nickname table (NBA by default; MLB callers pass
    MLB_TEAM_ALIASES so codes like 'OAK' resolve to 'Athletics')."""
    query_lower = query.strip().lower()

    # Try alias lookup first
    full_name = aliases.get(query_lower, "")

    for game in games:
        home_lower = game["home"].lower()
        away_lower = game["away"].lower()

        # Direct match on the resolved full name
        if full_name:
            if full_name.lower() == home_lower or full_name.lower() == away_lower:
                return game
            # H43: alias was resolved but the resolved team isn't playing today.
            # Do NOT fall through to substring on the raw alias string — short
            # aliases like "la" are substrings of "atlanta", "dallas",
            # "orlando", "portland" and would match the wrong game.
            continue

        # Substring match — only reached when NO alias was found (raw query
        # is e.g. a partial city/mascot not in the alias table).
        if (
            query_lower in home_lower or home_lower in query_lower
            or query_lower in away_lower or away_lower in query_lower
        ):
            return game

    return None


def resolve_nba_event(
    team: str = "", player: str = "", sport: str = "nba"
) -> Optional[str]:
    """
    Resolve a team or player name to a "Home v Away" event string.

    Checks today and tomorrow's schedule.
    Player name is used to find their team via roster lookup.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # If we have a player, look up their team
    search_team = team
    if not search_team and player:
        search_team = get_player_team(player, sport)

    if not search_team:
        log.warning(f"Cannot resolve event: no team for player '{player}'")
        return None

    # Handle "Team1/Team2" format (game totals)
    teams_to_try = [search_team]
    if "/" in search_team:
        teams_to_try = [t.strip() for t in search_team.split("/")]

    # Date priority for AEST -> US time:
    # - AEST morning/day = US yesterday (evening games still live or recently finished)
    # - AEST evening = US today (games starting)
    # For resolution, check yesterday/today/tomorrow in priority order.
    # If it's AEST afternoon/evening, today (AEST) = tomorrow (US-ish) - games haven't started
    hour = datetime.now().hour
    if hour < 14:
        # AEST morning: US yesterday's games most likely
        check_order = [yesterday, today, tomorrow]
    else:
        # AEST afternoon/evening: US today's games starting soon
        check_order = [today, tomorrow, yesterday]

    for check_date in check_order:
        games = _fetch_schedule(sport, check_date)
        for try_team in teams_to_try:
            game = _match_team(try_team, games)
            if game:
                event = f"{game['home']} v {game['away']}"
                log.info(f"Resolved '{search_team}' -> '{event}' on {check_date}")
                return event

    log.warning(f"No {sport.upper()} game found for '{search_team}'")
    return None


def resolve_mlb_event(team: str = "") -> Optional[str]:
    """Resolve an MLB team name to a 'Home v Away' event string via ESPN's MLB
    scoreboard (baseball/mlb). TEAM-based only for now — there's no MLB roster
    for player->team lookup, so an MLB player-prop tip must carry the team/game.
    Checks yesterday/today/tomorrow in AEST->US priority, same as NBA. Resolves
    via MLB_TEAM_ALIASES (abbreviation/nickname table — 'OAK' -> 'Athletics')
    then falls back to substring ('Yankees' matches 'New York Yankees').
    2026-06-01; MLB alias table added 2026-06-03."""
    if not (team or "").strip():
        log.warning("Cannot resolve MLB event: no team given")
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    hour = datetime.now().hour
    check_order = (
        [yesterday, today, tomorrow] if hour < 14 else [today, tomorrow, yesterday]
    )
    teams_to_try = [team] if "/" not in team else [t.strip() for t in team.split("/")]
    for check_date in check_order:
        games = _fetch_schedule("mlb", check_date)
        for try_team in teams_to_try:
            game = _match_team(try_team, games, MLB_TEAM_ALIASES)
            if game:
                event = f"{game['home']} v {game['away']}"
                log.info(f"Resolved MLB '{team}' -> '{event}' on {check_date}")
                return event
    log.warning(f"No MLB game found for '{team}'")
    return None


def resolve_nfl_event(team: str = "") -> Optional[str]:
    """Resolve an NFL team name to a 'Home v Away' event string via ESPN's NFL
    scoreboard (football/nfl). Resolves via NFL_TEAM_ALIASES (abbreviation/
    mascot/nickname table, built from roster.NFL_TEAM_ABBR_TO_FULL) then falls
    back to substring, same pattern as resolve_mlb_event.

    WIDER window than MLB/NBA's +-1 day (v6.22, 2026-09-10, widened again in
    code review): NFL plays weekly, not daily, and a tipster can post a play
    days ahead of kickoff (A1/4th&EV both do) -- checks today through +6 days
    (a FULL week ahead, so a Tuesday tip about that week's Sunday game still
    resolves; the original +4 day window missed exactly that case), then
    yesterday as a low-priority catch-all (a very late tip on an
    already-live/just-finished game)."""
    if not (team or "").strip():
        log.warning("Cannot resolve NFL event: no team given")
        return None
    now = datetime.now()
    check_order = [
        (now + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(0, 7)
    ] + [(now - timedelta(days=1)).strftime("%Y-%m-%d")]
    teams_to_try = [team] if "/" not in team else [t.strip() for t in team.split("/")]
    for check_date in check_order:
        games = _fetch_schedule("nfl", check_date)
        for try_team in teams_to_try:
            game = _match_team(try_team, games, NFL_TEAM_ALIASES)
            if game:
                event = f"{game['home']} v {game['away']}"
                log.info(f"Resolved NFL '{team}' -> '{event}' on {check_date}")
                return event
    log.warning(f"No NFL game found for '{team}' within the next 7 days or yesterday")
    return None
