"""
HyperBot API client.
Wraps the Imperial Wealth HyperBot API for session management,
price checking, and bet placement.

v4.2 (2026-05-16): /v3/price + /v3/balance migration. price_check_sports
and price_check_racing now route through /v3/price async; get_balance is
a URL swap to /v3/balance (which is sync per docs). Soup confirmed all
v2 endpoints moving to v3. The migration adapter is internal so callers
in main.py, racing_placer.py, and test_simulate.py do not change. Sports
markets are re-wrapped under {selections: [...]} per market name to
match the v2 contract; racing shape is already aligned.

v4.1 (2026-05-15): /v3/price_check + /v3/place_bet + /v3/start_session
+ /v3/restart_session migration. See tipbot_v4_1_spec.md for context.
"""

import time
import requests
import logging
from datetime import datetime
from config import HYPERBOT_API_KEY, HYPERBOT_BASE_URL

log = logging.getLogger(__name__)

# Retry config for transient upstream failures (5xx gateway errors, read timeout,
# connection errors). HyperBot occasionally drops ~60-120s before recovering;
# 3 attempts with backoff covers that window without pounding the API.
# 500 added 2026-05-29 after observing transient `/v3/price: 500 Server Error`
# during HyperBot's active deploy window. Safe to retry alongside 502/503/504
# because retries are caller-controlled via max_attempts (place_bet still uses
# max_attempts=1 to prevent double-bet on slow rejections).
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = [2, 5, 10]  # seconds between attempts
_RETRIABLE_STATUS = {500, 502, 503, 504}

# v3 async poll config. /api/bet_status is a sync universal poll endpoint
# that returns immediately. Use a staircase backoff: poll fast early to
# catch the typical 1-2s placement case at t=0.5s or t=1.5s, then back
# off to 2s for stragglers. Empirically a clean Sportsbet placement
# resolves server-side in ~1-2s; a flat 2s interval was catching it at
# t=4-6s instead of t=1.5s (observed 2026-05-16 AFL Fremantle ML which
# took 6.4s end-to-end on what should have been a ~1.5s placement).
# Each entry is the sleep duration BEFORE that poll attempt. The last
# value repeats for any polls beyond the schedule length.
_V3_POLL_SCHEDULE = [0.5, 1.0, 1.0, 1.5, 2.0]
_V3_POLL_GRACE_SEC = 5.0
# Fallback budget if the server doesn't return submitted_at/timeout_at on
# the initial response. Should never fire in practice.
_V3_POLL_FALLBACK_BUDGET_SEC = 60.0


class HyperBotClient:
    def __init__(self, api_key: str = HYPERBOT_API_KEY, base_url: str = HYPERBOT_BASE_URL):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict, timeout: int = 30,
              max_attempts: int | None = None) -> dict:
        url = f"{self.base_url}{path}"
        last_err = None
        # Caller can override _RETRY_ATTEMPTS for time-sensitive endpoints
        # (price_check_racing uses max_attempts=1 to bound dead-session waits
        # to the per-request timeout instead of timeout x retries + backoffs).
        attempts = max_attempts if max_attempts is not None else _RETRY_ATTEMPTS

        # 504/timeout means the request didn't reach the backend placement
        # logic IN THEORY — but in practice HyperBot can place the bet on
        # the bookie successfully and STILL take 10+ seconds to respond,
        # blowing through tipbot's read timeout. Retrying a place_bet
        # under those conditions doubles the stake on the bookie account.
        # Erasmus 2026-05-03 regression: $125 retry on slow rejection
        # caused $525 placed on a $400 tip. All place_bet methods pass
        # max_attempts=1 to bypass retry. Same rule applies under v3:
        # the v3 initial POST returns a cid quickly, but if it fails
        # transiently we must NOT re-fire (could double-bet).
        for attempt in range(attempts):
            try:
                resp = requests.post(url, headers=self.headers, json=payload, timeout=timeout)

                # Retriable server errors: back off and try again
                if resp.status_code in _RETRIABLE_STATUS and attempt < attempts - 1:
                    wait = _RETRY_BACKOFF[attempt]
                    log.warning(
                        f"{path} returned {resp.status_code}, retrying in {wait}s "
                        f"(attempt {attempt + 2}/{attempts})"
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout as e:
                last_err = "Request timed out"
                if attempt < attempts - 1:
                    wait = _RETRY_BACKOFF[attempt]
                    log.warning(
                        f"Timeout on {path}, retrying in {wait}s "
                        f"(attempt {attempt + 2}/{attempts})"
                    )
                    time.sleep(wait)
                    continue
                log.error(f"Timeout on {path} after {attempts} attempts")

            except requests.exceptions.ConnectionError as e:
                last_err = str(e)
                if attempt < attempts - 1:
                    wait = _RETRY_BACKOFF[attempt]
                    log.warning(
                        f"Connection error on {path}, retrying in {wait}s "
                        f"(attempt {attempt + 2}/{attempts})"
                    )
                    time.sleep(wait)
                    continue
                log.error(f"Connection error on {path} after {attempts} attempts: {e}")

            except requests.exceptions.RequestException as e:
                # Non-retriable HTTP error (4xx or unhandled) — fail immediately
                last_err = str(e)
                log.error(f"Request error on {path}: {e}")
                break

        return {"success": False, "error": last_err or "Request failed"}

    # ── v3 Async Helper ─────────────────────────────────────────────

    def _post_v3_async(self, path: str, payload: dict,
                       place_timeout: int = 15,
                       initial_post_max_attempts: int = 1) -> dict:
        """Fire a v3 async endpoint, poll /api/bet_status until all cids
        resolve or budget elapses. Returns the final /api/bet_status
        response with `statuses[]`, or an error envelope from _post if
        the initial POST failed entirely.

        Handles both v3 response shapes:
          - /v3/place_bet: single correlation_id at the root
          - /v3/price_check: queries[] with one cid per session

        initial_post_max_attempts defaults to 1 (Erasmus rule: retrying
        a v3 place_bet on transient failure could double-bet because the
        cid may already be in flight at the bookie). Idempotent callers
        (price_check_*) pass higher values to ride out transient 5xx /
        timeout errors during HyperBot deploy windows. 2026-05-27/28
        observed multiple 502s + timeouts on /v3/price_check and 500 on
        /v3/price; recovery was always under 2s once the next attempt
        fired, so retries here genuinely save tips from manual fallback.

        CALLER CONTRACT (Erasmus, 2026-05-31 LOW doc): max_attempts=1 only
        prevents a double POST WITHIN one call. It assumes the placement
        callers (place_single_sports_bet / place_sgm_bet / place_racing_bet)
        and their ladders NEVER re-invoke a place for the same stake on an
        ambiguous/uncertain outcome — that is enforced upstream by the
        slow-rejection guards (debit + blocklist, no retry/spill) and the
        ambiguous flag this function returns on a fast-failing POST. Do not
        add a retry loop around these place_* calls.
        """
        initial = self._post(
            path, payload, timeout=place_timeout,
            max_attempts=initial_post_max_attempts,
        )

        # If _post returned an error envelope, propagate as-is.
        # H24: for placement endpoints, a POST timeout is ambiguous — the
        # bet MAY have been accepted at the bookie before the connection
        # dropped. Tag with ambiguous=True so callers can trigger the
        # AMBIGUOUS_OUTCOME path.
        if initial.get("success") is False and not initial.get("queries") \
                and not initial.get("correlation_id"):
            if "/place_bet" in path:
                # H24: a POST timeout / connection drop is ambiguous — the bet
                # MAY have been accepted at the bookie before the connection
                # dropped — so tag it ambiguous for the AMBIGUOUS_OUTCOME path.
                # Fix H (2026-06-01): but a DEFINITIVE HTTP client rejection
                # (403 Forbidden / 401 Unauthorized / 400 Bad Request) means the
                # server REFUSED the request before any slip was submitted -> the
                # bet was NOT placed -> it is NOT ambiguous. Tagging these
                # ambiguous made the slow/fast-ambiguous guards blind-debit +
                # blocklist an auth/validation failure instead of spilling to
                # another session. A 5xx stays ambiguous (may have landed).
                _err_l = str(initial.get("error", "")).lower()
                _definitive_reject = any(
                    p in _err_l for p in (
                        "403 client error", "forbidden",
                        "401 client error", "unauthorized",
                        "400 client error", "bad request",
                    )
                )
                if not _definitive_reject:
                    return {**initial, "ambiguous": True}
            return initial

        cids, budget_sec = self._extract_cids_and_budget(initial)
        if not cids:
            # Sync response (no cid). Return as-is, caller can interpret.
            return initial

        # Poll until all cids non-pending or budget+grace elapses.
        # Use a staircase interval (0.5 -> 1.0 -> 1.0 -> 1.5 -> 2.0+) to
        # catch the common case (placement resolves in 1-2s) without
        # pounding the API when bookies are actually slow.
        start = time.time()
        last_poll: dict = {}
        poll_count = 0
        while time.time() - start < budget_sec + _V3_POLL_GRACE_SEC:
            interval = _V3_POLL_SCHEDULE[min(poll_count, len(_V3_POLL_SCHEDULE) - 1)]
            time.sleep(interval)
            poll_count += 1
            last_poll = self._post(
                "/api/bet_status",
                {"correlation_ids": cids},
                timeout=10,
                max_attempts=1,
            )
            statuses = last_poll.get("statuses") or []
            if statuses and all(s.get("status") != "pending" for s in statuses):
                break

        # H23: if the loop exited due to a failed last poll (not a clean
        # terminal status), attach the cids so the critical alert has them
        # for manual reconciliation.
        if last_poll.get("success") is False and "statuses" not in last_poll:
            return {**last_poll, "correlation_ids": cids}

        return last_poll

    def _extract_cids_and_budget(self, initial: dict) -> tuple[list[str], float]:
        """Pull cid(s) and a wall-clock poll budget from a v3 initial
        response. Returns ([], 0) if no cids found (sync response)."""
        cids: list[str] = []
        budget_sec = _V3_POLL_FALLBACK_BUDGET_SEC

        # /v3/place_bet shape: root-level correlation_id
        if initial.get("correlation_id"):
            cids.append(initial["correlation_id"])
            delta = self._compute_poll_budget(
                initial.get("submitted_at"), initial.get("timeout_at")
            )
            if delta is not None:
                budget_sec = delta
            return cids, budget_sec

        # /v3/price_check shape: queries[]
        queries = initial.get("queries") or []
        max_delta = 0.0
        for q in queries:
            cid = q.get("correlation_id")
            if cid:
                cids.append(cid)
                # L7: only factor this query's timestamps into the budget
                # when it has a valid cid. Queries without a cid never
                # produce a status to poll, so including their (potentially
                # large) timeout_at would over-inflate the poll window.
                delta = self._compute_poll_budget(
                    q.get("submitted_at"), q.get("timeout_at")
                )
                if delta is not None and delta > max_delta:
                    max_delta = delta
        if max_delta > 0:
            budget_sec = max_delta
        return cids, budget_sec

    @staticmethod
    def _compute_poll_budget(submitted_at: str | None,
                              timeout_at: str | None) -> float | None:
        """Compute budget from server-side timestamp delta. Both
        timestamps are naive ISO strings; using the delta (not absolute
        time) avoids any timezone confusion since both are in the same
        server clock. Returns None if either timestamp is missing or
        malformed."""
        if not submitted_at or not timeout_at:
            return None
        try:
            sub = datetime.fromisoformat(submitted_at)
            tim = datetime.fromisoformat(timeout_at)
            delta = (tim - sub).total_seconds()
            # Floor at 10s to give some breathing room even if server
            # returns a nonsense small delta.
            return max(delta, 10.0)
        except (ValueError, TypeError):
            return None

    def _pivot_v3_price_check(self, poll_result: dict) -> dict:
        """Pivot v3 per-cid prices[] back into v2 aggregated selections[]
        shape so existing callers (main.py:_odds_by_bookie_from_bulk,
        stat_fallback) don't change.

        v3 per-cid result.prices[] (sports):
          {proposition_id, market, player, selection, line, price}
        v3 per-cid result.prices[] (racing):
          {runner, price}

        v2 expected shape:
          {"success": True,
           "selections": [
             {market, player, selection, line, prices: [
               {session_id, siteName, price, proposition_id}
             ]}
           ],
           "errors": [{session_id, siteName, error}] or None}
        """
        statuses = poll_result.get("statuses") or []
        selections_map: dict = {}
        errors: list = []

        for status in statuses:
            state = status.get("status")
            result = status.get("result") or {}
            sid = status.get("session_id")
            bookie = status.get("bookie")

            if state != "completed" or not result.get("success"):
                # Failed/timed-out cid. Capture for caller diagnostics.
                err_msg = f"state={state}"
                if isinstance(result, dict) and result.get("error"):
                    err_msg += f": {result.get('error')}"
                errors.append({
                    "session_id": sid,
                    "siteName": bookie,
                    "error": err_msg,
                })
                continue

            for entry in result.get("prices") or []:
                # Racing entries have {runner, price} only. Sports entries
                # have {proposition_id, market, player, selection, line, price}.
                if "runner" in entry and "market" not in entry:
                    market = "win"
                    player = ""
                    selection = entry.get("runner") or ""
                    line_raw = ""
                else:
                    market = entry.get("market") or ""
                    player = entry.get("player") or ""
                    selection = entry.get("selection") or ""
                    line_raw = entry.get("line", "")

                # Normalise line to a stable key so 32.5/"32.5" collapse together
                line_key = "" if line_raw == "" or line_raw is None else str(line_raw)
                key = (market, player, selection, line_key)

                if key not in selections_map:
                    selections_map[key] = {
                        "market": market,
                        "player": player,
                        "selection": selection,
                        "line": line_raw,
                        "prices": [],
                    }
                selections_map[key]["prices"].append({
                    "session_id": sid,
                    "siteName": bookie,
                    "price": entry.get("price"),
                    "proposition_id": entry.get("proposition_id"),
                })

        # H21: if every cid failed and no selections were built, return a
        # failure envelope rather than success with an empty selections list.
        if not selections_map:
            return {
                "success": False,
                "error": f"{len(errors)} session(s) failed: {errors[0]['error'] if errors else 'unknown'}",
                "errors": errors,
            }

        return {
            "success": True,
            "selections": list(selections_map.values()),
            "errors": errors if errors else None,
        }

    def _unwrap_single_status(self, poll_result: dict) -> dict:
        """Unwrap a single-cid v3 poll result (place_bet style) back to
        the v2 success/error shape. Used by all place_bet methods plus
        start/restart_session.

        Returns:
          - completed state: result body (byte-identical to v2 success).
            For racing, HyperBot v3 omits `bookie` and sometimes
            `session_id` inside result (observed 2026-05-16 Neds racing
            placement). Those fields are backfilled from the cid envelope
            so callers reading result["bookie"] still get the right value
            (main.py's "PLACED: <bet_id> on <bookie>" log depends on it).
          - timeout state: {"success": False, "error": "cid timeout ...",
                            "ambiguous": True, "correlation_id": cid}
                           — racing_placer's >5s elapsed rule will then
                           trigger AMBIGUOUS_OUTCOME (debit + blocklist
                           + critical alert) because the wall-clock
                           elapsed on a cid timeout is ~5min.
          - other failure: {"success": False, "error": "state=...",
                            "correlation_id": cid}
        """
        statuses = poll_result.get("statuses") or []
        if not statuses:
            if poll_result.get("success") is False:
                return poll_result
            return {"success": False, "error": "No statuses returned from /api/bet_status"}

        status = statuses[0]
        state = status.get("status")
        cid = status.get("correlation_id")

        if state == "completed":
            result = status.get("result")
            if not isinstance(result, dict):
                log.warning(f"v3 placement: completed cid but no result body (cid={cid})")
                return {"success": False, "error": "completed cid but no result"}
            # M26: an empty dict result is equally unusable — the server
            # signalled completion but gave us nothing to act on.
            if not result:
                log.warning(f"v3 placement: completed cid but empty result body (cid={cid})")
                return {"success": False, "error": f"completed cid but empty result body (cid={cid})"}

            # Backfill bookie + session_id from the cid envelope if the
            # result body omits them. HyperBot v3/place_bet for RACING
            # returns bookie=None inside result (sports does include it).
            # Without this fallback, main.py's "PLACED: <bet_id> on
            # <bookie>" log line would show bookie=None for racing
            # placements. Observed 2026-05-16 on a $1 Neds placement
            # (Bunbury R6 Golden Lode @ 1.55, bet_id=019e302e-...).
            if result.get("success"):
                envelope_bookie = status.get("bookie")
                if envelope_bookie and not result.get("bookie"):
                    log.info(
                        f"v3 placement: backfilled bookie={envelope_bookie} "
                        f"from envelope (cid={cid}) - HB omitted from result"
                    )
                    result["bookie"] = envelope_bookie
                envelope_sid = status.get("session_id")
                if envelope_sid is not None and not result.get("session_id"):
                    result["session_id"] = envelope_sid
                # Canonical INFO line for grep-based diagnostics — captures
                # everything the caller will see, so we can compare logs
                # against actual bookie dashboards on questionable bets.
                log.info(
                    f"v3 placement OK: cid={cid} bet_id={result.get('bet_id')} "
                    f"odds={result.get('odds')} stake={result.get('stake')} "
                    f"bookie={result.get('bookie')} session={result.get('session_id')}"
                )
            else:
                # Bookie said no (e.g. stake too high, race closed). Not
                # ambiguous - we know it didn't place.
                log.info(
                    f"v3 placement REJECTED: cid={cid} "
                    f"bookie={status.get('bookie')} session={status.get('session_id')} "
                    f"error={result.get('error', 'unknown')}"
                )
            return result

        if state == "timeout":
            # Erasmus class under async. The bet MAY have placed at the
            # bookie even though we never got the cid resolution. Caller
            # treats as AMBIGUOUS_OUTCOME and reconciles via the existing
            # debit + blocklist + critical alert path.
            log.warning(
                f"v3 placement AMBIGUOUS: cid={cid} TIMEOUT - bet status unknown, "
                f"bookie={status.get('bookie')} session={status.get('session_id')}"
            )
            return {
                "success": False,
                "error": f"cid timeout - bet status unknown (cid={cid})",
                "ambiguous": True,
                "correlation_id": cid,
            }

        # H22: treat pending (poll budget exhausted without resolution) the
        # same as timeout — the bet MAY have placed, so mark ambiguous.
        if state == "pending":
            log.warning(
                f"v3 placement AMBIGUOUS: cid={cid} PENDING (budget exhausted) - "
                f"bet status unknown, bookie={status.get('bookie')} "
                f"session={status.get('session_id')}"
            )
            return {
                "success": False,
                "error": f"cid pending - poll budget exhausted, bet status unknown (cid={cid})",
                "ambiguous": True,
                "correlation_id": cid,
            }

        # Any other terminal state (unknown, etc.)
        log.warning(
            f"v3 placement: unexpected state={state} cid={cid} "
            f"bookie={status.get('bookie')} session={status.get('session_id')}"
        )
        return {
            "success": False,
            "error": f"cid state={state}",
            "correlation_id": cid,
        }

    def _unwrap_v3_price_sports(self, poll_result: dict) -> dict:
        """Unwrap /v3/price (sports singles) result back to the v2/price
        shape callers expect.

        v3/price returns (inside statuses[0].result):
          {success, event, sport, markets: {head_to_head: [...], line: [...],
           total_points: [...], player_points: [...], ...}, ...}
        where each market value is a flat list of {selection, odds, line,
        proposition_id, player?, direction?, ...}.

        v2/price callers in main.py read it like:
          markets_data = resp.get("markets", {})
          market_data = markets_data.get("player_points") or {}
          selections = market_data.get("selections", [])
          for s in selections: s.get("player"), s.get("odds"), s.get("line"), ...

        So we re-wrap each market's flat list under a {selections: [...]}
        dict to match the v2 caller contract. No data is dropped, just
        the nesting depth normalised.
        """
        statuses = poll_result.get("statuses") or []
        if not statuses:
            if poll_result.get("success") is False:
                return poll_result
            return {"success": False, "error": "No statuses returned"}

        status = statuses[0]
        state = status.get("status")
        if state == "timeout":
            return {
                "success": False,
                "error": f"cid timeout - price check unknown (cid={status.get('correlation_id')})",
                "transient": True,
            }
        if state != "completed":
            return {
                "success": False,
                "error": f"cid state={state}",
                "correlation_id": status.get("correlation_id"),
            }

        result = status.get("result") or {}
        if not result.get("success"):
            return result

        # Re-wrap markets to match v2 contract
        v3_markets = result.get("markets") or {}
        v2_markets: dict = {}
        for market_name, entries in v3_markets.items():
            if isinstance(entries, list):
                v2_markets[market_name] = {"selections": entries}
            elif isinstance(entries, dict):
                # Defensive: if v3 ever nests by sub-market, pass through
                v2_markets[market_name] = entries
            else:
                v2_markets[market_name] = {"selections": []}

        # Build a return dict that looks v2-ish for callers but keeps
        # everything else from result intact
        out = dict(result)
        out["markets"] = v2_markets
        return out

    def _unwrap_v3_price_racing(self, poll_result: dict) -> dict:
        """Unwrap /v3/price (racing singles) result. Shape already
        matches v2 contract: callers read resp.get('runners', []) and
        each runner has {runner, win, place, number} — exactly what v3
        returns. Just handle the cid envelope."""
        statuses = poll_result.get("statuses") or []
        if not statuses:
            if poll_result.get("success") is False:
                return poll_result
            return {"success": False, "error": "No statuses returned"}

        status = statuses[0]
        state = status.get("status")
        if state == "timeout":
            return {
                "success": False,
                "error": f"cid timeout - racing price check unknown (cid={status.get('correlation_id')})",
                "transient": True,
            }
        if state != "completed":
            return {
                "success": False,
                "error": f"cid state={state}",
                "correlation_id": status.get("correlation_id"),
            }

        result = status.get("result")
        # M27: empty or None result on a completed cid is a server-side
        # anomaly — return a structured failure rather than an empty dict
        # that callers would silently treat as "no runners".
        if not result:
            cid = status.get("correlation_id")
            log.warning(f"v3 racing price: completed cid but empty result body (cid={cid})")
            return {"success": False, "error": f"completed cid but empty result body (cid={cid})"}
        return result

    # ── Session Management ──────────────────────────────────────────

    def get_sessions(self) -> list[dict]:
        """Get all active sessions.

        On API failure returns []. Use `get_sessions_or_none()` instead when
        the caller needs to distinguish "API said no sessions" from "API
        unreachable / errored" (e.g. watchdog must not interpret a transient
        outage as every session dropping).

        /api/session_ids stays sync (universal list endpoint, no v3
        version deployed, returns 404 on /v3/session_ids per probe).
        """
        data = self._post("/api/session_ids", {})
        sessions = data.get("sessions", [])
        return [s for s in sessions if s.get("active")]

    def get_sessions_or_none(self) -> list[dict] | None:
        """Like get_sessions() but returns None on API failure.

        Watchdog needs this distinction. 2026-05-01 16:38: HyperBot was
        unreachable for 30 minutes during a platform-wide outage, every
        /api/session_ids call timed out, _post returned {"success": false}.
        get_sessions() collapsed that to [] which the watchdog read as
        "every session dropped" and fired 9 Critical Telegram alerts.
        With this method, the watchdog can keep its tracked-session set
        intact and just retry next cycle.
        """
        data = self._post("/api/session_ids", {})
        if data.get("success") is False or "sessions" not in data:
            return None
        sessions = data.get("sessions") or []
        return [s for s in sessions if s.get("active")]

    def get_pending_bets(self, account_id: str) -> dict:
        """GET /api/pending_bets?account_id=<uuid> — real-time UNSETTLED bets
        for an account. Sync endpoint (like /api/session_ids). Returns the raw
        body {"bets": [...], "count": N} on success, or
        {"success": False, "error": ...} on failure.

        This is the reconciliation source for ambiguous outcomes: after a slow
        rejection we can't tell from the response whether the bet landed, so we
        check whether it appears here (result=null = pending/unsettled). NOTE:
        keyed by account_id (NOT session_id) — get it from the session object
        returned by get_sessions() (field 'account_id'). /v3/transactions only
        returns SETTLED bets, so it can't do this (confirmed 2026-05-30).

        Each bet: {dt(ISO UTC), id, bet(text), odds, event, sport, stake,
        result(null=pending), bet_type, account_id, session_id(often null),
        bookie_bet_id}. Idempotent read — normal retries are safe (no Erasmus
        risk; this never places).
        """
        if not account_id:
            return {"success": False, "error": "no account_id"}
        url = f"{self.base_url}/api/pending_bets"
        last_err = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = requests.get(
                    url, headers=self.headers,
                    params={"account_id": account_id}, timeout=15,
                )
                if resp.status_code in _RETRIABLE_STATUS and attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF[attempt])
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                last_err = str(e)
                if attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF[attempt])
                    continue
                log.error(f"get_pending_bets failed for {account_id}: {e}")
        return {"success": False, "error": last_err or "Request failed"}

    def get_transactions(self, session_id: str, days_back: int = 14,
                         limit: int = 200) -> list:
        """POST /v3/transactions (async — 202 + cid, poll /api/bet_status) — returns
        SETTLED bets (+ deposits/withdrawals) for a session. Used by the bet-record
        feed to AUTO-RESULT placed bets: each settled bet of type 'bet' has
        {id (= the bookie bet_id, matches the ledger's bet_id), status
        ('won'|'lost'|'void'|...), pnl (net $ profit/loss, signed), winnings,
        stake, odds, bet_type, selections[]}. Returns the transactions list, or []
        on failure. Read-only / idempotent (never places — safe to retry/poll)."""
        if not session_id:
            return []
        try:
            res = self._post_v3_async(
                "/v3/transactions",
                {"session_id": str(session_id), "days_back": days_back, "limit": limit},
                # v5.69 (i3): /v3/transactions is read-only/idempotent (never
                # places), so ride out a transient 5xx/timeout on the initial
                # POST like the price-check callers do — the default of 1 left
                # auto-results blank for a session on a single transient error.
                initial_post_max_attempts=_RETRY_ATTEMPTS,
            )
            for s in (res or {}).get("statuses") or []:
                result = s.get("result") or {}
                if isinstance(result, dict) and isinstance(result.get("transactions"), list):
                    return result["transactions"]
            return []
        except Exception as e:
            log.error(f"get_transactions failed for {session_id}: {e}")
            return []

    def get_balance(self, session_id: str) -> dict:
        """v4.2: migrated to /v3/balance. Stays SYNC (per docs: 'Unlike
        other v3 endpoints, Get Balance is synchronous - it does not
        return 202 + correlation_id'). Drop-in URL swap, no shape change.

        /v2/balance had Deprecation + Sunset headers added 2026-05-16
        pointing at /v3/balance, sunset Sat 17 May. No tipbot callers in
        practice but migrating wrapper for completeness."""
        return self._post("/v3/balance", {"session_id": session_id})

    def start_session(self, session_id: str) -> dict:
        """v4.1: routes through /v3/start_session. Endpoint may be sync
        or async (probe returned 403 on garbage session_id so we can't
        tell shape). Route via _post_v3_async which falls through to a
        sync return if no cid is in the response."""
        result = self._post_v3_async(
            "/v3/start_session", {"session_id": session_id}
        )
        # If async (statuses[] present), unwrap to v2-ish shape. If sync,
        # the helper returned the immediate body, pass through.
        if isinstance(result, dict) and "statuses" in result:
            return self._unwrap_single_status(result)
        return result

    def restart_session(self, session_id: str) -> dict:
        """v4.1: routes through /v3/restart_session. Same async/sync
        handling as start_session."""
        result = self._post_v3_async(
            "/v3/restart_session", {"session_id": session_id}
        )
        if isinstance(result, dict) and "statuses" in result:
            return self._unwrap_single_status(result)
        return result

    # ── Price Check ─────────────────────────────────────────────────

    def price_check_sports(self, session_id: str, sport: str, event: str,
                           markets_filter: list[str] = None) -> dict:
        """Single-session sports price check.

        v4.2: migrated to /v3/price async (deprecation headers appeared
        2026-05-16 with sunset Sat 17 May). v3/price returns a flat
        markets dict at result.markets; the adapter re-wraps each market's
        entry list under {selections: [...]} to match the v2 contract
        that callers in main.py read (stat fallback + line auto-adjust).

        Used by main.py for stat-fallback discovery and within-1.0 line
        auto-adjust. Returns the {markets: {market_name: {selections: [...]}}}
        shape these callers expect."""
        payload = {
            "session_id": session_id,
            "category": "sports",
            "sport": sport,
            "event": event,
        }
        if markets_filter:
            payload["markets_filter"] = markets_filter
        # Idempotent read, safe to retry on transient 5xx / timeout.
        poll_result = self._post_v3_async(
            "/v3/price", payload,
            initial_post_max_attempts=_RETRY_ATTEMPTS,
        )
        if poll_result.get("success") is False and "statuses" not in poll_result:
            return poll_result
        return self._unwrap_v3_price_sports(poll_result)

    def price_check_multi_session(self, session_ids: list[str], sport: str,
                                  event: str, player: str = None) -> dict:
        """Bulk multi-session sports price check.

        v4.1: migrated to /v3/price_check (announced deprecated, Sunset
        Sat 17 May 2026). The v3 endpoint returns N cids (one per session)
        each with a flat per-bookie prices[] array. The migration pivots
        these back into the v2 aggregated shape so callers in main.py
        (_bulk_price_check_player at 1264, SGM prop_id enrichment at 3232)
        plus stat_fallback don't change.
        """
        payload = {
            "session_ids": session_ids,
            "category": "sports",
            "sport": sport,
            "event": event,
        }
        if player:
            payload["player"] = player
        # Idempotent read, safe to retry on transient 5xx / timeout.
        poll_result = self._post_v3_async(
            "/v3/price_check", payload,
            initial_post_max_attempts=_RETRY_ATTEMPTS,
        )
        # If the initial POST failed entirely, propagate the error envelope
        if poll_result.get("success") is False and "statuses" not in poll_result:
            return poll_result
        return self._pivot_v3_price_check(poll_result)

    # ── Bet Placement ───────────────────────────────────────────────

    def place_single_sports_bet(
        self, session_id: str, sport: str, event: str,
        market: str, selection: str, stake: float,
        player: str = None, stat: str = None,
        line: float = None, target_odds: float = None,
        proposition_id: str = None,
    ) -> dict:
        """Place a single sports bet (NBA, AFL, etc).

        v4.1: migrated to /v3/place_bet. Cid resolves in ~1s for clean
        placements; 5-minute cid timeout if the bookie session hangs.
        On cid timeout, returns ambiguous-marked envelope which trips
        racing_placer's >5s AMBIGUOUS_OUTCOME path (debit + blocklist).

        2026-05-03: max_attempts=1 to prevent retry-driven overstaking.
        Same rule under v3: never re-fire the initial POST. See
        place_racing_bet docstring for the Erasmus regression that
        forced this. Same retry-unsafe class applies to sports.
        """
        payload = {
            "session_id": session_id,
            "category": "sports",
            "sport": sport,
            "event": event,
            "market": market,
            "stake": stake,
        }
        # Omit selection when empty — AFL thresholds (Wilson 2026-05-31) send
        # player + integer line with NO selection (the integer line means N+).
        # Every other single sets a non-empty selection, so this only affects
        # the threshold path.
        if selection:
            payload["selection"] = selection
        if player:
            payload["player"] = player
        if stat:
            payload["stat"] = stat
        if line is not None:
            payload["line"] = line
        if target_odds:
            payload["target_odds"] = target_odds
        # pick_own_line / pick_own_total carry no `line` field (the line is
        # baked into the selection text, e.g. "GWS GIANTS (+50.5)"), so the
        # proposition_id is how HyperBot disambiguates the exact rung.
        # Optional everywhere else; omitted when not supplied so existing
        # singles (which match on market+selection+line) are unchanged.
        if proposition_id:
            payload["proposition_id"] = proposition_id
        poll_result = self._post_v3_async("/v3/place_bet", payload)
        if poll_result.get("success") is False and "statuses" not in poll_result:
            return poll_result
        return self._unwrap_single_status(poll_result)

    def place_sgm_bet(
        self, session_id: str, sport: str, event: str,
        legs: list[dict], stake: float, target_odds: float = None,
        use_boost: bool = False,
    ) -> dict:
        """Place a same-game multi (SGM) bet.

        v4.1: migrated to /v3/place_bet. See place_single_sports_bet
        docstring for migration notes.

        2026-05-03: max_attempts=1 to prevent retry-driven overstaking.
        See place_racing_bet docstring.
        """
        payload = {
            "session_id": session_id,
            "category": "sports",
            "is_same_event_multi": True,
            "sport": sport,
            "event": event,
            "legs": legs,
            "stake": stake,
        }
        if target_odds:
            payload["target_odds"] = target_odds
        if use_boost:
            # HyperBot boost: set bet_type=promo and use_boost=true. Only
            # applies to SGMs and only where the account has a boost token
            # available (e.g. Sportsbet gives Adam Tran 2 per day). On failure
            # (no token, or boost doesn't apply), caller should retry without.
            payload["bet_type"] = "promo"
            payload["use_boost"] = True
        poll_result = self._post_v3_async("/v3/place_bet", payload)
        if poll_result.get("success") is False and "statuses" not in poll_result:
            return poll_result
        return self._unwrap_single_status(poll_result)

    # ── Racing ──────────────────────────────────────────────────────

    def price_check_racing(
        self, session_id: str, track: str, race_num: int,
        race_type: str = "(R)", date: str = None,
    ) -> dict:
        """
        Get win/place odds for all runners in a race.

        v4.2: migrated to /v3/price async (deprecation headers appeared
        2026-05-16 with sunset Sat 17 May). The v3 result body shape
        for racing is already aligned with v2 callers: they read
        resp.get('runners', []) with {runner, win, place, number} per
        entry, which is exactly what v3 returns. Adapter just handles
        the cid envelope.

        2026-05-03: HyperBot tightened the (then-v2) price endpoint to
        require `date`. Used to default to today server-side; now
        returns HTTP 400 with {"detail": "track, race_num, and date are
        required for racing"}. Defaulting to today's local date when
        caller passes None.

        Erasmus-safe: _post_v3_async sends max_attempts=1 on the initial
        POST. The /v3/price cid timeout is 30s (well under v2 retry
        budget that hit 70s on dead sessions), so the dead-bookie cost
        bound is naturally tighter under v3.
        """
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "session_id": session_id,
            "category": "racing",
            "track": track,
            "race_num": race_num,
            "race_type": race_type,
            "date": date,
        }
        # Idempotent read, safe to retry on transient 5xx / timeout.
        poll_result = self._post_v3_async(
            "/v3/price", payload,
            initial_post_max_attempts=_RETRY_ATTEMPTS,
        )
        if poll_result.get("success") is False and "statuses" not in poll_result:
            return poll_result
        return self._unwrap_v3_price_racing(poll_result)

    def place_racing_bet(
        self, session_id: str, track: str, race_num: int,
        runner: str, stake: float, market: str = "win",
        race_type: str = "(R)", date: str = None,
        target_odds: float = None, use_bonus_bet: bool = False,
    ) -> dict:
        """Place a fixed-odds Win or Place bet on a single runner.

        v4.1: migrated to /v3/place_bet. Same async cid + poll pattern
        as place_single_sports_bet. Cid timeout maps to ambiguous
        envelope which trips racing_placer's existing >5s AMBIGUOUS_OUTCOME
        handling (debit + blocklist + critical alert).

        2026-05-03: same date-required tightening as /v2/price (see
        price_check_racing docstring). Defaulting to today on None.

        2026-05-03 Erasmus regression: place_bet retry-on-timeout is
        UNSAFE. HyperBot can succeed at the bookie then take 10+ seconds
        to respond, blowing through tipbot's 30s read timeout. tipbot
        retries, HyperBot places a SECOND bet on Sportsbet which is
        correctly rejected ('stake too high' because original is in
        play). Net result: bet placed silently, tipbot thinks it failed,
        ladders down + spillover = overstaking. Forced max_attempts=1.
        Caller's stake ladder handles retries. Same rule under v3:
        the initial POST gets max_attempts=1 in _post_v3_async.
        """
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "session_id": session_id,
            "category": "racing",
            "stake": stake,
            "market": market,
            "track": track,
            "race_num": race_num,
            "race_type": race_type,
            "runner": runner,
            "date": date,
        }
        if target_odds:
            payload["target_odds"] = target_odds
        if use_bonus_bet:
            payload["use_bonus_bet"] = True
        poll_result = self._post_v3_async("/v3/place_bet", payload)
        if poll_result.get("success") is False and "statuses" not in poll_result:
            return poll_result
        return self._unwrap_single_status(poll_result)

    def place_betfair_bsp(
        self, session_id: str, track: str, race_num: int, runner: str,
        size: float, market: str = "win", bet_direction: str = "back",
        race_type: str = "(R)", date: str = None,
    ) -> dict:
        """Place a Betfair Exchange bet at the STARTING PRICE (BSP) on one runner.

        v5.93 (2026-07-03): HyperBot now takes Betfair back/lay + BSP straight from
        /v3/place_bet — set bsp=true, no price needed (matches at SP when the race
        jumps), and pick the runner by track + runner NAME (HB resolves the Betfair
        market/selection — no IDs to look up). `size` is the STAKE for a back (or the
        liability for a lay). `market` = 'win' | 'place' (Betfair has separate BSP
        markets). Erasmus-safe: max_attempts=1 on the initial POST (same as
        place_racing_bet) — a BSP order can't be double-submitted safely.
        """
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "session_id": session_id,
            "category": "betfair",
            "bet_direction": bet_direction,
            "bsp": True,
            "market": market,
            "track": track,
            "race_num": race_num,
            "race_type": race_type,
            "runner": runner,
            "size": size,
            "date": date,
        }
        poll_result = self._post_v3_async("/v3/place_bet", payload)
        if poll_result.get("success") is False and "statuses" not in poll_result:
            # Maybe-landed guard: if HB issued a correlation_id the order REACHED the
            # exchange and MAY have matched at the Starting Price (only the bet_status
            # polling then died — _post_v3_async attaches the cids on that H23 path).
            # Mark it AMBIGUOUS so the caller VERIFIES rather than blindly re-backing.
            # A clean pre-placement reject (403/401/400, or a POST that never got a cid)
            # carries no correlation_ids and stays a definitive failure. _post_v3_async
            # already tags a fast POST-timeout ambiguous; this closes the
            # cid-obtained-then-poll-died gap on the BSP path.
            if poll_result.get("correlation_ids") and not poll_result.get("ambiguous"):
                poll_result = {**poll_result, "ambiguous": True}
            return poll_result
        return self._unwrap_single_status(poll_result)
