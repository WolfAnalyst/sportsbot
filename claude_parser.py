"""
Claude (Anthropic) tip parser + recovery layer.

Two roles:
  1. GLOBAL SWAP (legacy, INERT): a drop-in alternative LLM backend to
     `groq_parser`, selected via `config.TIP_PARSER_PROVIDER == "claude"` through
     the `tip_parser` facade. Uses `CLAUDE_PARSER_MODEL` (Sonnet 4.6).
  2. PER-CALL FALLBACK (v5.80, the live use): `tip_parser` calls these on a
     GENUINE Groq parse failure (repair-failed / 0-tips-on-a-bet-looking msg /
     empty vision). The fallback callers pass `model=CLAUDE_FALLBACK_MODEL`
     (Opus 4.8). Plus web-search RESOLVERS for a player/track the roster/catalog
     can't resolve. See CLAUDE_PARSER.md + config.CLAUDE_FALLBACK_ENABLED.

DESIGN — only the LLM call differs from Groq. This module reuses Groq's
battle-tested SYSTEM_PROMPT, IMAGE_PROMPT_*, _parse_json_with_repair, the Saiyan
emoji preprocessor, and the SHARED post-processing (tip_parser.build_tips_from_parsed)
— so Claude produces the identical ParsedTip shape Groq does. Only transport changes.

The `anthropic` import is LAZY (inside the call helpers) so importing this module
never requires the SDK installed — it compiles and unit-imports with `anthropic`
absent and no key set, returning the same `([], elapsed)` failure sentinel as Groq.

Claude specifics:
  - model = caller's `model` arg, else config.CLAUDE_PARSER_MODEL.
  - prompt caching: the large stable SYSTEM_PROMPT carries cache_control:ephemeral.
  - thinking DISABLED — fast structured extraction (valid on Opus 4.8 / Sonnet 4.6).
  - NO sampling params (temperature/top_p/top_k) — removed on 4.x, would 400.
  - api_key passed explicitly from config (works whether or not .env is exported).
  - the SDK auto-retries 429/5xx with backoff.
  - never raises — returns the `([], elapsed)` / "" / {} failure sentinels.
"""

import base64
import json
import logging
import time

from config import (
    ANTHROPIC_API_KEY, CLAUDE_PARSER_MODEL, CLAUDE_FALLBACK_MODEL, CLAUDE_WEBSEARCH_MODEL,
)
import groq_parser  # reuse SYSTEM_PROMPT, IMAGE_PROMPT_*, repair, helpers

log = logging.getLogger(__name__)

# Match Groq's output budget; 4000 is comfortably below the no-stream timeout.
_MAX_TOKENS = 4000
# web_search is a server-side tool that runs its own loop. COST CONTROL (v5.81):
# `max_uses` caps the number of searches per call (search fees + each result is
# billed as input on later turns), and the short pause_turn continuation cap keeps
# a misbehaving search from spinning. Sonnet (CLAUDE_WEBSEARCH_MODEL) + these caps
# keep each resolve well under a dollar (the Opus+uncapped path cost ~$2/call).
_WEBSEARCH_MAX_TURNS = 3
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}

_CLIENT = None


def _client():
    """Lazily construct (and cache) the Anthropic client. Imports `anthropic`
    only when first called — so this module is importable without the SDK.
    Passes the key explicitly so it works whether or not .env is exported into
    the process environment."""
    global _CLIENT
    if _CLIENT is None:
        import anthropic  # lazy
        _CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _CLIENT


def _text_from(msg) -> str:
    """Concatenate the text blocks of an Anthropic Message response."""
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _complete_text(system_prompt: str, user_content: str, model: str) -> str:
    """One Messages API call for a text tip. System prompt is cached."""
    resp = _client().messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        thinking={"type": "disabled"},
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )
    return _text_from(resp)


def _complete_vision(prompt: str, image_bytes: bytes, model: str) -> str:
    """One Messages API call for an image tip (base64 image + prompt)."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    mime = groq_parser._image_mime(image_bytes)
    resp = _client().messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        thinking={"type": "disabled"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return _text_from(resp)


def parse_with_claude(text, tipster, sport="nba", unit_size=1.0, default_units=1.0, model=None):
    """Parse a TEXT tip via Claude. Mirrors `groq_parser.parse_with_groq`:
    returns `(list[ParsedTip], elapsed_seconds)`; returns `([], elapsed)` on any
    failure (never raises). `model` defaults to CLAUDE_PARSER_MODEL; the per-call
    FALLBACK path passes CLAUDE_FALLBACK_MODEL (Opus 4.8)."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set, skipping Claude parsing")
        return [], 0
    model = model or CLAUDE_PARSER_MODEL

    # Same Saiyan emoji preprocessing as the Groq path (idempotent).
    model_input = text
    if tipster == "saiyan_afl":
        model_input = groq_parser._preprocess_saiyan_emojis(text)

    user_content = (
        f"Tipster: {tipster}\n"
        f"Sport: {sport if sport != 'auto' else 'DETECT FROM MESSAGE'}\n"
        f"Message:\n{model_input}"
    )

    start = time.time()
    try:
        content = _complete_text(groq_parser.SYSTEM_PROMPT, user_content, model)
    except Exception as e:
        log.error(f"Claude parse request failed on {tipster}/{sport}: {type(e).__name__}: {e}")
        return [], time.time() - start
    elapsed = time.time() - start

    content = content.replace("```json", "").replace("```", "").strip()
    parsed = groq_parser._parse_json_with_repair(content)
    if parsed is None:
        log.error("Claude returned invalid JSON (repair failed)")
        log.error(f"Raw response: {content[:500]}")
        return [], elapsed

    # Shared post-processing (one source of truth in tip_parser).
    from tip_parser import build_tips_from_parsed
    tips = build_tips_from_parsed(parsed, text, tipster, sport, unit_size, default_units)
    log.info(f"Claude ({model}) parsed {len(tips)} tip(s) in {elapsed:.2f}s")
    return tips, elapsed


def parse_tip_image_claude(image_bytes, tipster, sport, max_retries=4, model=None):
    """Vision-parse a tip IMAGE via Claude. Mirrors `groq_parser.parse_tip_image`:
    returns RAW extracted dicts (NOT ParsedTip) + elapsed. `model` defaults to
    CLAUDE_PARSER_MODEL; the FALLBACK passes CLAUDE_FALLBACK_MODEL. `max_retries`
    accepted for signature-compatibility (the SDK handles retry/backoff).

    v5.87 (Wilson, after the 10-opus review flagged the silent-drop): RAISES on a
    HARD failure (API/request error, no content, JSON-repair fail, malformed
    'tips') — mirroring `parse_racing_text_claude` — so the caller routes a
    genuinely-unparseable image to MANUAL ('never lose a real tip'). The old
    version swallowed every error and returned ([], t); combined with the
    _process_image_tip summary/recap 0-tip suppressor, a hard-failed image whose
    caption looked like a recap was SILENTLY DROPPED (no manual ping). Now only a
    CLEAN valid-but-empty parse (real chatter / a genuine 0-tip image) returns
    `([], elapsed)`; a hard failure propagates and the caller's try/except pings
    manual. (no-key / no-image stay `([], 0.0)` — Claude-unavailable, not a parse
    failure; they can't occur under CLAUDE_PRIMARY, which requires a key.)"""
    start = time.time()
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set, skipping Claude image parsing")
        return [], 0.0
    if not image_bytes:
        log.warning("parse_tip_image_claude: empty image bytes")
        return [], 0.0
    model = model or CLAUDE_PARSER_MODEL

    prompt = (
        groq_parser.IMAGE_PROMPT_RACING
        if (sport or "").lower() == "racing"
        else groq_parser.IMAGE_PROMPT_AFL
    )
    # NOTE: a request exception PROPAGATES (do NOT swallow) so the caller routes
    # the tip to manual rather than silently dropping it (v5.87).
    content = _complete_vision(prompt, image_bytes, model)
    elapsed = time.time() - start

    if not content:
        raise ValueError(f"parse_tip_image_claude: no content returned for {tipster}")

    content = content.replace("```json", "").replace("```", "").strip()
    parsed = groq_parser._parse_json_with_repair(content)
    if parsed is None:
        log.error(f"Raw response: {content[:500]}")
        raise ValueError(f"parse_tip_image_claude: invalid JSON (repair failed) for {tipster}")

    tips = parsed.get("tips", [])
    if not isinstance(tips, list):
        raise ValueError(f"parse_tip_image_claude: 'tips' not a list for {tipster}")
    log.info(
        f"parse_tip_image_claude ({model}): {tipster} ({sport}) extracted "
        f"{len(tips)} raw tip(s) in {elapsed:.2f}s"
    )
    return tips, elapsed


def parse_racing_text_claude(text, tipster, model=None):
    """Free-TEXT racing parse via Claude — mirrors `groq_parser.parse_racing_text`:
    uses the dedicated free-text TEXT_PROMPT_RACING (NOT the image OCR prompt) and
    RAISES on a HARD failure (API error / JSON-repair fail) so the caller routes a
    genuine tip to MANUAL ('never lose a real tip'), matching Groq's behaviour.
    Returns `([], elapsed)` ONLY for a clean valid-but-empty parse (real chatter).

    v5.84 (5-opus review BLOCKER): the old version swallowed every error and
    returned ([],t), so under CLAUDE PRIMARY a transient Claude failure on a real
    Zak/Trial racing tip was silently dropped as 'chatter' with no manual alert."""
    start = time.time()
    if not ANTHROPIC_API_KEY:
        return [], 0.0
    if not (text or "").strip():
        return [], 0.0
    model = model or CLAUDE_PARSER_MODEL
    user_content = f"Tipster: {tipster}\nMessage:\n{text}"
    # NOTE: deliberately NOT wrapped in try/except — a request exception PROPAGATES
    # so the caller's crash-recovery routes the tip to manual (do not swallow).
    content = _complete_text(groq_parser.TEXT_PROMPT_RACING, user_content, model)
    elapsed = time.time() - start

    content = content.replace("```json", "").replace("```", "").strip()
    parsed = groq_parser._parse_json_with_repair(content)
    if parsed is None:
        # HARD failure (gibberish / repair-fail) -> RAISE so the caller alerts to
        # manual, instead of returning [] which reads as 'chatter -> dropped'.
        raise ValueError(f"parse_racing_text_claude: invalid JSON (repair failed) for {tipster}")
    if not isinstance(parsed, dict):
        # v5.85: a top-level JSON array/scalar is a hard failure too -> raise
        # (was an AttributeError on .get below); routes the caller to manual.
        raise ValueError(f"parse_racing_text_claude: top-level JSON not an object for {tipster}")
    tips = parsed.get("tips", [])
    if not isinstance(tips, list):
        raise ValueError(f"parse_racing_text_claude: 'tips' not a list for {tipster}")
    # v5.85 (5-opus review): MIRROR groq_parser.parse_racing_text's no-runner-row
    # strip. A runner-less-but-saddled row (e.g. {"saddle":7,"runner":null}) would
    # otherwise survive the router guards and place the WRONG horse via the
    # racing_placer Pass-3 saddle-only fallback under CLAUDE PRIMARY.
    tips = [t for t in tips if isinstance(t, dict) and (t.get("runner") or "").strip()]
    log.info(f"parse_racing_text_claude ({model}): {tipster} extracted {len(tips)} raw tip(s) in {elapsed:.2f}s")
    return tips, elapsed


# ── Web-search RESOLVERS ─────────────────────────────────────────────
# When the roster/HB catalog can't resolve a player or track, ask Claude to
# look it up with the server-side web_search tool. The result is ONLY a hint —
# every downstream placement still gates on the bookie catalog + odds floor, so
# a wrong/hallucinated answer can only route to manual, never place a wrong bet.
# All resolvers fail SAFE (return "" / {}) and never raise.

def _websearch_complete(system_prompt: str, user_content: str, model: str) -> str:
    """Run a web_search agentic loop and return the final text. Handles the
    server-tool `pause_turn` by re-sending until `end_turn` (capped)."""
    # v5.84: bound each web-search round-trip (the resolvers currently run inline
    # on the event loop; a tight timeout caps the worst-case block + a hung search
    # fails to {} -> manual). Full run_in_executor offload is a tracked follow-up.
    client = _client().with_options(timeout=40.0)
    messages = [{"role": "user", "content": user_content}]
    last = None
    for _ in range(_WEBSEARCH_MAX_TURNS):
        last = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "disabled"},
            system=system_prompt,
            tools=[_WEB_SEARCH_TOOL],
            messages=messages,
        )
        if last.stop_reason == "pause_turn":
            # Server hit its tool-loop limit; APPEND the assistant turn and
            # re-send to continue (accumulate across multiple pauses — do NOT
            # rebuild a 2-element list, which drops earlier search context).
            messages.append({"role": "assistant", "content": last.content})
            continue
        break
    return _text_from(last) if last is not None else ""


def _websearch_json(system_prompt: str, user_content: str, model=None) -> dict:
    """web_search resolver that returns a parsed JSON object (or {} on failure).
    Defaults to CLAUDE_WEBSEARCH_MODEL (Sonnet) — the cheaper model for the costly
    web-search path; the Opus parse fallback is unaffected."""
    if not ANTHROPIC_API_KEY:
        return {}
    model = model or CLAUDE_WEBSEARCH_MODEL
    try:
        content = _websearch_complete(system_prompt, user_content, model)
    except Exception as e:
        log.error(f"Claude websearch resolve failed: {type(e).__name__}: {e}")
        return {}
    content = (content or "").replace("```json", "").replace("```", "").strip()
    parsed = groq_parser._parse_json_with_repair(content)
    if isinstance(parsed, dict):
        return parsed
    # Last resort: pull the first {...} object out of any surrounding prose.
    try:
        i, j = content.index("{"), content.rindex("}")
        obj = json.loads(content[i:j + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# Per-process memo for the inline AFL player->club web-search. Each web_search
# round-trip is ~14-22s and runs INLINE on the event loop, and the SAME player can
# be resolved several times within one tip (the 2026-06-25 McCartin SGM re-resolved
# the same player 4x across the enrich pass + the sequential _place_sgm_v4 fallback,
# ~68s). Memoising by lowercased name (incl. the empty "unresolved" answer) collapses
# those repeats to one search. Bounded staleness: main.py restarts daily.
_AFL_TEAM_MEMO: dict = {}


def resolve_afl_player_team(player: str, model=None) -> str:
    """Resolve an AFL player to their CURRENT club (e.g. a just-listed mid-season
    rookie absent from the stale roster). Returns the full club name or "" if
    Claude can't confidently resolve. The caller still confirms the player against
    the bookie's live player-prop catalog before placing."""
    if not (player or "").strip():
        return ""
    _key = player.strip().lower()
    if _key in _AFL_TEAM_MEMO:
        return _AFL_TEAM_MEMO[_key]
    system = (
        "You identify the CURRENT AFL (Australian Football League) club of a named player, "
        "for the live 2026 season. Use web_search to confirm recent listings, mid-season "
        "rookie drafts, and trades — fringe/just-listed players matter. "
        "Respond with ONLY a JSON object: {\"team\": \"<full club name e.g. Adelaide Crows>\", "
        "\"confident\": true|false}. If you cannot confidently determine the current club, "
        "set team to \"\" and confident to false. No other text."
    )
    out = _websearch_json(system, f"Which current AFL club does this player play for in 2026: {player}", model)
    result = ""
    if out.get("confident") and isinstance(out.get("team"), str):
        team = out["team"].strip()
        if team:
            log.info(f"Claude web-search resolved AFL player '{player}' -> '{team}'")
            result = team
    # Only memoise a COMPLETED search. _websearch_json returns {} on a transient
    # failure (network/429/timeout/5xx); caching that empty result would pin a
    # just-listed player as unresolved for the whole process. A genuine confident
    # answer (team or "") is a non-empty dict -> safe to cache.
    if out:
        _AFL_TEAM_MEMO[_key] = result
    return result


def resolve_sa_track_today(race_num, runner: str, date_str: str, candidate_tracks=None, model=None) -> str:
    """Resolve TODAY'S South Australian thoroughbred meeting that runs a given
    runner in a given race number. Returns the track name or "". The caller
    still confirms the runner number+NAME against that track's live card before
    placing (a wrong track simply fails the card match -> manual)."""
    if not (runner or "").strip():
        return ""
    hint = ""
    if candidate_tracks:
        hint = " Likely one of: " + ", ".join(candidate_tracks) + "."
    system = (
        "You identify which South Australian (SA) THOROUGHBRED race meeting runs on a given "
        "date and carries a specific runner. Use web_search against Australian racing sources "
        "(racing.com, racenet.com.au, tab.com.au). "
        "Respond with ONLY JSON: {\"track\": \"<SA track name>\", \"confident\": true|false}. "
        "If unsure, track \"\" and confident false. No other text." + hint
    )
    out = _websearch_json(
        system,
        f"On {date_str}, which SA thoroughbred track runs Race {race_num} containing the horse '{runner}'?",
        model,
    )
    if out.get("confident") and isinstance(out.get("track"), str) and out["track"].strip():
        track = out["track"].strip()
        log.info(f"Claude web-search resolved SA track for '{runner}' R{race_num} -> '{track}'")
        return track
    return ""


def resolve_racing_runner(runner: str, race_num, date_str: str, model=None) -> dict:
    """Resolve a racing runner (NAME is usually accurate even when the race number
    is ambiguous/wrong) to its track + race number for TODAY, with fuzzy tolerance
    for a vision spelling error. Returns {"track": str, "race_num": int} or {}.
    The caller confirms against the live card before placing."""
    if not (runner or "").strip():
        return {}
    system = (
        "You identify the race meeting (track) and race number for a named racehorse on a given "
        "date in Australian racing. The runner NAME is usually accurate; the race number may be "
        "wrong or missing — trust the name and correct the race number. Tolerate minor spelling "
        "errors. Use web_search (racenet.com.au/results, racing.com, tab.com.au). "
        "Respond with ONLY JSON: {\"track\": \"<track>\", \"race_num\": <int>, \"runner\": \"<corrected name>\", "
        "\"confident\": true|false}. If unsure, confident false. No other text."
    )
    out = _websearch_json(
        system,
        f"On {date_str}, what track and race number is the racehorse '{runner}' "
        f"(tipped race {race_num}) running in?",
        model,
    )
    if out.get("confident") and isinstance(out.get("track"), str) and out["track"].strip():
        res = {"track": out["track"].strip()}
        try:
            res["race_num"] = int(out.get("race_num"))
        except (TypeError, ValueError):
            res["race_num"] = race_num
        if isinstance(out.get("runner"), str) and out["runner"].strip():
            res["runner"] = out["runner"].strip()
        log.info(f"Claude web-search resolved runner '{runner}' -> {res}")
        return res
    return {}
