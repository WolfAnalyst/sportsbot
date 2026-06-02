"""
Parser for Saiyan AFL tipster messages.

Handles:
  - Player (TEAM) Over/Under X.5 Stat @ odds
  - Player (TEAM) X+ Stat @ odds
  - [EMOJI_TEAM] Player X+ Stat (no parens team)
  - u/o shorthand for under/over
  - TEAM Win (H2H legs in SGMs)
  - **bold markdown** wrapping
  - SGMs via / separator
  - Multiple tips per message (separate lines)
"""

import re
from typing import Optional
from models import ParsedTip, ParsedLeg
from config import AFL_TEAMS, AFL_STAT_MAP


# ── Cleaning ────────────────────────────────────────────────────────

DISCORD_EMOJI_RE = re.compile(r"<:([A-Za-z0-9_]+):\d+>")

CHANNEL_HEADER_RE = re.compile(
    r"#\uFE0F?\u20E3?aflmainplays[\U0001F3C9\U0001F3C8]?\s*",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Clean message while preserving team info from Discord emojis."""
    text = DISCORD_EMOJI_RE.sub(r"[\1]", text)
    text = text.replace("**", "")
    text = CHANNEL_HEADER_RE.sub("", text)
    text = text.replace("@everyone", "")
    text = text.replace("\u200b", "")
    text = re.sub(r"  +", " ", text)
    return text.strip()


# ── Leg Patterns ────────────────────────────────────────────────────

LEG_PARENS_RE = re.compile(
    r"(?:\[[A-Za-z0-9_]+\]\s*)?"
    r"(?P<player>[A-Z][a-zA-Z\s\-'\.]+?)"
    r"\s*\((?P<team>[A-Z]{2,4})\)\s*"
    r"(?:"
        r"(?P<dir>Over|Under|[oOuU])[\s]*(?P<line>\d+\.?\d*)"
        r"|"
        r"(?P<thresh>\d+)\+"
    r")\s*"
    r"(?P<stat>[A-Za-z][A-Za-z\s]*?)"
    r"\s*$",
    re.IGNORECASE,
)

LEG_EMOJI_RE = re.compile(
    r"\[(?P<team>[A-Za-z]{2,4})\]\s*"
    r"(?P<player>[A-Z][a-zA-Z\s\-'\.]+?)\s+"
    r"(?:"
        r"(?P<dir>Over|Under|[oOuU])[\s]*(?P<line>\d+\.?\d*)"
        r"|"
        r"(?P<thresh>\d+)\+"
    r")\s*"
    r"(?P<stat>[A-Za-z][A-Za-z\s]*?)"
    r"\s*$",
    re.IGNORECASE,
)

H2H_RE = re.compile(
    r"(?:\[(?P<eteam>[A-Za-z]{2,4})\]\s*)?"
    r"(?P<team>[A-Z]{2,4})\s+"
    r"(?:Win|ML|Moneyline)"
    r"\s*$",
    re.IGNORECASE,
)


def _normalise_direction(raw: str) -> str:
    r = raw.strip().lower()
    if r in ("o", "over"):
        return "over"
    if r in ("u", "under"):
        return "under"
    return r


def _parse_leg(raw: str) -> Optional[ParsedLeg]:
    raw = raw.strip()
    if not raw:
        return None

    m = H2H_RE.match(raw)
    if m:
        team_abbr = m.group("team").upper()
        team_full = AFL_TEAMS.get(team_abbr, team_abbr)
        return ParsedLeg(
            market="h2h",
            team_abbr=team_abbr,
            team_full=team_full,
            selection=team_full,
            raw_text=raw,
        )

    # Team line/handicap: "Geelong +1.5"
    m = TEAM_LINE_RE.match(raw)
    if m:
        team = m.group("team").strip()
        # Check if team name is a known AFL team
        team_match = None
        for abbr, full in AFL_TEAMS.items():
            if team.lower() in full.lower() or full.lower() in team.lower():
                team_match = full
                break
        if team_match:
            line = float(m.group("line"))
            return ParsedLeg(
                market="line",
                team_full=team_match,
                selection=team_match,
                line=line,
                raw_text=raw,
            )

    m = LEG_PARENS_RE.match(raw)
    if m:
        return _build_prop_leg(m, raw)

    m = LEG_EMOJI_RE.match(raw)
    if m:
        return _build_prop_leg(m, raw)

    return None


def _build_prop_leg(m: re.Match, raw: str) -> ParsedLeg:
    player = m.group("player").strip()
    team_abbr = m.group("team").upper()
    team_full = AFL_TEAMS.get(team_abbr, team_abbr)
    stat_raw = m.group("stat").strip().lower()
    stat = AFL_STAT_MAP.get(stat_raw, stat_raw)

    if m.group("dir"):
        selection = _normalise_direction(m.group("dir"))
        line = float(m.group("line"))
        is_threshold = False
    else:
        threshold = int(m.group("thresh"))
        line = float(threshold)
        selection = player
        is_threshold = True

    leg = ParsedLeg(
        market="player_prop",
        player=player,
        team_abbr=team_abbr,
        team_full=team_full,
        stat=stat,
        line=line,
        selection=selection,
        raw_text=raw,
    )
    leg._is_threshold = is_threshold
    return leg


# ── Leg Splitting ───────────────────────────────────────────────────

# Pattern for team line leg: "Geelong +1.5" or "Carlton -5.5"
TEAM_LINE_RE = re.compile(
    r"(?:\[[A-Za-z]{2,4}\]\s*)?"
    r"(?P<team>[A-Z][a-zA-Z\s\.]+?)\s+"
    r"(?P<line>[+-]\d+\.?\d*)"
    r"\s*$",
    re.IGNORECASE,
)


def _split_legs(legs_text: str) -> list[str]:
    parts = legs_text.split("/")
    legs = []
    buffer = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue

        is_new_leg = (
            re.search(r"\[[A-Z]{2,4}\]", part, re.IGNORECASE)
            or re.search(r"\([A-Z]{2,4}\)", part)
            or re.match(r"[A-Z]{2,4}\s+(?:Win|ML)", part, re.IGNORECASE)
            # Team line/handicap: "Geelong +1.5", "Carlton -5.5"
            or re.match(r"[A-Z][a-zA-Z\s\.]+\s+[+-]\d+\.?\d*$", part)
        )

        if is_new_leg:
            if buffer:
                legs.append(buffer)
            buffer = part
        else:
            if buffer:
                buffer = buffer + "/" + part
            else:
                buffer = part

    if buffer:
        legs.append(buffer)

    return legs


# ── Tip Line Parsing ────────────────────────────────────────────────

def _parse_tip_line(
    line: str, default_units: float, unit_size: float
) -> Optional[ParsedTip]:
    if "@" not in line:
        return None

    at_idx = line.index("@")
    legs_text = line[:at_idx].strip()

    if not legs_text:
        return None

    raw_legs = _split_legs(legs_text)
    parsed_legs = [_parse_leg(l) for l in raw_legs]
    parsed_legs = [l for l in parsed_legs if l is not None]

    if not parsed_legs:
        return None

    is_sgm = len(parsed_legs) > 1

    return ParsedTip(
        tipster="saiyan_afl",
        sport="afl",
        is_sgm=is_sgm,
        legs=parsed_legs,
        units=default_units,
        unit_size=unit_size,
        raw_message=line,
        # SGMs -> alert only (HyperBot SGM line bug)
        alert_only=is_sgm,
        alert_reason="SGM - place manually" if is_sgm else "",
    )


# ── Public API ──────────────────────────────────────────────────────

def parse_saiyan_message(
    text: str,
    default_units: float = 1.5,
    unit_size: float = 50.0,
) -> list[ParsedTip]:
    """
    Parse a Saiyan AFL message into a list of ParsedTip objects.
    Returns an empty list if no recognisable tips found.
    """
    cleaned = _clean(text)
    tips = []

    for line in cleaned.strip().split("\n"):
        line = line.strip()
        if not line or "@" not in line:
            continue

        # Filter commentary lines (prefixed with *)
        if line.startswith("*"):
            continue

        has_parens_team = re.search(r"\([A-Z]{2,4}\)", line)
        has_emoji_team = re.search(r"\[[A-Z]{2,4}\]", line, re.IGNORECASE)
        if not has_parens_team and not has_emoji_team:
            continue

        tip = _parse_tip_line(line, default_units, unit_size)
        if tip:
            tips.append(tip)

    return tips
