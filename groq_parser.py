"""
Groq LLM tip parser.

Sends raw tipster messages to Groq's Llama model and gets back
structured JSON with all fields needed for HyperBot placement.

One API call per message, handles all tipster formats including
Kev's obfuscation, AusBets, Saiyan, and Shook.
"""

import base64
import json
import logging
import os
import re
import time
import requests
from typing import Optional
from models import ParsedTip, ParsedLeg
from config import NBA_STAT_MAP

log = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


def _parse_json_with_repair(content: str) -> Optional[dict]:
    """
    Parse Groq's JSON response, with repair for common truncation failures.
    Groq can truncate mid-response when hitting max_tokens, leaving unclosed
    braces/brackets. On 2026-04-23 a KAT tip arrived with JSON ending at
    '"alt_line": null}]' — missing the outer closing '}'.
    """
    # Fast path: clean JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Strip trailing junk
    trimmed = content.rstrip().rstrip(",")

    # Try appending progressively more close-tokens
    suffixes = ["}", "]}", "]]}", "}}", "]}}", "\"}", "\"}]", "null}]}", "null}}]}"]
    for suffix in suffixes:
        try:
            candidate = trimmed + suffix
            parsed = json.loads(candidate)
            log.warning(
                f"Groq JSON repair succeeded with suffix '{suffix}' "
                f"(original was truncated at {len(content)} chars)"
            )
            return _validate_sgm_legs_after_repair(parsed)
        except json.JSONDecodeError:
            continue

    # Mid-key truncation salvage: Groq sometimes truncates mid-field like
    # '"selection":' (key + colon, no value). Walk back to the last valid
    # field-terminator (comma or opening brace) and try closing from there.
    # This is the 2026-04-24 Ajay Mitchell case: truncation at `"selection":`.
    for cut_char in (",", "{"):
        last_cut = trimmed.rfind(cut_char)
        if last_cut > 0:
            # Drop the trailing broken field, close whatever's open
            stub = trimmed[:last_cut].rstrip().rstrip(",")
            for suffix in ["}]}", "}}]}", "]}"]:
                try:
                    candidate = stub + suffix
                    parsed = json.loads(candidate)
                    log.warning(
                        f"Groq JSON repair (mid-key salvage): dropped broken "
                        f"field after pos {last_cut}, closed with '{suffix}' "
                        f"(recovered {len(parsed.get('tips', []))} tip(s))"
                    )
                    return _validate_sgm_legs_after_repair(parsed)
                except json.JSONDecodeError:
                    continue

    # Last resort: salvage last complete tip object
    last_obj_end = trimmed.rfind("}")
    if last_obj_end > 0:
        tips_start = trimmed.find('"tips"')
        if tips_start > 0:
            array_start = trimmed.find("[", tips_start)
            if array_start > 0 and array_start < last_obj_end:
                try:
                    candidate = (
                        "{\"tips\":"
                        + trimmed[array_start:last_obj_end + 1]
                        + "]}"
                    )
                    parsed = json.loads(candidate)
                    log.warning(
                        "Groq JSON repair: salvaged partial tips array "
                        f"(recovered {len(parsed.get('tips', []))} tip(s))"
                    )
                    return _validate_sgm_legs_after_repair(parsed)
                except json.JSONDecodeError:
                    pass

    return None


def _validate_sgm_legs_after_repair(parsed: dict) -> dict:
    """H30: After JSON repair, validate that SGM tips still have >=2 legs.

    JSON repair can drop the last leg if it was mid-truncation. Rather than
    crashing or silently placing a 1-leg 'SGM', mark such tips as alert_only
    with a clear reason so Wilson can place manually.
    """
    tips = parsed.get("tips", [])
    for td in tips:
        if not td.get("is_sgm", False):
            continue
        legs = td.get("raw_legs") or []
        if len(legs) < 2:
            td["alert_only"] = True
            td["alert_reason"] = "SGM repair dropped a leg"
            log.warning(
                f"H30: SGM tip has only {len(legs)} leg(s) after JSON repair — "
                f"marking alert_only (manual review required)"
            )
    return parsed


SYSTEM_PROMPT = """You are a sports betting tip parser. You receive raw messages from tipster channels and extract structured bet information.

TIPSTER FORMATS:

CRITICAL UNIVERSAL RULE — applies to ALL tipsters and ALL sports:
If a single line contains multiple bet legs separated by "/", it is
ALWAYS one SGM tip with is_sgm=true. Put every leg into raw_legs.
NEVER split such a line into multiple separate tips.

These are ALL single SGM tips (one tip each, multiple legs):
  "Brunson u36.5 points/o4.5 assists"          -> 1 tip, 2 legs (NBA, mixed O/U)
  "Maxey o25.5 points/o6.5 assists"            -> 1 tip, 2 legs (NBA, both over)
  "Brunson u37.5 points/4+ assists"            -> 1 tip, 2 legs (NBA, mixed O/U + threshold)
  "LeBron 25+/Davis 12+ rebs"                  -> 1 tip, 2 legs (NBA, both threshold)
  "Suns -7.5/Devin Booker 25+"                 -> 1 tip, 2 legs (NBA, team + player)
  "Player A 25+/Player B 18+ disposals/Treacy AGS" -> 1 tip, 3 legs (AFL)
  "Rowell 24+ Disposals/Parish 19+ Disposals/Suns -7.5" -> 1 tip, 3 legs (AFL)

The "/" separator on one line ALWAYS means SGM regardless of:
  - whether legs are O/U, threshold, or team markets
  - whether legs are all the same format or mixed
  - whether legs are for the same player or different players
  - which sport the tip is in

Tips on DIFFERENT lines (each its own line/emoji) = SEPARATE tips, not SGM.

1. SAIYAN AFL: Posts AFL player props and SGMs

   AFL TEAMS (you must know ALL of these codes):
   - ADE/ADEL = Adelaide
   - BRI/BR/BL/BRIS = Brisbane Lions
   - CAR/CARL = Carlton
   - COL/COLL = Collingwood
   - ESS/ESSE = Essendon
   - FRE/FREO = Fremantle
   - GEE/GEEL/CATS = Geelong
   - GC/GCS/GCFC/SUNS = Gold Coast
   - GWS/GIANTS = Greater Western Sydney
   - HAW/HAWTH = Hawthorn
   - MEL/MELB/DEES = Melbourne
   - NM/NMFC/KANGAS = North Melbourne
   - PA/PAFC/PORT = Port Adelaide
   - RIC/RICH/TIGERS = Richmond
   - STK/STKFC/SAINTS = St Kilda
   - SYD/SWANS = Sydney
   - WCE/WEST/EAGLES = West Coast
   - WBD/WB/DOGS/BULLDOGS = Western Bulldogs

   AFL PLAYER NICKNAMES (these are PLAYERS, not teams - never map to a team):
   - NWM = Nasiah Wanganeen-Milera (St Kilda)
   - BONT = Marcus Bontempelli (Western Bulldogs)
   When you see these initialisms next to a team emoji, treat them as the
   named player on that team for a player prop.

   AFL STATS (thresholds are whole numbers, O/U are .5 lines):
   - Disposals (10+ to 40+), goals (1+ to 6+), marks (2+ to 10+)
   - Tackles (2+ to 10+), kicks (10+ to 28+), handballs (8+ to 26+)
   - Clearances (4+ to 12+), hitouts (15+ to 50+), fantasy_points (70+ to 130+)

   AGS = anytime goalscorer = "1+ goals" in AFL. AGS only ever appears as
   a leg INSIDE an SGM (Saiyan never tips it as a standalone single). When
   you see "AGS" or "ags" inside an SGM leg, treat it as a goals threshold:
     stat=goals, line=1, selection=over, is_threshold=true,
     market=goalscorer_threshold_afl
   Example SGM leg: ".../Treacy AGS/..."
     -> {player: "Josh Treacy", stat: "goals", line: 1, selection: "over",
         is_threshold: true, market: "goalscorer_threshold_afl"}
   Do NOT emit AGS as a standalone single tip.

   FORMATS:
   - Standard O/U: "<emoji> Player (TEAM) Under 22.5 Disposals @ 1.85 Sportsbet"
   - Threshold: "<emoji> Player (TEAM) 25+ Disposals @ 1.80 Sportsbet"
   - SGM (legs separated by "/"): ONE tip with is_sgm=true, ALL legs in raw_legs
     Example: "Matt Rowell 24+ Disposals/Darcy Parish 19+ Disposals/Suns -7.5 @ 1.80 Sportsbet"
     -> ONE tip with 3 legs, NOT 3 separate tips
   - SGM with AGS leg: "Jackson 17+/Hollands 15+/Treacy AGS @ 1.80 Sportsbet"
     -> 3 legs: Jackson 17+ disposals, Hollands 15+ disposals, Treacy 1+ goals
   - Same-player shorthand in SGM: "Sam Darcy 10+ Disposals/2+ Goals" means BOTH legs are Sam Darcy
     If a leg has stat but no player named, USE THE PREVIOUS LEG'S PLAYER

   MULTIPLE TIPS IN ONE MESSAGE (each on its own line with its own emoji):
   - "<emoji> Player A (TEAM1) Under 22.5 Disposals @ 1.85
      <emoji> Player B (TEAM2) Under 19.5 Disposals @ 1.90
      <emoji> Player C (TEAM2) Under 16.5 Disposals @ 1.87"
   - = 3 SEPARATE tips (each on own line)

   DISTINGUISHING SGM vs SEPARATE TIPS:
   - Legs on SAME LINE separated by "/" = SGM (one bet)
   - Tips on DIFFERENT LINES = separate bets

   - Commentary lines starting with * should be ignored
   - Default units: 1.0u per tip
   - For player props, always include full player name in "player" field
   - Include 3-letter team code in "team" field (we resolve it server-side)

   LOW-ODDS SGM DETECTION:
   - If SGM odds < 1.90 AND any leg is a threshold (X+ format), it's likely Pick-Your-Own-Line
   - Set is_pyo_sgm=true for these (we'll use pick_own_line market)

2. AUSBETS NBA: Posts NBA player props, spreads, totals
   - Format: "Aus NBA:1U - Player Over/Under X.5 STAT (bookie: $odds) @everyone"
   - Threshold: "1U - Player 20+ P (SB: $1.93)"
   - Carry-forward: "0.5U - 25+ P (SB: $3.15)" = same player as previous tip
   - Spread: "1U - Team -5.5 (SB: $1.88)"
   - 1st Half: "1.5U - Team 1st Half -5.5 (SB: $1.81)"
   - ML: "1U - Team ML (SB: $1.10)"
   - Alt/SGM: legs with "/" = is_sgm true, put all legs in raw_legs
   - Stat codes (always output with UNDERSCORES, never "+"):
       P=points, R=rebounds, A=assists,
       PR -> "points_rebounds", PA -> "points_assists",
       PRA -> "points_rebounds_assists"

   NBA TEAM ABBREVIATIONS (used by AusBets and Kev — extract these as the
   team field even when the message is mostly analysis prose):
   - ATL=Atlanta Hawks, BOS=Boston Celtics, BKN=Brooklyn Nets,
     CHA=Charlotte Hornets, CHI=Chicago Bulls, CLE=Cleveland Cavaliers,
     DAL=Dallas Mavericks, DEN=Denver Nuggets, DET=Detroit Pistons,
     GSW=Golden State Warriors, HOU=Houston Rockets, IND=Indiana Pacers,
     LAC=Los Angeles Clippers, LAL=Los Angeles Lakers, MEM=Memphis Grizzlies,
     MIA=Miami Heat, MIL=Milwaukee Bucks, MIN=Minnesota Timberwolves,
     NOP=New Orleans Pelicans, NYK=New York Knicks, OKC=Oklahoma City Thunder,
     ORL=Orlando Magic, PHI=Philadelphia 76ers, PHX=Phoenix Suns,
     POR=Portland Trail Blazers, SAC=Sacramento Kings,
     SAS/SA=San Antonio Spurs, TOR=Toronto Raptors, UTA=Utah Jazz,
     WAS=Washington Wizards
   When a message contains analysis text but mentions a team abbreviation
   plus a spread/line/total at the end (e.g. "SAS opened -3 and is now at
   -5.5"), extract the team and the bet anyway — don't return an empty
   team field.

3. KEV NBA/NBL: Posts NBA and NBL tips with OBFUSCATED player names
   - DEOBFUSCATION: ! -> i, @ -> a, 0 -> o, 3 -> e, 1 -> l, $ -> s, 5 -> s
   - Examples: K!spert = Kispert, J0kic = Jokic, F!lipowski = Filipowski, M@nn = Mann, L3br0n = Lebron
   - Inline: "Player o22.5pra @ 1.9 with B365 - 1 unit"
   - Threshold: "Player 20+ pts @ 5.75 with B365 - 0.2 units"
   - Follow-up: "15+ @ 9.00 with 365 - 0.15 units" = same player as previous bet in message
   - SGM: legs with "/" = is_sgm true, put all legs in raw_legs
   - LIVE: prefixed with "LIVE:" = alert only
   - o = over, u = under
   - pra -> "points_rebounds_assists", pts -> "points", ast -> "assists",
     pr -> "points_rebounds", pa -> "points_assists", rbd/reb -> "rebounds".
     Always output stat with UNDERSCORES, never "+".

4. SHOOK NBA/NFL/WNBA/MLB: US-focused tipster. Multiple messages per tip.
   - Main tip: "@everyone [Full Name] M [line] [stat] [american_odds] [bookies] [units]u"
   - M = More = MUST output selection = "over" (NOT "more")
   - L = Less = MUST output selection = "under" (NOT "less")
   - Examples: "@everyone Karl Anthony Towns M 32.5 PRA -121 CZRs, -122 FD 0.4u" -> selection="over"
   - "@everyone Alex Caruso M 5.5 Points -136 FD 0.4u"
   - "@everyone Nicolas Claxton L 25.5 PRA -125 HR 0.3u"
   - Team bets: "@everyone Heat +1.5 FD 0.65u", "@everyone Lakers Money Line -125 CZRS 0.65u"
   - Game context may appear in RECENT CONTEXT section: "NYK/LAC" or "MEM/BKN"
   - Ignore American odds (-121 etc), we don't need them for Australian bookies
   - US bookies (CZRs, FD, DK, HR, Fliff, Builder, MGM, BOV) should NOT be used as bookie value
   - Set bookie to "sportsbet" for Shook tips (we place on AU bookies)
   - Alt lines: "21.5 0.4" means same player at 21.5 for 0.4u
     If "alt" keyword is used (e.g. "30.5 PR same unit alt"), it's a FALLBACK, not a separate bet
     -> Set primary bet's alt_line = {stat, line, selection} for fallback if primary fails
   - "13.5 pts valid as well" = same player at 13.5 points, treat as separate tip (no "alt" keyword)
   - Double Double tips: "@everyone Rudy Gobert Double Double -125 FD 0.3u" -> alert_only (can't automate)
   - Stat codes (input -> output stat string):
       PRA -> "points_rebounds_assists"
       PR  -> "points_rebounds"
       PA  -> "points_assists"
       RA  -> "assists_rebounds"
       3s  -> "threes"
     IMPORTANT: always output stat with UNDERSCORES, never "+". Sending
     "points+assists" instead of "points_assists" breaks downstream market
     mapping.
   - If you see "RECENT CONTEXT" section, use it for game info but the CURRENT MESSAGE has the actual bet
   - DETECT SPORT from context clues:
     - HRR/Hits/Runs/RBIs/Batting/Pitching/Innings = MLB (sport: "mlb")
     - Rushing/Receiving/Passing Yards/Touchdowns/Sacks = NFL (sport: "nfl")
     - NBA team names or NBA stats (PRA/PR/PA) = NBA (sport: "nba")
     - WNBA player names or "W" prefix = WNBA (sport: "wnba")
     - Game context like "CLE/BAL" with HRR = MLB, "CLE/BAL" with PRA = NBA
   - When sport is "auto" or unclear, you MUST determine the sport from message content
   - ALWAYS include the team name in the "team" field for event resolution (e.g. "Cleveland Guardians", "Baltimore Orioles")
   - For MLB on Sportsbet: stat "hrr"/"HRR" -> send as stat="h_r_rbi" (the
     Hits+Runs+RBIs combined market). Shook's "M 1.5 HRR" = Over 1.5 = 2+ HRRBI:
     output player=full name, stat="h_r_rbi", line=1.5, selection="over",
     is_threshold=false, sport="mlb". Player name format works.
   - For MLB: H=hits, R=runs, RBI=rbis, K=strikeouts, TB=total_bases, HR=home_runs
   - For NFL: rushing=rushing_yards, receiving=receiving_yards, passing=passing_yards, TDs=touchdowns
   - For MLB tips, ALWAYS set bookie to "sportsbet"

5. TEST (TipBot Test): Direct tip in any format. Apply universal rules.
   - User specifies sport at start ("nba", "afl") on its own line OR inline
   - If "afl" mentioned anywhere in message, set sport="afl"
   - Otherwise default to sport="nba"
   - Default units = 1.0u

   NBA examples:
   - "1U Kawhi Over 42.5 PRA"                    -> 1U, NBA O/U, points_rebounds_assists
   - "Maxey Over 29.5 Points"                    -> 1U, NBA O/U, points
   - "brunson u36.5 points/o4.5 assists"         -> 1U, NBA SGM (universal rule), 2 legs
   - "lebron 25+/davis 12+ rebs"                 -> 1U, NBA SGM, 2 threshold legs

   AFL examples (sport="afl"):
   - "afl josh treacy AGS"                       -> 1U, AFL, AGS standalone
                                                    NOTE: AGS as a single bet IS allowed
                                                    on test (Saiyan rule about AGS-only-in-SGMs
                                                    does not apply here). Output:
                                                    {player: "Josh Treacy", stat: "goals",
                                                     line: 1, selection: "over",
                                                     is_threshold: true, sport: "afl",
                                                     market: "goalscorer_threshold_afl"}
   - "afl josh treacy 1+ goal fremantle"         -> 1U, AFL AGS (1+ goal = same as AGS).
                                                    "fremantle" is the team. Same output as above
                                                    plus team="Fremantle".
   - "afl rowell 24+ disposals"                  -> 1U, AFL threshold, stat=disposals, line=24
   - "afl rowell o22.5 disposals"                -> 1U, AFL O/U, stat=disposals, line=22.5
   - "afl collingwood ml"                        -> 1U, AFL h2h, team=Collingwood
   - "afl rowell 24+ disp/parish 19+ disp"       -> 1U, AFL SGM (universal rule), 2 legs

   Parse aggressively - if it looks like a bet, parse it. Apply the universal
   "/" SGM rule and the universal stat-mapping rule (all stats use UNDERSCORES,
   never "+").

STAT MAPPING (use these exact values):
- points, rebounds, assists, threes, blocks, steals
- points_rebounds, points_assists, assists_rebounds, points_rebounds_assists

MARKET TYPES:
- player_prop: player stat bets (over/under or threshold)
- h2h: moneyline/head-to-head
- line: point spread/handicap
- total: game total over/under
- first_half_line: 1st half spread
- team_total: single team total

N+ THRESHOLD SHORTHAND (CRITICAL - READ CAREFULLY):
When a tip uses "N+ Stat" format (e.g. "4+ A", "20+ P", "25+ PRA", "6+ Rebounds"), you MUST distinguish between two cases:

CASE A - STANDALONE tip (not inside an SGM):
- Keep as threshold: line = N (whole number), selection = "over", is_threshold = true
- These use the pick-your-own-line market downstream
- Example: "1U - Player 20+ P" -> line=20, selection=over, is_threshold=true

CASE B - SGM LEG (one leg of a multi-leg bet with is_sgm=true, ANY sport):
- KEEP as threshold: line = N (whole number), selection = "over", is_threshold = true
- SGM legs use sport-specific threshold markets on the bookie (player_pts_threshold,
  player_disposals_threshold, goalscorer_threshold_afl, etc.) which accept integer lines.
- Do NOT subtract 0.5 and do NOT clear the threshold flag.
- Sportsbet does NOT offer O/U lines at N-0.5 for every player; converting 15+ to
  Over 14.5 fails with "line moved" because Sportsbet's O/U anchor is elsewhere
  (e.g. Brunson's main O/U was 26.5, threshold line 15 is a separate market).
- Examples:
  * "4+ A" in NBA SGM -> line=4, selection=over, is_threshold=true
  * "6+ R" in NBA SGM -> line=6, selection=over, is_threshold=true
  * "20+ P" in NBA SGM -> line=20, selection=over, is_threshold=true
  * "25+ PRA" in NBA SGM -> line=25, selection=over, is_threshold=true
  * "11+ Disposals" in AFL SGM -> line=11, selection=over, is_threshold=true
  * "2+ Goals" in AFL SGM -> line=2, selection=over, is_threshold=true

Formula for SGM legs (all sports): N+ X -> Over N X with is_threshold=true (keep integer)

RULES:
- For threshold tips (20+ P standalone), set: line = 20 (the whole number), selection = "over", is_threshold = true
- For over/under line tips (Over 29.5 P), set: line = 29.5, selection = "over", is_threshold = false
- Deobfuscate Kev's player names and return the REAL name
- For Kev, resolve last-name-only to full name if you know it (e.g. "Jokic" -> "Nikola Jokic")
- SGMs (multiple legs in one bet) should have is_sgm = true, alert_only = false. Put legs in raw_legs.
- For SGM same-player legs: if a leg has stat but no explicit player, USE THE PREVIOUS LEG'S PLAYER
  Example: "Sam Darcy 10+ Disposals/2+ Goals" -> leg 1 = Sam Darcy 10 Disp over, is_threshold=true, leg 2 = Sam Darcy 2 Goals over, is_threshold=true
- Low-odds SGMs (odds < 1.90) that contain threshold legs are likely PICK-YOUR-OWN-LINE multis. Set is_pyo_sgm = true.
  For is_pyo_sgm=true SGMs, keep the integer line (pick_own_line market accepts integers).
- ALT LINE FALLBACKS: If message says "X pts same unit alt" or similar "alt" keyword, add alt_line to the primary tip (do NOT create a separate tip):
  - alt_line = {"stat": "...", "line": N, "selection": "over/under", "market": "..."}
- LIVE bets should have alert_only = true, is_live = true, alert_reason = "LIVE bet - place manually"
- Commentary/recaps with no bet info should return empty tips array
- Each individual bet is a separate tip object
- sport should be: "afl", "nba", "nbl", "nfl", "mlb", "nhl", "soccer", "tennis", "cricket", "rugby_union", "mma", "boxing" etc.
- bookie: "sportsbet", "bet365", "tab", "pointsbet", "ladbrokes", etc.
- For Shook tips, always set bookie to "sportsbet"
- For MLB tips, always set bookie to "sportsbet"
- Extract odds from the tip (decimal format for AU bookies, ignore American odds from Shook)
- For line/handicap bets, selection must be the TEAM NAME (e.g. "Golden State"), not "over"/"under"
- For H2H/ML bets, selection must be the TEAM NAME
- For total/over-under game bets, selection should be "over" or "under"

Respond with ONLY valid JSON, no markdown fences, no explanation. Format:
{"tips": [{"player": "", "team": "", "stat": "", "line": 0, "selection": "", "market": "", "units": 1.0, "odds": 0, "bookie": "", "is_sgm": false, "is_pyo_sgm": false, "is_live": false, "is_threshold": false, "alert_only": false, "alert_reason": "", "sport": "nba", "raw_legs": [], "alt_line": null}]}

For SGMs, put all legs in raw_legs: [{"player": "", "stat": "", "line": 0, "selection": "", "market": "", "is_threshold": false}]
For non-SGM tips, raw_legs should be empty [].
For alt_line (fallback): {"stat": "...", "line": N, "selection": "over/under", "market": "player_prop", "is_threshold": false} or null if no fallback.
If the message is commentary/noise with no bets, return: {"tips": []}"""


# Explicit bare sport keywords a tipster can lead a message with. Used as a
# DETERMINISTIC override for Groq's sport field, which is non-deterministic
# even at temperature 0: 2026-05-31 "afl clayton oliver 22.5+ disposal/..."
# parsed as AFL three times then as NBA on the fourth (the prompt asks Groq to
# set sport=afl when "afl" appears, but it intermittently ignores it). When the
# message starts with one of these tokens we trust it over the LLM.
_EXPLICIT_SPORT_TOKENS = {
    "afl": "afl", "nba": "nba", "nbl": "nbl", "wnba": "wnba",
    "nfl": "nfl", "nrl": "nrl", "mlb": "mlb",
}


def _explicit_leading_sport(text: str) -> str | None:
    """Return the sport if `text` begins with a bare sport keyword (e.g.
    'afl ...', 'nba: ...'), else None. Deterministic guard against Groq's
    flaky sport detection — only triggers on an explicit LEADING token, so
    mid-message mentions and emoji-prefixed (Saiyan) messages are unaffected."""
    if not text:
        return None
    # M (2026-05-31): take the leading RUN OF LETTERS as the candidate token.
    # This gives an explicit token boundary (the run ends at the first non-letter)
    # so "aflfoobar ..." does NOT match, while both "afl clayton ..." (space
    # boundary) and "nba:lebron ..." (colon boundary, no space) resolve. Only an
    # exact sport keyword is honoured.
    m = re.match(r"[A-Za-z]+", text.strip())
    if not m:
        return None
    return _EXPLICIT_SPORT_TOKENS.get(m.group(0).lower())


def _merge_slash_line_into_sgm(tips: list, text: str) -> list:
    """Deterministic "/" = SGM (Wilson 2026-05-31).

    The system prompt declares a single line of "/"-separated legs is ALWAYS one
    SGM, but Groq is non-deterministic and intermittently splits such a message
    into separate tips — especially mixed player-prop + team-handicap combos
    (2026-05-31: "...23+ disposal/gws +50.5 hc" split, the near-identical
    "...giants +50.5hc" did not). When the message is a SINGLE line containing
    "/" and Groq returned multiple tips, merge them into one SGM ticket.
    Multi-line messages (legs on separate lines) are left untouched, per the
    same rule ("Tips on DIFFERENT lines = SEPARATE tips").
    """
    if len(tips) < 2 or "/" not in (text or ""):
        return tips
    # Strip the Shook buffer prefix so we judge the actual bet line.
    bet_text = text.split("CURRENT MESSAGE:", 1)[-1] if "CURRENT MESSAGE:" in text else text
    if "\n" in bet_text.strip():
        return tips  # legs on separate lines stay separate tips

    # Collect every (tip, leg) pair WITHOUT mutating yet. The per-leg threshold
    # flag is only meaningful for the MERGED SGM, so we must NOT stamp it onto
    # legs we might return UNMERGED — a single tip carrying a leg-level
    # _is_threshold would be misrouted to a threshold market downstream
    # (main.py reads leg._is_threshold). v4.6 audit re-verify fix: mutate only
    # after the guards below confirm we are actually merging.
    leg_pairs = [(t, lg) for t in tips for lg in t.legs]
    if len(leg_pairs) < 2:
        return tips

    # Event-consistency guard (Wilson 2026-05-31): a valid SGM is SAME-GAME. An
    # AFL/NBA game has exactly two teams, so:
    #   - 3+ distinct KNOWN teams              -> cannot be one game (Groq
    #     mis-split a cross-game line).
    #   - 2 distinct known teams AND a team-LESS leg -> the unknown leg could
    #     belong to a THIRD game, so we cannot confirm same-game.
    # Either way keep the tips SEPARATE (each places as Groq returned it). We
    # CANNOT detect the 2-known-teams-from-2-different-games case at parse time
    # (the fixture isn't known until resolution); the safety net there is that
    # the bookie REJECTS a cross-game SGM (SGMs must be same-game) so the tip
    # routes to manual — no wrong/over bet is ever placed. v4.6: the team-less
    # clause closes the gap a verifier flagged (a missing team_full silently
    # dropping a leg from the cross-game count).
    distinct_teams = {
        (lg.team_full or "").strip().lower()
        for _t, lg in leg_pairs
        if (lg.team_full or "").strip()
    }
    has_teamless_leg = any(not (lg.team_full or "").strip() for _t, lg in leg_pairs)
    if len(distinct_teams) > 2 or (len(distinct_teams) >= 2 and has_teamless_leg):
        log.warning(
            f"'/'=SGM merge SKIPPED: cannot confirm same-game (distinct teams="
            f"{sorted(distinct_teams)}, teamless_leg={has_teamless_leg}) — "
            f"keeping tips separate"
        )
        return tips

    # Confirmed mergeable: NOW stamp the per-leg threshold flag and build the
    # merged leg list (the SGM builder reads leg._is_threshold per-leg).
    merged_legs = []
    for t, lg in leg_pairs:
        if getattr(t, "_is_threshold", False) and not getattr(lg, "_is_threshold", False):
            lg._is_threshold = True
        merged_legs.append(lg)

    base = tips[0]
    # Bookie-metadata warning (2026-05-31): the merged SGM keeps tip[0]'s
    # suggested_bookie. If the split tips named DIFFERENT bookies, that signal is
    # dropped — warn so it's visible in the log rather than silently lost.
    _bookies = {
        (getattr(t, "suggested_bookie", "") or "").strip().lower()
        for t in tips
        if (getattr(t, "suggested_bookie", "") or "").strip()
    }
    if len(_bookies) > 1:
        log.warning(
            f"'/'=SGM merge: split tips named different bookies {sorted(_bookies)}; "
            f"using tip[0]'s '{base.suggested_bookie}' for the merged SGM"
        )
    base.legs = merged_legs
    base.is_sgm = True
    base.is_pyo_sgm = any(getattr(t, "is_pyo_sgm", False) for t in tips)
    # Groq split the ticket, so there is no reliable combined SGM price — accept
    # the bookie's computed multi price (no per-leg odds floor to apply).
    base.suggested_odds = 0.0
    base._is_threshold = False  # threshold is per-leg for an SGM
    log.info(
        f"Deterministic '/'=SGM: Groq split a single '/'-line into {len(tips)} "
        f"tips; merged into 1 SGM ({len(merged_legs)} legs)"
    )
    return [base]


def _fix_sgm_threshold_leg(leg: ParsedLeg, is_pyo_sgm: bool, sport: str = "nba") -> ParsedLeg:
    """
    Safety net for SGM legs with integer player-prop lines.

    As of v3.9, SGM N+ legs route to threshold markets (player_pts_threshold,
    player_disposals_threshold, goalscorer_threshold_afl, etc.) for ALL sports.
    The markets accept integer lines natively, so we PROMOTE any integer-line
    player_prop leg to is_threshold=true rather than subtracting 0.5.

    This replaces the v3.8 behaviour of converting NBA SGM integers to O/U
    at N-0.5, which was broken — Sportsbet's main O/U for most players sits
    elsewhere (e.g. Brunson's O/U anchor was 26.5, so Over 14.5 could not be
    placed and was rejected with "line moved 14.5 → 26.5"). The threshold
    market at line=15 is the correct destination.

    Skipped when is_pyo_sgm=True (those want O/U-style PYO custom lines).
    """
    if is_pyo_sgm:
        return leg
    if leg.market != "player_prop":
        return leg
    try:
        line_f = float(leg.line)
    except (TypeError, ValueError):
        return leg
    if line_f <= 0 or not line_f.is_integer():
        return leg

    # Integer line on a player-prop SGM leg => threshold market
    if not getattr(leg, "_is_threshold", False):
        leg._is_threshold = True
        if not leg.selection or leg.selection.lower() in ("", "over"):
            leg.selection = "over"
        log.info(
            f"SGM threshold promoted: {leg.player} {int(line_f)}+ {leg.stat} "
            f"(is_threshold=true, sport={sport})"
        )
    return leg


def _safe_float(value, default: float = 0.0) -> float:
    """float() that tolerates None and bad strings.

    2026-05-03 Ayton regression: Shook tip 'Deandre Ayton L 18.5 Points+
    Rebounds' caused Groq to return JSON with `line: null` (Groq couldn't
    map the unusual 'L' prefix). Bare `float(td.get("line", 0))` blew up
    because dict.get returns None when the KEY is present with a null
    value (the default kwarg only kicks in for missing keys). Result:
    parser crash with no fallback, tipbot fired a generic PARSE ERROR
    alert and the tip was lost.

    Use this anywhere we're pulling numeric fields out of Groq's response.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_name(name: str) -> str:
    """
    Normalise player/team name strings. Strips non-breaking spaces (\\u00a0)
    and other unicode whitespace artifacts that leak through from Telegram
    messages (Saiyan AFL uses NBSP between first and last name, which got
    through to HyperBot payloads on 2026-04-24 — it accepted AFL bets at
    1.88/1.89 but could silently fail on other bookies).
    """
    if not name:
        return name
    # Replace NBSP (U+00A0), narrow NBSP (U+202F), zero-width space (U+200B)
    # and any other unicode whitespace with a regular space
    import re as _re
    cleaned = name.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200b", "")
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# Canonical stat strings the downstream market mapper (NBA_OU_MARKETS in
# main.py) keys on. Anything not on the canonical list gets normalised below.
_STAT_ALIASES = {
    # Common Groq slip: emits "+" because earlier prompts described shorthand
    # as "PA=points+assists" and Groq echoed the symbol literally. Bug
    # confirmed 2026-04-30 on Shook Maxey "31.5 Points+Assists" tip: stat
    # came through as "points+assists", market mapper missed it, HyperBot
    # rejected with "Market 'player_prop' not found".
    "points+assists": "points_assists",
    "points+rebounds": "points_rebounds",
    "points+rebounds+assists": "points_rebounds_assists",
    "assists+rebounds": "assists_rebounds",
    "rebounds+assists": "assists_rebounds",
    # Underscore-reversed: only the assists_rebounds case is broken because
    # NBA_OU_MARKETS keys it alphabetically ("assists_rebounds") rather than
    # input-order. Tipsters write "R+A", Groq emits "rebounds_assists",
    # canonical lookup misses, market stays "player_prop", HyperBot rejects.
    # The other 2-element and 3-element combos (PR, PA, PRA) all key in
    # input-order so input-order Groq emissions match canonical natively.
    # Bug confirmed 2026-05-05 on Shook Karl-Anthony Towns "Rebounds+Assists"
    # tip. Same regression class as the 2026-04-30 "+" symbol fix above.
    "rebounds_assists": "assists_rebounds",
    # Shorthand abbreviations that Groq sometimes passes through verbatim
    "pra": "points_rebounds_assists",
    "pr": "points_rebounds",
    "pa": "points_assists",
    "ra": "assists_rebounds",
    "ar": "assists_rebounds",
    "pts": "points",
    "rebs": "rebounds",
    "asts": "assists",
    "ast": "assists",
    "reb": "rebounds",
    "rbd": "rebounds",
}


def _normalise_stat(stat: str) -> str:
    """
    Map common stat-string variants to the canonical underscore form expected
    by NBA_OU_MARKETS. No-op when stat is already canonical or unrecognised.
    """
    if not stat:
        return stat
    s = stat.strip().lower()
    if s in _STAT_ALIASES:
        return _STAT_ALIASES[s]
    # Belt and braces: any "+" still in a stat string becomes "_"
    if "+" in s:
        return s.replace("+", "_")
    return s


def _normalise_alt_dict(alt):
    """Apply _normalise_stat to an alt_line dict's stat field."""
    if not alt:
        return alt
    out = dict(alt)
    if "stat" in out:
        out["stat"] = _normalise_stat(out.get("stat", ""))
    return out


# ── Saiyan AFL emoji pre-processor ──────────────────────────────────
# Saiyan tags every leg with a Discord team emoji like <:GWS:1465...>.
# Groq doesn't reliably read these as team identifiers, so we rewrite
# them into the (TEAM) format the prompt expects before sending. Two
# rewrites:
#
#   1. Standalone tip:    "<:GWS:...> Lachie Ash Under 29.5 Disposals"
#                      -> "Lachie Ash (GWS) Under 29.5 Disposals"
#
#   2. SGM leg with player name (no parens):
#      "<:GWS:...> Oliver 26+/<:GC:...> Petracca 24+"
#   -> "Oliver (GWS) 26+/Petracca (GC) 24+"
#
# Codes are validated against AFL_TEAMS so unknown codes fall through
# unchanged (the regex parser path will still see the original).
import re as _re_saiyan

# Trailing class `[\s*]*` consumes whitespace AND markdown bold/italic
# asterisks that Saiyan sometimes flushes against the emoji closer with no
# space: "<:HAW:1465...>** Moore 16+". Without consuming the **, the second
# pass below could not capture "Moore" as the player (it saw "*" as the
# next char), defensive cleanup stripped the sentinel, team was lost, and
# Groq mis-inferred Moore as Darcy Moore (Collingwood) instead of HAW.
# Resolver then picked the wrong fixture entirely (Collingwood v West
# Coast). 2026-05-16 11:08 Moore/Sparrow SGM regression.
# `_` deliberately NOT added to the char class. It would collide with the
# __SAIYAN_TEAM_ sentinel delimiter and corrupt the second pass.
_SAIYAN_EMOJI_RE = _re_saiyan.compile(r"<:([A-Z]{2,4}):\d+>[\s*]*")


def _preprocess_saiyan_emojis(text: str) -> str:
    """Rewrite Saiyan Discord team emojis into Groq-friendly (TEAM) tags.

    Walks the message, and for every <:CODE:digits> prefix, captures the
    next player-name token cluster and inserts (CODE) immediately after
    the player name. Player name is taken as 1-3 capitalised words after
    the emoji, before the next number/keyword. Keeps the rest of the
    line untouched so existing parser logic for stats/lines/odds isn't
    affected.

    Falls back to leaving the emoji as-is if the code isn't a known AFL
    team — better to do nothing than emit a garbled team tag.
    """
    try:
        from config import AFL_TEAMS
    except Exception:
        return text

    def _rewrite(match: _re_saiyan.Match) -> str:
        code = match.group(1).upper()
        if code not in AFL_TEAMS:
            return match.group(0)  # keep emoji as-is
        # Look ahead in the source text after this emoji to find the
        # player name. We do this in the outer loop instead, so just
        # mark the position with a sentinel.
        return f"__SAIYAN_TEAM_{code}__ "

    # First pass: replace each <:CODE:digits> with a sentinel that carries
    # the team code. This is unambiguous and won't collide with any text
    # Saiyan ever sends.
    sentinelled = _SAIYAN_EMOJI_RE.sub(_rewrite, text)

    # Second pass: walk each sentinel and rewrite "__SAIYAN_TEAM_X__ Player ..."
    # into "Player (X) ..." — capture 1-2 words as the player name.
    # Pattern per word: First letter uppercase, then EITHER 1+ lowercase
    # letters OR an apostrophe + letters (handles "O'Connor"). The optional
    # `[A-Z][a-z]+` group handles internal capitals from Mc/Mac/Du/De/Le/D'
    # style names: "McAuliffe" (regression 2026-05-10 Kane McAuliffe SGM
    # second leg got 'Auliffe' as player), "MacIntosh", "DeVries". Optional
    # trailing hyphen/apostrophe segments handle "Wanganeen-Milera" and
    # "Karl-Anthony". Filters out all-caps tokens like "AGS", "ML" that
    # would otherwise be greedy-matched as part of the player name.
    word = (
        r"[A-Z](?:[a-z]+|'[a-zA-Z]+)"
        r"(?:[A-Z][a-z]+)?"
        r"(?:[-'][a-zA-Z]+)*"
    )
    # Negative lookahead `(?!\s*\(<TEAM>\))`: if the captured player is
    # immediately followed by `(TEAM)` in the source, skip expansion to
    # avoid duplicating the team tag. Failure case 2026-05-10 Jordan Dawson
    # (ADE): without this guard, output was "Jordan Dawson (ADE) (ADE)".
    sentinel_re = _re_saiyan.compile(
        r"__SAIYAN_TEAM_([A-Z]{2,4})__\s+"
        rf"(?P<player>{word}(?:\s+{word})?)"
    )

    def _expand(m: _re_saiyan.Match) -> str:
        code = m.group(1)
        player = m.group("player")
        # If the source text already has `(CODE)` right after the player,
        # don't add another. Find the position immediately after the match
        # in the source and check.
        end = m.end()
        tail = sentinelled[end:end + 10]
        if _re_saiyan.match(rf"\s*\({code}\)", tail):
            return f"{player} "
        return f"{player} ({code}) "

    rewritten = sentinel_re.sub(_expand, sentinelled)

    # Third pass: handle all-caps player initialisms like "NWM" or "BONT".
    # The standard player-name regex above requires uppercase-then-lowercase,
    # so "NWM" doesn't match and the sentinel falls through to defensive
    # cleanup. That sends raw "NWM 26+" to Groq, which then misreads NWM
    # as a team code (and historically as North Melbourne via team alias),
    # producing a malformed team-line tip instead of a player threshold.
    # 2026-05-09 NWM/Andrew SGM regression. Look up against AFL_NICKNAMES
    # and expand to full name + (TEAM) so Groq sees an unambiguous player.
    try:
        from roster import AFL_NICKNAMES
        initialism_re = _re_saiyan.compile(
            r"__SAIYAN_TEAM_([A-Z]{2,4})__\s+([A-Z]{2,5})\b"
        )

        def _expand_initialism(m: _re_saiyan.Match) -> str:
            team = m.group(1)
            token = m.group(2)
            full_name = AFL_NICKNAMES.get(token.lower())
            if full_name:
                return f"{full_name} ({team}) "
            return m.group(0)  # leave for defensive cleanup

        rewritten = initialism_re.sub(_expand_initialism, rewritten)
    except Exception:
        pass

    # Defensive: if any sentinels remain (e.g. emoji wasn't followed by a
    # capitalised player name), strip them so they don't leak into Groq's
    # input as garbage tokens.
    rewritten = _re_saiyan.sub(r"__SAIYAN_TEAM_[A-Z]{2,4}__\s*", "", rewritten)
    return rewritten


# ── Image (vision) tip parsing ───────────────────────────────────────
# Three Telegram CHANNELS post tips as images (Eddie's Bets AFL, Zak
# Trussell SA Racing, The Trial Sniper). GROQ_MODEL (Llama-4 Scout) is
# multimodal, so the SAME endpoint + model reads the image when the user
# message carries an image_url content block (not the plain string the text
# path uses). parse_tip_image returns RAW extracted dicts (not ParsedTip):
# racing dicts go to the racing pipeline, AFL dicts are built into ParsedTip
# by main.py. The prompts force a JSON-only response with a fixed schema, and
# the same _parse_json_with_repair as the text path handles the response.

IMAGE_PROMPT_RACING = (
    "You are an OCR + extraction tool for horse/greyhound RACING betting-tip "
    "images. Read the image and extract EVERY individual bet EXACTLY as "
    "printed. Respond with ONLY valid JSON, no markdown fences, in this "
    'shape: {"tips": [ {"track": str|null, "date": str|null, "race": '
    'int|null, "saddle": int|null, "runner": str, "odds": number|null, '
    '"units": number|null, "market": "win"|"place", "rated": number|null} ]}. '
    "Rules: extract the saddle/runner NUMBER as `saddle` (an integer) and the "
    "horse name as `runner`. Convert prices to decimal numbers ($4.20 -> "
    "4.20; if multiple bookie prices are shown, use the BEST/highest). "
    "Convert stake to a number (1.2u -> 1.2). `market` is \"win\" unless the "
    "tip explicitly says place or each-way. `rated` is the tipster's rated/"
    "assessed price if shown, else null. Use null for ANY field not printed "
    "on the image — do NOT guess a track, race or price. "
    "DATE IS IMPORTANT: many tips are for a FUTURE day (e.g. Saturday's card "
    "posted mid-week). If the image shows a meeting date or day ANYWHERE (a "
    "header/caption like 'Saturday', 'Sat 7 June', 'SAT', '07/06', '7/6/26'), "
    "copy it EXACTLY-as-printed into `date` on EVERY tip row (same value on all "
    "rows). A weekday word ('Saturday') is fine — return it verbatim, do not "
    "convert it. Only use null for `date` if NO date/day appears on the image. "
    "RACE NUMBER: if several selections sit under ONE race (e.g. a 'Race 7' / "
    "'R7' heading followed by two horses), set `race` to that race number on "
    "EVERY selection in that group — NEVER leave the 2nd/3rd selection's `race` "
    "null just because the heading isn't repeated on its row. "
    "SKIP rows that are not bets (venue/date headers as their own rows, "
    "commentary, 'GOOD LUCK', totals). Preserve exact numbers. JSON only."
)

IMAGE_PROMPT_AFL = (
    "You are an OCR + extraction tool for AFL betting-tip images. Respond with "
    "ONLY valid JSON, no markdown fences, in this shape: "
    '{"tips": [ {"player": str|null, "team": str|null, "stat": str|null, '
    '"side": "over"|"under"|null, "line": number|null, "odds": number|null, '
    '"bookie": str|null, "units": number|null, "period": "full"|"1st_half"|'
    '"2nd_half"|"1st_quarter"|"2nd_quarter"|"3rd_quarter"|"4th_quarter"|null, '
    '"market_type": "player_prop"|'
    '"team_line"|"margin"|"total"|"other"} ]}. '
    "EXTRACT ONLY THE TIPSTER'S ACTUAL SELECTION(S) — the bet(s) they are "
    "backing. This is the HIGHLIGHTED / boxed / bold / centred / largest "
    "selection, or the one added to a betslip/bet-card (the one shown with the "
    "stake or 'BET' button). DO NOT extract the surrounding odds board, market "
    "grid, or other available options shown in the background — those are NOT "
    "the tip. Most images contain exactly ONE bet; only return multiple if the "
    "tipster clearly lists several distinct selections. NEVER output both the "
    "OVER and the UNDER of the same line (only one side can be the tip). "
    "Rules: for a player statistic bet set market_type=\"player_prop\", "
    "`player`=full printed name, and `stat` to ONE lowercase word from: "
    "disposals, goals, marks, tackles, kicks, handballs, clearances, hitouts, "
    "fantasy_points. `side` is \"over\" for 'over'/'N+'/'2+' lines and "
    "\"under\" for 'under'. `line` is the number (23.5, or for 'N+' use N-0.5 "
    "e.g. '24+' -> 23.5). GOALS shorthand: 'AGS' / 'anytime goal' / 'anytime "
    "goalscorer' / '1+ goal(s)' ALL mean a goals bet — set market_type="
    "\"player_prop\", stat=\"goals\", side=\"over\", line=0.5 (a player can have "
    "BOTH an AGS/1+ AND a separate '2+ goals' line in one image — extract BOTH "
    "as distinct tips, do NOT drop the AGS row). Convert prices to decimal "
    "numbers ($1.87 -> 1.87). "
    "Convert stake to a number (2.5u -> 2.5). For a team handicap/line bet use "
    "market_type=\"team_line\", put the team in `team`, and set `line` to the "
    "handicap WITH ITS SIGN — negative if the team is favourite / giving start "
    "(e.g. 'Geelong -8.5' -> -8.5), positive if receiving (e.g. '+8.5' -> 8.5). "
    "For a match/quarter total (combined points, e.g. 'Under 172.5') use "
    "market_type=\"total\", `side`=over/under, `line`=the number, and put one "
    "competing team in `team`. For a winning-margin bet use \"margin\"; "
    "anything else \"other\". PERIOD: capture any half/quarter qualifier on the "
    "bet — '1st/2nd Half', a 'Half Line'/'Half Total', or a quarter ('1st "
    "Quarter' etc.) -> set `period` to e.g. \"2nd_half\" / \"1st_quarter\"; a "
    "full-game/full-match bet uses \"full\" or null. The period is CRITICAL — "
    "NEVER drop a Half/Quarter qualifier ('Hawthorn -5.5 2nd Half Line' is a "
    "2nd-half line, NOT a full-game line). Use null for ANY field not printed. "
    "JSON only."
)


def _image_mime(image_bytes: bytes) -> str:
    """Sniff the image mime from magic bytes (default jpeg)."""
    if image_bytes[:8].startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def parse_tip_image(
    image_bytes: bytes,
    tipster: str,
    sport: str,
    max_retries: int = 4,
) -> tuple[list[dict], float]:
    """Vision-parse a tip IMAGE into RAW extracted tip dicts (NOT ParsedTip).

    Uses GROQ_MODEL (Llama-4 Scout, multimodal), one image per call. Picks a
    per-sport prompt (racing vs AFL). Returns (tips, elapsed_seconds); each
    tip dict follows the schema in the chosen prompt. main.py adapts racing
    dicts into the racing pipeline and AFL dicts into ParsedTip/place_tip.
    Returns ([], elapsed) on any failure (caller routes to a manual/image
    alert) — never raises.
    """
    start = time.time()
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set, skipping image parsing")
        return [], 0.0
    if not image_bytes:
        log.warning("parse_tip_image: empty image bytes")
        return [], 0.0

    prompt = IMAGE_PROMPT_RACING if (sport or "").lower() == "racing" else IMAGE_PROMPT_AFL
    b64 = base64.b64encode(image_bytes).decode()
    mime = _image_mime(image_bytes)
    body = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": 4000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    }

    content = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=90,  # vision inference is slower than text
            )
            if resp.status_code == 429 and attempt < max_retries:
                ra = resp.headers.get("retry-after")
                wait = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) \
                    else 8.0 * (attempt + 1)
                log.warning(
                    f"parse_tip_image: 429 for {tipster}, backing off "
                    f"{wait:.0f}s (attempt {attempt + 1})"
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            break
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                time.sleep(5.0 * (attempt + 1))
                continue
            log.error(f"parse_tip_image: Groq request failed for {tipster}: {e}")
            return [], time.time() - start
        except Exception as e:
            log.error(f"parse_tip_image: unexpected error for {tipster}: {e}")
            return [], time.time() - start

    elapsed = time.time() - start
    if not content:
        log.error(f"parse_tip_image: no content returned for {tipster}")
        return [], elapsed

    content = content.replace("```json", "").replace("```", "").strip()
    parsed = _parse_json_with_repair(content)
    if parsed is None:
        log.error(f"parse_tip_image: invalid JSON (repair failed) for {tipster}")
        log.error(f"Raw response: {content[:500]}")
        return [], elapsed

    tips = parsed.get("tips", [])
    if not isinstance(tips, list):
        log.error(f"parse_tip_image: 'tips' not a list for {tipster}")
        return [], elapsed
    log.info(
        f"parse_tip_image: {tipster} ({sport}) extracted {len(tips)} raw tip(s) "
        f"in {elapsed:.2f}s"
    )
    return tips, elapsed


TEXT_PROMPT_RACING = (
    "You are an extraction tool for horse/greyhound RACING betting-tip TEXT "
    "MESSAGES from a tipster channel. Read the message and extract EVERY "
    "individual bet/selection the tipster is BACKING. Respond with ONLY valid "
    "JSON, no markdown fences, in this shape: "
    '{"tips": [ {"track": str|null, "date": str|null, "race": int|null, '
    '"saddle": int|null, "runner": str, "odds": number|null, "units": '
    'number|null, "market": "win"|"place", "rated": number|null} ]}. '
    "CRITICAL: if the message is NOT an actual bet — chit-chat, 'good luck', "
    "results/wrap-ups, commentary, a question, thanks, or emoji — return "
    '{"tips": []}. NEVER invent a bet. '
    "Rules: `runner` is the horse/greyhound NAME being backed (e.g. 'Adding "
    "Lingani for tomorrow' -> runner=\"Lingani\"; 'lock of the day is Sea Of "
    "Class' -> runner=\"Sea Of Class\"). `saddle` is the saddlecloth NUMBER if "
    "stated (integer), else null. Convert prices to decimal numbers ($4.20 -> "
    "4.20; if several bookies, use the BEST/highest). Convert stake to a number "
    "(1.2u -> 1.2), else null. `market` is \"win\" unless it explicitly says "
    "place or each-way. `rated` is the tipster's rated/assessed price if shown, "
    "else null. Use null for ANY field NOT stated — do NOT guess a track, race "
    "or price. "
    "DATE IS IMPORTANT: copy any day/date the tipster gives EXACTLY-AS-WRITTEN "
    "into `date` on EVERY tip row ('tomorrow', 'today', 'tonight', 'Saturday', "
    "'Sat', '6/6', '6/6/2026', 'June 6'). Return weekday words and 'tomorrow'/"
    "'today'/'tonight' VERBATIM — do NOT convert them to a number. Only use null "
    "for `date` if NO day/date is mentioned anywhere in the message. "
    "RACE GROUPING: if several runners sit under one race ('R7'/'Race 7'), set "
    "`race` to that number on EVERY selection in that group. "
    "Preserve exact numbers. JSON only."
)


def parse_racing_text(
    text: str,
    tipster: str,
    max_retries: int = 4,
) -> tuple[list[dict], float]:
    """Parse a free-TEXT racing tip MESSAGE into RAW racing tip dicts — the SAME
    schema parse_tip_image emits for racing, so main.py can feed the result into
    the identical racing pipeline. For Zak/Trial text posts that are real tips
    ('Adding Lingani for tomorrow', 'R4 Sandown #7 win'). Uses GROQ_MODEL
    (Llama-4 Scout) as a text call. Returns (tips, elapsed). A VALID-but-empty
    parse (the message is chatter / no runner) returns ([], elapsed) so the caller
    DROPS it (no manual ping). A HARD failure (no Groq key, request error after
    retries, bad JSON) RAISES (v5.22) so the caller routes the tip to MANUAL —
    NOT silently dropping a genuine tip on a Groq outage. Rows with no runner are
    stripped from a successful parse.
    """
    start = time.time()
    if not GROQ_API_KEY:
        raise RuntimeError("parse_racing_text: GROQ_API_KEY not set")
    if not (text or "").strip():
        return [], 0.0
    body = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": 2000,
        "messages": [
            {"role": "system", "content": TEXT_PROMPT_RACING},
            {"role": "user", "content": text.strip()},
        ],
    }
    content = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if resp.status_code == 429 and attempt < max_retries:
                ra = resp.headers.get("retry-after")
                wait = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) \
                    else 6.0 * (attempt + 1)
                log.warning(
                    f"parse_racing_text: 429 for {tipster}, backing off "
                    f"{wait:.0f}s (attempt {attempt + 1})"
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            break
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                time.sleep(4.0 * (attempt + 1))
                continue
            log.error(f"parse_racing_text: Groq request failed for {tipster}: {e}")
            raise  # v5.22: hard failure -> caller routes to MANUAL (don't lose a real tip)
        except Exception as e:
            log.error(f"parse_racing_text: unexpected error for {tipster}: {e}")
            raise

    elapsed = time.time() - start
    if not content:
        raise RuntimeError("parse_racing_text: no content returned")
    content = content.replace("```json", "").replace("```", "").strip()
    parsed = _parse_json_with_repair(content)
    if parsed is None:
        log.error(f"parse_racing_text: invalid JSON (repair failed) for {tipster}; raw: {content[:300]}")
        raise RuntimeError("parse_racing_text: invalid JSON")
    tips = parsed.get("tips", [])
    if not isinstance(tips, list):
        raise RuntimeError("parse_racing_text: 'tips' not a list")
    # Strip rows with no runner (chatter / placeholder rows the model can emit).
    tips = [t for t in tips if isinstance(t, dict) and (t.get("runner") or "").strip()]
    log.info(
        f"parse_racing_text: {tipster} extracted {len(tips)} raw tip(s) "
        f"in {elapsed:.2f}s"
    )
    return tips, elapsed


def parse_with_groq(
    text: str,
    tipster: str,
    sport: str = "nba",
    unit_size: float = 1.0,
    default_units: float = 1.0,
) -> tuple[list[ParsedTip], float]:
    """
    Parse a tip message using Groq LLM.

    Returns:
        (list of ParsedTip, elapsed_seconds)
    """
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set, skipping LLM parsing")
        return [], 0

    # 2026-05-03 Saiyan SGM regression: Brisbane Lions v Carlton wrong-event.
    # Saiyan SGM tip "<:GWS:...> Oliver 26+/<:GC:...> Petracca 24+" got Groq
    # to return team='' for Oliver because Groq doesn't reliably read the
    # Discord emoji prefix. The AFL team-from-roster fallback then inferred
    # 'Carlton' from "Oliver Hollands" (score 0.95), which set the SGM's
    # primary_team to Carlton. Resolver picked the next Carlton game,
    # Brisbane Lions v Carlton — completely wrong fixture. Bet failed.
    #
    # Fix: rewrite Saiyan emoji prefixes into the (TEAM) format that
    # Groq's prompt already understands ("Player (TEAM) line stat"). This
    # makes team extraction reliable without depending on Groq parsing
    # custom Discord emoji syntax. Keep `text` as the ORIGINAL (with
    # emojis) so it ends up in raw_message for clean Telegram alerts.
    groq_input = text
    if tipster == "saiyan_afl":
        groq_input = _preprocess_saiyan_emojis(text)

    start = time.time()

    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Tipster: {tipster}\nSport: {sport if sport != 'auto' else 'DETECT FROM MESSAGE'}\nMessage:\n{groq_input}"},
                ],
                "temperature": 0,
                # Bumped from 2000 to 4000. Shook's 4-15 context buffer can
                # produce responses long enough to hit the limit and get
                # truncated mid-JSON. Truncation caused the 2026-04-23 KAT
                # tip to silently drop (no notification, no placement).
                "max_tokens": 4000,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        elapsed = time.time() - start

        # Extract text content
        content = data["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        content = content.replace("```json", "").replace("```", "").strip()

        parsed = _parse_json_with_repair(content)
        if parsed is None:
            log.error(f"Groq returned invalid JSON (repair failed)")
            log.error(f"Raw response: {content[:500]}")
            return [], elapsed
        tips_data = parsed.get("tips", [])

        # Deterministic sport override: if the tipster led the message with an
        # explicit sport keyword ("afl ..."), trust it over Groq's per-tip
        # `sport` field (which flip-flops, 2026-05-31). Computed once from the
        # original message; applies to every tip parsed from it.
        forced_sport = _explicit_leading_sport(text)

        tips = []
        for td in tips_data:
            # Build legs
            legs = []
            is_sgm = td.get("is_sgm", False)
            is_pyo_sgm = td.get("is_pyo_sgm", False)

            if is_sgm and td.get("raw_legs"):
                # Track previous player for same-player shorthand in SGMs
                prev_player = ""
                for rl in td["raw_legs"]:
                    player = rl.get("player", "")
                    # If no player specified, use previous leg's player (Sam Darcy 10+ Disp/2+ Goals)
                    if not player and prev_player:
                        player = prev_player
                        log.info(f"SGM same-player inference: using '{prev_player}' for leg")
                    if player:
                        prev_player = player

                    leg = ParsedLeg(
                        market=rl.get("market", "player_prop"),
                        player=_clean_name(player),
                        stat=_normalise_stat(rl.get("stat", "")),
                        line=_safe_float(rl.get("line"), 0.0),
                        selection=_clean_name(rl.get("selection", "")),
                        team_full=_clean_name(rl.get("team", "")),
                        raw_text="",
                    )
                    # Tag per-leg threshold flag
                    leg._is_threshold = rl.get("is_threshold", False)

                    # Safety net: catch N+ shorthand that slipped through as
                    # integer O/U lines on non-PYO SGM legs
                    leg = _fix_sgm_threshold_leg(leg, is_pyo_sgm, sport=forced_sport or td.get("sport", sport))

                    legs.append(leg)
            else:
                market = td.get("market", "player_prop")
                leg = ParsedLeg(
                    market=market,
                    player=_clean_name(td.get("player", "")),
                    stat=_normalise_stat(td.get("stat", "")),
                    line=_safe_float(td.get("line"), 0.0),
                    selection=_clean_name(td.get("selection", "")),
                    team_full=_clean_name(td.get("team", "")),
                    raw_text="",
                )
                legs.append(leg)

            # Handle threshold bets - auto-place with threshold market
            is_threshold = td.get("is_threshold", False)
            alert_only = td.get("alert_only", False)
            alert_reason = td.get("alert_reason", "")

            # Shook unit transform: his "0.3u" is conservative sizing. We scale
            # by 3 then round to the nearest 0.25 unit to match our risk level.
            # 0.3u -> 0.9 -> 1.0u, 0.4u -> 1.2 -> 1.25u, 0.5u -> 1.5 -> 1.5u
            raw_units = _safe_float(td.get("units"), default_units)
            # Did the tipster give an EXPLICIT unit, or did we default it? Some
            # tipsters (aus/kev) must carry a unit to count as a bet — gated in
            # main._process_tip via UNITS_REQUIRED_TIPSTERS. 2026-06-04.
            _parsed_units = _safe_float(td.get("units"), None)
            units_explicit = _parsed_units is not None and _parsed_units > 0
            if tipster == "shook":
                scaled = raw_units * 3
                final_units = round(scaled * 4) / 4  # nearest 0.25
                log.info(
                    f"Shook unit transform: {raw_units}u x3 = {scaled:.2f} "
                    f"-> {final_units}u"
                )
            else:
                final_units = raw_units

            # For Shook, the `text` we got includes the buffer context
            # (prefixed "RECENT CONTEXT:\n...\nCURRENT MESSAGE:\n..."). The
            # buffer is needed for Groq to parse correctly (game context,
            # units) but it pollutes notifications — it made Hardaway's
            # manual alert show Tobias Harris's tip text because Tobias was
            # in the buffer. For raw_message (used in notifications), strip
            # to just the trigger message.
            display_raw = text
            if "CURRENT MESSAGE:" in text:
                display_raw = text.split("CURRENT MESSAGE:", 1)[-1].strip()

            tip = ParsedTip(
                tipster=tipster,
                sport=forced_sport or td.get("sport", sport),
                is_sgm=is_sgm,
                legs=legs,
                units=final_units,
                unit_size=unit_size,
                raw_message=display_raw,
                is_live=td.get("is_live", False),
                alert_only=alert_only,
                alert_reason=alert_reason,
                suggested_bookie=td.get("bookie", ""),
                suggested_odds=_safe_float(td.get("odds"), 0.0),
                is_pyo_sgm=is_pyo_sgm,
                alt_line=_normalise_alt_dict(td.get("alt_line")),
                units_explicit=units_explicit,
            )

            # For AFL, if Groq didn't extract a team but we have a player,
            # infer team from roster so event resolution can succeed.
            if tip.sport == "afl":
                try:
                    from roster import get_player_team
                    for leg in tip.legs:
                        if not leg.team_full and leg.player:
                            inferred = get_player_team(leg.player, "afl")
                            if inferred:
                                leg.team_full = inferred
                                log.info(
                                    f"Inferred AFL team from roster: "
                                    f"'{leg.player}' -> '{inferred}'"
                                )
                except Exception as e:
                    log.warning(f"Roster team inference failed: {e}")

            # Tag threshold tips so _execute_bet uses threshold market
            if is_threshold:
                tip._is_threshold = True
            tips.append(tip)

        # Deterministic "/" = SGM: a single "/"-line that Groq wrongly split
        # into separate tips is merged back into one SGM ticket.
        tips = _merge_slash_line_into_sgm(tips, text)

        log.info(f"Groq parsed {len(tips)} tip(s) in {elapsed:.2f}s")
        return tips, elapsed

    except json.JSONDecodeError as e:
        elapsed = time.time() - start
        log.error(f"Groq returned invalid JSON: {e}")
        log.error(f"Raw response: {content[:500]}")
        return [], elapsed

    except requests.exceptions.RequestException as e:
        elapsed = time.time() - start
        log.error(f"Groq API request failed: {e}")
        return [], elapsed

    except Exception as e:
        # 2026-05-03 Ayton regression: Shook tip 'Deandre Ayton L 18.5
        # Points+Rebounds' caused float(None) crash via td.get("line", 0)
        # returning None when the key was present with a null value.
        # _safe_float() prevents that class of bug now, but we keep this
        # catch-all and add full diagnostics so the next regression isn't
        # opaque. Original log just said "Groq parsing error: {error}"
        # with no clue what tip / response shape triggered it.
        elapsed = time.time() - start
        import traceback
        log.error(
            f"Groq parsing error on {tipster}/{sport}: {type(e).__name__}: {e}"
        )
        log.error(f"Input text (first 300 chars): {text[:300]!r}")
        try:
            log.error(f"Groq response (first 500 chars): {content[:500]!r}")
        except NameError:
            log.error("Groq response not captured (error pre-response)")
        log.error(f"Traceback:\n{traceback.format_exc()}")
        return [], elapsed
