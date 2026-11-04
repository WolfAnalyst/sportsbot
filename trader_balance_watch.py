"""v6.173 (Wilson 2026-10-30: "i need the tipbot to be able to reread betdeluxe and see the pending
bet"): a balance signal for a bet the bookie referred to a trader. v6.203 (Wilson 1 Nov: "a quicker
realisation it has placed"): STEP matching, a fast poll of its own, a register of the bot's own sends, no hold.

HyperBot cannot show an open Betdeluxe bet: /api/pending_bets lists none of them (account
a2e60426..., 30 Oct: Slugger $27.78 open on the account, pending_bets count 0), /v3/transactions
shows a bet only once it settles, and /v3/balance carries the cash balance only. So the trader
follow (racing_placer, v6.172) learns of an accepted bet hours later, when it settles.

The one live clue is the balance. 31 Oct, "Our Ultimate Jessy" (Betdeluxe, $18.00, referred 18:09:25): the
balance fell by exactly $18.00 at 18:10:01, 36 s later; at 18:11:49 an unrelated $3000.00 left the account;
the follow's first balance look came at 18:12:42 and compared with the referral baseline, saw a fall of
$3018.00, and said nothing for the rest of the follow. So:

- STEP matching: each look is compared with the PREVIOUS look, not with the referral baseline. One step of
  exactly the stake (less what the bot's own sends explain) is the signal. An unrelated debit or credit is
  its own step: it neither hides the stake's step nor is mistaken for it. Two debits in one step are tried
  as the stake plus the stakes of the bot's OWN sends in doubt (any 1 to 3), and the message names them. A step
  that can only be explained with the help of OTHER open referrals' stakes ("one or more of the open
  referrals ($18.00 + $25.00)") is sent as "cannot tell which one", never as ACCEPTED: an unrelated debit of
  the right size looks the same. A step no combination explains is logged at WARNING with the numbers; one
  fully explained by the bot's own placed sends is logged at INFO ("explained by own sends").
- THE SEND REGISTER (trader_send_registry): every bet POST the bot sends on a trader session, from before it
  goes out until its reply (session, stake, start, end, outcome; kept 30 minutes), whichever path sends it.
  The in-flight file covers the trader-leg sends only and the ledger stamps a row when it is WRITTEN (the
  end of the tip), so neither says WHEN a debit happened. A send that PLACED and ENDED inside the window
  between the previous look and this one explains its debit, however early it began (a debit before the
  previous look would have moved that look); a send still out, ended after this look, ended within
  SEND_LAG_SEC (600 s, the register's open cap: the real reply-to-debit lag is not known) BEFORE the previous
  look (the debit can lag the reply, so it may fall in this step), or
  with an unknown outcome is in doubt: one of the SAME stake could be the whole step, so the message is
  withheld AND the previous look is kept. The step is judged again at the next look: it is released when the
  send turns out refused, or placed inside the window; a send of unknown outcome, or one that ended BEFORE the
  previous look (the lag rule), keeps it withheld until the balance moves again or the send is pruned from
  the register (30 minutes after it ended), and then there is a WARNING with the numbers and silence (the look
  is re-baselined): a prune never turns an unresolved step into an alert. When a
  withheld step later turns out to hold more than the stake (an unrelated debit on top), the message is "cannot
  tell", never silence. A send that was refused debits nothing. An entry with no reply after 10 minutes
  (trader_send_registry.OPEN_MAX_SEC) counts as unknown (then pruned); the in-flight records of another
  process are in doubt only while younger than the same 600 s.
- A FAST POLL of its own: one thread per SESSION reads /v3/balance once per pass for every referral open on
  it, every FAST_POLL_SEC (20 s) for the first FAST_WINDOW_SEC (10 min) after a referral, then every
  SLOW_POLL_SEC (60 s) until WATCH_SEC (2 h) after it, then only the follow's own cycle (every 5 minutes for the
  first hour, then every 15), which carries on beside it throughout. A reading older than the latest look (a
  stale poller pass) is dropped. The OTHER open referrals a step could belong to are all the live follows on the
  session (no age limit), and a referral alerted for this very step still counts.
- No hold (v6.173 held a match 900 s because the bot's own bet shows before its ledger row; the register
  replaces that).
- One alert, "consistent with the trader having ACCEPTED it. Check the Betdeluxe app before re-placing". It is only an
  alert: it never records, places or re-places anything and writes NO ledger row (there is no bet id until the
  bet settles; the follow records it then). After an alert the poller keeps watching for a rise by the
  stake (a refund or void) and sends ONE info line if it comes.

What it cannot tell (stated in the message where it matters):
- any other debit of exactly the stake looks the same (Wilson's own bet in the app, another referral being
  accepted); when another open referral on the same account has the same stake, the message says it
  cannot tell which one;
- a payout or a refund between two looks can hide a real acceptance in the same step (then nothing is said);
- if Betdeluxe already holds the stake at the referral (not known), the starting balance excludes
  it and an acceptance moves nothing: then nothing is said either; if it places a hold AFTER the starting
  read, the hold looks like an acceptance;
- a follow resumed after a restart compares with the last balance CHANGE saved on its record (a look at an
  unchanged balance is not saved), and the bot's sends from before the restart are known only from the in-flight
  file (the register is in memory). A step WITHHELD for a send in doubt is saved on the record (the send ids as
  strings, and the look time) whenever the withhold is set or cleared, so a restart cannot turn it into an
  alert: the register is empty after it, so the step is re-baselined silently (a WARNING with the numbers). A
  record with an open withhold but no look time is re-baselined the same way, never judged;
- a coincidence of unrelated debits and credits that happens to add up to the stake, and a POST hung for more
  than the register's 10-minute open cap, are not told apart (the wording is "consistent with ... check the
  app", an alert only);
- a balance RISE of the stake before any alert is only logged: that the stake is held during the review is not
  known, and a void refund looks the same.
"At most once" holds while the follows file can be written: the marker is saved before the text
is handed back, and a refused save is logged at ERROR (the text still goes: a lost hint is worse)."""
import atexit
import itertools
import logging
import math
import os
import threading
import time
from datetime import datetime

import trader_send_registry

log = logging.getLogger("tipbot.trader_balance")

ENABLED = os.getenv("TRADER_BALANCE_CHECK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
READ_TIMEOUT_SEC = 8     # per socket operation (requests), one attempt
# the fast poll (v6.203): every FAST_POLL_SEC for the first FAST_WINDOW_SEC after a referral, then every
# SLOW_POLL_SEC until the follow's deadline. Off by default under TIPBOT_TESTING (no test starts a thread unless
# it asks for it).
FAST_POLL_SEC = 20.0
FAST_WINDOW_SEC = 600.0
SLOW_POLL_SEC = 60.0
WATCH_SEC = 2 * 3600          # the poller watches a referral this long (20 s, then 60 s); after it the follow's cycle only
FOREIGN_INFLIGHT_SEC = float(trader_send_registry.OPEN_MAX_SEC)   # an in-flight record of ANOTHER process counts as a
                                                                  # send in doubt this long: the register's own open cap
SEND_LAG_SEC = float(trader_send_registry.OPEN_MAX_SEC)   # a debit can lag the reply, and how long is not known: a
                              # placed own send that ended this long BEFORE the previous look may still be the debit
                              # that falls in the next step
FAST_POLL_ENABLED = os.getenv("TRADER_BALANCE_FAST_POLL",
                              "false" if os.getenv("TIPBOT_TESTING") else "true").strip().lower() in (
    "1", "true", "yes", "on")
_EPS = 0.005
_DEFAULT_FOLLOW_SEC = 14 * 3600     # a record with no deadline of its own
# When this process started. A step is never judged across it: the register of the bot's own sends is in memory, so a
# send that debited just before a restart is invisible to the first look after it. A look older than this is
# re-baselined silently. Tests run with 0 (every look is "after the start") unless they set it.
_PROCESS_START = 0.0 if os.getenv("TIPBOT_TESTING") else time.time()


def read_balance(hb, session_id):
    """The session's cash balance from /v3/balance, or None when it cannot be read. One attempt
    with a short timeout: it runs on a follow's thread, often several at once."""
    try:
        r = hb.get_balance(str(session_id), timeout=READ_TIMEOUT_SEC, max_attempts=1)
    except Exception as e:
        _read_failed(session_id, f"failed: {e}")
        return None
    if not isinstance(r, dict) or r.get("success") is False:
        _read_failed(session_id, "gave no balance")
        return None
    try:
        b = float(r.get("balance"))
    except (TypeError, ValueError):
        global _SHAPE_WARNED
        if not _SHAPE_WARNED:
            _SHAPE_WARNED = True
            log.warning(f"/v3/balance answer has no usable 'balance' (keys {sorted(r)[:12]}): the "
                        f"trader balance watch is blind until that is fixed")
        return None
    return b if math.isfinite(b) else None


_SHAPE_WARNED = False
_RISE_LOGGED: set = set()      # follow keys whose balance rise was logged (once per process)
_READ_WARN_SEC = 600.0
_READ_WARNED: dict = {}        # session id -> when its last read failure was logged


def _read_failed(session_id, why: str) -> None:
    """A balance read that failed: at most ONE WARNING per session per 10 minutes (the poller reads every 20 s)."""
    now = time.time()
    sid = str(session_id)
    if now - _READ_WARNED.get(sid, 0.0) >= _READ_WARN_SEC:
        _READ_WARNED[sid] = now
        log.warning(f"balance read for s{sid} {why} (further failures on this session are not logged for "
                    f"{int(_READ_WARN_SEC // 60)} minutes)")


def _cents(x) -> int:
    return int(round(float(x) * 100))


def _money(x) -> str:
    return f"${float(x):.2f}"


def _send_id(e):
    return e.get("id") or (round(float(e.get("stake") or 0), 2), round(float(e.get("start") or 0), 1))


def _classify(sends, t0: float, t1: float):
    """Split the bot's own sends overlapping (t0, t1] into (certain, in_doubt, ids). `certain`: the total of the
    sends that PLACED and ENDED inside the window, however early they started (a debit that came before the
    previous look would have moved that look, which was judged: so this step holds it). `in_doubt`: the stakes of
    the others that may have debited, as (stake, id): still out, ended after this look, an unknown outcome, or a
    PLACED send that ended up to SEND_LAG_SEC before the previous look (its debit can lag the reply and fall in
    this step). A refused send debits nothing. `ids`: every send seen."""
    certain, in_doubt, ids = 0.0, [], set()
    for e in sends or ():
        try:
            ids.add(_send_id(e))
            if e.get("outcome") == "refused":
                continue
            stake = float(e.get("stake") or 0)
            if stake <= 0:
                continue
            if (e.get("outcome") == "placed" and e.get("end") is not None
                    and float(e["end"]) <= t0 - SEND_LAG_SEC):
                continue                  # placed and over long before the previous look: that look already holds its debit
            ended_in = e.get("end") is not None and t0 < float(e["end"]) <= t1
            if e.get("outcome") == "placed" and ended_in:
                certain += float(e.get("actual") or stake)
            else:
                in_doubt.append((stake, _send_id(e)))      # incl. a placed send that ended just BEFORE t0 (the lag)
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return certain, in_doubt, ids


def _explain(need: float, own_in_doubt, referrals):
    """The stakes of the bot's own sends in doubt and of the other open referrals (any 1 to 3 of them) whose total is
    `need`: [(kind, amount), ...] or None. Smaller combinations first, own sends before referrals."""
    items = [("own", float(s)) for s in own_in_doubt][:6] + [("referral", float(s)) for s in referrals][:6]
    for n in (1, 2, 3):
        for combo in itertools.combinations(items, n):
            if abs(sum(a for _k, a in combo) - need) < _EPS:
                return list(combo)
    return None


def _describe(parts) -> str:
    own = [a for k, a in parts if k == "own"]
    refs = [a for k, a in parts if k == "referral"]
    bits = []
    if own:
        bits.append("possibly the bot's own " + " + ".join(_money(a) for a in own) + " send"
                    + ("s" if len(own) > 1 else ""))
    if refs:
        bits.append("one or more of the open referrals (" + " + ".join(_money(a) for a in refs) + ")")
    return " and ".join(bits)


def _when(rec: dict, t_read: float) -> str:
    try:
        ref = rec.get("submit_ts") or rec.get("balance_b0_ts")
        what = "refusal" if rec.get("refusal_watch") else "trader referral"
        return (f" at {datetime.fromtimestamp(t_read):%H:%M:%S}, {int(t_read - float(ref))} s after the "
                f"{what}") if ref else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def check(rec: dict, hb, *, label: str, sends=(), other_open_stakes=(), reading=None):
    """One balance look for a trader-referred bet. `rec` carries session_id, stake and bookie and keeps this
    check's own state: balance_b0 / balance_b0_ts (the first look), balance_last / balance_last_ts (the previous
    look, saved only when the balance changed), balance_look_ts (the latest look at that balance, kept in memory
    only), balance_alerted, balance_rise_told. The look is compared with the PREVIOUS one: a step of exactly the
    stake, less the bot's own placed sends inside the window, is the signal; the stakes of the bot's own sends in
    doubt and of the other open referrals can explain the rest of a bigger step. `sends` are
    trader_send_registry entries overlapping (previous look, this look], or a callable (t0, t1) giving them
    (called AFTER the balance read; None = not readable: say nothing and keep the previous look).
    `other_open_stakes`: the stakes of the OTHER open trader follows on the same account. `reading`: (balance,
    time the request began[, time it RETURNED]) already read for the session this pass (the fast poll reads once
    per session). A look is dated by the time its read RETURNED (a send that ended while the request was out may
    already be in the balance), and a reading whose request began before the previous look returned is dropped. A step is judged once:
    when it is withheld (a same-stake send of the bot's own in doubt) the previous look is kept, so the next look
    judges it again. Returns a message to send (once) or None; after an alert a later rise by the stake sets
    rec["balance_msg_kind"] = "refund" and returns its info line. Never raises."""
    try:
        if not ENABLED:
            return None
        alerted = rec.get("balance_alerted")
        if alerted and rec.get("balance_rise_told"):
            return None
        if reading is not None:
            b, t_start = reading[0], reading[1]
            t_read = reading[2] if len(reading) > 2 else t_start
        else:
            t_start = time.time()
            b = read_balance(hb, rec.get("session_id"))
            t_read = time.time()                 # AFTER the read returned: a send that ended meanwhile may be in b
        if b is None:
            return None
        if rec.get("balance_b0") is None:
            rec["balance_b0"], rec["balance_b0_ts"] = b, t_read
            rec["balance_last"], rec["balance_last_ts"], rec["balance_look_ts"] = b, t_read, t_read
            log.info(f"{label}: balance at the first look ${b:.2f}")
            return None
        last = rec.get("balance_last")
        if last is None:                          # a record from before step matching: the baseline is the last look
            last, rec["balance_last_ts"] = rec["balance_b0"], rec.get("balance_b0_ts")
        if rec.get("balance_withheld_ids") and not rec.get("balance_look_ts") and not alerted:
            # a resumed record with an open withhold but no look time: the withheld step cannot be placed in time, and
            # the register is gone. Never judge it: re-baseline silently.
            log.warning(f"{label}: a withheld step was open but its look time is missing (a restart): nothing said, "
                        f"re-baselined at ${b:.2f} (withheld send ids {sorted(map(str, rec['balance_withheld_ids']))})")
            rec.pop("balance_withheld_ids", None)
            rec["balance_last"], rec["balance_last_ts"], rec["balance_look_ts"] = b, t_read, t_read
            return None
        try:
            t0 = float(rec.get("balance_look_ts") or rec.get("balance_last_ts") or 0)
        except (TypeError, ValueError):
            t0 = 0.0
        if t0 <= 0:
            return None                           # a baseline without its time: the bot's own sends cannot be told apart
        if t0 < _PROCESS_START:
            # the previous look was taken by an earlier process: whatever the bot sent then is not in the register, so
            # the step that spans the restart is never judged. Re-baseline silently and SAVE the look time.
            log.warning(f"{label}: TipBot restarted since the last look: step not judged (balance "
                        f"{float(last):.2f} -> {b:.2f}, stake {_money(rec.get('stake') or 0)}), re-baselined at "
                        f"${b:.2f}")
            rec.pop("balance_withheld_ids", None)
            rec["balance_last"], rec["balance_last_ts"], rec["balance_look_ts"] = b, t_read, t_read
            rec["balance_persist_look"] = True
            return None
        if reading is not None and t_start <= t0:
            return None                           # a reading started before the latest look returned (stale): drop it
        stake_d = float(rec.get("stake") or 0)
        if _cents(stake_d) <= 0:
            return None
        step_d = float(last) - b                  # > 0: the balance went down since the previous look
        if abs(step_d) < _EPS:
            rec["balance_look_ts"] = t_read       # nothing moved: the next step lies after this look
            return None
        if callable(sends):
            sends = sends(t0 - SEND_LAG_SEC, t_read)        # from the lag before the previous look: see _classify
        if sends is None:
            return None                           # the bot's own sends are not known: keep the previous look
        others = [float(s) for s in other_open_stakes or ()]
        certain, doubt, ids = _classify(sends, t0, t_read)
        uncertain = [s for s, _i in doubt]
        bookie = str(rec.get("bookie") or "bookie")
        withheld = rec.get("balance_withheld_ids")
        if withheld and not ({str(i) for i in withheld} & {str(i) for i in ids}) and not alerted:
            # an earlier look withheld this step for a send in doubt, and that send is no longer in the register
            # (pruned while still in doubt): the step can never be judged. Say nothing for it, and re-baseline.
            log.warning(f"{label}: a step withheld for a send in doubt can no longer be judged (the send left the "
                        f"register): nothing said, re-baselined at ${b:.2f} (step {step_d:+.2f}, stake "
                        f"{_money(stake_d)}, withheld send ids {sorted(map(str, withheld))})")
            rec.pop("balance_withheld_ids", None)
            rec["balance_last"], rec["balance_last_ts"], rec["balance_look_ts"] = b, t_read, t_read
            return None

        def advance():
            rec["balance_last"], rec["balance_last_ts"], rec["balance_look_ts"] = b, t_read, t_read

        if alerted:                               # already told: watch for the stake coming back (a refund / void)
            if abs(-step_d - (stake_d - certain)) < _EPS:
                rec["balance_rise_told"] = True
                rec["balance_msg_kind"] = "refund"
                advance()
                log.info(f"{label}: balance rose by the stake after the alert (a decline or a void)")
                return (f"🏇 {label}: after the earlier message, the {bookie} balance ROSE by {_money(stake_d)} "
                        f"(a decline or a void refund looks like this). Check the {bookie} app before "
                        f"assuming anything about this bet.")
            advance()
            return None

        need = step_d - stake_d - certain
        parts, via_others = None, False
        if abs(need) < _EPS:
            same = [i for s, i in doubt if abs(s - stake_d) < _EPS]
            if same:
                same = [str(i) for i in same]        # strings: they are saved on the follow record
                if rec.get("balance_withheld_ids") != same:          # once per withhold, not at every look
                    log.info(f"{label}: balance fell {_money(step_d)}, but the bot has its own {_money(stake_d)} "
                             f"send in doubt on this account: withheld, the previous look is kept")
                rec["balance_withheld_ids"] = same
                return None                       # NOT advanced: judged again at the next look
            parts = []
        else:
            parts = _explain(need, uncertain, []) if need > 0 else None      # ONLY the bot's own sends may help
            if parts is None and need > 0:
                with_others = _explain(need, uncertain, others)
                if with_others is not None:
                    parts, via_others = with_others, True
        prior_withheld = rec.get("balance_withheld_ids")
        if (parts is None and prior_withheld and step_d > 0
                and step_d - certain >= stake_d - _EPS and need > _EPS):
            # an earlier look withheld a step of exactly the stake because an own send of that size was in doubt;
            # that send has cleared (refused or placed elsewhere) and the step is now BIGGER than the stake with
            # nothing explaining the excess: the stake may be inside it. Never silence: "cannot tell".
            rec.pop("balance_withheld_ids", None)
            advance()
            rec["balance_alerted"] = "ambiguous"
            rec["balance_alert_ts"] = t_read
            log.info(f"{label}: a withheld step grew to {_money(step_d)} with {_money(need)} unexplained: "
                     f"cannot tell, sent")
            when = _when(rec, t_read)
            return (f"🏇 {label}: the {bookie} balance fell by {_money(step_d)}{when}. An earlier look of "
                    f"{_money(stake_d)} was held back because the bot had its own {_money(stake_d)} send in doubt on "
                    f"this account; that send has since cleared, so {_money(stake_d)} of the fall may be this bet "
                    f"being accepted, but {_money(need)} of it is not explained by anything the bot knows. This "
                    f"cannot tell whether the trader took it. Check the {bookie} app. Do not re-place anything on "
                    f"this message alone.")
        if parts is None:
            if abs(-step_d - (stake_d - certain)) < _EPS and rec.get("key") not in _RISE_LOGGED:
                _RISE_LOGGED.add(rec.get("key"))
                log.info(f"{label}: balance rose by the stake {_money(stake_d)} since the previous look "
                         f"(a decline if {bookie} held the stake, or a refund); not alerted")
            elif certain > 0 and abs(step_d - certain) < _EPS:
                log.info(f"{label}: balance step {step_d:+.2f} explained by own sends (the bot's own placed sends "
                         f"in the window total {_money(certain)})")
            else:
                log.warning(f"{label}: unexplained balance step {step_d:+.2f} (stake {_money(stake_d)}, the bot's own "
                            f"placed sends in the window {_money(certain)}, in doubt {uncertain}, other open "
                            f"referrals {others})")
            rec.pop("balance_withheld_ids", None)
            advance()
            return None
        rec.pop("balance_withheld_ids", None)
        advance()
        rec["balance_alert_ts"] = t_read
        when = _when(rec, t_read)
        if parts:
            sized = _money(step_d)
            how = f" ({_money(stake_d)} of it this bet, the rest {_describe(parts)})"
        else:
            sized, how = _money(stake_d), ""
        if via_others:
            # the step is only explained WITH other open referrals' stakes: it cannot be said that THIS bet was
            # accepted (an unrelated debit of the right size looks the same)
            how = f" ({_money(stake_d)} plus {_describe(parts)})"
            rec["balance_alerted"] = "ambiguous"
            log.info(f"{label}: balance fell {sized}{how}: only explained with other open referrals: ambiguous")
            return (f"🏇 {label}: the {bookie} balance fell by {sized}{when}{how}, so this cannot tell which of the "
                    f"open referrals the trader took. Check the {bookie} app. Do not re-place anything on this "
                    f"message alone.")
        if not parts and _cents(stake_d) in {_cents(s) for s in others}:
            rec["balance_alerted"] = "same_stake"
            log.info(f"{label}: balance fell {_money(stake_d)}, but another referred bet of the "
                     f"same stake is open on the account")
            return (f"🏇 {label}: the {bookie} balance fell by {sized}{when}, but another "
                    f"referred bet of the same stake is open on this account, so this cannot tell which one "
                    f"the trader took. Check the {bookie} app. Do not re-place anything on this message alone.")
        rec["balance_alerted"] = "fell"
        if rec.get("refusal_watch"):
            log.info(f"{label}: balance fell {sized} in one step{how}: BetRight may have taken a refused bet")
            return (f"🏇 {label}: BetRight may have taken a refused bet. The {bookie} balance fell by {sized}{when}{how}, "
                    f"equal to the stake of a bet the bot treated as REFUSED (BetRight's \"dividend changed\" reply) and "
                    f"spilled to the other accounts. If the bet is in the {bookie} app it is staked twice: check the app. "
                    f"Any other {_money(stake_d)} debit would look the same. This message holds, re-bets and records "
                    f"nothing.")
        log.info(f"{label}: balance fell {sized} in one step{how}: consistent with accepted")
        return (f"🏇 {label}: the {bookie} balance fell by {sized}{when}{how}: consistent with the trader having "
                f"ACCEPTED it. Check the {bookie} app before re-placing. Any other {_money(stake_d)} debit would look "
                f"the same, and HyperBot lists no open {bookie} bets, so no Bet Record row is written now: the bot "
                f"records it once it settles.")
    except Exception as e:
        log.warning(f"{label}: balance check failed ({e})")
        return None


# ── racing_placer trader-follow hooks (v6.172's TRADER_POLL_HOOKS, v6.173's TRADER_START_HOOKS) ──

def _label(rec: dict) -> str:
    tip = rec.get("tip") or {}
    try:
        import session_priority
        acct = session_priority.session_label(rec.get("session_id"), rec.get("bookie"))
    except Exception:
        acct = f"{rec.get('bookie')} s{rec.get('session_id')}"
    try:
        stake = f"${float(rec.get('stake') or 0):.2f}"
    except (TypeError, ValueError):
        stake = "$?"
    return (f"{rec.get('runner') or tip.get('runner') or '?'} - {tip.get('track', '')} "
            f"R{tip.get('race_num', '')} {tip.get('market', '')}: {stake} @ {rec.get('odds')} on {acct}")


def _persist(rec: dict, **fields) -> None:
    rec.update(fields)
    try:
        import racing_placer
        if rec.get("key") and not racing_placer.follow_update(rec["key"], **fields):
            log.error(f"{_label(rec)}: could not save {sorted(fields)} on the follow record (a "
                      f"restart may repeat the balance message)")
    except Exception as e:
        log.error(f"could not save the balance state on the follow ({e})")


def _other_open_stakes(rec: dict) -> list:
    """Stakes of the OTHER trader follows running in this process on the same session: every live follow, with no
    age limit (a 3-hour-old referral can still be the one accepted). One already reported counts too while this
    record's step can still be claimed by it: it was alerted at or after this record's previous look (the very
    step being judged, e.g. two same-stake referrals and one debit); a referral alerted for an earlier step is
    settled and does not count."""
    try:
        import racing_placer
        with racing_placer._FOLLOWS_LOCK:
            live = list(racing_placer._LIVE_RECS.values())
        try:
            t0 = float(rec.get("balance_look_ts") or rec.get("balance_last_ts") or 0)
        except (TypeError, ValueError):
            t0 = 0.0
        out = []
        for r in live:
            if r is rec or r.get("key") == rec.get("key") or str(r.get("session_id")) != str(rec.get("session_id")):
                continue
            if r.get("balance_alerted"):
                try:
                    if float(r.get("balance_alert_ts") or 0) < t0:
                        continue
                except (TypeError, ValueError):
                    continue
            out.append(float(r.get("stake") or 0))
        return out
    except Exception:
        return []


def _sends_for(rec: dict, t0: float, t1: float):
    """The bot's own sends on this session overlapping (t0, t1], this referral's tip left out: the process-wide
    register, plus the in-flight file's records written by an EARLIER process (a restart in between), which are
    in doubt. None when the in-flight file cannot be read."""
    sid = str(rec.get("session_id"))
    tip_id = str((rec.get("tip") or {}).get("id"))
    out = trader_send_registry.window(sid, t0, t1, exclude_tip=tip_id)
    try:
        import racing_placer
        recs, ok = racing_placer._inflight_read(racing_placer.Path(racing_placer.TRADER_INFLIGHT_PATH))
        if not ok:
            return None
        for r in recs:
            if (str(r.get("session_id")) == sid and str((r.get("tip") or {}).get("id")) != tip_id
                    and r.get("pid") not in (None, os.getpid())):
                sent = float(r.get("sent_ts") or 0)
                if sent <= t1 and time.time() - sent <= FOREIGN_INFLIGHT_SEC:
                    out.append({"sid": sid, "stake": float(r.get("stake") or 0), "start": sent, "end": None,
                                "outcome": None, "actual": None, "tip": str((r.get("tip") or {}).get("id"))})
    except Exception as e:
        log.warning(f"in-flight read for the balance watch failed ({e})")
        return None
    return out


def start_hook(rec: dict, hb):
    """At the referral, before the follow's first wait: the starting balance (persisted on the
    follow record). Returns None (nothing to say yet)."""
    if not ENABLED or rec.get("balance_b0") is not None:
        return None
    for attempt in (1, 2):                   # one retry: a missed start leaves a late baseline
        b = read_balance(hb, rec.get("session_id"))
        t_read = time.time()                 # dated by the return of the read (see check)
        if b is not None:
            _persist(rec, balance_b0=b, balance_b0_ts=t_read, balance_last=b, balance_last_ts=t_read)
            rec["balance_look_ts"] = t_read
            log.info(f"{_label(rec)}: balance at the referral ${b:.2f}")
            _ensure_poller(rec, hb)
            return None
    log.warning(f"{_label(rec)}: balance at the referral could not be read; the first poll's "
                f"balance is the baseline (an acceptance before it would not show)")
    return None


_LOCK = threading.RLock()      # the fast poller and the follow's own cycle never check one record at once


def poll_hook(rec: dict, hb, reading=None, others=None):
    """Each poll of the follow (and each fast poll): the balance compared with the previous look (see check). The
    marker is saved on the follow BEFORE the text is handed back to be sent. `reading`: a balance already read
    for the session this pass; `others`: the stakes of the other open referrals as they stood when the pass began
    (so two same-stake referrals both see each other). A refund line (after an alert) goes out here as an info
    and returns None."""
    if not ENABLED or (rec.get("balance_alerted") and rec.get("balance_rise_told")):
        return None
    with _LOCK:
        if rec.get("balance_alerted") and rec.get("balance_rise_told"):
            return None
        work = dict(rec)
        work.pop("balance_msg_kind", None)
        try:
            label = _label(rec)
        except Exception:
            label = f"trader follow {rec.get('key')}"
        text = check(work, hb, label=label, sends=lambda t0, t1: _sends_for(rec, t0, t1),
                     other_open_stakes=_other_open_stakes(rec) if others is None else others, reading=reading)
        changed = {}
        for k in ("balance_b0", "balance_b0_ts"):
            if work.get(k) is not None and work.get(k) != rec.get(k):
                changed[k] = work[k]
        if work.get("balance_last") is not None and work.get("balance_last") != rec.get("balance_last"):
            # saved only when the balance CHANGED: an unchanged look would rewrite the follows file every 20 s
            changed["balance_last"], changed["balance_last_ts"] = work["balance_last"], work.get("balance_last_ts")
        for k in ("balance_alerted", "balance_rise_told", "balance_alert_ts"):
            if work.get(k) and work.get(k) != rec.get(k):
                changed[k] = work[k]
        if work.get("balance_look_ts") is not None:
            rec["balance_look_ts"] = work["balance_look_ts"]          # in memory, saved with a withhold change (below)
        if work.pop("balance_persist_look", None) and work.get("balance_look_ts") is not None:
            changed["balance_look_ts"] = work["balance_look_ts"]       # a restart re-baseline: saved
            changed["balance_last_ts"] = work.get("balance_last_ts")
        new_w = [str(i) for i in (work.get("balance_withheld_ids") or [])]
        old_w = [str(i) for i in (rec.get("balance_withheld_ids") or [])]
        if new_w != old_w:
            # a withhold set or cleared is SAVED (the ids as strings, [] = cleared) with the look time, so a restart
            # cannot judge the step it holds: the resumed record is re-baselined silently (see check)
            changed["balance_withheld_ids"] = new_w
            if work.get("balance_look_ts") is not None:
                changed["balance_look_ts"] = work["balance_look_ts"]
        if new_w:
            rec["balance_withheld_ids"] = new_w
        else:
            rec.pop("balance_withheld_ids", None)
        if changed:
            _persist(rec, **changed)
        if work.get("balance_msg_kind") == "refund" and text:
            try:
                import notifier
                notifier.notify_info(text)
            except Exception as e:
                log.warning(f"{label}: the refund line could not be sent ({e})")
            return None
        if rec.get("balance_b0") is not None:
            _ensure_poller(rec, hb)
        return text


# ── the fast poll: one thread per session ────────────────────────────────────────────────────────────

_STOP = threading.Event()
_POLLERS: dict = {}            # session id -> its thread
_POLLERS_LOCK = threading.Lock()
_HBS: dict = {}                # session id -> the client the hooks were given


def _referral_age(rec: dict):
    try:
        ref = rec.get("submit_ts") or rec.get("balance_b0_ts")
        return time.time() - float(ref) if ref else None
    except (TypeError, ValueError):
        return None


def _deadline(rec: dict):
    try:
        if rec.get("deadline_ts"):
            return float(rec["deadline_ts"])
        ref = rec.get("submit_ts") or rec.get("balance_b0_ts")
        return float(ref) + _DEFAULT_FOLLOW_SEC if ref else None
    except (TypeError, ValueError):
        return None


def _watching(rec: dict) -> bool:
    """Is this follow still worth a poller look: it has a baseline, its deadline has not passed, it is inside the
    WATCH_SEC (2 h) after the referral (the follow's own cycle, 5 minutes for the first hour then 15, covers the rest), and it is not finished
    (alerted AND its refund watch told)."""
    if rec.get("balance_b0") is None or (rec.get("balance_alerted") and rec.get("balance_rise_told")):
        return False
    age = _referral_age(rec)
    if age is None or age > WATCH_SEC:
        return False
    dl = _deadline(rec)
    return dl is not None and time.time() <= dl


def _fast_candidates(session_id) -> list:
    """The live trader follows on this session the poller still looks at."""
    try:
        import racing_placer
        with racing_placer._FOLLOWS_LOCK:
            live = list(racing_placer._LIVE_RECS.values())
    except Exception:
        return []
    return [r for r in live if str(r.get("session_id")) == str(session_id) and _watching(r)]


def _interval(cands) -> float:
    """FAST_POLL_SEC while any candidate's referral is inside the fast window, else SLOW_POLL_SEC."""
    ages = [a for a in (_referral_age(r) for r in cands) if a is not None]
    return FAST_POLL_SEC if any(a <= FAST_WINDOW_SEC for a in ages) else SLOW_POLL_SEC


def _ensure_poller(rec: dict, hb) -> None:
    """Start this session's poller if it is not running and the follow is still being watched. One thread per
    session, a daemon, stopped by shutdown()."""
    if not (ENABLED and FAST_POLL_ENABLED) or _STOP.is_set() or not _watching(rec):
        return
    sid = str(rec.get("session_id"))
    with _POLLERS_LOCK:
        _HBS[sid] = hb
        th = _POLLERS.get(sid)
        if th is not None and th.is_alive():
            return
        th = threading.Thread(target=_poller, args=(sid,), name=f"balance-poll-{sid}", daemon=True)
        _POLLERS[sid] = th
        th.start()


def poll_session_once(session_id) -> int:
    """One pass over the session: ONE balance read, shared by every candidate follow on it; an alert goes out through
    the notifier. Returns the number of alerts sent. Never raises."""
    sent = 0
    hb = _HBS.get(str(session_id))
    cands = _fast_candidates(session_id)
    if not cands:
        return 0
    t_read = time.time()
    b = read_balance(hb, session_id)
    t_ret = time.time()                       # the window of the bot's own sends ends when the read RETURNED
    if b is None:
        return 0
    for rec in cands:
        try:
            # the other open referrals are every live follow on the session (not only the ones this poller watches:
            # a 3-hour-old referral can still be the one accepted), worked out at each record's turn so that a
            # referral alerted a moment ago for this very step still counts (see _other_open_stakes)
            text = poll_hook(rec, hb, reading=(b, t_read, t_ret), others=None)
            if text:
                import notifier
                notifier.notify_critical(text)
                sent += 1
        except Exception as e:
            log.warning(f"fast balance poll for s{session_id} failed ({e})")
    return sent


def _poller(session_id: str) -> None:
    try:
        while not _STOP.is_set():
            with _POLLERS_LOCK:
                # decide to exit AND leave the register in one step: a referral arriving now finds no thread and
                # starts a new one, never a poller that is about to end
                cands = _fast_candidates(session_id)
                if not cands:
                    if _POLLERS.get(session_id) is threading.current_thread():
                        _POLLERS.pop(session_id, None)
                    return
                wait = _interval(cands)
            poll_session_once(session_id)
            if _STOP.wait(wait):
                break
    finally:
        with _POLLERS_LOCK:
            if _POLLERS.get(session_id) is threading.current_thread():
                _POLLERS.pop(session_id, None)


# ── the refusal watch (v6.227): a safety net under BetRight's "dividend changed" refusal ────────────────────────
# BetRight's "[odds_moved] dividend changed" reply is treated as a clean refusal at any speed and the stake spills.
# None of the 18 such replies since June ever landed, but the reply is the bookie answering, so a landing cannot be
# ruled out for good. This watches the account's balance for the SAME windows as a referral (every FAST_POLL_SEC for
# FAST_WINDOW_SEC after the refusal, then every SLOW_POLL_SEC until WATCH_SEC) and uses check()'s step matching and the
# send register unchanged: ONE fall of exactly the stake that nothing the bot sent explains is alerted as "BetRight may
# have taken a refused bet". Alert only: no follow, no hold, no ledger row, nothing re-bet, nothing persisted. The
# record is in memory (a restart ends it) and the baseline is the first read after the refusal (a debit made before
# it is not seen).

def watch_refused(hb, *, session_id, bookie, stake, tip=None, odds=None, runner=None, now=None):
    """Start the refusal watch for one refused bet. Returns its record (also when the poller is off, e.g. under test),
    or None when the watch is off or the stake is not a positive finite number. Never raises, never blocks."""
    try:
        if not ENABLED:
            return None
        s = float(stake)
        if isinstance(stake, bool) or not math.isfinite(s) or s <= 0:
            return None
        t = time.time() if now is None else float(now)
        tip = tip or {}
        rec = {"session_id": str(session_id), "bookie": str(bookie or ""), "stake": round(s, 2), "odds": odds,
               "runner": runner or tip.get("runner"),
               "tip": {"id": None, "track": tip.get("track"), "race_num": tip.get("race_num"),
                       "market": tip.get("market")},       # id None: the same tip's OTHER sends on the account count
               "submit_ts": t, "deadline_ts": t + WATCH_SEC, "refusal_watch": True,
               "key": f"refusal|{tip.get('id', '?')}|{session_id}|{round(s * 100)}|{t:.3f}"}
        if FAST_POLL_ENABLED and not _STOP.is_set():
            threading.Thread(target=_refusal_watch, args=(rec, hb), name=f"refusal-watch-{session_id}",
                             daemon=True).start()
        return rec
    except Exception as e:
        log.warning(f"refusal watch not started ({e})")
        return None


def refusal_poll_once(rec: dict, hb) -> str:
    """One look of the refusal watch (the first one only takes the baseline). An alert goes out through the notifier.
    Returns the message or None. Never raises."""
    try:
        text = check(rec, hb, label=_label(rec), sends=lambda t0, t1: _sends_for(rec, t0, t1), other_open_stakes=())
        if text:
            import notifier
            notifier.notify_critical(text)
        return text
    except Exception as e:
        log.warning(f"refusal watch look failed ({e})")
        return None


def _refusal_watch(rec: dict, hb) -> None:
    try:
        while not _STOP.is_set():
            age = time.time() - float(rec["submit_ts"])
            if age > WATCH_SEC or rec.get("balance_alerted"):
                return
            refusal_poll_once(rec, hb)
            if _STOP.wait(FAST_POLL_SEC if age <= FAST_WINDOW_SEC else SLOW_POLL_SEC):
                return
    except Exception as e:
        log.warning(f"refusal watch ended on an error ({e})")


def shutdown(timeout: float = 5.0) -> None:
    """Stop every fast poller (they are daemons and wake at once). Safe to call twice."""
    _STOP.set()
    with _POLLERS_LOCK:
        threads = list(_POLLERS.values())
    for th in threads:
        if th is not threading.current_thread():
            th.join(timeout)


def _restart_for_tests() -> None:
    _STOP.clear()
    with _POLLERS_LOCK:
        _POLLERS.clear()
        _HBS.clear()


atexit.register(shutdown, 1.0)


def register() -> None:
    """Add the hooks to racing_placer's trader follow (once). Safe to call more than once."""
    try:
        import racing_placer
        for name, fn in (("TRADER_START_HOOKS", start_hook), ("TRADER_POLL_HOOKS", poll_hook)):
            hooks = getattr(racing_placer, name, None)
            if isinstance(hooks, list) and fn not in hooks:
                hooks.append(fn)
    except Exception as e:
        log.warning(f"could not register the trader balance hooks ({e})")
