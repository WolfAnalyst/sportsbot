"""
Telegram notification sender. One bot token, four chat destinations:

- MANUAL: unfilled/failed bets, manual placement, image tips, SGM fallback
- SUCCESS: successful placements (consolidated per-tip)
- MAINTENANCE: startup, roster refresh, info logs, self-check
- CRITICAL: infrastructure failures (session drops, API outages, crashes)

All four chat IDs are optional. If unset, fall back to NOTIFY_CHAT_ID
(legacy single-destination mode).
"""

import re
import time
import requests
import logging
import os
from config import NOTIFY_BOT_TOKEN, NOTIFY_CHAT_ID, NOTIFY_SUCCESS_CHAT_ID

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"

# ── Chat destinations (loaded from .env with fallback to NOTIFY_CHAT_ID) ──
NOTIFY_MANUAL_CHAT_ID = os.getenv("NOTIFY_MANUAL_CHAT_ID", "") or NOTIFY_CHAT_ID
NOTIFY_MAINTENANCE_CHAT_ID = os.getenv("NOTIFY_MAINTENANCE_CHAT_ID", "") or NOTIFY_CHAT_ID
NOTIFY_CRITICAL_CHAT_ID = os.getenv("NOTIFY_CRITICAL_CHAT_ID", "") or NOTIFY_CHAT_ID
# NOTIFY_SUCCESS_CHAT_ID imported from config; fallback handled in _send_success


def _escape_html(text: str) -> str:
    """Escape HTML special chars and strip Discord emoji tags."""
    text = re.sub(r"<:[A-Za-z0-9_]+:\d+>", "", text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _session_label(session_id, fallback_bookie: str = "") -> str:
    """
    Return a friendly label for a session in placement notifications.
    Format: "Adam Tran Sportsbet (s65465)".

    Looks up name + bookmaker from sessions.yaml via session_priority. Falls
    back to "<bookie> (s<id>)" if the session isn't in yaml — keeps notifs
    rendering even if a session was placed via legacy path or yaml is stale.
    """
    sid = str(session_id) if session_id is not None else ""
    try:
        # Local import to avoid circular: notifier is imported by main, and
        # session_priority is imported by main too. Importing here is fine.
        import session_priority
        meta = session_priority.get_session_meta(sid)
    except Exception:
        meta = None

    if meta and meta.name:
        # Capitalise bookmaker for display ("sportsbet" -> "Sportsbet")
        bookie = (meta.bookmaker or fallback_bookie or "").strip()
        bookie_pretty = bookie.capitalize() if bookie else ""
        if bookie_pretty:
            return f"{meta.name} {bookie_pretty} (s{sid})"
        return f"{meta.name} (s{sid})"

    # Fallback: bookie + session id (legacy format)
    bookie = (fallback_bookie or "").strip()
    if bookie:
        return f"{bookie} (s{sid})"
    return f"s{sid}"


def _render_timing_block(
    phase_timing, total_elapsed_sec=None,
    session_timing=None, concurrent_bookies=False,
) -> str:
    """Render the consolidated 'End-to-end' timing line for a placement summary.

    Shared by notify_tip_placed_summary / notify_tip_unfilled_with_placements /
    notify_tiptitans_placed so every BET PLACED summary renders the same
    RECONCILING breakdown (v5.37). The named phases SUM to the end-to-end:

        parse + resolve + price-check + place-wall (bookies) + other == end-to-end

    `phase_timing` is the tip._timing dict (set by the message handlers + the
    place_tip resolve wrap; threaded explicitly for racing). Optional keys:
      t0             — epoch (time.time()) the tip ARRIVED at the Telegram handler
      parse_sec      — Groq/regex/vision/text parse time
      resolve_sec    — resolve_event(tip) time
      price_check_sec— the resolve-once / enrichment catalog price-check time

    When t0 IS present, the end-to-end is the TRUE arrival->now span
    (time.time() - t0) and the parse/resolve/price-check splits are shown;
    whatever is left after the named phases is 'other' (price-shop overhead,
    audit, the Telegram send itself) so the line ALWAYS reconciles.

    When t0 is ABSENT (a direct placement call in a test, or a path that didn't
    stamp arrival) it FALLS BACK to total_elapsed_sec + the v5.35 bookies/other
    split only — byte-identical to the prior behaviour, so existing callers and
    tests are unaffected.

    place-wall = MAX(session elapsed) for a CONCURRENT fan-out (accounts place in
    parallel, so the bookie-phase wall-clock is the slowest account), else SUM
    (sequential spillover, where the per-session times don't overlap). Returns ""
    (no line) when there's nothing to show, else a leading-newline HTML fragment.
    """
    pt = phase_timing if isinstance(phase_timing, dict) else {}
    t0 = pt.get("t0")
    use_phase = bool(t0)
    if use_phase:
        try:
            end_to_end = max(0.0, time.time() - float(t0))
        except (TypeError, ValueError):
            use_phase = False
            end_to_end = total_elapsed_sec
    else:
        end_to_end = total_elapsed_sec
    if end_to_end is None:
        return ""

    _elapseds = [t.get("elapsed_sec", 0) or 0 for t in (session_timing or [])]
    place_wall = (max(_elapseds) if _elapseds else 0.0) if concurrent_bookies \
        else sum(_elapseds)

    named: list[str] = []   # the components that SUM toward end-to-end
    accounted = 0.0
    if use_phase:
        for key, label in (("parse_sec", "parse"),
                           ("resolve_sec", "resolve"),
                           ("price_check_sec", "price-check")):
            v = pt.get(key)
            if v is not None:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                named.append(f"{label} {v:.1f}s")
                accounted += max(0.0, v)
    if session_timing:
        bk_label = "bookies concurrent, slowest" if concurrent_bookies else "bookies"
        named.append(f"{bk_label} {place_wall:.1f}s")
        accounted += place_wall

    if not named:
        # No phase data and no session timing -> just the total (legacy fallback).
        return f"\n<b>End-to-end:</b> {end_to_end:.1f}s"
    other = max(0.0, end_to_end - accounted)
    return (f"\n<b>End-to-end:</b> {end_to_end:.1f}s "
            f"({', '.join(named)}, other {other:.1f}s)")


def _send(text: str, chat_id: str = "", parse_mode: str = "HTML") -> bool:
    """Send a message via the notification bot.

    v5.56 (audit): transient failures (network exception, HTTP 5xx, 429) get
    ONE retry after a short pause, and a FINAL failure always logs the
    distinctive needle 'NOTIFY LOST (final)' — previously a single failed
    POST silently dropped the message (including CRITICALs and manual-bet
    alerts) with only a generic error line."""
    target = chat_id or NOTIFY_CHAT_ID
    if not NOTIFY_BOT_TOKEN or not target:
        log.warning("Notification bot not configured, skipping")
        print(f"[NOTIFY] {text}")
        return False

    # Short preview for traceability — every send logs one line so we can
    # verify in the log that a notification was attempted, even if Telegram
    # drops it silently.
    preview = text.replace("\n", " ")[:80]

    url = f"{TELEGRAM_API.format(token=NOTIFY_BOT_TOKEN)}/sendMessage"
    for attempt in (1, 2):
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": target,
                    "text": text,
                    "parse_mode": parse_mode,
                },
                timeout=10,
            )
        except Exception as e:
            log.error(f"Telegram send EXCEPTION (chat={target}, attempt "
                      f"{attempt}/2): {e} | preview: {preview}")
            if attempt == 1:
                time.sleep(2)
                continue
            log.error(f"NOTIFY LOST (final): NOT delivered after retry "
                      f"(chat={target}) | preview: {preview}")
            return False

        if resp.status_code == 200:
            log.info(f"Telegram send ok (chat={target}): {preview}")
            return True

        log.error(
            f"Telegram send FAILED (chat={target}, status={resp.status_code}, "
            f"attempt {attempt}/2): {resp.text[:300]} | preview: {preview}"
        )
        if "can't parse entities" in resp.text:
            # H (2026-05-31): the retry already omits parse_mode (so Telegram
            # does NOT re-parse entities and the message DOES deliver) — but
            # the raw "<b>...</b>" tags would show literally. Strip tags so
            # the plain-text fallback is readable. Decode the handful of HTML
            # entities our templates emit (&lt; &gt; &amp;) so they don't show
            # as escapes.
            plain = re.sub(r"<[^>]+>", "", text)
            plain = (
                plain.replace("&lt;", "<")
                     .replace("&gt;", ">")
                     .replace("&amp;", "&")
            )
            try:
                resp2 = requests.post(
                    url,
                    json={"chat_id": target, "text": plain},
                    timeout=10,
                )
            except Exception as e2:
                log.error(f"Telegram plain-text retry EXCEPTION "
                          f"(chat={target}): {e2}")
                resp2 = None
            if resp2 is not None and resp2.status_code == 200:
                log.info(
                    f"Telegram send ok (plain-text retry, chat={target}): {preview}"
                )
                return True
            if resp2 is not None:
                log.error(
                    f"Telegram plain-text retry also failed (chat={target}): "
                    f"{resp2.text[:300]}"
                )
            log.error(f"NOTIFY LOST (final): NOT delivered (chat={target}) "
                      f"| preview: {preview}")
            return False
        # Transient server-side classes get the one retry; other 4xx
        # (chat not found, bot blocked...) won't improve — fail loud now.
        if attempt == 1 and (resp.status_code == 429 or resp.status_code >= 500):
            time.sleep(2)
            continue
        log.error(f"NOTIFY LOST (final): NOT delivered (chat={target}) "
                  f"| preview: {preview}")
        return False
    return False


# ── Destination helpers ─────────────────────────────────────────────

def _send_manual(text: str) -> bool:
    """Chat #1: action-required bets (unfilled, failed, manual, image, SGM fallback)."""
    return _send(text, chat_id=NOTIFY_MANUAL_CHAT_ID)


def _send_success(text: str) -> bool:
    """Chat #2: successful placements, one per tip."""
    target = NOTIFY_SUCCESS_CHAT_ID or NOTIFY_CHAT_ID
    return _send(text, chat_id=target)


def _send_maintenance(text: str) -> bool:
    """Chat #3: startup, roster refresh, self-check, info."""
    return _send(text, chat_id=NOTIFY_MAINTENANCE_CHAT_ID)


def _send_critical(text: str) -> bool:
    """Chat #4: infrastructure failures (session drop, API down)."""
    return _send(text, chat_id=NOTIFY_CRITICAL_CHAT_ID)


# Legacy alias: default alert-level messages still resolve to manual chat
# (these are mostly "action required" anyway). Kept so we don't break existing callers.
def _send_alert(text: str) -> bool:
    return _send_manual(text)


def notify_bet_placed(result) -> bool:
    tip = result.tip
    # v5.36: SGM consolidation. When _place_sgm_v4 (non-orchestrated) sets
    # tip._sgm_consolidate, the per-account placements are rolled up into ONE
    # notify_tip_placed_summary at the SGM tail (which ALSO writes the per-leg
    # ledger rows), so suppress the per-placement BET PLACED message + its
    # ledger write here — this is what fixes the "2 messages for a 2-account
    # SGM spillover" Wilson flagged.
    if getattr(tip, "_sgm_consolidate", False):
        return True
    legs_lines = []

    # For singles where we recorded the actual placed values, prefer those
    # over tip.legs[0] so the Telegram message reflects what HyperBot actually
    # received (post line-tolerance auto-adjust, post handicap sign-flip).
    # Falls back to tip.legs for SGMs (multi-leg) or any path that doesn't
    # populate placed_*.
    used_placed_snapshot = False
    if (
        not tip.is_sgm
        and getattr(result, "placed_market", None) is not None
        and len(tip.legs) <= 1
    ):
        market = result.placed_market or ""
        player = result.placed_player or ""
        stat = result.placed_stat or ""
        line = result.placed_line
        selection = result.placed_selection or ""
        sel_disp = selection
        if player and selection.lower().startswith(player.lower()):
            sel_disp = selection[len(player):].strip()
        if market in ("h2h", "head_to_head"):
            legs_lines.append(f"  {selection} Win")
        elif market in ("total", "total_points", "line"):
            legs_lines.append(f"  {selection} {line}")
        elif player:
            legs_lines.append(f"  {player} {sel_disp} {line} {stat}".rstrip())
        else:
            legs_lines.append(f"  {selection} {line} {stat}".rstrip())
        used_placed_snapshot = True

    if not used_placed_snapshot:
        for leg in tip.legs:
            if leg.market in ("h2h", "head_to_head"):
                legs_lines.append(f"  {leg.selection} Win")
            elif leg.market in ("total", "total_points", "line"):
                legs_lines.append(f"  {leg.selection} {leg.line}")
            else:
                legs_lines.append(
                    f"  {leg.player} {leg.selection} {leg.line} {leg.stat}"
                )

    legs_str = "\n".join(legs_lines)
    sgm_tag = " [SGM]" if tip.is_sgm else ""

    # Per-placement elapsed (bookie round-trip). Captured in main.py
    # _execute_bet (singles) and v4 SGM placement loop. Renders inline
    # so slow bookies are visible. None / missing fallback gracefully.
    elapsed_str = ""
    elapsed_sec = getattr(result, "elapsed_sec", None)
    if elapsed_sec is not None:
        elapsed_str = f"\n<b>Elapsed:</b> {elapsed_sec:.1f}s"

    text = (
        f"<b>BET PLACED</b>{sgm_tag}\n"
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}\n"
        f"<b>Bookie:</b> {result.bookie}\n"
        f"<b>Event:</b> {_escape_html(tip.event or 'N/A')}\n"
        f"<pre>{_escape_html(legs_str)}</pre>\n"
        f"<b>Stake:</b> ${result.stake:.2f} @ {result.odds}\n"
        f"<b>Bet ID:</b> <code>{result.bet_id}</code>"
        f"{elapsed_str}"
    )
    try:  # v5.21 bets_placed.csv ledger (guarded; never breaks placement)
        from bet_ledger import log_sports_bet
        log_sports_bet(tip, result,
                       _session_label(getattr(result, "session_id", ""), result.bookie))
    except Exception:
        pass
    return _send_success(text)


def notify_bet_failed(result) -> bool:
    """Bet placement failed - goes to Manual Bets (#1).

    Enriched payload includes tip_id (Telegram message id when available),
    structured legs (player/selection/line/stat per leg), bookie that the
    failure came from, and the FULL error including any 'Available: [...]'
    list bookies return on selection-not-found errors. Without the full
    Available list, the user has to log-grep to see what selections the
    bookie did offer (e.g. when 'Lachie Ash Under' fails because Lachie
    Ash isn't on the market for the resolved game). 2026-05-03 enrichment.
    """
    tip = result.tip
    legs_lines = []
    for leg in tip.legs:
        if leg.market in ("h2h", "head_to_head"):
            legs_lines.append(f"  {leg.selection} Win")
        elif leg.market in ("line", "first_half_line"):
            sign = "+" if (leg.line or 0) >= 0 else ""
            legs_lines.append(f"  {leg.selection} {sign}{leg.line}")
        else:
            legs_lines.append(
                f"  {leg.player} {leg.selection} {leg.line} {leg.stat}"
            )
    legs_str = "\n".join(legs_lines)

    # Tip ID (Telegram message id is the most diagnostic-useful identifier)
    tip_id_str = ""
    msg_id = getattr(tip, "telegram_msg_id", None)
    if msg_id:
        tip_id_str = f"<b>Tip ID:</b> {msg_id}\n"

    # Bookie that the failure came from (when known)
    bookie_str = ""
    if getattr(result, "bookie", None):
        bookie_str = f"<b>Bookie:</b> {result.bookie}\n"

    # Stake context
    stake_str = ""
    if getattr(result, "stake", None):
        stake_str = f"<b>Stake attempted:</b> ${result.stake:.2f}\n"

    # Error - keep more context than before. Available: [...] lists were
    # being truncated at 120 chars and losing the diagnostic info.
    err = result.error or ""
    err_truncated = err[:1500] if len(err) > 1500 else err
    if len(err) > 1500:
        err_truncated += " ...[truncated]"

    text = (
        f"<b>❌ BET FAILED</b>\n"
        f"{tip_id_str}"
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}\n"
        f"<b>Sport:</b> {tip.sport.upper()}\n"
        f"<b>Event:</b> {_escape_html(tip.event or 'UNRESOLVED')}\n"
        f"{bookie_str}"
        f"{stake_str}"
        f"<b>Legs:</b>\n<pre>{_escape_html(legs_str)}</pre>\n"
        f"<b>Error:</b>\n<pre>{_escape_html(err_truncated)}</pre>"
    )
    return _send_alert(text)


def notify_manual_alert(tip) -> bool:
    """Alert for tips that need manual placement (SGMs, LIVE, etc)."""
    legs_lines = []
    for leg in tip.legs:
        if leg.market in ("h2h", "head_to_head"):
            legs_lines.append(f"  {leg.selection} Win")
        elif leg.market in ("total", "total_points", "line"):
            legs_lines.append(f"  {leg.selection} {leg.line}")
        elif leg.market == "player_prop":
            legs_lines.append(
                f"  {leg.player} {leg.selection} {leg.line} {leg.stat}"
            )
        else:
            legs_lines.append(f"  {_escape_html(leg.raw_text)}")

    if legs_lines:
        legs_str = "\n".join(legs_lines)
    else:
        legs_str = "  (see raw message)"

    sgm_tag = " [SGM]" if tip.is_sgm else ""
    live_tag = " [LIVE]" if tip.is_live else ""
    bookie = f"\n<b>Bookie:</b> {_escape_html(tip.suggested_bookie)}" if tip.suggested_bookie else ""

    # Empty Reason field showed up on Saiyan SGM 2026-04-30: the v4 SGM
    # priority filter dropped all candidates but didn't set tip.alert_reason,
    # so Telegram showed "Reason:  " with nothing useful. Substitute a
    # default so the user at least sees the bet shape and knows it needs
    # manual placement, even if the calling site forgot to set a reason.
    reason = tip.alert_reason
    if not reason:
        if tip.is_sgm:
            reason = "SGM auto-place skipped (no priority sessions or upstream rejection)"
        elif tip.is_live:
            reason = "LIVE bet, alert only"
        else:
            reason = "Auto-placement skipped, manual review required"

    text = (
        f"<b>MANUAL BET ALERT</b>{sgm_tag}{live_tag}\n"
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}\n"
        f"<b>Reason:</b> {_escape_html(reason)}\n"
        f"<b>Sport:</b> {tip.sport.upper()}"
        f"{bookie}\n"
        f"<b>Stake:</b> ${tip.stake_dollars:.2f}\n"
        f"<pre>{_escape_html(legs_str)}</pre>\n"
        f"<b>Raw:</b>\n<pre>{_escape_html(tip.raw_message[:300])}</pre>"
    )
    return _send_alert(text)


def notify_parse_error(tipster: str, raw_message: str, error: str) -> bool:
    preview = raw_message[:200] + "..." if len(raw_message) > 200 else raw_message
    text = (
        f"<b>PARSE ERROR</b>\n"
        f"<b>Tipster:</b> {_escape_html(tipster)}\n"
        f"<b>Error:</b> {_escape_html(error)}\n"
        f"<b>Message:</b>\n<pre>{_escape_html(preview)}</pre>"
    )
    return _send_alert(text)


def notify_event_not_found(tipster: str, team: str, raw_message: str, tip=None) -> bool:
    """
    Alert when fixture resolution failed.
    If `tip` is provided, renders in Raw/Parsed style with the player name
    visible (not just the team that failed to resolve). Falls back to
    legacy format if `tip` is None.
    """
    preview = (raw_message or "").strip()[:250]

    if tip is not None:
        # Render the parsed leg(s) so the player name is visible — avoids
        # "Dallas Mavericks not found" alerts where you can't tell which
        # player the tip was about.
        try:
            from main import _format_leg_human
            parsed = " / ".join(_format_leg_human(l) for l in (tip.legs or []))
        except Exception:
            parsed = "(unable to render)"
        sport = (tip.sport or "?").upper()
        text = (
            f"<b>❌ EVENT NOT FOUND</b>\n"
            f"<b>Tipster:</b> {_escape_html(tipster)}\n"
            f"<b>Sport:</b> {sport}\n"
            f"<b>Team searched:</b> {_escape_html(team)}\n"
            f"\n"
            f"<b>Raw:</b>\n<pre>{_escape_html(preview)}</pre>\n"
            f"<b>Parsed:</b>\n<pre>{_escape_html(parsed)}</pre>\n"
            f"No fixture found. Tip skipped — place manually if valid."
        )
    else:
        text = (
            f"<b>EVENT NOT FOUND</b>\n"
            f"<b>Tipster:</b> {_escape_html(tipster)}\n"
            f"<b>Team:</b> {_escape_html(team)}\n"
            f"<b>Message:</b>\n<pre>{_escape_html(preview)}</pre>\n"
            f"No fixture found for today or tomorrow. Tip skipped."
        )
    return _send_alert(text)


def notify_startup() -> bool:
    return _send_maintenance("<b>TipBot started</b>\nListening for tips...")


def notify_info(message: str) -> bool:
    """Info/maintenance log message. Goes to maintenance chat."""
    return _send_maintenance(f"<b>INFO:</b> {_escape_html(message)}")


def notify_critical(message: str) -> bool:
    """Infrastructure failure. Goes to critical chat."""
    return _send_critical(f"<b>🚨 CRITICAL:</b> {_escape_html(message)}")


def notify_image_alert(tipster: str, raw_message: str) -> bool:
    """Alert when a tipster sends an image (can't parse automatically)."""
    preview = raw_message[:200] if raw_message else "(image only)"
    text = (
        f"<b>IMAGE TIP</b>\n"
        f"<b>Tipster:</b> {_escape_html(tipster)}\n"
        f"<b>Message:</b>\n<pre>{_escape_html(preview)}</pre>\n"
        f"Image bet detected. Check and place manually."
    )
    return _send_alert(text)


def notify_tip_placed_summary(
    tip, placed_results, intended_stake, unfilled,
    total_elapsed_sec=None,
    session_timing=None,
    concurrent_bookies=False,
) -> bool:
    """One consolidated success message per tip with all placements rolled up.

    `total_elapsed_sec` is the end-to-end time from _place_singles_v4 entry
    to this notify call.

    `session_timing` (added 2026-05-17 v4.2) is a list of per-session
    attempt summaries: [{session_id, bookie, elapsed_sec, attempts, fails,
    succeeded}, ...] including BOTH filled and failed sessions. Renders
    one line per session attempted so Wilson can see WHERE the time went,
    not just the winning placement. Previous version only showed elapsed
    per successful placement via BetResult.elapsed_sec, hiding the cost
    of failed ladders on prior sessions.
    """
    legs_lines = []
    for leg in tip.legs:
        if leg.market in ("h2h", "head_to_head"):
            legs_lines.append(f"  {leg.selection} Win")
        elif leg.market in ("total", "total_points", "line"):
            legs_lines.append(f"  {leg.selection} {leg.line}")
        else:
            legs_lines.append(
                f"  {leg.player} {leg.selection} {leg.line} {leg.stat}"
            )
    legs_str = "\n".join(legs_lines)
    sgm_tag = " [SGM]" if tip.is_sgm else ""

    # Roll up placements
    total_placed = sum(r.stake or 0 for r in placed_results)
    weighted_odds = (
        sum((r.odds or 0) * (r.stake or 0) for r in placed_results) / total_placed
        if total_placed > 0 else 0
    )

    # Reference values from the parsed tip's first leg for diff detection.
    # If a placement's actual values differ (e.g. line tolerance auto-shift
    # 21.5 -> 22.5, or handicap sign flip 11.0 -> -11.0, or stat fallback
    # PRA -> Points), append the actual values so the user sees what really
    # got placed rather than what was tipped.
    ref_line = tip.legs[0].line if tip.legs else None
    ref_stat = tip.legs[0].stat if tip.legs else ""
    ref_market = tip.legs[0].market if tip.legs else ""

    # Index session_timing by session_id for per-line lookup. Falls back
    # to BetResult.elapsed_sec when timing isn't supplied (e.g. legacy
    # callers, racing). The new format renders both elapsed AND attempt
    # count so 6-attempt 25s ladders are visible: "Daniel Sportsbet (s68723):
    # $83 @ 1.81 [bet_id] (25.1s, 6 attempts)".
    timing_map = {}
    for t in (session_timing or []):
        timing_map[str(t.get("session_id"))] = t

    def _timing_suffix(sid: str, fallback_elapsed: float | None) -> str:
        meta = timing_map.get(str(sid))
        if meta:
            elapsed = meta.get("elapsed_sec")
            attempts = meta.get("attempts", 0)
            if elapsed is None:
                return ""
            if attempts > 1:
                return f"  ({elapsed:.1f}s, {attempts} attempts)"
            return f"  ({elapsed:.1f}s)"
        if fallback_elapsed is not None:
            return f"  ({fallback_elapsed:.1f}s)"
        return ""

    placement_lines = []
    rendered_sids: set[str] = set()
    for r in placed_results:
        sid_str = str(r.session_id) if r.session_id is not None else ""
        rendered_sids.add(sid_str)
        base = (
            f"  {_session_label(r.session_id, r.bookie)}: "
            f"${r.stake:.2f} @ {r.odds} [{r.bet_id}]"
        )
        # ALWAYS show the actual placed line/market/stat per account so the
        # operator can verify each bet — lines legitimately differ per account
        # after a ±1.0 catalog snap (e.g. Soligo 18.5 on some, 17.5 on others;
        # Cook 17.5 tipped -> 16.5 placed). Previously this only appeared when
        # the placed value DIFFERED from the tipped one, so an exact-match line
        # (Soligo 18.5) showed nothing. Flag the tipped value when it was
        # adjusted. 2026-06-05.
        detail_parts = []
        placed_line = getattr(r, "placed_line", None)
        placed_stat = getattr(r, "placed_stat", None)
        placed_market = getattr(r, "placed_market", None)
        if placed_line is not None:
            try:
                if ref_line is not None and abs(float(placed_line) - float(ref_line)) > 0.01:
                    detail_parts.append(f"line={placed_line} (tipped {ref_line})")
                else:
                    detail_parts.append(f"line={placed_line}")
            except (TypeError, ValueError):
                detail_parts.append(f"line={placed_line}")
        if placed_market and ref_market and placed_market != ref_market:
            detail_parts.append(f"market={placed_market}")
        if placed_stat and ref_stat and placed_stat != ref_stat:
            detail_parts.append(f"stat={placed_stat}")
        if detail_parts:
            base += f"  (placed {', '.join(detail_parts)})"
        base += _timing_suffix(sid_str, getattr(r, "elapsed_sec", None))
        placement_lines.append(base)

    # Failed sessions get a separate "tried" block so the user sees
    # which accounts ate clock without filling. Only present when
    # session_timing supplied (v4 path); legacy callers see empty.
    tried_lines = []
    for t in (session_timing or []):
        sid = str(t.get("session_id", ""))
        if sid in rendered_sids:
            continue
        if t.get("succeeded"):
            # Should be in placed_results already, but guard anyway
            continue
        bookie = t.get("bookie", "") or ""
        elapsed = t.get("elapsed_sec")
        attempts = t.get("attempts", 0)
        fails = t.get("fails", 0)
        if elapsed is None:
            continue
        tried_lines.append(
            f"  {_session_label(sid, bookie)}: rejected "
            f"({elapsed:.1f}s, {attempts} attempts, {fails} fails)"
        )

    placements_str = "\n".join(placement_lines) if placement_lines else "  (none)"
    tried_block = ""
    if tried_lines:
        tried_block = (
            f"\n<b>Also tried:</b>\n<pre>{_escape_html(chr(10).join(tried_lines))}</pre>"
        )

    fill_tag = ""
    if unfilled >= 1:
        fill_tag = f"\n<b>⚠️ Unfilled:</b> ${unfilled:.2f}"

    # v5.43: when the size was a no-units FALLBACK (the image had no readable unit
    # sizing, so 2.5u was assumed, capped $1000 stake / $1000 liability), make it
    # CLEAR in the bet log so a misread of a genuine 1u tip is visible (Wilson —
    # replaces the v5.42 maintenance ping).
    fallback_tag = ""
    if getattr(tip, "_units_fallback", False):
        fallback_tag = (
            f"\n<b>⚠️ FALLBACK SIZING</b> — image had NO unit size; assumed "
            f"{tip.units:g}u (capped $1000 stake / $1000 liability). Verify the size."
        )

    # v5.37: one RECONCILING end-to-end line. When the message handler stamped
    # tip._timing (t0 + parse/resolve/price-check), this shows the TRUE arrival->
    # now span broken into parse / resolve / price-check / bookies(place-wall) /
    # other (which all SUM to the total). Falls back to the v5.35 bookies/other
    # split off total_elapsed_sec when no phase data is present (tests, legacy).
    elapsed_tag = _render_timing_block(
        getattr(tip, "_timing", None),
        total_elapsed_sec, session_timing, concurrent_bookies,
    )

    text = (
        f"<b>BET PLACED</b>{sgm_tag}{fallback_tag}\n"
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}\n"
        f"<b>Event:</b> {_escape_html(tip.event or 'N/A')}\n"
        f"<pre>{_escape_html(legs_str)}</pre>\n"
        f"<b>Total placed:</b> ${total_placed:.2f} of ${intended_stake:.2f} "
        f"@ avg {weighted_odds:.3f}"
        f"{fill_tag}"
        f"{elapsed_tag}\n"
        f"<b>Placements:</b>\n<pre>{_escape_html(placements_str)}</pre>"
        f"{tried_block}"
    )
    try:  # v5.21 bets_placed.csv ledger — one row per landed leg (guarded)
        from bet_ledger import log_sports_bet
        for _r in (placed_results or []):
            if getattr(_r, "success", False) and getattr(_r, "bet_id", None):
                log_sports_bet(tip, _r,
                               _session_label(getattr(_r, "session_id", ""),
                                              getattr(_r, "bookie", "")))
    except Exception:
        pass
    return _send_success(text)


def notify_tip_unfilled(tip, intended_stake, placed_stake, unfilled, failed_results) -> bool:
    """Alert when a tip couldn't be fully filled - manual placement needed for remainder."""
    legs_lines = []
    for leg in tip.legs:
        if leg.market in ("h2h", "head_to_head"):
            legs_lines.append(f"  {leg.selection} Win")
        elif leg.market in ("total", "total_points", "line"):
            legs_lines.append(f"  {leg.selection} {leg.line}")
        else:
            legs_lines.append(
                f"  {leg.player} {leg.selection} {leg.line} {leg.stat}"
            )
    legs_str = "\n".join(legs_lines)

    last_err = failed_results[-1].error if failed_results else "no error captured"
    last_err_short = (last_err or "")[:200]

    title = "BET UNFILLED" if placed_stake > 0 else "BET FAILED"
    text = (
        f"<b>{title}</b>\n"
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}\n"
        f"<b>Event:</b> {_escape_html(tip.event or 'UNRESOLVED')}\n"
        f"<pre>{_escape_html(legs_str)}</pre>\n"
        f"<b>Placed:</b> ${placed_stake:.2f} of ${intended_stake:.2f}\n"
        f"<b>Manual remainder:</b> ${unfilled:.2f}\n"
        f"<b>Last error:</b> <pre>{_escape_html(last_err_short)}</pre>"
    )
    return _send_alert(text)


def notify_tip_unfilled_with_placements(
    tip, intended_stake, placed_stake, unfilled,
    placed_results, failed_results,
    session_timing=None,
    total_elapsed_sec=None,
    concurrent_bookies=False,
) -> bool:
    """
    Richer unfilled alert that also shows what was already placed (useful when
    alt spillover got some fills but not all). Preferred over notify_tip_unfilled
    when placement results are available.

    `session_timing` (2026-05-17 v4.2) renders per-session elapsed and
    attempt count alongside each line so failed sessions that ate clock
    are visible. Same shape as notify_tip_placed_summary.

    `total_elapsed_sec` / `concurrent_bookies` (v5.37) feed the shared
    _render_timing_block so the unfilled alert ALSO shows the reconciling
    end-to-end (parse / resolve / price-check / bookies / other) when the
    handler stamped tip._timing. Both optional — sequential callers omit them.
    """
    raw_short = (tip.raw_message or "").strip()[:300]

    try:
        from main import _format_leg_human
        parsed = " / ".join(_format_leg_human(l) for l in (tip.legs or []))
    except Exception:
        parsed = "(unable to render)"

    last_err = "no error captured"
    for r in reversed(failed_results or []):
        if getattr(r, "error", None):
            last_err = r.error
            break
    last_err_short = (last_err or "")[:250]

    # Index session_timing for per-line lookup (matches placed_summary pattern)
    timing_map = {}
    for t in (session_timing or []):
        timing_map[str(t.get("session_id"))] = t

    def _timing_suffix(sid: str) -> str:
        meta = timing_map.get(str(sid))
        if not meta:
            return ""
        elapsed = meta.get("elapsed_sec")
        attempts = meta.get("attempts", 0)
        if elapsed is None:
            return ""
        if attempts > 1:
            return f"  ({elapsed:.1f}s, {attempts} attempts)"
        return f"  ({elapsed:.1f}s)"

    # Build placements block by iterating placed_results directly. Earlier
    # versions tried to import a `_group_placements` helper that never
    # existed, silently falling through to "(unable to render)" via the
    # except branch. Match the format used in notify_tiptitans_placed for
    # consistency: friendly session label + stake + odds + bet id.
    # Regression watch: make sure boost flag survives — failure case was
    # "boost wins not visibly tagged in unfilled alerts".
    rendered_sids: set[str] = set()
    if placed_results:
        lines = []
        for r in placed_results:
            sid = getattr(r, "session_id", "") or ""
            rendered_sids.add(str(sid))
            bookie = getattr(r, "bookie", "") or ""
            stake = getattr(r, "stake", 0) or 0
            odds = getattr(r, "odds", 0) or 0
            boost_tag = " ⚡BOOST" if getattr(r, "used_boost", False) else ""
            lines.append(
                f"  {_session_label(sid, bookie)}{boost_tag}: "
                f"${stake:.2f} @ {odds}{_timing_suffix(sid)}"
            )
        placements_block = "\n".join(lines) if lines else "  (none)"
    else:
        placements_block = "  (none)"

    # Failed-session "Also tried" block — same pattern as placed_summary
    tried_lines = []
    for t in (session_timing or []):
        sid = str(t.get("session_id", ""))
        if sid in rendered_sids:
            continue
        if t.get("succeeded"):
            continue
        bookie = t.get("bookie", "") or ""
        elapsed = t.get("elapsed_sec")
        attempts = t.get("attempts", 0)
        fails = t.get("fails", 0)
        if elapsed is None:
            continue
        tried_lines.append(
            f"  {_session_label(sid, bookie)}: rejected "
            f"({elapsed:.1f}s, {attempts} attempts, {fails} fails)"
        )

    title = "⚠️ BET UNFILLED" if placed_stake > 0 else "❌ BET FAILED"

    parts = [
        f"<b>{title}</b>",
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}",
        f"<b>Event:</b> {_escape_html(tip.event or 'UNRESOLVED')}",
        f"",
        f"<b>Raw:</b>\n<pre>{_escape_html(raw_short)}</pre>",
        f"<b>Parsed:</b>\n<pre>{_escape_html(parsed)}</pre>",
        f"<b>Already placed:</b>\n<pre>{_escape_html(placements_block)}</pre>",
    ]
    if tried_lines:
        parts.append(
            f"<b>Also tried:</b>\n<pre>"
            f"{_escape_html(chr(10).join(tried_lines))}</pre>"
        )
    parts.extend([
        f"<b>Placed:</b> ${placed_stake:.2f} of ${intended_stake:.2f}",
        f"<b>Remaining:</b> ${unfilled:.2f}",
    ])
    # v5.37: same reconciling end-to-end line as the placed summary (when the
    # handler stamped tip._timing). Leading newline stripped — parts are joined.
    _timing = _render_timing_block(
        getattr(tip, "_timing", None),
        total_elapsed_sec, session_timing, concurrent_bookies,
    )
    if _timing:
        parts.append(_timing.lstrip("\n"))
    parts.append(f"<b>Last error:</b>\n<pre>{_escape_html(last_err_short)}</pre>")
    return _send_alert("\n".join(parts))


# ── Tip Titans notifications ────────────────────────────────────────

def notify_tiptitans_placed(
    tip_id, parsed, intended_stake, placed, unfilled, test_mode=False,
    total_elapsed_sec=None, session_timing=None, phase_timing=None,
) -> bool:
    """Success message for a Tip Titans placement - goes to Bet Log (#2).

    `total_elapsed_sec` is end-to-end time from process_tip entry to this
    notify call. `session_timing` is per-session bookie elapsed from
    racing_placer's session_elapsed timer. Both render inline so slow
    bookies are visible at a glance — same pattern as AFL/NBA alerts.

    `phase_timing` (v5.37) is the optional racing-handler timing dict
    ({t0, parse_sec}) — when present (the Zak/Trial image+text auto-place
    path threads it), the end-to-end is measured from the tip's ARRIVAL at
    the Telegram handler and the vision/text parse split is shown. When
    absent (the Tip Titans channel, which has no upstream parse), it falls
    back to total_elapsed_sec + the bookies/other split — UNCHANGED.
    """
    test_tag = " [TEST MODE]" if test_mode else ""

    total_placed = sum(p["stake"] for p in placed)
    if total_placed > 0:
        weighted_odds = sum(p["odds"] * p["stake"] for p in placed) / total_placed
    else:
        weighted_odds = 0

    # Index session timings by session_id so each placement line can be
    # decorated with its own elapsed. Missing entries (price-shop fail,
    # skipped) render without timing rather than blocking the line.
    timing_map = {}
    for t in (session_timing or []):
        timing_map[str(t.get("session_id"))] = t.get("elapsed_sec")

    placement_lines = []
    for p in placed:
        elapsed = timing_map.get(str(p["session_id"]))
        elapsed_str = f"  ({elapsed:.1f}s)" if elapsed is not None else ""
        placement_lines.append(
            f"  {_session_label(p['session_id'], p.get('bookie', ''))}: "
            f"${p['stake']:.2f} @ {p['odds']} [{p['bet_id']}]{elapsed_str}"
        )
    placements_str = "\n".join(placement_lines)

    unfilled_tag = ""
    if unfilled >= 1:
        unfilled_tag = f"\n<b>⚠️ Unfilled:</b> ${unfilled:.2f}"

    # v5.37: shared reconciling end-to-end line. Racing places SEQUENTIALLY
    # (spillover), so concurrent_bookies=False -> bookies = SUM (unchanged). When
    # the Zak/Trial racing handler threads phase_timing (t0 + vision/text parse),
    # the end-to-end is measured from arrival and the parse split is shown; the
    # Tip Titans channel passes phase_timing=None -> falls back to the prior
    # total_elapsed_sec + bookies/other split (byte-identical).
    elapsed_tag = _render_timing_block(
        phase_timing, total_elapsed_sec, session_timing, concurrent_bookies=False,
    )

    saddle_str = f"{parsed.get('saddle')}. " if parsed.get("saddle") else ""
    text = (
        f"<b>🏇 TIP TITANS PLACED</b>{test_tag}\n"
        f"<b>Titan:</b> {_escape_html(parsed['titan'])}\n"
        f"<b>Track:</b> {_escape_html(parsed['track'])} R{parsed['race_num']} "
        f"{parsed['race_type']}\n"
        f"<b>Runner:</b> {saddle_str}{_escape_html(parsed['runner'])}\n"
        f"<b>Market:</b> {parsed['market'].capitalize()} "
        f"@ tipster odds {parsed['tipster_odds']}\n"
        f"<b>Total:</b> ${total_placed:.2f} of ${intended_stake:.2f} "
        f"@ avg {weighted_odds:.3f}"
        f"{unfilled_tag}"
        f"{elapsed_tag}\n"
        f"<b>Placements:</b>\n<pre>{_escape_html(placements_str)}</pre>"
    )
    try:  # v5.21 bets_placed.csv ledger — one row per landed racing leg (guarded)
        from bet_ledger import log_racing_bet
        for _p in (placed or []):
            if _p.get("bet_id"):
                log_racing_bet(parsed, _p,
                               _session_label(_p.get("session_id", ""), _p.get("bookie", "")))
    except Exception:
        pass
    return _send_success(text)


def notify_tiptitans_ladder_maintenance(tip_id, parsed, attempts) -> bool:
    """Maintenance channel alert for stake-ladder rejections.

    Summarises which sessions hit "stake too high" and at what amounts.
    Recurring rejections at low stakes on the same account is the early
    indicator of bookie-side limiting. Goes to Maintenance (#3) rather
    than the main Bet Log so the success-path stays clean.
    """
    # Group by session_id for readability — multiple rungs from the same
    # ladder land on the same line.
    by_session: dict = {}
    for a in attempts:
        key = (a["bookie"], a["session_id"])
        by_session.setdefault(key, []).append(a["stake_rejected"])

    lines = []
    for (bookie, sid), stakes in by_session.items():
        stakes_sorted = sorted(stakes, reverse=True)
        stakes_str = " -> ".join(f"${s:.0f}" for s in stakes_sorted)
        lines.append(
            f"  {_session_label(sid, bookie)}: rejected {stakes_str}"
        )
    detail_str = "\n".join(lines)

    saddle_str = f"{parsed.get('saddle')}. " if parsed.get("saddle") else ""
    text = (
        f"<b>📉 LADDER ACTIVITY</b>\n"
        f"<b>Tip ID:</b> {tip_id}\n"
        f"<b>Track:</b> {_escape_html(parsed['track'])} R{parsed['race_num']} "
        f"{parsed['race_type']}\n"
        f"<b>Runner:</b> {saddle_str}{_escape_html(parsed['runner'])}\n"
        f"<b>Stake-too-high rejections:</b>\n<pre>{_escape_html(detail_str)}</pre>"
    )
    return _send_maintenance(text)


# ── Sports ladder + MBL alerts ──────────────────────────────────────
#
# Mirror of the racing tiptitans pair (notify_tiptitans_ladder_maintenance
# + the inline MBL critical alert in tiptitans_processor) but driven from
# main._place_singles_v4 / _place_sgm_v4 for NBA/AFL singles + SGMs.
#
# Same suppression rule: caller skips the ladder maintenance alert when
# any MBL violation was detected, since the Critical alert covers the
# same data and double-pinging across two channels is noise.

def _format_sports_tip_header(tip) -> str:
    """One-line tip identifier for sports ladder/MBL alerts."""
    sgm_tag = " [SGM]" if getattr(tip, "is_sgm", False) else ""
    sport = (getattr(tip, "sport", "") or "?").upper()
    event = getattr(tip, "event", "") or "?"
    tipster = getattr(tip, "tipster", "") or "?"
    return (
        f"<b>Tipster:</b> {_escape_html(tipster)}{sgm_tag}\n"
        f"<b>Sport:</b> {sport}\n"
        f"<b>Event:</b> {_escape_html(event)}"
    )


def notify_sports_ladder_maintenance(tip, attempts) -> bool:
    """Maintenance channel alert for sports stake-ladder rejections.

    `attempts` is a list of dicts with keys: bookie, session_id,
    stake_rejected, error. Same shape as racing's `result["ladder_attempts"]`.

    Caller is responsible for suppressing this when an MBL violation was
    also detected on the same tip — the Critical MBL alert already covers
    the same rejections.
    """
    by_session: dict = {}
    for a in attempts:
        key = (a.get("bookie", "?"), a.get("session_id", "?"))
        by_session.setdefault(key, []).append(a.get("stake_rejected", 0))

    lines = []
    for (bookie, sid), stakes in by_session.items():
        stakes_sorted = sorted(stakes, reverse=True)
        stakes_str = " -> ".join(f"${s:.0f}" for s in stakes_sorted)
        lines.append(
            f"  {_session_label(sid, bookie)}: rejected {stakes_str}"
        )
    detail_str = "\n".join(lines) or "  (no detail captured)"

    text = (
        f"<b>📉 LADDER ACTIVITY (sports)</b>\n"
        f"{_format_sports_tip_header(tip)}\n"
        f"<b>Stake-too-high rejections:</b>\n"
        f"<pre>{_escape_html(detail_str)}</pre>"
    )
    return _send_maintenance(text)


def notify_sports_mbl_violation(tip, details) -> bool:
    """Maintenance channel alert: the BOOKIE capped our stake below our cap.

    Triggered when a bookie rejects a stake at or below the liability cap we
    sized to — i.e. its own max-bet limit is lower than our configured cap
    (e.g. sportsbet's automated in-play max, 2026-06-11 Vassell PRA 19.5:
    $400 config cap, ~$60-80 bookie cap, code 538 down the whole ladder).
    That is NOT a violation of OUR cap, so v5.52 (Wilson) relabelled it
    'BOOKIE STAKE CAP' and downgraded it Critical -> Maintenance. Label +
    severity ONLY: the _should_alert_mbl_violation gate, call sites, and
    placement logic are unchanged, and the genuine-shortfall action alert
    still fires separately via notify_tip_unfilled_with_placements (manual).

    `details` is a list of dicts with keys: bookie, session_id, stake_tried,
    mbl_max, liability_cap, odds, error, market.
    """
    lines = []
    for d in details:
        lines.append(
            f"  {d.get('bookie','?')}:{d.get('session_id','?')} "
            f"tried ${d.get('stake_tried', 0):.2f} "
            f"(cap ${d.get('liability_cap', 0):.0f} -> max stake "
            f"${d.get('mbl_max', 0):.2f} @ odds {d.get('odds', '?')}, "
            f"market={d.get('market', '?')}): {(d.get('error', '') or '')[:120]}"
        )
    detail_str = "\n".join(lines) or "  (no detail captured)"

    text = (
        f"<b>⚠️ BOOKIE STAKE CAP (sports)</b>\n"
        f"{_format_sports_tip_header(tip)}\n"
        f"Bookie max-bet limit below our configured cap — bet underfilled.\n"
        f"<pre>{_escape_html(detail_str)}</pre>"
    )
    return _send_maintenance(text)


def notify_tiptitans_unfilled(
    tip_id, parsed, intended_stake, placed_stake, unfilled,
    failures, below_floor, tipster_odds, bookie_quotes=None,
) -> bool:
    """Unfilled stake alert - goes to Manual Bets (#1).

    `bookie_quotes` (optional) shows the price-shop snapshot inline so the
    user can see what was available without log lookup.
    """
    saddle_str = f"{parsed.get('saddle')}. " if parsed.get("saddle") else ""
    failure_lines = []
    for f in failures[-3:]:  # last 3 failures
        err = (f.get("error") or "")[:160]
        failure_lines.append(
            f"  {f.get('bookie', '?')} (s{f.get('session_id', '?')}) "
            f"${f.get('stake_tried', 0):.0f}: {err}"
        )
    failures_str = "\n".join(failure_lines) or "  (no failure details)"

    reason = ""
    if below_floor:
        reason = f"\n<b>⚠️ All bookies below 90% of tipster odds ({tipster_odds})</b>"

    quotes_block = _format_bookie_quotes(bookie_quotes, tipster_odds=tipster_odds)

    title = "BET UNFILLED" if placed_stake > 0 else "BET FAILED"
    text = (
        f"<b>🏇 TIP TITANS {title}</b>\n"
        f"<b>Tip ID:</b> {tip_id}\n"
        f"<b>Titan:</b> {_escape_html(parsed['titan'])}\n"
        f"<b>Track:</b> {_escape_html(parsed['track'])} R{parsed['race_num']}\n"
        f"<b>Runner:</b> {saddle_str}{_escape_html(parsed['runner'])}\n"
        f"<b>Market:</b> {parsed['market'].capitalize()}"
        f"{reason}\n"
        f"<b>Placed:</b> ${placed_stake:.2f} of ${intended_stake:.2f}\n"
        f"<b>Manual remainder:</b> ${unfilled:.2f}\n"
        f"<b>Recent failures:</b>\n<pre>{_escape_html(failures_str)}</pre>"
        f"{quotes_block}"
    )
    return _send_manual(text)


def notify_tiptitans_manual_alert(
    tip_id, titan, event, title, bet_type, units, odds, reason,
    bookie_quotes=None,
) -> bool:
    """Any Tip Titans tip that isn't auto-placeable - goes to Manual Bets (#1).

    `bookie_quotes` is an optional list of {bookie, odds, runner_match, status}
    surfaced from the price-shop. Renders a Bookie Odds block so the user can
    see exactly what each bookie offered without having to check the log.
    Status decorators help spot WHY each bookie was rejected:
      - placeable: within tolerance, would have been used
      - below_floor: <90% of tipster's odds
      - above_ceiling: >150% of tipster's, wrong-horse risk
    """
    quotes_block = _format_bookie_quotes(bookie_quotes, tipster_odds=odds)

    text = (
        f"<b>🏇 TIP TITANS MANUAL</b>\n"
        f"<b>Tip ID:</b> {tip_id}\n"
        f"<b>Titan:</b> {_escape_html(titan)}\n"
        f"<b>Event:</b> {_escape_html(event)}\n"
        f"<b>Selection:</b> {_escape_html(title)}\n"
        f"<b>Market:</b> {_escape_html(bet_type)} "
        f"@ {odds} for {units}u\n"
        f"<b>Reason:</b> {_escape_html(reason)}"
        f"{quotes_block}"
    )
    return _send_manual(text)


def _format_bookie_quotes(quotes, tipster_odds=None) -> str:
    """Render the price-shop result as a Telegram Bookie Odds block.

    Returns empty string when no quotes (so callers can unconditionally
    concat). Sorts by odds desc so highest price comes first. Tags each
    line with a status indicator so the failure reason is self-evident:

      Bookie Odds (tipster: 12.0):
        Bet365      $13.0  [BELOW FLOOR]
        Sportsbet   $11.0  [BELOW FLOOR]
        Pointsbet   $10.5  [BELOW FLOOR]
    """
    if not quotes:
        return ""
    # Sort highest odds first; None odds at bottom
    sorted_quotes = sorted(
        quotes,
        key=lambda q: (q.get("odds") is None, -(q.get("odds") or 0)),
    )
    lines = []
    for q in sorted_quotes:
        bk = q.get("bookie", "?")
        o = q.get("odds")
        status = q.get("status", "")
        odds_str = f"${o}" if o is not None else "—"
        status_tag = ""
        if status == "below_floor":
            status_tag = " [BELOW FLOOR]"
        elif status == "above_ceiling":
            status_tag = " [ABOVE CEILING]"
        elif status == "placeable":
            status_tag = " [OK]"
        lines.append(f"  {bk:<11}{odds_str}{status_tag}")
    quotes_str = "\n".join(lines)
    header = "Bookie Odds:"
    if tipster_odds is not None:
        header = f"Bookie Odds (tipster: {tipster_odds}):"
    return f"\n<b>{header}</b>\n<pre>{_escape_html(quotes_str)}</pre>"
