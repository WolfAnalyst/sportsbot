"""NBA player-props model: the machine feed parser (tipbot contract v3).

The NBA props model posts one bet per Telegram message. Every actionable message
carries a single machine line:

    BET|v3|evt=7c1f9a2b|pid=1627824|player=Guerschon Yabusele|team=Los Angeles Clippers|
    opp=Houston Rockets|market=player_rebounds|side=under|line=5.5|price=1.85|
    stake=500|target=500|proj=4.31|gap=1.19|kind=base|start=2026-10-24T01:30:00Z|
    key=7c1f9a2b~1627824~5.5~T1-REB-U2~base~0

18 pipe-separated fields: "BET", the schema version, then 16 `k=v` pairs in a FIXED order.
The envelope is the AFL v2 contract (DISPOSALS_MODEL_CONTRACT_v2.md, parsers/disposals_model.py)
unchanged. What differs in v3 (docs/PIPELINE-DESIGN.md section 3.4 of the model repo):

  * market   `player_rebounds` only (config-driven allow-list, so a prop can be added later)
  * team/opp the 30 NBA teams, exactly one full-name spelling each (NBA_TEAMS below)
  * pid      the NBA player_id: a plain positive integer
  * key      `<evt>~<pid>~<line>~<tag>~<kind>~<attempt>`: the kind and an attempt number
             (0 for a base or add-on, 1, 2, ... for retries) make every key unique. The key
             is cross-checked against the evt/pid/line/kind fields, so a key that disagrees
             with its own message is a refusal.

Same rules as the AFL parser, deliberately: PURE `re`/`str`, NO LLM, NO network. The line is
the trigger, so a malformed line is REFUSED, never guessed at.

    parse(text) -> None                     # no BET| line anywhere: chatter, drop
    parse(text) -> ParseResult(rows, refusals)

`rows` are fully validated dicts. `refusals` are (raw_line, reason) pairs the CALLER MUST
ALERT ON. A message containing a BET| line never returns None.

The parser does NO roster work and NO event lookup. It validates shape and vocabulary only;
main.py and nba_model_core.py own everything that touches money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Only v3 is understood here. A v2 (AFL) line is refused on sight: it has no business on the
# NBA path, and its `team` vocabulary is the 18 AFL clubs.
SUPPORTED_VERSIONS = ("v3",)

# Field order is FIXED and exact. Position-checked as well as key-checked.
EXPECTED_KEYS = (
    "evt", "pid", "player", "team", "opp", "market", "side", "line", "price",
    "stake", "target", "proj", "gap", "kind", "start", "key",
)
N_FIELDS = 2 + len(EXPECTED_KEYS)  # "BET" + version + 16 pairs = 18

# ---------------------------------------------------------------------------------------
# THE TEAM VOCABULARY. The single place the 30 names are written down.
#
# Exactly one full-name spelling per club. "Los Angeles Clippers", never the "LA Clippers"
# alias that ESPN and tipbot's roster/schedule use (see ROSTER_TEAM_NAMES in nba_model_core.py,
# which maps this vocabulary onto those). The model side builds the same list from
# `game_links.TEAM_TRICODES` minus the "LA Clippers" alias (`emit.TEAM_FULL_NAMES`).
# ---------------------------------------------------------------------------------------
NBA_TEAMS = (
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
    "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
    "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
    "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat",
    "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans",
    "New York Knicks", "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers",
    "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
    "Toronto Raptors", "Utah Jazz", "Washington Wizards",
)
assert len(NBA_TEAMS) == 30 and len(set(NBA_TEAMS)) == 30

# Everything this parser accepts by default. Anything else is a refusal, never a default.
# The market list and the tag list are the config-driven parts (config.NBA_MODEL_MARKETS /
# NBA_MODEL_TAGS are passed to parse()); a market added there also needs a stat in
# MARKET_TO_STAT and a liability ladder in nba_model_core.LADDER_CONFIG_NAMES, or it is refused
# at placement.
ALLOWED_MARKETS = ("player_rebounds",)
MARKET_TO_STAT = {"player_rebounds": "rebounds"}
ALLOWED_TAGS = ("T1-REB-U2",)
ALLOWED_SIDES = ("under",)          # Tier 1 is under-only
ALLOWED_KINDS = ("base", "addon", "retry")

# Blank is legal only for `opp` (as in v2). Every other value must be non-empty.
OPTIONAL_VALUE_KEYS = ("opp",)

_NUMERIC_KEYS = ("line", "price", "stake", "target", "proj", "gap")

# NBA player_id: a plain positive integer (1627824). No sign, no leading zero, no id prefix.
_PID_RE = re.compile(r"^[1-9][0-9]{0,9}$")

# `|` is the delimiter so no value may contain one; the key uses `~` internally.
_KEY_RE = re.compile(r"^[A-Za-z0-9_.~:+-]{3,160}$")

BET_PREFIX = "BET|"

# Telegram HTML wrapper: `<code>BET|v3|...</code>`. See parsers/disposals_model.py for why
# this is stripped defensively (a lost parse_mode would glue `</code>` onto the key).
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,15}\s*/?>")


@dataclass
class ParseResult:
    """Validated rows plus the refusals the caller must alert on."""
    rows: list = field(default_factory=list)
    refusals: list = field(default_factory=list)  # [(raw_line, reason), ...]
    # One entry per refusal, parallel to `refusals`: the salvaged identity row (see
    # salvage_row) taken from the FULL line BEFORE it is truncated for the alert, or None.
    refusal_rows: list = field(default_factory=list)

    def __bool__(self) -> bool:  # truthy if the message carried anything at all
        return bool(self.rows or self.refusals)


def _coerce_float(raw: str):
    """Strict float coercion; None on anything that is not a plain decimal number.

    Rejects '$500', '1,000', 'nan', 'inf', '1e999': a stake or a line is money, and a
    silently-coerced garbage value is worse than a refusal."""
    s = (raw or "").strip()
    if not s or not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _check_key(key: str, pairs: dict, line: float, kind: str):
    """Cross-check the v3 key against the message's own fields.

    `<evt>~<pid>~<line>~<tag>~<kind>~<attempt>`. Returns (tag, attempt, None) or
    (None, None, reason). A key that disagrees with its own message is how a replayed or
    hand-edited line walks past the permanent dedup under a fresh-looking identity."""
    parts = key.split("~")
    if len(parts) != 6:
        return None, None, (f"key {key!r} must be <evt>~<pid>~<line>~<tag>~<kind>~<attempt> "
                            f"(6 '~'-separated parts), got {len(parts)}")
    k_evt, k_pid, k_line, k_tag, k_kind, k_attempt = parts
    if k_evt != pairs["evt"].strip():
        return None, None, f"key evt {k_evt!r} does not match evt={pairs['evt'].strip()!r}"
    if k_pid != pairs["pid"].strip():
        return None, None, f"key pid {k_pid!r} does not match pid={pairs['pid'].strip()!r}"
    k_line_val = _coerce_float(k_line)
    if k_line_val is None or abs(k_line_val - line) > 1e-9:
        return None, None, f"key line {k_line!r} does not match line={line}"
    if k_kind != kind:
        return None, None, f"key kind {k_kind!r} does not match kind={kind!r}"
    if not re.fullmatch(r"[0-9]{1,3}", k_attempt):
        return None, None, f"key attempt {k_attempt!r} is not a small non-negative integer"
    attempt = int(k_attempt)
    # A retry is attempt >= 1; a base or add-on is the first (and only) send of its tranche.
    if kind == "retry" and attempt < 1:
        return None, None, "a retry key must carry attempt >= 1"
    if kind != "retry" and attempt != 0:
        return None, None, f"a {kind} key must carry attempt 0, got {attempt}"
    return k_tag, attempt, None


def _validate(pairs: dict, raw_line: str, markets, tags):
    """Validate one already-split BET| line. Returns (row, None) or (None, reason)."""
    # --- vocabulary -------------------------------------------------------------
    market = pairs["market"].strip().lower()
    if market not in markets:
        return None, f"market {market!r} not in {tuple(markets)}"

    side = pairs["side"].strip().lower()
    if side not in ALLOWED_SIDES:
        return None, (f"side {side!r} is not supported (only {ALLOWED_SIDES}); "
                      f"refusing rather than placing an unvalidated direction")

    kind = pairs["kind"].strip().lower()
    if kind not in ALLOWED_KINDS:
        return None, f"kind {kind!r} not in {ALLOWED_KINDS}"

    team = pairs["team"].strip()
    if team not in NBA_TEAMS:
        return None, (f"team {team!r} is not one of the 30 NBA team names "
                      f"(exactly one full-name spelling each, e.g. 'Los Angeles Clippers')")
    opp = pairs["opp"].strip()
    if opp and opp not in NBA_TEAMS:
        return None, f"opp {opp!r} is not one of the 30 NBA team names"
    if opp and opp == team:
        return None, f"team and opp are the same club ({team!r})"

    # --- numerics ---------------------------------------------------------------
    nums = {}
    for k in _NUMERIC_KEYS:
        v = _coerce_float(pairs[k])
        if v is None:
            return None, f"{k}={pairs[k]!r} is not a plain number"
        nums[k] = v

    if nums["line"] <= 0:
        return None, f"line must be > 0, got {nums['line']}"
    if nums["price"] <= 1.0:
        return None, f"price must be > 1.0, got {nums['price']}"
    if nums["stake"] <= 0:
        return None, f"stake must be > 0, got {nums['stake']}"
    if nums["target"] <= 0:
        return None, f"target must be > 0, got {nums['target']}"
    # stake is "place this now", target is the tranche total. stake > target means the
    # emitter has lost track of the tranche; refuse rather than over-stake it.
    if nums["stake"] > nums["target"] + 0.005:
        return None, (f"stake {nums['stake']} exceeds tranche target "
                      f"{nums['target']}, refusing (over-stake guard)")

    # --- identity ---------------------------------------------------------------
    evt = pairs["evt"].strip()
    if not evt or "~" in evt:
        return None, f"evt {evt!r} must be non-empty and contain no '~'"
    pid = pairs["pid"].strip()
    if not _PID_RE.fullmatch(pid):
        return None, (f"pid {pid!r} is not an NBA player_id (a plain positive integer); "
                      f"a bad pid silently disables the per-selection guards")
    player = pairs["player"].strip()
    if not player:
        return None, "player is empty"

    key = pairs["key"].strip()
    if not _KEY_RE.fullmatch(key):
        return None, f"key {key!r} is not a plausible idempotency key"
    tag, attempt, why = _check_key(key, pairs, nums["line"], kind)
    if why:
        return None, why
    if tag not in tags:
        return None, f"key tag {tag!r} not in {tuple(tags)}"

    start = pairs["start"].strip()
    if not start:
        return None, "start is empty"

    return {
        "evt": evt,
        "pid": pid,
        "player": player,
        "team": team,
        "opp": opp,
        "market": market,
        "side": side,
        "line": nums["line"],
        "price": nums["price"],
        "stake": nums["stake"],
        "target": nums["target"],
        "proj": nums["proj"],
        "gap": nums["gap"],
        "kind": kind,
        "start": start,
        "key": key,
        "tag": tag,
        "attempt": attempt,
        "raw": raw_line,
    }, None


def _parse_line(raw_line: str, markets, tags):
    """Split + shape-check one candidate BET| line. (row, None) or (None, reason)."""
    line = raw_line.strip()
    parts = line.split("|")
    if len(parts) != N_FIELDS:
        return None, (f"expected {N_FIELDS} pipe-separated fields, got {len(parts)} "
                      f"(schema is BET|<version>|{'|'.join(k + '=' for k in EXPECTED_KEYS)})")
    if parts[0].strip().upper() != "BET":
        return None, f"first field is {parts[0]!r}, expected 'BET'"

    version = parts[1].strip().lower()
    if version not in SUPPORTED_VERSIONS:
        return None, (f"unsupported schema version {version!r} "
                      f"(the NBA path understands {SUPPORTED_VERSIONS}); refusing")

    pairs = {}
    for idx, (expected_key, chunk) in enumerate(zip(EXPECTED_KEYS, parts[2:])):
        if "=" not in chunk:
            return None, f"field {idx + 3} ({chunk!r}) is not k=v"
        k, _, v = chunk.partition("=")
        k = k.strip()
        if k != expected_key:
            return None, (f"field {idx + 3} is {k!r}, expected {expected_key!r} "
                          f"(field order is fixed)")
        if not v.strip() and k not in OPTIONAL_VALUE_KEYS:
            return None, f"{k} has an empty value"
        pairs[k] = v
    return _validate(pairs, line, markets, tags)


def salvage_row(raw_line: str, market=None):
    """Best effort: an identity row for a REFUSED line that still carries a well-formed v3 key.

    The selection identity comes from the KEY (evt, pid, line, kind, attempt), never from the
    refused line's own fields, which may be exactly what was wrong with it; the side is always
    `under` and the market is the configured one (`market`, default the first allowed). Other
    fields (player, team, opp, target) are taken from the line when present. Returns None when
    there is no well-formed key. Used only so a refused line can still be ANSWERED in the fills
    file: the model otherwise counts an unanswered key as a full-target loss."""
    candidate = _HTML_TAG_RE.sub("", str(raw_line or "")).strip()
    pairs = {}
    for chunk in candidate.split("|"):
        k, sep, v = chunk.partition("=")
        if sep and k.strip() in EXPECTED_KEYS:
            pairs[k.strip()] = v.strip()
    key = pairs.get("key", "")
    parts = key.split("~")
    if not _KEY_RE.fullmatch(key) or len(parts) != 6 or parts[4] not in ALLOWED_KINDS:
        return None
    k_line = _coerce_float(parts[2])
    if not _PID_RE.fullmatch(parts[1]) or not parts[0] or k_line is None:
        return None
    try:
        attempt = int(parts[5])
    except ValueError:
        return None
    target = _coerce_float(pairs.get("target", ""))
    return {"evt": parts[0], "pid": parts[1], "player": pairs.get("player", ""),
            "team": pairs.get("team", ""), "opp": pairs.get("opp", ""),
            "market": (market or ALLOWED_MARKETS[0]), "side": "under", "line": k_line,
            "price": _coerce_float(pairs.get("price", "")) or 0.0,
            "stake": _coerce_float(pairs.get("stake", "")) or 0.0,
            "target": target if target is not None else 0.0, "kind": parts[4],
            "attempt": attempt, "tag": parts[3], "key": key}


def parse(text: str, markets=None, tags=None):
    """Parse a Telegram message from the NBA model feed.

    Returns None when the message contains no BET| line at all (chatter: drop silently, no
    alert). Otherwise returns a ParseResult whose `refusals` the caller MUST surface.

    `markets` / `tags` are the config-driven allow-lists (None = the module defaults)."""
    if not text or BET_PREFIX not in text:
        return None
    markets = tuple(m.strip().lower() for m in (markets or ALLOWED_MARKETS))
    tags = tuple(t.strip() for t in (tags or ALLOWED_TAGS))

    result = ParseResult()
    seen_keys = set()
    for raw_line in text.splitlines():
        if BET_PREFIX not in raw_line:
            continue
        candidate = _HTML_TAG_RE.sub("", raw_line[raw_line.index(BET_PREFIX):]).strip()
        row, reason = _parse_line(candidate, markets, tags)
        if row is None:
            result.refusals.append((candidate.strip()[:400], reason))
            result.refusal_rows.append(salvage_row(candidate, markets[0]))
            continue
        # A single message repeating one key is an upstream bug, not two bets.
        if row["key"] in seen_keys:
            result.refusals.append(
                (candidate.strip()[:400],
                 f"key {row['key']!r} appears more than once in the same message")
            )
            result.refusal_rows.append(None)       # the real row answers this key
            continue
        seen_keys.add(row["key"])
        result.rows.append(row)
    return result
