"""
NBA/NBL roster lookup with fuzzy matching.

Maintains a cached roster JSON and provides fuzzy matching
for deobfuscated player names from Kev's tips.

Roster file: roster_nba.json / roster_nbl.json
Format: {"Player Full Name": "Team Name", ...}

To regenerate rosters, run: python roster.py --update
"""

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

ROSTER_DIR = Path(__file__).parent
NBA_ROSTER_FILE = ROSTER_DIR / "roster_nba.json"
NBL_ROSTER_FILE = ROSTER_DIR / "roster_nbl.json"
AFL_ROSTER_FILE = ROSTER_DIR / "roster_afl.json"
MLB_ROSTER_FILE = ROSTER_DIR / "roster_mlb.json"

# Cached rosters: {full_name_lower: {"name": full_name, "team": team}}
_nba_roster: dict = {}
_nbl_roster: dict = {}
_afl_roster: dict = {}
_mlb_roster: dict = {}
_loaded = False


# ── NBA nickname resolution ─────────────────────────────────────────
# Pre-pass before fuzzy match: common NBA nicknames and shorthand that
# wouldn't match directly via SequenceMatcher. Keys are lowercased.
NBA_NICKNAMES = {
    "steph": "Stephen Curry",
    "kat": "Karl-Anthony Towns",
    "ant": "Anthony Edwards",
    "giannis": "Giannis Antetokounmpo",
    "greek freak": "Giannis Antetokounmpo",
    "lebron": "LeBron James",
    "bron": "LeBron James",
    "jokic": "Nikola Jokic",
    "joker": "Nikola Jokic",
    "luka": "Luka Doncic",
    "kawhi": "Kawhi Leonard",
    "pg": "Paul George",
    "pg13": "Paul George",
    "dame": "Damian Lillard",
    "tyrese": "Tyrese Maxey",
    "maxey": "Tyrese Maxey",
    "hali": "Tyrese Haliburton",
    "halli": "Tyrese Haliburton",
    "haliburton": "Tyrese Haliburton",
    "franz": "Franz Wagner",
    "podz": "Brandin Podziemski",
    "bam": "Bam Adebayo",
    "dyson": "Dyson Daniels",
    "trae": "Trae Young",
    "cp3": "Chris Paul",
    "klay": "Klay Thompson",
    "book": "Devin Booker",
    "booker": "Devin Booker",
    "kd": "Kevin Durant",
    "ad": "Anthony Davis",
    "jimmy": "Jimmy Butler",
    "butler": "Jimmy Butler",
    "kyrie": "Kyrie Irving",
    "russ": "Russell Westbrook",
    "melo": "Carmelo Anthony",
    "tatum": "Jayson Tatum",
    "jt": "Jayson Tatum",
    "jaylen": "Jaylen Brown",
    "jb": "Jaylen Brown",
    "zion": "Zion Williamson",
    "ja": "Ja Morant",
    "sga": "Shai Gilgeous-Alexander",
    "shai": "Shai Gilgeous-Alexander",
    "wemby": "Victor Wembanyama",
    "scoot": "Scoot Henderson",
    "cade": "Cade Cunningham",
    "paolo": "Paolo Banchero",
    "jalen": "Jalen Brunson",
    "brunson": "Jalen Brunson",
    "embiid": "Joel Embiid",
    "jrue": "Jrue Holiday",
    "onyeka": "Onyeka Okongwu",
    "jdub": "Jalen Williams",  # Kev NBA 2026-05-21 missed tip
}


def _resolve_nickname(query: str, sport: str) -> str:
    """If query matches a known nickname, return the full player name.
    Currently covers NBA and AFL (Saiyan often uses initialisms like NWM).
    """
    stripped = query.strip().lower()
    full: str | None = None
    if sport == "nba":
        full = NBA_NICKNAMES.get(stripped)
    elif sport == "afl":
        full = AFL_NICKNAMES.get(stripped)
    if full:
        log.info(f"Nickname resolved: '{query}' -> '{full}'")
        return full
    return query


# ── AFL nickname resolution ─────────────────────────────────────────
# Saiyan AFL tips occasionally use initialisms or short forms that
# wouldn't match via SequenceMatcher. Keys are lowercased. Add new
# entries as Wilson encounters them in tipster messages.
#
# 2026-05-02: Walsh+NWM SGM failed because "NWM" was sent verbatim to
# HyperBot. NWM = Nasiah Wanganeen-Milera (St Kilda).
AFL_NICKNAMES = {
    "nwm": "Nasiah Wanganeen-Milera",
    "bont": "Marcus Bontempelli",
}


def _load_rosters():
    """Load roster JSON files into memory."""
    global _nba_roster, _nbl_roster, _afl_roster, _mlb_roster, _loaded
    if _loaded:
        return

    for path, cache in [(NBA_ROSTER_FILE, "_nba"), (NBL_ROSTER_FILE, "_nbl"),
                        (AFL_ROSTER_FILE, "_afl"), (MLB_ROSTER_FILE, "_mlb")]:
        roster = {}
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                for name, team in data.items():
                    roster[name.lower()] = {"name": name, "team": team}
                log.info(f"Loaded {len(roster)} players from {path.name}")
            except Exception as e:
                log.warning(f"Failed to load {path}: {e}")

        if cache == "_nba":
            _nba_roster = roster
        elif cache == "_nbl":
            _nbl_roster = roster
        elif cache == "_afl":
            _afl_roster = roster
        else:
            _mlb_roster = roster

    _loaded = True


def _upgrade_to_full_name(match: dict, sport: str) -> dict:
    """Upgrade a surname-only match to its full-name counterpart on the
    same team, when one uniquely exists.

    Roster generation creates surname-only aliases for fuzzy matching
    (e.g. "McDaniels" -> Minnesota Timberwolves) alongside the full-name
    entry ("Jaden McDaniels"). When the matcher returns the surname-only
    entry, downstream code passes player='McDaniels' to HyperBot. If
    HyperBot has multiple candidates for the same player (e.g. one with
    line embedded in the selection label, one without), it can't
    disambiguate from the surname alone and rejects with a generic
    "did not match" error. 2026-05-09 McDaniels regression on Kev tip.

    Skip the upgrade if multiple full-name entries on that team end with
    the same surname (genuine ambiguity — let the original match stand
    and let downstream handle).
    """
    if not match:
        return match
    name = match.get("name", "")
    name_parts = name.split()
    if len(name_parts) > 1:
        return match  # already a full name
    surname_lower = name.lower()
    team = match.get("team", "")
    if not team:
        return match
    # Use the relevant roster cache directly. _load_rosters has already
    # been called by the matcher.
    if sport == "nba":
        roster = _nba_roster
    elif sport == "nbl":
        roster = _nbl_roster
    elif sport == "afl":
        roster = _afl_roster
    elif sport == "mlb":
        roster = _mlb_roster
    else:
        return match
    full_name_candidates: list[str] = []
    for _, info in roster.items():
        if info.get("team") != team:
            continue
        candidate = info.get("name", "")
        candidate_parts = candidate.split()
        if len(candidate_parts) <= 1:
            continue  # surname-only entries skipped
        if candidate_parts[-1].lower() == surname_lower:
            full_name_candidates.append(candidate)
    # Dedupe (multiple aliases can point to the same name)
    full_name_candidates = list(dict.fromkeys(full_name_candidates))
    if len(full_name_candidates) == 1:
        upgraded = full_name_candidates[0]
        log.info(
            f"Upgrading surname-only '{name}' -> '{upgraded}' (same team: {team})"
        )
        return {
            "name": upgraded,
            "team": team,
            "score": match.get("score", 0),
        }

    # M40: zero candidates on the expected team — possible post-trade stale
    # alias (alias still maps to old team, full-name entry updated to new team).
    # Retry globally. If exactly one full-name result exists across all teams,
    # log a warning (stale alias) and return it. If ambiguous, return None to
    # force the caller to handle via other means rather than risk a bad bet.
    if len(full_name_candidates) == 0:
        global_candidates: list[str] = []
        for _, info in roster.items():
            candidate = info.get("name", "")
            candidate_parts = candidate.split()
            if len(candidate_parts) <= 1:
                continue
            if candidate_parts[-1].lower() == surname_lower:
                global_candidates.append(candidate)
        global_candidates = list(dict.fromkeys(global_candidates))
        if len(global_candidates) == 1:
            upgraded = global_candidates[0]
            # Find updated team from roster
            new_team = ""
            for _, info in roster.items():
                if info.get("name") == upgraded:
                    new_team = info.get("team", "")
                    break
            log.warning(
                f"Stale alias detected: surname '{name}' matched team '{team}' "
                f"in roster but full name '{upgraded}' is on '{new_team}'. "
                f"Alias may need updating."
            )
            return {
                "name": upgraded,
                "team": new_team or team,
                "score": match.get("score", 0),
            }
        # Ambiguous globally or still zero — leave as-is
        return match

    # Ambiguous on the original team (len > 1) — leave as-is
    return match


def _team_matches(roster_team: str, query_team: str) -> bool:
    """Lenient team comparison for roster filtering.

    Roster team names sometimes carry suffixes ('Eagles', 'Cats', 'Crows')
    that AFL_TEAMS code mappings and Groq output omit. Lowercase the
    inputs and accept either-direction substring containment so e.g.
    'West Coast' matches 'West Coast Eagles' and 'Adelaide' matches
    'Adelaide Crows'.

    2026-05-03 Petracca regression: Groq returned team='GC' (raw 2-letter
    code) for the Saiyan SGM leg. _team_matches('Gold Coast Suns', 'GC')
    returned False (no substring overlap), so the team filter dropped to
    global match. Now expand 2-letter and 3-letter codes through the
    config's AFL_TEAMS dict before comparison so 'GC' becomes 'Gold Coast'
    and matches 'Gold Coast Suns' via substring.
    """
    if not roster_team or not query_team:
        return False

    # Expand short codes through AFL_TEAMS (NBA aliases handled in
    # nba_resolver — separate concern). Keep this lazy and tolerant:
    # if config can't be imported (test isolation, circular imports),
    # fall through to the original substring match.
    q_expanded = query_team
    # H (2026-05-31): map ANY AFL_TEAMS code/nickname, regardless of length or
    # case. The old `len <= 4 and upper()==self` guard silently skipped the 5+
    # char nicknames (SAINTS/GIANTS/TIGERS/EAGLES/SWANS/KANGAS/...) so "Giants"
    # never expanded to "Greater Western Sydney" for the substring compare —
    # the SAME bug class fixed in resolver.resolve_afl_event this session.
    # AFL_TEAMS.get() returns None for non-keys (e.g. a full team name), so a
    # full name like "Greater Western Sydney" is left untouched and matched by
    # the substring logic below.
    try:
        from config import AFL_TEAMS
        mapped = AFL_TEAMS.get(query_team.strip().upper())
        if mapped:
            q_expanded = mapped
    except Exception:
        pass

    r = roster_team.strip().lower()
    q = q_expanded.strip().lower()
    return r == q or r in q or q in r


def _scope_roster_to_team(roster: dict, team: str) -> dict:
    """Return the subset of roster entries on the given team.
    Empty dict means no players matched the team filter (likely an
    unrecognised team name) — caller should warn and fall back.
    """
    if not team:
        return roster
    return {k: v for k, v in roster.items() if _team_matches(v["team"], team)}


def _passes_token_overlap_gate(query: str, matched: str) -> bool:
    """Reject fuzzy matches that share no >=3-char tokens with the query.

    Catches false positives like "O'Sullivan" -> "Sullivan Robey" (the
    apostrophe makes "o'sullivan" and "sullivan" distinct tokens, so
    set-intersection is empty) and "Davis" -> "Hugh Davies" (same).

    Lifted from the singles-path gate in main.py so SGM leg resolution
    gets the same protection. Skips when the query is too short for the
    >=3-char filter to leave any tokens behind.
    """
    orig_tokens = {t for t in query.lower().split() if len(t) >= 3}
    if not orig_tokens:
        return True
    matched_tokens = set(matched.lower().split())
    return bool(orig_tokens & matched_tokens)


def exact_match_player(query: str, sport: str = "nba", team: str = "") -> dict:
    """
    Strict exact-match lookup against the roster — no fuzzy scoring, no
    partial matches. Use this when you trust the input is a full, correctly
    spelled player name (e.g. Shook tipster) and you only want a roster
    answer if the player exists exactly. Returns the current team for that
    player straight from the roster (which is API-fresh, unlike Groq).

    Returns {"name": "Full Name", "team": "Team", "score": 1.0}
    or empty dict if no exact match.

    Falls through to nickname resolution first so e.g. "KAT" -> "Karl-Anthony Towns"
    or "NWM" -> "Nasiah Wanganeen-Milera" still works.

    If `team` is provided, only entries on that team are eligible. Lenient
    team match: 'West Coast' accepts roster entry 'West Coast Eagles'.
    Empty result on team mismatch is intentional — let the caller manual-route
    rather than risk wrong-team placement.
    """
    if not query:
        return {}

    # Nickname pre-pass (NBA + AFL)
    query = _resolve_nickname(query, sport)

    _load_rosters()
    if sport == "nba":
        roster = _nba_roster
    elif sport == "nbl":
        roster = _nbl_roster
    elif sport == "afl":
        roster = _afl_roster
    elif sport == "mlb":
        roster = _mlb_roster
    else:
        return {}

    info = roster.get(query.strip().lower())
    if info:
        # Team filter: lenient match against the entry's team
        if team and not _team_matches(info["team"], team):
            return {}
        return {"name": info["name"], "team": info["team"], "score": 1.0}
    return {}


def fuzzy_match_player(
    query: str, sport: str = "nba", threshold: float = 0.6,
    team: str = "",
) -> dict:
    """
    Fuzzy match a (potentially partial) player name against the roster.

    Args:
        query: Deobfuscated player name (e.g. "Kispert", "Vince Williams")
        sport: "nba", "nbl", or "afl"
        threshold: Minimum similarity score (0-1)
        team: Optional team filter. When supplied, restricts the match to
              roster entries on that team BEFORE scoring. Saiyan AFL always
              includes a team code per leg, so passing it here prevents
              cross-team fuzzy collisions like "Davis" -> "Hugh Davies"
              (Fremantle, 0.85) when the actual tip was for Hamish Davis
              on West Coast Eagles. Lenient team match — 'West Coast'
              filter accepts roster entry 'West Coast Eagles'.
              If team is given but matches no roster entries (unrecognised
              team name), warns and falls back to global match.

    Returns:
        {"name": "Full Name", "team": "Team", "score": 0.85}
        or empty dict if no match above threshold or sanity gate rejects.
    """
    # Nickname pre-pass (NBA + AFL)
    query = _resolve_nickname(query, sport)

    _load_rosters()
    if sport == "nba":
        roster = _nba_roster
    elif sport == "nbl":
        roster = _nbl_roster
    elif sport == "afl":
        roster = _afl_roster
    elif sport == "mlb":
        roster = _mlb_roster
    else:
        roster = {}

    if not roster:
        log.warning(f"No {sport} roster loaded, cannot fuzzy match")
        return {}

    query_lower = query.strip().lower()
    query_parts = query_lower.split()

    def _match_against(roster_subset: dict) -> dict:
        """Score the query against one roster dict and return the best match
        (post threshold + token-overlap gate) as a result dict, or {} if none.
        Extracted so we can try the team-scoped roster first and, on a miss,
        a guarded global fallback (Wilson 2026-05-31)."""
        best_match = None
        best_score = 0

        for key, info in roster_subset.items():
            name_parts = key.split()

            # Exact last name match (most common for Kev - he usually posts last name only)
            if query_parts[-1] == name_parts[-1]:
                # If first name also partially matches, boost score
                if len(query_parts) > 1 and len(name_parts) > 1:
                    first_score = SequenceMatcher(
                        None, query_parts[0], name_parts[0]
                    ).ratio()
                    score = 0.8 + (0.2 * first_score)
                else:
                    score = 0.85  # Last name exact match

            # Single-word query: also check first name match
            elif len(query_parts) == 1 and len(name_parts) >= 1:
                first_score = SequenceMatcher(
                    None, query_parts[0], name_parts[0].lower()
                ).ratio()
                last_score = SequenceMatcher(
                    None, query_parts[0], name_parts[-1].lower()
                ).ratio()
                # Strong first or last name match
                if first_score > 0.85:
                    score = 0.85 + (0.1 * first_score)
                elif last_score > 0.85:
                    score = 0.85
                else:
                    score = max(first_score, last_score) * 0.8

            else:
                # Full string similarity
                score = SequenceMatcher(None, query_lower, key).ratio()

                # Also check last name similarity
                last_score = SequenceMatcher(
                    None, query_parts[-1], name_parts[-1]
                ).ratio()
                score = max(score, last_score * 0.9)

            if score > best_score:
                best_score = score
                best_match = info

        if best_match and best_score >= threshold:
            # Token-overlap sanity gate: reject matches that share no
            # meaningful tokens with the query. Catches "O'Sullivan" ->
            # "Sullivan Robey" (apostrophe splits "o'sullivan" from
            # "sullivan") and "Davis" -> "Hugh Davies" (different strings).
            # Previously only enforced on the singles path in main.py;
            # promoted here so SGM leg resolution gets the same protection.
            # This gate is ALSO what makes the global fallback below safe.
            if not _passes_token_overlap_gate(query_lower, best_match["name"]):
                log.warning(
                    f"Discarding suspicious fuzzy match: '{query}' -> "
                    f"'{best_match['name']}' (score={best_score:.3f}) — "
                    f"no shared name tokens"
                )
                return {}
            return _upgrade_to_full_name({
                "name": best_match["name"],
                "team": best_match["team"],
                "score": round(best_score, 3),
            }, sport)
        return {}

    # Team filter: scope to that team's players only. If team is given but
    # the filter yields nothing (unrecognised team name), warn and match
    # against the full roster directly. When the filter HAS entries but no
    # player matches, we now attempt a guarded global fallback (Wilson
    # 2026-05-31) rather than going straight to manual.
    scoped_to_team = False
    match_roster = roster
    if team:
        scoped = _scope_roster_to_team(roster, team)
        if not scoped:
            log.warning(
                f"Team filter '{team}' matched no roster entries; "
                f"matching globally for query '{query}'"
            )
        else:
            match_roster = scoped
            scoped_to_team = True

    result = _match_against(match_roster)

    # Guarded global fallback (Wilson 2026-05-31): when the player wasn't found
    # on the ASSIGNED team (Groq can tag the wrong but valid team), retry against
    # the FULL roster. The token-overlap gate inside _match_against still rejects
    # same-surname-different-player collisions (e.g. "Davis" -> "Hugh Davies"),
    # so this trades fewer manual routes for a small, gated wrong-player risk.
    if not result and scoped_to_team:
        log.info(
            f"Team-scoped match failed for '{query}' on '{team}'; "
            f"trying guarded global roster fallback"
        )
        result = _match_against(roster)
        if result:
            log.warning(
                f"Global fallback resolved '{query}' -> '{result.get('name')}' "
                f"({result.get('team')}) after team '{team}' yielded no match — "
                f"verify the team was correct"
            )

    return result


def fuzzy_match_all(
    query: str, sport: str = "nba", threshold: float = 0.6
) -> list[dict]:
    """
    Like fuzzy_match_player but returns ALL candidates above threshold,
    sorted by score descending. Used for disambiguating common surnames
    (e.g. "Wiggins" -> Andrew Wiggins, Aaron Wiggins) when the resolver
    needs to pick the one whose team is actually playing today.
    """
    # Nickname pre-pass (NBA only)
    query = _resolve_nickname(query, sport)

    _load_rosters()
    if sport == "nba":
        roster = _nba_roster
    elif sport == "nbl":
        roster = _nbl_roster
    elif sport == "afl":
        roster = _afl_roster
    elif sport == "mlb":
        roster = _mlb_roster
    else:
        roster = {}

    if not roster:
        return []

    query_lower = query.strip().lower()
    query_parts = query_lower.split()

    candidates = []
    for key, info in roster.items():
        name_parts = key.split()

        if query_parts[-1] == name_parts[-1]:
            if len(query_parts) > 1 and len(name_parts) > 1:
                first_score = SequenceMatcher(
                    None, query_parts[0], name_parts[0]
                ).ratio()
                score = 0.8 + (0.2 * first_score)
            else:
                score = 0.85
        elif len(query_parts) == 1 and len(name_parts) >= 1:
            first_score = SequenceMatcher(
                None, query_parts[0], name_parts[0].lower()
            ).ratio()
            last_score = SequenceMatcher(
                None, query_parts[0], name_parts[-1].lower()
            ).ratio()
            if first_score > 0.85:
                score = 0.85 + (0.1 * first_score)
            elif last_score > 0.85:
                score = 0.85
            else:
                score = max(first_score, last_score) * 0.8
        else:
            score = SequenceMatcher(None, query_lower, key).ratio()
            last_score = SequenceMatcher(
                None, query_parts[-1], name_parts[-1]
            ).ratio()
            score = max(score, last_score * 0.9)

        if score >= threshold:
            # H42: apply the same token-overlap gate that fuzzy_match_player
            # uses. Without this, fuzzy_match_all can return false-positive
            # candidates (e.g. query "Davis" -> "Hugh Davies" at 0.85) that
            # lead to wrong-player bets when the resolver picks by schedule.
            if not _passes_token_overlap_gate(query_lower, info["name"]):
                log.debug(
                    f"fuzzy_match_all: discarding '{info['name']}' for query "
                    f"'{query}' (score={score:.3f}) — no shared name tokens"
                )
                continue
            candidates.append({
                "name": info["name"],
                "team": info["team"],
                "score": round(score, 3),
            })

    # Dedupe by name (multiple roster keys can point to same player via last-name aliases)
    seen = set()
    unique = []
    for c in candidates:
        upgraded = _upgrade_to_full_name(c, sport)
        if upgraded["name"] not in seen:
            seen.add(upgraded["name"])
            unique.append(upgraded)

    return sorted(unique, key=lambda x: x["score"], reverse=True)


def resolve_player_name(query: str, sport: str = "nba", team: str = "") -> str:
    """
    Resolve a query to a full player name for HyperBot.
    Returns the original query if no match found.

    `team` is plumbed through to fuzzy_match_player so callers (e.g.
    the SGM leg resolver) can scope to the leg's team prefix when known.
    """
    match = fuzzy_match_player(query, sport, team=team)
    if match:
        log.info(f"Roster match: '{query}' -> '{match['name']}' ({match['team']}) score={match['score']}")
        return match["name"]
    log.warning(f"No roster match for '{query}' in {sport}")
    return query


def get_player_team(query: str, sport: str = "nba", team: str = "") -> str:
    """Get a player's team name. Returns empty string if not found.
    `team` filter optional — same semantics as fuzzy_match_player.
    """
    match = fuzzy_match_player(query, sport, team=team)
    return match.get("team", "")


def _afl_token_norm(s: str) -> str:
    """Lowercase + strip accents + collapse whitespace, for surname matching of
    Eddie's bare-token player props. Keeps word chars, spaces, apostrophes and
    hyphens; drops other punctuation."""
    s = (s or "").replace(" ", " ")
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[^\w\s'-]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def afl_surname_candidates(token: str) -> list:
    """All AFL roster players whose SURNAME matches `token` (normalised).

    A player matches if `token` equals their last-name token OR their full
    surname (everything after the first name, e.g. 'Ah Chee'). Returns
    [{"name": full_name, "team": team}, ...] — possibly empty, possibly several
    (the caller scopes to the game about to start and requires uniqueness).

    FIRST names are NOT matched: Eddie sends last-name-only player props, and a
    first-name hit ('Bailey' -> Bailey Smith, 'Daniel' -> Daniel Rioli) would be
    a wrong-player mismatch. So 'Daniel' -> Caleb Daniel (surname), never the
    several players whose FIRST name is Daniel; 'Bailey' -> Zac Bailey (surname),
    never Bailey Smith/Dale/etc."""
    _load_rosters()
    tok = _afl_token_norm(token)
    if not tok:
        return []
    out = []
    for _, info in _afl_roster.items():
        name = info.get("name", "")
        parts = name.split()
        if len(parts) < 2:
            continue  # need a first + last name to have a surname
        last_tok = _afl_token_norm(parts[-1])
        full_surname = _afl_token_norm(" ".join(parts[1:]))
        if tok == last_tok or tok == full_surname:
            out.append({"name": name, "team": info.get("team", "")})
    return out


def update_roster_from_api():
    """
    Fetch current NBA rosters from NBA.com via nba_api package.
    No API key needed. Run locally: python roster.py --update

    Requires: pip install nba_api
    """
    try:
        from nba_api.stats.endpoints import commonallplayers
        import time
    except ImportError:
        log.error("nba_api not installed. Run: pip install nba_api")
        return {}

    log.info("Fetching NBA roster from stats.nba.com...")
    roster = {}

    try:
        # Get all current season players with team info
        result = commonallplayers.CommonAllPlayers(
            is_only_current_season=1,
            season="2025-26",
        )
        df = result.get_data_frames()[0]

        for _, row in df.iterrows():
            name = row.get("DISPLAY_FIRST_LAST", "")
            team_city = row.get("TEAM_CITY", "")
            team_name = row.get("TEAM_NAME", "")

            if name and team_name:
                full_team = f"{team_city} {team_name}".strip()
                roster[name] = full_team

                # Also add last-name-only entry for fuzzy matching
                parts = name.split()
                if len(parts) >= 2:
                    last = parts[-1]
                    # Don't overwrite if last name already exists (ambiguous)
                    if last not in roster:
                        roster[last] = full_team

        log.info(f"Got {len(df)} players from NBA.com")

    except Exception as e:
        log.error(f"NBA roster update failed: {e}")
        log.info("Falling back to nba_api static player list...")

        # Fallback: use static list (no team info)
        from nba_api.stats.static import players
        for p in players.get_active_players():
            roster[p["full_name"]] = ""

    if roster:
        with open(NBA_ROSTER_FILE, "w", encoding="utf-8") as f:
            json.dump(roster, f, indent=2, ensure_ascii=False)
        log.info(f"Saved {len(roster)} entries to {NBA_ROSTER_FILE}")

    return roster


def update_mlb_roster_from_api(season: int = 2026):
    """Fetch current MLB rosters from the MLB Stats API (statsapi.mlb.com).
    No API key needed. Run locally: python roster.py --update-mlb

    Builds {full_name: team_name} where team_name is the MLB Stats API team
    name (e.g. "Atlanta Braves") — the SAME full names ESPN uses, so
    resolve_mlb_event substring-matches them straight to the fixture. Adds a
    surname-only alias for fuzzy matching (skipped when the surname is
    ambiguous across teams). Used ONLY when Shook omits the team (v5.33):
    get_player_team(player, "mlb") -> team -> resolve_mlb_event.
    """
    import urllib.request

    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "tipbot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    log.info(f"Fetching MLB roster from statsapi.mlb.com (season {season})...")
    roster: dict = {}
    try:
        teams = _get(
            f"https://statsapi.mlb.com/api/v1/teams?sportId=1&season={season}"
        ).get("teams", [])
        tmap = {t["id"]: t["name"] for t in teams if t.get("id") and t.get("name")}
        people = _get(
            f"https://statsapi.mlb.com/api/v1/sports/1/players?season={season}"
        ).get("people", [])

        # First pass: full names. Track surnames (ACCENT-NORMALISED, so
        # 'Díaz'/'Diaz' count as the SAME surname) to skip ambiguous aliases.
        def _surnorm(s: str) -> str:
            s = unicodedata.normalize("NFD", s or "")
            return "".join(c for c in s if not unicodedata.combining(c)).lower()

        surname_count: dict = {}
        for p in people:
            name = (p.get("fullName") or "").strip()
            tid = (p.get("currentTeam") or {}).get("id")
            team = tmap.get(tid)
            if not (name and team):
                continue
            roster[name] = team
            parts = name.split()
            if len(parts) >= 2:
                key = _surnorm(parts[-1])
                surname_count[key] = surname_count.get(key, 0) + 1

        # Second pass: surname-only aliases, ONLY when the surname is unambiguous
        # (exactly one player, accent-insensitive) and not already a full-name
        # key. These aliases are currently unused by the MLB path (which requires
        # a 2+ token exact full name), but keeping them clean avoids a latent
        # wrong-team alias if a 1-token lookup is ever enabled.
        for p in people:
            name = (p.get("fullName") or "").strip()
            tid = (p.get("currentTeam") or {}).get("id")
            team = tmap.get(tid)
            if not (name and team):
                continue
            parts = name.split()
            if len(parts) >= 2:
                last = parts[-1]
                if surname_count.get(_surnorm(last)) == 1 and last not in roster:
                    roster[last] = team

        log.info(f"Got {len(people)} MLB players across {len(tmap)} teams")
    except Exception as e:
        log.error(f"MLB roster update failed: {e}")
        return {}

    if roster:
        with open(MLB_ROSTER_FILE, "w", encoding="utf-8") as f:
            json.dump(roster, f, indent=2, ensure_ascii=False)
        log.info(f"Saved {len(roster)} entries to {MLB_ROSTER_FILE}")

    return roster


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if "--update" in sys.argv:
        update_roster_from_api()
    elif "--update-mlb" in sys.argv:
        update_mlb_roster_from_api()
    else:
        # Quick test
        _load_rosters()
        test_names = [
            "Kispert", "Vince Williams", "Huff", "Jerome", "Filipowski",
            "Hali", "KAT", "Steph", "SGA",
        ]
        for name in test_names:
            result = fuzzy_match_player(name, "nba")
            if result:
                print(f"  {name} -> {result['name']} ({result['team']}) [{result['score']}]")
            else:
                print(f"  {name} -> NO MATCH")
