"""
Ambiguous-outcome reconciliation against HyperBot /api/pending_bets.

When a placement gets a slow rejection (>= STAKE_REJECT_LATENCY_THRESHOLD_SEC,
the Erasmus class), the response alone can't tell us whether the bet actually
landed at the bookie. Instead of guessing from latency, we query the account's
real-time PENDING (unsettled) bets and check whether our attempt is there:

  - landed True  -> the bet IS on the account. Record it, debit the actual
                    stake, do NOT spill (would double-bet).
  - landed False -> the bet is NOT there. Safe to ladder/spill — this is the
                    fix for the Jack Ross / Levi / Dan false-positives that
                    were debited-as-placed but never actually placed.
  - reconcile_failed True -> the pending_bets API itself failed. Fall back to
                    the caller's existing conservative behaviour (debit-as-
                    placed + blocklist + alert, no spill). Never spill on
                    uncertainty — that's the Erasmus invariant.

Match key (confirmed from a live /api/pending_bets sample 2026-05-30):
each pending bet = {dt(ISO UTC), id, bet(text), odds, event, sport, stake,
result(null=pending), bet_type, account_id, bookie_bet_id}.

The hard discriminator is the bounded dt WINDOW: a pending bet only matches if
its dt is in [attempt_submit_ts - grace, now + grace]. This is what separates
"the bet we just fired" from an older bet on the SAME event+account (the live
sample had a day-old Wembanyama bet sharing the exact event string with today's
Spurs bets — event+stake alone would have mismatched them; the dt window does
not). event + stake(+auto-cap) + sport are additional gates; odds is a soft
check only (a single account places at most one bet per tip, so within the
window there is only one candidate — we don't need odds to disambiguate
ml-vs-line, and odds can drift between attempt and fill).
"""

import logging
import time
from datetime import datetime

log = logging.getLogger("tipbot.reconcile")

# Defaults — tune via the caller. dt_grace covers clock skew between this
# machine and HyperBot's server-side dt stamps (assume NTP-synced, ~seconds).
DEFAULT_MAX_WAIT_SEC = 20.0
DEFAULT_POLL_INTERVAL_SEC = 4.0
DEFAULT_DT_GRACE_SEC = 12.0
DEFAULT_STAKE_TOL = 0.5


def _parse_iso_utc(dt_str):
    """ISO 8601 (e.g. '2026-05-30T13:18:59+00:00') -> epoch seconds, or None."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _norm(s):
    """Lowercase alphanumerics only — for fuzzy event/text comparison."""
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def pending_bet_matches(pending, *, event, stake, sport=None, selection_text=None,
                        after_ts, now_ts=None, dt_grace_sec=DEFAULT_DT_GRACE_SEC,
                        stake_tol=DEFAULT_STAKE_TOL):
    """True if a /api/pending_bets record is the bet we just attempted.

    Gates (all must pass):
      - result is None (unsettled; pending_bets should only return these)
      - dt in [after_ts - grace, now_ts + grace]  <- the key discriminator
      - event matches (normalized substring, either direction) when both present
      - stake: 0 < pending_stake <= attempted_stake + tol  (auto-cap => smaller OK)
      - sport matches when both present
    selection_text, if given, is logged as a soft confirmation but is NOT a gate
    (racing pending bets had empty runner text in one sample).
    """
    if pending.get("result") is not None:
        return False

    now_ts = now_ts if now_ts is not None else time.time()
    pdt = _parse_iso_utc(pending.get("dt"))
    if pdt is None:
        return False
    if pdt < after_ts - dt_grace_sec or pdt > now_ts + dt_grace_sec:
        return False

    pe, ev = _norm(pending.get("event")), _norm(event)
    if ev and pe and not (ev in pe or pe in ev):
        return False

    try:
        pstake = float(pending.get("stake"))
    except (TypeError, ValueError):
        return False
    if pstake <= 0 or pstake > stake + stake_tol:
        return False

    if sport:
        ps = _norm(pending.get("sport"))
        if ps and _norm(sport) and ps != _norm(sport):
            return False

    if selection_text:
        if _norm(selection_text) and _norm(selection_text) not in _norm(pending.get("bet")):
            log.debug(
                f"reconcile: dt/event/stake matched but selection text "
                f"'{selection_text}' not in pending bet '{pending.get('bet')}' "
                f"(soft — not rejecting)"
            )
    return True


def verify_bet_landed(hb, account_id, *, event, stake, sport=None,
                      selection_text=None, after_ts,
                      max_wait_sec=DEFAULT_MAX_WAIT_SEC,
                      poll_interval_sec=DEFAULT_POLL_INTERVAL_SEC):
    """Poll /api/pending_bets until a matching bet appears or max_wait elapses.

    Returns:
      {"landed": True,  "match": <pending bet dict>, "reconcile_failed": False}
      {"landed": False, "match": None,               "reconcile_failed": False}
      {"landed": None,  "match": None,               "reconcile_failed": True}

    reconcile_failed True means EVERY pending_bets call this round errored — the
    caller must fall back to its conservative ambiguous handling (no spill).
    """
    if not account_id:
        log.warning("reconcile: no account_id, cannot verify — treating as reconcile_failed")
        return {"landed": None, "match": None, "reconcile_failed": True}

    deadline = time.time() + max_wait_sec
    api_ok_once = False
    while True:
        resp = hb.get_pending_bets(account_id)
        if resp.get("success") is False:
            log.warning(f"reconcile: get_pending_bets failed: {resp.get('error')}")
        else:
            api_ok_once = True
            for b in resp.get("bets", []):
                if pending_bet_matches(b, event=event, stake=stake, sport=sport,
                                       selection_text=selection_text, after_ts=after_ts):
                    log.info(
                        f"reconcile: LANDED — matched pending bet id={b.get('id')} "
                        f"bookie_bet_id={b.get('bookie_bet_id')} stake=${b.get('stake')} "
                        f"odds={b.get('odds')} event='{b.get('event')}'"
                    )
                    return {"landed": True, "match": b, "reconcile_failed": False}
        if time.time() >= deadline:
            break
        time.sleep(poll_interval_sec)

    if api_ok_once:
        log.info(
            f"reconcile: NOT FOUND after {max_wait_sec}s — bet did not land "
            f"(event='{event}' stake=${stake}); caller may spill"
        )
        return {"landed": False, "match": None, "reconcile_failed": False}
    log.warning("reconcile: pending_bets unreachable all round — reconcile_failed")
    return {"landed": None, "match": None, "reconcile_failed": True}


def decide_ambiguous(hb, account_id, *, event, stake, sport, selection,
                     submit_ts, reconcile_enabled, spill_enabled):
    """Shared SLOW-REJECTION reconciliation decision used by BOTH main.py sports
    placement and racing_placer.py (lives here, not main.py, to avoid a circular
    import). Encodes Wilson's 2026-05-31 decisions on top of verify_bet_landed.
    Flags are passed in (config.RECONCILE_AMBIGUOUS / RECONCILE_SPILL) so this
    module stays config-free. Returns:

      {'action': 'placed', 'match': <pending bet>, 'actual_stake': float}
          -> bet IS on the account: record it, debit the ACTUAL stake (auto-cap:
             smaller counts), do NOT spill. Tier 1 (safe regardless of feed lag).
      {'action': 'spill'}
          -> confirmed NOT placed AND spill_enabled: recover the stake
             (ladder/spill to another session). Tier 2.
      {'action': 'conservative', 'reason': str}
          -> fall back to the caller's existing behaviour (debit-as-placed +
             blocklist + critical alert). Fires when reconciliation disabled, no
             account_id, pending_bets failed (never spill on uncertainty), or
             confirmed not-found while spill is off (Tier 1).

    Only for the SLOW-REJECTION class — NOT text-pattern ambiguous (Pointsbet
    'intercepted'), which always stays conservative (decision 2)."""
    if not reconcile_enabled:
        return {'action': 'conservative', 'reason': 'reconciliation disabled'}
    if not account_id:
        return {'action': 'conservative', 'reason': 'no account_id on session'}
    try:
        v = verify_bet_landed(
            hb, account_id, event=event, stake=stake, sport=sport,
            selection_text=selection, after_ts=submit_ts,
        )
    except Exception as e:
        log.error(f"reconcile: verify_bet_landed raised: {e}")
        return {'action': 'conservative', 'reason': f'reconcile error: {e}'}
    if v.get('landed') is True:
        m = v.get('match') or {}
        try:
            actual = float(m.get('stake', stake) or stake)
        except (TypeError, ValueError):
            actual = stake
        if actual <= 0:
            actual = stake
        return {'action': 'placed', 'match': m, 'actual_stake': actual}
    if v.get('reconcile_failed'):
        return {'action': 'conservative', 'reason': 'pending_bets API unavailable'}
    # landed is False — confirmed not placed.
    if spill_enabled:
        return {'action': 'spill'}
    return {'action': 'conservative',
            'reason': 'confirmed not-placed but spill off (Tier 1)'}


# ── Self-test against the live 2026-05-30 sample ────────────────────────────
if __name__ == "__main__":
    def _epoch(iso):
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    SAMPLE = [
        {"dt": "2026-05-29T16:45:52+00:00", "id": 47520796, "bet": "Victor Wembanyama 26.5+ points (Victor Wembanyama Over)",
         "odds": 1.96, "event": "Oklahoma City Thunder v San Antonio Spurs", "sport": "NBA",
         "stake": 1.0, "result": None, "bookie_bet_id": "6836816944"},
        {"dt": "2026-05-30T13:18:59+00:00", "id": 47540149, "bet": "San Antonio Spurs",
         "odds": 2.27, "event": "Oklahoma City Thunder v San Antonio Spurs", "sport": "NBA",
         "stake": 1.0, "result": None, "bookie_bet_id": "6841375142"},
        {"dt": "2026-05-30T13:20:05+00:00", "id": 47540150, "bet": "line San Antonio Spurs -3.5",
         "odds": 1.88, "event": "Oklahoma City Thunder v San Antonio Spurs", "sport": "NBA",
         "stake": 1.0, "result": None, "bookie_bet_id": "6841376677"},
    ]

    def _match_ids(**kw):
        return [b["id"] for b in SAMPLE if pending_bet_matches(b, **kw)]

    passed = True

    # 1. ml attempt window -> ONLY the ml bet (47540149). Wembanyama excluded by
    #    lower bound (day-old), line excluded by upper bound (placed 66s later).
    r = _match_ids(event="Oklahoma City Thunder v San Antonio Spurs", stake=1.0, sport="nba",
                   after_ts=_epoch("2026-05-30T13:18:55+00:00"),
                   now_ts=_epoch("2026-05-30T13:19:15+00:00"))
    print(f"test1 ml-window -> {r} (expect [47540149])"); passed &= (r == [47540149])

    # 2. line attempt window -> ONLY the line bet (47540150).
    r = _match_ids(event="Oklahoma City Thunder v San Antonio Spurs", stake=1.0, sport="nba",
                   after_ts=_epoch("2026-05-30T13:20:00+00:00"),
                   now_ts=_epoch("2026-05-30T13:20:20+00:00"))
    print(f"test2 line-window -> {r} (expect [47540150])"); passed &= (r == [47540150])

    # 3. a bet that was never placed (different event) -> no match.
    r = _match_ids(event="Carlton v Essendon", stake=1.0, sport="afl",
                   after_ts=_epoch("2026-05-30T13:18:55+00:00"),
                   now_ts=_epoch("2026-05-30T13:19:15+00:00"))
    print(f"test3 wrong-event -> {r} (expect [])"); passed &= (r == [])

    # 4. auto-cap: attempted $2 but only $1 landed -> still matches (smaller OK).
    r = _match_ids(event="Oklahoma City Thunder v San Antonio Spurs", stake=2.0, sport="nba",
                   after_ts=_epoch("2026-05-30T13:18:55+00:00"),
                   now_ts=_epoch("2026-05-30T13:19:15+00:00"))
    print(f"test4 auto-cap(attempt $2) -> {r} (expect [47540149])"); passed &= (r == [47540149])

    # 5. stake too large in pending vs attempted -> NOT a match (never larger).
    r = _match_ids(event="Oklahoma City Thunder v San Antonio Spurs", stake=0.4, sport="nba",
                   after_ts=_epoch("2026-05-30T13:18:55+00:00"),
                   now_ts=_epoch("2026-05-30T13:19:15+00:00"))
    print(f"test5 attempt-smaller-than-pending -> {r} (expect [])"); passed &= (r == [])

    print("\nALL PASS" if passed else "\nFAILURES ABOVE")
