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
# Curated roster-spelling -> Sportsbet-spelling overrides for AFL player names
# (see afl_name_overrides.json). Applied at load so the resolved `player` field
# sent to HyperBot matches what Sportsbet lists in its player-prop markets, even
# after the daily Draftguru scrape reintroduces a short form ('Brad Hill').
AFL_NAME_OVERRIDES_FILE = ROSTER_DIR / "afl_name_overrides.json"

# Cached rosters: {full_name_lower: {"name": full_name, "team": team}}
_nba_roster: dict = {}
_nbl_roster: dict = {}
_afl_roster: dict = {}
_mlb_roster: dict = {}
# MLB same-full-name collisions {name_lower: [teams]} — two different players
# (e.g. a star and a minor-leaguer) sharing an exact full name on different teams.
# Populated from roster_mlb.json's "__collisions__" block; consulted by the resolver
# to refuse a blind team-override/inference (2026-06-25 Pete Alonso/Max Muncy fix).
_mlb_collisions: dict = {}
# AFL placement-time alias map {roster_name_lower: [alt_spelling, ...]} from
# afl_name_overrides.json['aliases'] — alternate Sportsbet spellings tried against
# the LIVE catalog by main._afl_canonical_catalog_player (see afl_name_aliases()).
_afl_name_aliases: dict = {}
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

# Curated MLB same-full-name collisions that a SINGLE statsapi pull may under-report.
# Two GENUINELY DIFFERENT MLB players can share an exact full name on different teams
# (the Dodgers' Max Muncy and a younger Athletics infielder of the same name). If a
# given /sports/1/players snapshot happens to contain only one of them, within-pull
# detection would write a confident single mapping; seeding the known pairs keeps the
# resolver treating them as ambiguous (use a stated team, else manual) regardless of
# the snapshot. Add a name ONLY when two distinct MLB players really share it -- do
# NOT add a single player who merely changed clubs (e.g. Pete Alonso, one real player,
# is correctly resolved to his current team and must keep auto-placing).
MLB_KNOWN_COLLISIONS = {
    "Max Muncy": ["Athletics", "Los Angeles Dodgers"],
}


def _load_rosters():
    """Load roster JSON files into memory."""
    global _nba_roster, _nbl_roster, _afl_roster, _mlb_roster, _mlb_collisions, _loaded
    if _loaded:
        return

    for path, cache in [(NBA_ROSTER_FILE, "_nba"), (NBL_ROSTER_FILE, "_nbl"),
                        (AFL_ROSTER_FILE, "_afl"), (MLB_ROSTER_FILE, "_mlb")]:
        roster = {}
        collisions = {}
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                for name, team in data.items():
                    # "__collisions__" is a reserved meta key {fullName: [teams]},
                    # NOT a player — same-full-name MLB collisions (2026-06-25). Load
                    # it aside so the resolver can refuse a blind override for such a
                    # name; never let it become a bogus roster entry.
                    if name == "__collisions__":
                        if isinstance(team, dict):
                            collisions = {k.lower(): list(v) for k, v in team.items()}
                        continue
                    roster[name.lower()] = {"name": name, "team": team}
                log.info(f"Loaded {len(roster)} players from {path.name}"
                         + (f" ({len(collisions)} same-name collisions)" if collisions else ""))
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
            _mlb_collisions = collisions

    _apply_afl_name_overrides()
    _loaded = True


def afl_name_aliases() -> dict:
    """The AFL placement-time alias map {roster_name_lower: [alt_spelling, ...]}
    from afl_name_overrides.json['aliases']. Consulted by
    main._afl_canonical_catalog_player to try alternate Sportsbet spellings against
    the LIVE catalog and use whichever the board actually carries (one lookup, NO
    extra POST). Empty until a load has run."""
    _load_rosters()
    return _afl_name_aliases


def _apply_afl_name_overrides() -> None:
    """Apply the curated afl_name_overrides.json to the loaded AFL roster + alias map.

    Why (2026-07-06, Brad Hill fan-out fault): the roster is scraped daily from
    Draftguru, which lists common short forms ('Brad Hill') and can mis-assign
    same-name players (two 'Max King's collide -> last-writer-wins) or keep retired
    players. Sportsbet's player-prop market lists formal names and HyperBot matches
    on the `player` field. Every section here is re-applied on each load (and at
    generation), so a Draftguru refresh can't undo it.

    Sections:
      overrides       {roster short-form: Sportsbet name} -- CONFIRMED rename; both
                      spellings resolve to the Sportsbet name (payload player matches).
      aliases         {roster name: [alt Sportsbet spellings]} -- placement-time
                      candidates tried against the LIVE catalog (never a rename here).
      team_overrides  {name: club} -- force a player's club (fixes a same-name scrape
                      collision); adds the entry if missing.
      add             {name: club} -- insert a missing player.
      remove          [name, ...] -- drop a retired/delisted player the scrape lists.

    Money-path safety: an override is skipped if its target spelling is ALREADY a
    DIFFERENT player on another team (never clobber a real roster entry)."""
    global _afl_name_aliases
    _afl_name_aliases = {}
    if not AFL_NAME_OVERRIDES_FILE.exists():
        return
    try:
        with open(AFL_NAME_OVERRIDES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"Failed to load {AFL_NAME_OVERRIDES_FILE.name}: {e}")
        return
    if not isinstance(data, dict):
        return

    # 1) remove — drop retired/delisted players the daily scrape still lists.
    removed = 0
    for name in (data.get("remove") or []):
        if isinstance(name, str) and _afl_roster.pop(name.strip().lower(), None):
            removed += 1

    # 2) add — insert a missing player (name -> club). Distinct key; a same-name
    #    player on another club stays under its own key (event-scoping disambiguates).
    added = 0
    add = data.get("add")
    if isinstance(add, dict):
        for name, team in add.items():
            if (isinstance(name, str) and isinstance(team, str)
                    and name.strip() and team.strip()):
                _afl_roster[name.strip().lower()] = {"name": name.strip(),
                                                     "team": team.strip()}
                added += 1

    # 3) team_overrides — force a player's club (fixes a same-name scrape collision,
    #    e.g. two 'Max King's). Preserves the existing display name if present.
    team_fixed = 0
    tov = data.get("team_overrides")
    if isinstance(tov, dict):
        for name, team in tov.items():
            if not (isinstance(name, str) and isinstance(team, str)
                    and name.strip() and team.strip()):
                continue
            nl = name.strip().lower()
            keep = _afl_roster.get(nl, {}).get("name") or name.strip()
            _afl_roster[nl] = {"name": keep, "team": team.strip()}
            team_fixed += 1

    # 4) overrides — CONFIRMED roster->Sportsbet rename; both spellings resolve to it.
    applied = 0
    overrides = data.get("overrides")
    if isinstance(overrides, dict):
        for common, sportsbet in overrides.items():
            if not isinstance(common, str) or not isinstance(sportsbet, str):
                continue
            common_l, sb_l = common.strip().lower(), sportsbet.strip().lower()
            if not common_l or not sb_l:
                continue
            entry = _afl_roster.get(common_l) or _afl_roster.get(sb_l)
            if not entry:
                continue  # neither spelling on the current roster -> nothing to do
            team = entry.get("team", "")
            existing = _afl_roster.get(sb_l)
            if (existing and existing.get("team") and team
                    and existing["team"] != team):
                log.warning(
                    f"AFL name override skipped: '{common}' -> '{sportsbet}' would "
                    f"collide with an existing '{sportsbet}' on {existing['team']} "
                    f"(override target player is on {team})"
                )
                continue
            canonical = {"name": sportsbet, "team": team}
            _afl_roster[sb_l] = dict(canonical)      # Sportsbet spelling -> itself
            _afl_roster[common_l] = dict(canonical)  # short form -> the Sportsbet name
            applied += 1

    # 5) aliases — placement-time candidates tried against the LIVE catalog (no rename).
    aliases = data.get("aliases")
    if isinstance(aliases, dict):
        for name, alts in aliases.items():
            if not isinstance(name, str) or not isinstance(alts, list):
                continue
            key = name.strip().lower()
            cands = [a.strip() for a in alts if isinstance(a, str) and a.strip()]
            if key and cands:
                _afl_name_aliases[key] = cands

    if any((removed, added, team_fixed, applied, _afl_name_aliases)):
        log.info(
            f"AFL overrides applied from {AFL_NAME_OVERRIDES_FILE.name}: "
            f"{applied} rename(s), {len(_afl_name_aliases)} alias-set(s), "
            f"{team_fixed} team-fix, {added} add, {removed} remove"
        )


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


def _fold_accents(s: str) -> str:
    """Strip diacritics for accent-insensitive matching (José -> jose). Used by
    the MLB exact-match retry so an ASCII Shook tip matches the accented roster
    key. Lowercase is applied by the caller."""
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if not unicodedata.combining(c))


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
    if not info and sport == "mlb":
        # Accent-insensitive retry for MLB (v5.34): the MLB Stats API stores
        # accented names ('José Alvarado', 'Yandy Díaz'), but Shook tips them
        # ASCII ('Jose Alvarado'), so the direct lookup misses. Fold accents on
        # BOTH sides and match exact-on-the-folded-form. This is still an EXACT
        # match (no fuzzy/partial), so it cannot drift to a different player; an
        # accent-only collision across DIFFERENT teams (essentially impossible)
        # is guarded by requiring a single team. Low-frequency path (only a
        # teamless MLB tip), so the O(n) fold scan is fine.
        q_fold = _fold_accents(query.strip().lower())
        folded = [v for k, v in roster.items() if _fold_accents(k) == q_fold]
        if folded and len({m["team"] for m in folded}) == 1:
            info = folded[0]
    if info:
        # Team filter: lenient match against the entry's team
        if team and not _team_matches(info["team"], team):
            return {}
        return {"name": info["name"], "team": info["team"], "score": 1.0}
    return {}


def is_mlb_name_collision(name: str) -> list:
    """Return the list of teams an MLB same-full-name collision spans, or [] if the
    name is unambiguous. Two different players (a star + a minor-leaguer) can share
    an exact full name on different teams; the roster cannot disambiguate by name
    alone, so the resolver must NOT apply a blind team-override/inference for such a
    name (2026-06-25 Pete Alonso $399.99 wrong-game fault / Max Muncy latent). See
    update_mlb_roster_from_api's "__collisions__" build guard."""
    if not name:
        return []
    _load_rosters()
    key = name.strip().lower()
    teams = _mlb_collisions.get(key)
    if teams:
        return list(teams)
    # Belt-and-braces: honour the curated list even if the loaded roster_mlb.json
    # predates the seed (a stale JSON must NOT silently reopen the wrong-game fault).
    for _cn, _ct in MLB_KNOWN_COLLISIONS.items():
        if _cn.strip().lower() == key:
            return list(_ct)
    return []


def fuzzy_match_player(
    query: str, sport: str = "nba", threshold: float = 0.6,
    team: str = "", teams: "list | None" = None,
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
        teams: Optional list of teams to scope to (e.g. BOTH teams of the
              resolved fixture). When supplied (and at least one is
              recognised) the match is scoped to the UNION of those teams'
              players and the guarded GLOBAL FALLBACK is FORBIDDEN — the
              result is a player on one of those teams or {} (never a
              wrong-GAME player). BUG A (Wilson 2026-06-21): a bare surname
              scoped to a single team ('Richards' on 'STK') missed and the
              global fallback grabbed 'Joe Richards' (Port Adelaide — not in
              the St Kilda v Western Bulldogs game) instead of Ed Richards
              (Western Bulldogs). Scoping to both event teams resolves the
              surname uniquely and never escapes the fixture. Takes
              precedence over `team` for scoping.

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
    # BUG A (Wilson 2026-06-21) + v5.86 review: a `teams` list scopes an AFL
    # surname to the resolved fixture and FORBIDS the cross-game global fallback.
    # CRITICAL ordering (review BLOCKER): scope to the leg's OWN team FIRST, and
    # only widen to the UNION of both event teams on a MISS. A bare surname that
    # ALSO equals an OPPONENT'S FIRST NAME would otherwise out-score the intended
    # player across the union (e.g. 'Ryan' on Fremantle -> Luke Ryan scores 0.85,
    # but Ryan Byrnes/St Kilda scores 0.95 on the first name -> WRONG-team player
    # in the same game). Own-team-first keeps Saiyan singles correct (Luke Ryan)
    # while the union still rescues EasyMoney's 'Richards' on STK -> Ed Richards
    # (WB). BOTH steps forbid the global fallback (never a wrong-GAME player).
    if teams:
        if team:
            own = _scope_roster_to_team(roster, team)
            if own:
                r = _match_against(own)
                if r:
                    return r
        union: dict = {}
        for t in teams:
            sub = _scope_roster_to_team(roster, t)
            if sub:
                union.update(sub)
        if union:
            return _match_against(union)
        # No event team matched the roster (malformed event string) -> NO match
        # (caller routes to manual / keeps the bare surname for the event-scoped
        # catalog matcher); NEVER a global (wrong-game) match.
        log.warning(
            f"Team list {teams} matched no roster entries for '{query}' -> no "
            f"match (manual; global fallback forbidden when event-scoped)"
        )
        return {}
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
    # (The event-scoped `teams` path above returns early and NEVER reaches this
    # global fallback — BUG A: it resolved 'Richards' -> Joe Richards/Port
    # Adelaide, a wrong-GAME player. Only the single-`team` path falls through.)
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


def resolve_player_name(query: str, sport: str = "nba", team: str = "",
                        teams: "list | None" = None) -> str:
    """
    Resolve a query to a full player name for HyperBot.
    Returns the original query if no match found.

    `team` is plumbed through to fuzzy_match_player so callers (e.g.
    the SGM leg resolver) can scope to the leg's team prefix when known.
    `teams` (BUG A) scopes to the UNION of the resolved fixture's teams and
    forbids the cross-game global fallback (never resolve to a wrong-game
    player); takes precedence over `team`.
    """
    match = fuzzy_match_player(query, sport, team=team, teams=teams)
    if match:
        log.info(f"Roster match: '{query}' -> '{match['name']}' ({match['team']}) score={match['score']}")
        return match["name"]
    log.warning(f"No roster match for '{query}' in {sport}")
    return query


def get_player_team(query: str, sport: str = "nba", team: str = "",
                    teams: "list | None" = None) -> str:
    """Get a player's team name. Returns empty string if not found.
    `team`/`teams` filter optional — same semantics as fuzzy_match_player.
    """
    match = fuzzy_match_player(query, sport, team=team, teams=teams)
    return match.get("team", "")


def mlb_fuzzy_player(query: str, threshold: float = 0.9) -> dict:
    """GUARDED fuzzy MLB player resolver for a TYPO / minor VARIANT of a full
    name. BUG D (Wilson 2026-06-21): 'Jung Ho Lee' (tip) vs roster 'Jung Hoo Lee'
    (San Francisco Giants) — the exact match missed (1-char), so the tip routed
    to manual ("No fixture found").

    Deliberately STRICTER than fuzzy_match_player: FULL-STRING similarity ONLY
    (no surname-token boosting — that is exactly what drifts 'Juan Soto' ->
    'Gregory Soto'), a high threshold (>=0.9 ≈ a typo, not a different player),
    a 2+ token query (never a bare surname), AND resolves ONLY when EXACTLY ONE
    roster player is within threshold (any ambiguity -> {} -> manual). So it can
    correct a near-identical spelling but can never pick a same-surname player on
    the wrong team. Returns {"name","team","score"} or {}.
    """
    q = (query or "").strip().lower()
    if not q or len(q.split()) < 2:
        return {}
    _load_rosters()
    if not _mlb_roster:
        return {}
    q_fold = _fold_accents(q)
    hits = []
    for info in _mlb_roster.values():
        n = (info.get("name") or "")
        nl = n.lower()
        score = max(
            SequenceMatcher(None, q, nl).ratio(),
            SequenceMatcher(None, q_fold, _fold_accents(nl)).ratio(),
        )
        if score >= threshold:
            hits.append((score, info))
    if not hits:
        return {}
    names = {info["name"] for _, info in hits}
    if len(names) != 1:
        log.warning(
            f"MLB fuzzy '{query}' ambiguous across {sorted(names)} (>= {threshold}) "
            f"-> manual (never guess a player)"
        )
        return {}
    score, info = max(hits, key=lambda x: x[0])
    return {"name": info["name"], "team": info["team"], "score": round(score, 3)}


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


def afl_fuzzy_surname_candidates(token: str, threshold: float = 0.85) -> list:
    """v5.77: like afl_surname_candidates but FUZZY — AFL players whose surname is
    within `threshold` (difflib SequenceMatcher ratio) of the normalised `token`.

    For an Eddie bare-surname the EXACT match missed because of a vision typo
    (2026-06-19: "D'Ambrossio" vision-read vs roster "D'Ambrosio" -> exact miss ->
    manual; the misspelling scores ~0.95). Returns [{"name","team","score"}, ...]
    sorted by score desc, possibly empty/several. SAFETY: the CALLER MUST scope to
    the in-play teams AND require uniqueness — NEVER resolve a $400 bet on a fuzzy
    surname league-wide. Short tokens (<4 chars) return [] (too risky to fuzz)."""
    _load_rosters()
    tok = _afl_token_norm(token)
    if not tok or len(tok) < 4:
        return []
    import difflib
    out = []
    for _, info in _afl_roster.items():
        name = info.get("name", "")
        parts = name.split()
        if len(parts) < 2:
            continue
        last_tok = _afl_token_norm(parts[-1])
        full_surname = _afl_token_norm(" ".join(parts[1:]))
        score = max(
            difflib.SequenceMatcher(None, tok, last_tok).ratio(),
            difflib.SequenceMatcher(None, tok, full_surname).ratio(),
        )
        if score >= threshold:
            out.append({"name": name, "team": info.get("team", ""),
                        "score": round(score, 3)})
    out.sort(key=lambda c: c["score"], reverse=True)
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
        fullname_teams: dict = {}  # fullName -> set of distinct teams (collision detector)
        for p in people:
            name = (p.get("fullName") or "").strip()
            tid = (p.get("currentTeam") or {}).get("id")
            team = tmap.get(tid)
            if not (name and team):
                continue
            fullname_teams.setdefault(name, set()).add(team)
            parts = name.split()
            if len(parts) >= 2:
                key = _surnorm(parts[-1])
                surname_count[key] = surname_count.get(key, 0) + 1

        # Same-full-name collision guard (2026-06-25; Pete Alonso/Max Muncy fault).
        # /sports/1/players includes minor-leaguers, so two DIFFERENT players can
        # share an exact fullName on DIFFERENT teams. The old `roster[name] = team`
        # was last-writer-wins and silently clobbered the star (the Mets' Pete Alonso
        # overwritten by a minor-league Orioles Pete Alonso), after which main.py's
        # unconditional team override placed real money on the WRONG game with no
        # alert. So: a name on exactly ONE team -> a normal name->team entry; a name
        # on >1 team -> NO bare name->team mapping (it cannot be disambiguated by name
        # alone), recorded under "__collisions__" so the resolver routes it to manual.
        collisions = {n: sorted(ts) for n, ts in fullname_teams.items() if len(ts) > 1}
        # Merge CURATED known collisions so a single-pull snapshot that contains only
        # one same-named player (the Mets' Pete Alonso was absent 2026-06-25) cannot
        # silently write a confident WRONG single mapping. Union the curated clubs
        # with any within-pull detection and the lone snapshot team.
        for _cn, _ct in MLB_KNOWN_COLLISIONS.items():
            _seen = set(collisions.get(_cn, [])) | set(_ct) | set(fullname_teams.get(_cn, set()))
            collisions[_cn] = sorted(_seen)
        # Names on exactly ONE team get a normal mapping -- EXCEPT any that are now a
        # collision (within-pull OR curated): those must carry NO bare name->team key.
        for name, teams in fullname_teams.items():
            if len(teams) == 1 and name not in collisions:
                roster[name] = next(iter(teams))
        if collisions:
            # Persist the collision map so the loader + resolver can refuse a blind
            # override for these names (see _load_rosters / is_mlb_name_collision).
            roster["__collisions__"] = collisions
            log.warning(
                f"MLB same-name collisions dropped from name->team (disambiguate by "
                f"slate, else manual): {collisions}"
            )

        # Surnames belonging to a collision fullname must NOT become a bare alias even
        # when surname_count==1: the colliding partner may carry a different surname,
        # so e.g. 'Alonso' (count 1 = only the Orioles minor-leaguer in this pull)
        # would otherwise alias to the WRONG same-name player.
        _collision_surnames = {_surnorm(n.split()[-1]) for n in collisions if n.split()}

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
                if (surname_count.get(_surnorm(last)) == 1 and last not in roster
                        and _surnorm(last) not in _collision_surnames):
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
