"""
Tip-parser provider facade.

Selects the LLM backend that parses tipster messages — **Groq** (default,
LIVE) or **Claude/Sonnet** (scaffolded, INERT) — behind one env var
(`TIP_PARSER_PROVIDER`), so the backend can be swapped without rewriting call
sites.

ARCHITECTURE
------------
The ONLY provider-specific part of parsing is the LLM call. Everything else —
the system prompt, JSON repair, and the conversion of the model's JSON into
`ParsedTip` objects — is provider-agnostic and lives ONCE here
(`build_tips_from_parsed`) so Groq and Claude can never diverge.

    route_message(text)                     parse_image(image_bytes)
          │                                        │
          ▼                                        ▼
    tip_parser.parse_text  ──TIP_PARSER_PROVIDER──▶ groq | claude
          │                                        │
          ├── groq   -> groq_parser.parse_with_groq / parse_tip_image  (LIVE)
          └── claude -> claude_parser.*            (INERT until configured)
                              │
                              └── build_tips_from_parsed(...)  ◀── shared here

ACTIVATING CLAUDE (it does NOTHING until ALL of these are done — see
CLAUDE_PARSER.md):
  1. `pip install anthropic`
  2. set `ANTHROPIC_API_KEY` in .env
  3. set `TIP_PARSER_PROVIDER=claude` in .env
  4. point main.py's parser import at this module (one line), then restart
If "claude" is selected but unusable (missing key / SDK), this FAILS SAFE back
to Groq — it never silently drops tips.

NOTE: `groq_parser.parse_with_groq` still has its own inline copy of the build
loop (left untouched so the LIVE path is byte-identical). When Claude is
actually adopted + validated, point `parse_with_groq` at
`build_tips_from_parsed` too and delete its inline copy — one source of truth.
"""

import logging
import os

from config import (
    TIP_PARSER_PROVIDER, ANTHROPIC_API_KEY,
    CLAUDE_FALLBACK_ENABLED, CLAUDE_WEBSEARCH_RESOLVE, CLAUDE_PRIMARY,
)
from models import ParsedTip, ParsedLeg
import groq_parser
# Pure, provider-agnostic helpers reused verbatim from the Groq parser.
from groq_parser import (
    _preprocess_saiyan_emojis,   # re-exported for callers (noqa: F401)
    _clean_name,
    _normalise_stat,
    _normalise_alt_dict,
    _safe_float,
    _explicit_leading_sport,
    _fix_sgm_threshold_leg,
    _merge_slash_line_into_sgm,
)

log = logging.getLogger(__name__)


# ── Provider selection ───────────────────────────────────────────────
def _claude_available() -> bool:
    """Claude path is usable only if it's configured AND the SDK is importable."""
    if not ANTHROPIC_API_KEY:
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _claude_fallback_enabled() -> bool:
    """v5.80 per-call RECOVERY LAYER gate — distinct from the global
    `active_provider()` swap. True only when CLAUDE_FALLBACK_ENABLED is set AND
    Claude is usable (key present + `anthropic` importable). When False, every
    fallback call site short-circuits to a no-op, so the bot is byte-identical
    to pre-v5.80 (Groq-only). The CALLER must additionally gate on a GENUINE
    parse failure (looks-like-a-bet / repair-failed) — never fire on a no-bet /
    summary / chatter message (would re-open the AusBets phantom-bet hole)."""
    if os.getenv("TIPBOT_TESTING"):
        return False  # the unit suite must NEVER hit the Claude API (cost/flakiness)
    return CLAUDE_FALLBACK_ENABLED and _claude_available()


def _claude_websearch_enabled() -> bool:
    """Gate for the Claude web-search resolvers (player->team, SA track, runner).
    Requires the fallback to be on AND the web-search knob on."""
    if os.getenv("TIPBOT_TESTING"):
        return False  # the unit suite must NEVER hit the Claude web_search API
    return CLAUDE_FALLBACK_ENABLED and CLAUDE_WEBSEARCH_RESOLVE and _claude_available()


def _claude_primary_enabled() -> bool:
    """v5.83 CLAUDE PRIMARY gate: parse EVERYTHING with Claude up front, skipping
    Groq. True only when CLAUDE_PRIMARY is set AND Claude is usable (key + SDK).
    Inert under TIPBOT_TESTING (tests use the mocked Groq path) and FALSE when
    Claude is unavailable (so the fork without a key stays on Groq)."""
    if os.getenv("TIPBOT_TESTING"):
        return False
    return CLAUDE_PRIMARY and _claude_available()


# ── Per-call fallback wrappers (Opus 4.8) ────────────────────────────
# main.py calls these on a GENUINE Groq failure, OR as the PRIMARY parser under
# CLAUDE_PRIMARY. They default the model to CLAUDE_FALLBACK_MODEL. The CALLER owns
# the genuine-failure gate. CONTRACT DIFFERS PER WRAPPER (v5.85):
#  - parse_text_fallback / parse_image_fallback: never raise (return the Groq-style
#    ([], t) sentinel).
#  - parse_racing_text_fallback: RAISES on a HARD failure BY DESIGN (so the caller
#    routes a real racing tip to MANUAL, matching Groq's parse_racing_text) — it
#    MUST be called inside a try/except. Returns ([], t) only on a clean empty parse.
def parse_text_fallback(text, tipster, sport, unit_size, default_units):
    import claude_parser
    from config import CLAUDE_FALLBACK_MODEL
    return claude_parser.parse_with_claude(
        text, tipster=tipster, sport=sport,
        unit_size=unit_size, default_units=default_units,
        model=CLAUDE_FALLBACK_MODEL,
    )


def parse_image_fallback(image_bytes, tipster, sport):
    import claude_parser
    from config import CLAUDE_FALLBACK_MODEL
    return claude_parser.parse_tip_image_claude(
        image_bytes, tipster=tipster, sport=sport, model=CLAUDE_FALLBACK_MODEL,
    )


def parse_racing_text_fallback(text, tipster):
    import claude_parser
    from config import CLAUDE_FALLBACK_MODEL
    return claude_parser.parse_racing_text_claude(text, tipster=tipster, model=CLAUDE_FALLBACK_MODEL)


def active_provider() -> str:
    """Resolve the active provider. Fails SAFE to 'groq' if 'claude' is
    selected but not usable (missing ANTHROPIC_API_KEY or `anthropic`), so a
    misconfiguration never drops tips."""
    p = (TIP_PARSER_PROVIDER or "groq").strip().lower()
    if p == "claude":
        if _claude_available():
            return "claude"
        log.error(
            "TIP_PARSER_PROVIDER=claude but Claude is NOT usable "
            "(ANTHROPIC_API_KEY unset or `anthropic` not installed) — "
            "FALLING BACK to Groq. See CLAUDE_PARSER.md."
        )
        return "groq"
    return "groq"


# ── Provider-agnostic entry points (drop-in for the groq_parser fns) ──
def parse_text(text, tipster, sport="nba", unit_size=1.0, default_units=1.0):
    """Parse a TEXT tip via the active provider.

    Drop-in for `groq_parser.parse_with_groq` — identical signature and return
    `(list[ParsedTip], elapsed_seconds)`.
    """
    if active_provider() == "claude":
        import claude_parser  # lazy: never imported unless Claude is active
        return claude_parser.parse_with_claude(
            text, tipster=tipster, sport=sport,
            unit_size=unit_size, default_units=default_units,
        )
    return groq_parser.parse_with_groq(
        text, tipster=tipster, sport=sport,
        unit_size=unit_size, default_units=default_units,
    )


def parse_image(image_bytes, tipster, sport, max_retries=4):
    """Vision-parse a tip IMAGE via the active provider.

    Drop-in for `groq_parser.parse_tip_image` — identical signature and return
    `(list[dict], elapsed_seconds)`.
    """
    if active_provider() == "claude":
        import claude_parser
        return claude_parser.parse_tip_image_claude(
            image_bytes, tipster=tipster, sport=sport, max_retries=max_retries,
        )
    return groq_parser.parse_tip_image(image_bytes, tipster, sport, max_retries=max_retries)


# ── Shared post-processing: model JSON -> list[ParsedTip] ────────────
def build_tips_from_parsed(parsed, text, tipster, sport, unit_size, default_units):
    """Turn a provider's parsed-JSON dict (``{"tips": [...]}``) into a list of
    ``ParsedTip``. Provider-AGNOSTIC — this is a faithful mirror of the build
    loop inside ``groq_parser.parse_with_groq`` so Groq and Claude produce
    identical tips from identical JSON. Keep the two in sync until the Groq path
    is pointed here too (see module docstring).
    """
    tips_data = parsed.get("tips", [])

    # Deterministic sport override: trust an explicit LEADING sport keyword
    # ("afl ...") over the model's flaky per-tip `sport` field.
    forced_sport = _explicit_leading_sport(text)

    tips = []
    for td in tips_data:
        legs = []
        is_sgm = td.get("is_sgm", False)
        is_pyo_sgm = td.get("is_pyo_sgm", False)

        if is_sgm and td.get("raw_legs"):
            prev_player = ""
            for rl in td["raw_legs"]:
                player = rl.get("player", "")
                # Same-player shorthand in SGMs: blank player -> previous leg's.
                # v5.69 (m2): port the v5.23 guard from groq_parser — only carry
                # the previous player when THIS leg has a stat. A team-line /
                # handicap leg ("FRE +0.5") has no stat and must NOT inherit a
                # player, or it masquerades as a player prop and evades the
                # handicap->manual guard (the Fremantle +0.5 SGM bug).
                if not player and prev_player and (rl.get("stat") or "").strip():
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
                leg._is_threshold = rl.get("is_threshold", False)
                leg = _fix_sgm_threshold_leg(
                    leg, is_pyo_sgm, sport=forced_sport or td.get("sport", sport)
                )
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

        is_threshold = td.get("is_threshold", False)
        alert_only = td.get("alert_only", False)
        alert_reason = td.get("alert_reason", "")

        # Shook unit transform: 0.3u-style conservative sizing scaled x3, rounded
        # to nearest 0.25u (matches our risk level). Provider-agnostic.
        raw_units = _safe_float(td.get("units"), default_units)
        # v5.69 (m2): compute units_explicit (was missing from this shared
        # builder, so the Claude path lost the UNITS_REQUIRED_TIPSTERS gate).
        # True only when the model actually returned a positive unit value.
        _parsed_units = _safe_float(td.get("units"), None)
        units_explicit = _parsed_units is not None and _parsed_units > 0
        if tipster == "shook":
            scaled = raw_units * 3
            final_units = round(scaled * 4) / 4
            log.info(f"Shook unit transform: {raw_units}u x3 = {scaled:.2f} -> {final_units}u")
        else:
            final_units = raw_units

        # Shook buffer prefix pollutes notifications — strip to the trigger msg.
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

        # AFL: infer team from roster when the model didn't extract one.
        if tip.sport == "afl":
            try:
                from roster import get_player_team
                for leg in tip.legs:
                    if not leg.team_full and leg.player:
                        inferred = get_player_team(leg.player, "afl")
                        if inferred:
                            leg.team_full = inferred
                            log.info(f"Inferred AFL team from roster: '{leg.player}' -> '{inferred}'")
            except Exception as e:
                log.warning(f"Roster team inference failed: {e}")

        if is_threshold:
            tip._is_threshold = True
        tips.append(tip)

    # Deterministic "/" = SGM: a single "/"-line wrongly split into separate
    # tips is merged back into one SGM ticket.
    tips = _merge_slash_line_into_sgm(tips, text)
    return tips
