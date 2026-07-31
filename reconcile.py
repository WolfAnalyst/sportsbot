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

import json
import logging
import os
import time
from datetime import datetime

log = logging.getLogger("tipbot.reconcile")

# Defaults — tune via the caller. dt_grace covers clock skew between this
# machine and HyperBot's server-side dt stamps (assume NTP-synced, ~seconds).
# v5.56 (audit): max wait 20s -> 30s — trader-review bets (Pointsbet, over-MBL)
# can land >20s after submission and were reading as "not found" right at the
# edge of the window. Normal placements appear in pending_bets <10s, so the
# extra 10s only extends the RARE ambiguous path, never the happy path.
DEFAULT_MAX_WAIT_SEC = 30.0
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


def _toks(s):
    """Significant (>=3 alnum char) lowercased tokens of s (split on non-alnum,
    exact-token equality). Shared by pending_bet_matches and the startup orphan
    sweep for fuzzy selection comparison."""
    return {t for t in "".join(
        c.lower() if c.isalnum() else " " for c in (s or "")
    ).split() if len(t) >= 3}


def _market_contradicts(pending, market) -> bool:
    """v6.07 (sweep HIGH #6): True when the pending record is provably the OTHER
    racing market (win vs place) than the one we attempted.

    Without this, an ambiguous PLACE attempt could "confirm" against the WIN bet
    already sitting on the same account for the SAME runner: same event, same window,
    same selection tokens, and a place stake is usually <= the win stake so the stake
    gate passes too. That returns a false PLACED (the place bet is recorded as landed
    when it never did) and, on the racing path, suppresses the spill that would have
    filled it. Same-account win+place pairs on one runner are routine (38 such pairs
    in logs/bets_placed.csv).

    STRUCTURED FIELD ONLY, deliberately. The asymmetry matters:
      - gate does NOT fire when it should  -> the pre-existing behaviour (a place
        attempt may falsely 'confirm' against the win bet -> recorded placed ->
        under-fill). Bad, bounded, and the status quo.
      - gate fires when it should NOT (false-reject of a bet that DID land) ->
        landed=False -> racing Tier-2 SPILL re-shops the same stake onto the next
        bookie -> DOUBLE BET. Strictly worse.
    So we only reject on an UNAMBIGUOUS structured market field. An earlier version
    also inferred the market from a '(win)'/'(place)' token in the bet text, but a
    log sweep found 10 '(win)' occurrences and ZERO '(place)' ones, and no bet_type
    value anywhere — i.e. there is NO captured evidence that HyperBot labels PLACE
    pending records with a '(place)' token. If it instead labels them with the win
    convention (or omits the market), that text arm would false-reject a landed place
    bet and spill it. Absent a real place-market sample, the text arm is not safe, so
    it is gone: when the market is unknown we simply do not gate (unchanged behaviour,
    no new risk). Revisit if a real PLACE pending_bets sample is ever captured.

    ★ v6.07 AUDIT (2026-07-31) — SCHEMA NOW CAPTURED LIVE, AND THIS GATE IS INERT.
    I read /api/pending_bets across all 37 active accounts (83 real racing records).
    The record shape is:
        [account_id, bet, bet_type, bookie_bet_id, dt, event, id, odds, result,
         session_id, sport, stake, verified]
    and the two fields this gate reads are BOTH unusable:
      * `bet_type` is the PROMO field — it was "non_promo" on 83 of 83 records, never
        win/place.
      * there is NO `market` key at all (0 of 83 records have one).
    The win/place label exists ONLY inside the `bet` TEXT ("Jarrito (win)").
    So `bt` is always "non_promo" here and this function ALWAYS returns False in
    production: the gate can never fire, which is money-SAFE (it cannot false-reject,
    so it cannot cause the Tier-2 spill/double-bet above) but it is also DEAD as a
    safety feature — the same phantom-field trap as the v6.06 #21 period guard.
    DO NOT "fix" it by reading the bet text without new evidence: all 83 records were
    "(win)", and the ledger confirms ZERO place bets were placed on the day those
    records were created (2026-07-29), so their uniformity is fully explained by there
    being no place bets to see — it is NOT evidence that HB labels places as "(win)",
    but it is not evidence against it either. The hazard Wilson identified stands
    unresolved. To resurrect this gate, capture a pending_bets record for a KNOWN place
    bet first (place one deliberately, then read the endpoint while it is unsettled),
    and only then key off whatever field that sample proves carries the market.
    Pinned by test_reconcile_market_gate_is_inert_against_the_real_schema.
    """
    m = _norm(market)
    if m not in ("win", "place"):
        return False
    # v6.07 audit: consult EVERY candidate field and take the first that actually
    # carries a win/place label. The old `bet_type or market` chain short-circuited on
    # the FIRST TRUTHY value, and bet_type is always the truthy promo string
    # ("non_promo"), so the `market` fallback was unreachable -- meaning that even if
    # HyperBot later starts emitting a real market field (the documented way to
    # resurrect this gate) it would still never be read. No behaviour change today:
    # with bet_type="non_promo" and no market key, this stays inert exactly as before.
    for _field in ("bet_type", "market", "bet_market", "market_type"):
        bt = _norm(pending.get(_field) or "")
        if bt in ("win", "place"):
            return bt != m
    return False


def pending_bet_matches(pending, *, event, stake, sport=None, selection_text=None,
                        after_ts, now_ts=None, dt_grace_sec=DEFAULT_DT_GRACE_SEC,
                        stake_tol=DEFAULT_STAKE_TOL, market=None):
    """True if a /api/pending_bets record is the bet we just attempted.

    Gates (all must pass):
      - result is None (unsettled; pending_bets should only return these)
      - dt in [after_ts - grace, now_ts + grace]  <- the key discriminator
      - event matches (normalized substring, either direction) when both present
      - stake: 0 < pending_stake <= attempted_stake + tol  (auto-cap => smaller OK)
      - sport matches when both present
      - selection (v5.56, audit): when BOTH the pending record's bet text and
        selection_text are non-empty, at least ONE significant token (>=3
        alnum chars) of selection_text must appear in the bet text — a HARD
        gate. The old soft version could match a DIFFERENT bet on the same
        event in the same window, a real scenario since v5.54's concurrent
        tips (two runners in ONE race on ONE account, seconds apart). Token
        matching (not full-string) keeps name-format drift safe ("Victor
        Wembanyama Over" matches "Victor Wembanyama 26.5+ points (...)" via
        the 'wembanyama' token). Stays SOFT only when the pending bet text is
        EMPTY (racing pending bets had empty runner text in one sample).
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

    # v6.07 (sweep HIGH #6): HARD win/place gate. A racing PLACE attempt must never
    # confirm against the WIN bet on the same runner/account (or vice-versa) — every
    # other gate (event, window, selection tokens, stake) passes for that pair.
    if market and _market_contradicts(pending, market):
        log.info(
            f"reconcile: dt/event/stake matched but the pending bet is the "
            f"OTHER market (attempted '{market}', pending "
            f"bet_type={pending.get('bet_type') or pending.get('market')!r} "
            f"bet={str(pending.get('bet'))[:60]!r}): rejecting (not our bet)"
        )
        return False

    if selection_text:
        # v5.57 (soundness check on v5.56): compare TOKEN SETS, not
        # substring-in-blob. The v5.56 version normalised the pending bet text
        # into one alnum blob and substring-matched selection tokens against
        # it — so "ROCK NIEN"'s token 'rock' falsely matched
        # "ROCKNROLL DA GAMA win" ('rocknrolldagamawin' contains 'rock'),
        # exactly the concurrent same-meeting pair from 2026-06-12 (tips
        # 62161/62166). Tokenising BOTH sides (split on non-alnum, >=3 chars,
        # EXACT token equality) kills prefix/suffix accidents while keeping
        # format drift safe ('wembanyama' token matches inside "Victor
        # Wembanyama 26.5+ points (...)" via its own token). Residual,
        # documented: two runners SHARING a whole name word ("FLYING FOX" vs
        # "FLYING SCOTSMAN" share 'flying') still pass this gate — the dt
        # window + stake gate remain the discriminators there; requiring ALL
        # tokens would flip the risk to false-REJECTS (-> racing spill ->
        # double-bet), the worse direction.
        sel_toks = _toks(selection_text)
        bet_toks = _toks(pending.get("bet"))
        if bet_toks and sel_toks and not (sel_toks & bet_toks):
            # HARD reject — same account, same event, same window, but the
            # bet text names a DIFFERENT selection. Without this, an
            # ambiguous runner-A attempt could "confirm" against runner-B's
            # pending bet (concurrent tips on one race).
            log.info(
                f"reconcile: dt/event/stake matched but NO selection token of "
                f"'{selection_text}' in pending bet '{pending.get('bet')}' — "
                f"rejecting (different selection, not our bet)"
            )
            return False
        if not bet_toks:
            log.debug(
                f"reconcile: pending bet text empty — selection "
                f"'{selection_text}' unverifiable (soft accept, racing sample)"
            )
    return True


def verify_bet_landed(hb, account_id, *, event, stake, sport=None,
                      selection_text=None, after_ts,
                      max_wait_sec=DEFAULT_MAX_WAIT_SEC,
                      poll_interval_sec=DEFAULT_POLL_INTERVAL_SEC,
                      market=None):
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
                                       selection_text=selection_text, after_ts=after_ts,
                                       market=market):
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
                     submit_ts, reconcile_enabled, spill_enabled, market=None):
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
      {'action': 'not_placed', 'reason': str}
          -> pending_bets POSITIVELY confirmed NOT placed, spill off (Tier 1).
             Caller must NOT debit-as-placed (we KNOW nothing landed): leave the
             stake unfilled so it surfaces for manual re-placement. Racing
             honours this; sports paths that only check 'placed'/'spill' treat
             it as their existing default (debit-as-placed) — unchanged.
      {'action': 'conservative', 'reason': str}
          -> fall back to the caller's existing behaviour (debit-as-placed +
             blocklist + critical alert). Fires on UNCERTAINTY only:
             reconciliation disabled, no account_id, or pending_bets API failed
             (never spill/abandon on uncertainty).

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
            # v6.07 (sweep HIGH #6): racing win/place discriminator so a PLACE
            # attempt can't confirm against the WIN bet on the same runner.
            market=market,
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
    # Spill off, but pending_bets POSITIVELY confirmed the bet is NOT on the
    # account. This is distinct from 'conservative' (which means "we don't
    # know"): here we KNOW nothing landed, so the caller must NOT debit-as-
    # placed. Racing (racing_placer) treats this as a clean failure -> the
    # stake stays unfilled and surfaces for manual re-placement. Sports paths
    # (main.py) only branch on 'placed'/'spill', so 'not_placed' falls through
    # to their existing debit-as-placed default — behaviour unchanged there.
    # WHY this matters: tip 62051 (TRACER BULLET, 2026-06-03) — bet365 returned
    # "Bet placement failed", reconcile confirmed not-placed, but the old
    # 'conservative' path counted the $200 as tentatively placed, so
    # unfilled=$0 and the shortfall never reached manual.
    return {'action': 'not_placed',
            'reason': 'confirmed not-placed, spill off (Tier 1)'}


# ── Startup ORPHAN sweep (incident 2026-07-17, v6.03) ───────────────────────
# On process restart (freeze-watchdog os._exit / crash / reconnect) a bet that was
# IN FLIGHT when the process died is never reconciled -> invisible to the ledger +
# Wilson. find_orphan_pending queries /api/pending_bets for OWNED sessions and
# returns pending bets (within a recent window) that are NOT accounted-for in the
# ledger. It is ALERT-ONLY: it reads pending_bets + the ledger and NEVER places
# anything (an in-flight bet MAY have landed; re-placing = double stake). The
# caller alerts Wilson to verify + record manually. See main._startup_pending_reconcile.
DEFAULT_STARTUP_LOOKBACK_SEC = 1800  # 30 min


def _ledger_row_matches(pending, ledger_rows, *, pdt, lookback_sec,
                        dt_grace_sec=DEFAULT_DT_GRACE_SEC, stake_tol=DEFAULT_STAKE_TOL):
    """True if some ledger row plausibly IS this pending bet (=> accounted-for, NOT
    an orphan). Mirrors pending_bet_matches' gates: event normalized-substring,
    stake within tol, selection-token overlap, and (best-effort) placed_at within
    the window of the pending dt. A row with an unparseable placed_at skips only the
    time gate (still matched on event+stake+selection)."""
    pe = _norm(pending.get("event"))
    try:
        pstake = float(pending.get("stake"))
    except (TypeError, ValueError):
        pstake = None
    pbet_toks = _toks(pending.get("bet"))
    for r in ledger_rows:
        pa = r.get("placed_at")
        if pa is not None and abs(pa - pdt) > lookback_sec + dt_grace_sec:
            continue
        re_ = _norm(r.get("event"))
        if pe and re_ and not (pe in re_ or re_ in pe):
            continue
        if pstake is not None:
            try:
                rstake = float(r.get("stake"))
            except (TypeError, ValueError):
                rstake = None
            if rstake is not None and abs(pstake - rstake) > stake_tol:
                continue
        rsel_toks = _toks(r.get("selection"))
        if pbet_toks and rsel_toks and not (pbet_toks & rsel_toks):
            continue
        return True
    return False


def find_orphan_pending(hb, owned_sessions, *, ledger_rows, seen_ids,
                        lookback_sec=DEFAULT_STARTUP_LOOKBACK_SEC, now_ts=None,
                        dt_grace_sec=DEFAULT_DT_GRACE_SEC):
    """Return ORPHAN pending-bet dicts: PENDING bets on an owned account within the
    lookback window that are NOT accounted-for in the ledger (likely in-flight when
    the process died, never recorded). ALERT-ONLY — reads pending_bets + the ledger,
    NEVER places. Accounted-for = the pending id/bookie_bet_id is a known ledger
    bet_id/correlation_id, OR _ledger_row_matches. seen_ids suppresses re-alerting the
    same orphan across restarts. A per-session API failure is logged and SKIPPED
    (never emitted as an orphan — an API failure is not proof of an orphan)."""
    now_ts = now_ts if now_ts is not None else time.time()
    ledger_ids = {str(r.get("bet_id")).strip() for r in (ledger_rows or []) if r.get("bet_id")}
    ledger_ids |= {str(r.get("correlation_id")).strip() for r in (ledger_rows or []) if r.get("correlation_id")}
    seen = set(str(x) for x in (seen_ids or ()))
    orphans = []
    for s in (owned_sessions or []):
        acct = s.get("account_id")
        if not acct:
            log.info(f"startup-reconcile: session {s.get('session_id')} has no account_id "
                     f"— skipping pending-bets check")
            continue
        try:
            resp = hb.get_pending_bets(acct)
        except Exception as e:
            log.warning(f"startup-reconcile: get_pending_bets raised for acct {acct}: {e} "
                        f"— skipping (API fail != orphan)")
            continue
        if not resp or resp.get("success") is False:
            log.warning(f"startup-reconcile: get_pending_bets failed for acct {acct} "
                        f"({(resp or {}).get('error')}) — skipping (API fail != orphan)")
            continue
        for b in resp.get("bets", []):
            if b.get("result") is not None:
                continue  # settled, not pending
            pdt = _parse_iso_utc(b.get("dt"))
            if pdt is None:
                continue
            if pdt < now_ts - lookback_sec - dt_grace_sec:
                continue  # older than the in-flight-at-crash window — ignore
            bid = str(b.get("id") or "").strip()
            if bid and bid in seen:
                continue  # already alerted on a previous restart
            bbid = str(b.get("bookie_bet_id") or "").strip()
            if (bid and bid in ledger_ids) or (bbid and bbid in ledger_ids):
                continue  # exact id join -> accounted-for
            if _ledger_row_matches(b, ledger_rows or [], pdt=pdt, lookback_sec=lookback_sec,
                                   dt_grace_sec=dt_grace_sec):
                continue  # fuzzy ledger match -> accounted-for
            orphans.append({
                "account_id": acct,
                "session_id": s.get("session_id"),
                "bookie": s.get("bookie", ""),
                "event": b.get("event", ""),
                "bet": b.get("bet", ""),
                "stake": b.get("stake"),
                "odds": b.get("odds"),
                "dt": b.get("dt"),
                "id": b.get("id"),
                "bookie_bet_id": b.get("bookie_bet_id"),
            })
    return orphans


def load_seen_orphans(path):
    """Load the persisted set of already-alerted orphan pending-bet ids (str).
    Missing/unreadable/garbage -> empty set (fail open: at worst we re-alert once)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(str(x) for x in json.load(f))
    except Exception:
        return set()


def save_seen_orphans(path, ids):
    """Persist the set of alerted orphan ids (JSON list) atomically. Best-effort."""
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(str(x) for x in ids), f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning(f"startup-reconcile: could not persist seen-orphans to {path}: {e}")


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
