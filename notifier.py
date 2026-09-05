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
from requests.exceptions import ReadTimeout  # v5.71: module-level so the
# except clause resolves independently of a mocked `requests` in tests
import logging
import os
import json
import threading
import sys
from config import NOTIFY_BOT_TOKEN, NOTIFY_CHAT_ID, NOTIFY_SUCCESS_CHAT_ID

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"

# ── v6.14 UN-POPPABLE TEST GUARD ─────────────────────────────────────────────
# Set True ONLY by a test that exercises the transport with requests.post patched, so
# nothing can leave the machine. Everything else is blocked under pytest by default.
_ALLOW_TEST_TRANSPORT = False


def _blocked_by_test_guard() -> bool:
    """True when this process must not send a real Telegram.

    v6.14. The TIPBOT_TESTING env var was not a sufficient guard, because any test can
    unset it for the WHOLE process: test_notify_rate_limit.py did exactly that at module
    level (`os.environ.pop("TIPBOT_TESTING", None)`), so every test collected afterwards
    ran unguarded.

    MEASURED 2026-08-22 with a network spy: one full-suite run made 13 REAL Telegram
    calls to the maintenance channel, all "Saiyan top-up: $100.00 more on Patrick
    Dangerfield". The pre-commit gate runs the full suite on EVERY commit, so five commits
    in a day sent roughly 65 junk messages that never appear in logs/tipbot.log, because a
    test process logs to a temp file. That is both the spam Wilson saw and a block of
    UNLOGGED traffic against the bot token, which is very likely part of why the 429
    analysis kept coming up short.

    So the guard no longer trusts an env var alone: `pytest in sys.modules` cannot be
    popped by a test. Fails CLOSED, i.e. blocked, which is the safe direction.
    """
    if _ALLOW_TEST_TRANSPORT:
        return False
    if "pytest" in sys.modules:
        return True
    return os.getenv("TIPBOT_TESTING", "").strip().lower() in ("1", "true", "yes")

# ── v6.13 DURABLE NOTIFICATION QUEUE ─────────────────────────────────────────
# A message that cannot be sent now is kept on disk and sent later. Nothing is ever
# discarded except the one case where resending would DUPLICATE (see _nq_drain).
#
# Why a queue and not more retries: measured on 2026-08-21, Telegram refused a single
# message sent after five hours of silence, at 6/min against a ~20/min limit, across all
# four chats at once, with zero 400s. The throttle is adaptive and on their side, so no
# retry policy can beat it. Late delivery of a bet log is fine. Loss is not.
_NQ_PATH = os.getenv("NOTIFY_QUEUE_PATH", "logs/notify_queue.jsonl")
# Pace: Telegram's group-chat guidance is ~20 messages/minute, so 4s between sends (15/min)
# leaves headroom. Only applies when there is a backlog; a lone message goes immediately.
_NQ_MIN_INTERVAL = float(os.getenv("NOTIFY_MIN_INTERVAL_SEC", "4.0"))
# How long a message may sit before we shout about it in the log. It stays queued.
_NQ_STALE_SEC = float(os.getenv("NOTIFY_STALE_ALERT_SEC", "900"))
# After a 429, stop sending on EVERY chat for this long. Telegram asked for 8s on
# 2026-08-21; 20s is deliberately generous because the documented penalty for knocking
# early is an ESCALATING cooldown, so over-waiting is cheap and under-waiting is not.
_NQ_COOLDOWN_SEC = float(os.getenv("NOTIFY_COOLDOWN_SEC", "20.0"))
_nq_lock = threading.Lock()
_nq_thread = None
_nq_cooldown_until = 0.0     # global: set by a 429, blocks ALL chats (the limit is bot-wide)
_nq_last_outcome = ""        # set by _send_now so the drainer can classify the failure
_nq_last_send_ts = 0.0
# v6.18: how many sends are mid-POST. A record is popped only AFTER _send_now returns,
# so an empty QUEUE FILE does not mean nothing is in flight; flush() waits on this too.
_nq_inflight = 0


_NQ_LOCK_ACQUIRE_SEC = float(os.getenv("NOTIFY_LOCK_ACQUIRE_SEC", "5"))
_NQ_SEND_SUFFIX = ".send.lock"      # held across a transmission, drainers only
_NQ_FILE_SUFFIX = ".lock"           # held across a file read/write, everyone


class _nq_xlock:
    """Cross-PROCESS advisory lock. Non-blocking acquire with a retry deadline.

    v6.18. `_nq_lock` is a `threading.Lock`, which is process-local. main.py and
    x_watcher.py import notifier and run as separate long-lived interpreters against the
    SAME logs/notify_queue.jsonl, so the pacing gate, the bot-wide 429 cooldown and the
    read-decide-write pop shared nothing between them. A duplicate BET PLACED is worse
    than a late one, because it reads as a double bet.

    Two distinct locks, and the split is the whole point:

      `.lock`       fast file mutations (append, pop). NEVER held across a POST, because
                    enqueue takes it and an alert must never wait on the network.
      `.send.lock`  the whole read-pace-send-pop cycle, taken by DRAINERS ONLY. This is
                    what actually closes the duplicate window, and it makes the 4s pacing
                    gate and the bot-wide 429 cooldown effective ACROSS processes, which
                    matters because Telegram's 429 is per-bot: knocking again before it
                    expires escalates the block.

    NOT blocking-with-`LK_LOCK`: msvcrt retries ~10 times at 1s then raises, and a POST is
    allowed 20s, so a blocking acquire would time out mid-transmission and fail open into
    exactly the race it exists to prevent. LK_NBLCK plus our own deadline is explicit.

    `fail_open=True` (file lock): on failure, proceed unlocked and warn. An unavailable
    lock must degrade to the old racy behaviour rather than block a money alert.
    `fail_open=False` (send lock): on failure, `acquired` stays False and the caller must
    NOT transmit. Another drainer owns transmission, and a delayed alert beats a duplicate.

    The sentinel is a SEPARATE file, never the queue itself: _nq_save does an os.replace,
    an atomic rename that would silently detach a lock held on the old inode. Nothing ever
    WRITES to the sentinel and nothing should: the locked range is byte 0, and content
    would make the locked byte depend on each opener's append-mode seek position, which
    defeats mutual exclusion silently. It is never deleted either, because removing a lock
    file another process holds a handle to is the actual anti-pattern.
    """

    def __init__(self, path: str, suffix: str = _NQ_FILE_SUFFIX,
                 timeout: float = None, fail_open: bool = True):
        self._path = path + suffix
        self._timeout = _NQ_LOCK_ACQUIRE_SEC if timeout is None else timeout
        self._fail_open = fail_open
        self._fh = None
        self.acquired = False

    def __enter__(self):
        deadline = time.time() + max(0.0, self._timeout)
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            fh = open(self._path, "a+b")
        except Exception as e:
            log.debug(f"notify lock file {self._path} unusable ({e})")
            return self
        while True:
            try:
                try:
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except ImportError:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                self.acquired = True
                return self
            except Exception:
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
        try:
            fh.close()
        except Exception:
            pass
        if self._fail_open:
            log.warning(
                f"notify queue: could not take {os.path.basename(self._path)} within "
                f"{self._timeout:.1f}s — proceeding UNLOCKED (a concurrent write is "
                f"possible; a message could be duplicated or lost)")
        return self

    def __exit__(self, *exc):
        if self._fh is None:
            return False
        try:
            try:
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        self.acquired = False
        return False


def _nq_shared_pacing() -> tuple:
    """(last_send_ts, cooldown_until) as last written by ANY process.

    v6.18. Both were process-local globals. Telegram's 429 is bot-wide, so when main.py
    got throttled and set a 20s cooldown, x_watcher.py knew nothing and kept knocking,
    which Telegram's own docs say ESCALATES the block. Read under the send lock.
    """
    try:
        with open(_NQ_PATH + ".pacing", encoding="utf-8") as f:
            d = json.load(f)
        return float(d.get("last_send_ts") or 0), float(d.get("cooldown_until") or 0)
    except Exception:
        return 0.0, 0.0


def _nq_write_shared_pacing(last_send_ts: float, cooldown_until: float) -> None:
    try:
        os.makedirs(os.path.dirname(_NQ_PATH) or ".", exist_ok=True)
        with open(_NQ_PATH + ".pacing", "w", encoding="utf-8") as f:
            json.dump({"last_send_ts": last_send_ts,
                       "cooldown_until": cooldown_until}, f)
    except Exception as e:
        log.debug(f"notify pacing state not written: {e}")


def flush(timeout: float = 60.0) -> int:
    """Drain the queue SYNCHRONOUSLY. Returns the number still pending.

    v6.18. For one-shot scripts. Since v6.13 `_send` only enqueues and delivery happens on
    a `daemon=True` thread, so a script that sends and then returns from main() kills the
    drainer before the POST. check_session_health.py does exactly that, and it is the
    outage backstop: it has not alerted since 2026-06-14, so there are 71 days with no
    evidence in either direction and, since v6.13, a specific reason to think it could not.

    WAITS ON THE IN-FLIGHT SEND, not just on the file. A record is popped only AFTER
    `_send_now` returns, so an empty-file check alone can read "done" while a POST is still
    open. Exiting there kills the socket mid-request with the record still queued, and the
    next scheduled run resends it: a possible DUPLICATE, in exactly the rate-limited case
    this machinery exists for. The default budget is sized for the worst legitimate case
    (a 20s bot-wide cooldown plus a 20s POST plus slack) rather than the 20s that a single
    cooldown could eat on its own.

    Call this at the end of any short-lived process that sends notifications.
    """
    _nq_start()
    end = time.time() + max(0.0, timeout)
    while time.time() < end:
        if not _nq_load() and _nq_inflight == 0:
            return 0
        time.sleep(0.25)
    left = len(_nq_load())
    if left or _nq_inflight:
        log.error(
            f"notify flush: {left} message(s) still queued after {timeout:.0f}s"
            + (" and a send is STILL IN FLIGHT — exiting now may resend it on the next "
               "run" if _nq_inflight else "")
            + ". They stay on disk and go out on the next run.")
    return left


def _nq_mark(outcome: str) -> None:
    """Record WHY the last _send_now failed, so the drainer can act on it.

    Three classes, and the distinction is money-critical:
      throttled  -> temporary, message is fine, REQUEUE (429, connect failure)
      uncertain  -> the POST was transmitted, so a resend DUPLICATES. Drop, loudly.
      (anything) -> permanent rejection, drop; requeueing would spin forever.
    """
    global _nq_last_outcome
    _nq_last_outcome = outcome
    if outcome == "throttled":
        # A 429 is bot-wide, not per-chat: on 2026-08-21 it hit all four chats at once.
        # So pause EVERY chat, and honour Telegram's own delay. The docs are explicit that
        # knocking again before it expires escalates the cooldown.
        global _nq_cooldown_until
        _nq_cooldown_until = max(_nq_cooldown_until, time.time() + _NQ_COOLDOWN_SEC)


def _nq_enqueue(text: str, chat_id: str, parse_mode: str) -> bool:
    """Append to the durable queue and make sure the drainer is running."""
    if not NOTIFY_BOT_TOKEN or not chat_id:
        log.warning("Notification bot not configured, skipping")
        print(f"[NOTIFY] {text}")
        return False
    rec = {"text": text, "chat_id": str(chat_id), "parse_mode": parse_mode,
           "queued_at": time.time(), "attempts": 0}
    try:
        with _nq_lock:
            os.makedirs(os.path.dirname(_NQ_PATH) or ".", exist_ok=True)
            # v6.18: also take the CROSS-PROCESS lock. main.py and x_watcher.py append to
            # the same file from separate interpreters, sharing no thread lock.
            with _nq_xlock(_NQ_PATH):
                with open(_NQ_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
    except Exception as e:
        # Last resort: the queue file is unwritable. Try the wire directly rather than
        # lose the message, which is the whole point of this machinery.
        log.error(f"notify queue unwritable ({e}) — sending inline instead")
        return _send_now(text, chat_id, parse_mode)
    _nq_start()
    return True


def _nq_thread_name(path: str) -> str:
    """One drainer per queue FILE, which is the invariant that actually matters."""
    try:
        return "notify-queue:" + os.path.abspath(path)
    except Exception:
        return "notify-queue:" + str(path)


def _nq_start() -> None:
    global _nq_thread
    with _nq_lock:
        if _nq_thread is not None and _nq_thread.is_alive():
            return
        # v6.18: ONE DRAINER PER PROCESS, checked by thread name rather than by this
        # module global alone. `importlib.reload` re-executes the module into the SAME
        # globals dict, so it resets `_nq_thread` to None while the existing drainer
        # thread keeps running against those very globals — and then reads the NEW
        # _NQ_PATH. Trusting the global alone starts a second drainer on the same file,
        # and two drainers popping the same queue is how a message gets sent twice or out
        # of order. Reload only happens in tests today, but the invariant is the point:
        # nothing else in the file enforces it.
        _me = threading.current_thread()
        _name = _nq_thread_name(_NQ_PATH)
        for _t in threading.enumerate():
            if _t.name == _name and _t.is_alive() and _t is not _me:
                _nq_thread = _t
                return
        _nq_thread = threading.Thread(target=_nq_drain, name=_name, daemon=True)
        _nq_thread.start()
    log.info(f"notify queue drainer started (pace {_NQ_MIN_INTERVAL:.1f}s between sends)")


def _nq_load() -> list:
    try:
        if not os.path.exists(_NQ_PATH):
            return []
        out = []
        with open(_NQ_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    # v6.18: SAY SO. This was a bare continue, unlike the outer
                    # handler below. Two os._exit watchdogs and a power-cut
                    # history can kill the process mid-append, and a lost alert
                    # with nothing to grep for is the exact failure class this
                    # queue exists to prevent.
                    log.error(f"notify queue: DISCARDING a torn line "
                              f"({line[:80]!r}) - a message may have been lost")
                    continue
        return out
    except Exception as e:
        log.error(f"notify queue unreadable: {e}")
        return []


def _nq_save(items: list) -> None:
    try:
        tmp = _NQ_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it) + "\n")
        os.replace(tmp, _NQ_PATH)
    except Exception as e:
        log.error(f"notify queue not saved: {e}")


def _nq_drain() -> None:
    """Send queued messages, oldest first, pacing and honouring Telegram's cooldown."""
    global _nq_cooldown_until, _nq_last_send_ts
    idle = 0
    blocked = 0        # consecutive failures to take the transmission lock
    my_path = _NQ_PATH
    while True:
        try:
            # v6.18: retire if the queue path has moved out from under us. `_nq_start`
            # keys the one-drainer-per-file guard on the path, so a drainer left bound to
            # a stale one must step aside rather than linger and pop a file it no longer
            # owns. (importlib.reload re-executes into the SAME globals dict, so a running
            # drainer really can find _NQ_PATH changed beneath it.)
            if _NQ_PATH != my_path:
                log.info(f"notify queue path changed ({my_path} -> {_NQ_PATH}) — "
                         f"this drainer is retiring")
                return
            with _nq_lock, _nq_xlock(_NQ_PATH):
                items = _nq_load()
            if not items:
                idle += 1
                # Exit after ~5 minutes idle; the next enqueue restarts us.
                if idle > 150:
                    log.info("notify queue empty — drainer exiting until next message")
                    return
                time.sleep(2)
                continue
            idle = 0

            # v6.18: THE TRANSMISSION LOCK, and this is the one that closes the duplicate
            # window. The file lock alone does NOT: it is released across the POST, so two
            # drainers in two processes could each read the same head, each transmit it,
            # and only the pop would be serialised. Held from here through the pop.
            #
            # Deliberately NOT wrapped around the idle branch above: main.py's drainer
            # idles for up to 5 minutes before exiting, and holding this across that would
            # lock x_watcher.py out of sending anything at all for those 5 minutes.
            with _nq_xlock(_NQ_PATH, suffix=_NQ_SEND_SUFFIX, timeout=1.0,
                           fail_open=False) as _sendlock:
                if not _sendlock.acquired:
                    # Another process is mid-transmission. Wait rather than race it.
                    #
                    # Normally this resolves in a second or two. But `acquired` is also
                    # False when the sentinel file itself cannot be OPENED (a directory
                    # ACL, an AV handle), and that state never resolves: the drainer would
                    # loop here forever, and because the stale-BACKLOG alarm lives INSIDE
                    # this lock it could not fire either — the one mechanism meant to say
                    # a queue is stuck, disabled by the thing that stuck it. So count the
                    # consecutive misses and escalate.
                    blocked += 1
                    if blocked in (30, 300) or (blocked and blocked % 900 == 0):
                        log.error(
                            f"notify queue: could not take the transmission lock "
                            f"{blocked} times in a row (~{blocked}s). Either another "
                            f"process is wedged mid-send, or "
                            f"{os.path.basename(_NQ_PATH)}{_NQ_SEND_SUFFIX} cannot be "
                            f"opened. NOTHING IS BEING SENT.")
                    time.sleep(1.0)
                    continue
                blocked = 0

                # Re-read under the lock: the other drainer may have just sent this record
                # and popped it while we were queueing for the lock.
                with _nq_lock, _nq_xlock(_NQ_PATH):
                    items = _nq_load()
                if not items:
                    continue

                now = time.time()
                _shared_last, _shared_cool = _nq_shared_pacing()
                _nq_cooldown_until = max(_nq_cooldown_until, _shared_cool)
                if now < _nq_cooldown_until:
                    time.sleep(min(5.0, _nq_cooldown_until - now))
                    continue
                gap = _NQ_MIN_INTERVAL - (now - max(_nq_last_send_ts, _shared_last))
                if len(items) > 1 and gap > 0:
                    time.sleep(gap)

                rec = items[0]
                waited = time.time() - float(rec.get("queued_at") or 0)
                if waited > _NQ_STALE_SEC and rec.get("attempts", 0) % 20 == 0:
                    log.error(
                        f"notify queue BACKLOG: oldest message has waited "
                        f"{waited / 60:.0f}min ({len(items)} queued). NOT lost, still "
                        f"retrying. preview: {str(rec.get('text'))[:70]}")

                # v6.19a: RE-VERIFY OWNERSHIP IMMEDIATELY BEFORE TRANSMITTING. The
                # top-of-loop check is up to ~9 s stale by now (a 4 s pacing gap plus a
                # 5 s cooldown sleep both sit between them), and in that window another
                # drainer can take over this queue. In production there is only ever one
                # path so this never fires; under test, importlib.reload swaps _NQ_PATH
                # underneath a running drainer, which is the residual cause of the
                # intermittent test_order_is_preserved failure that v6.18's per-file
                # thread naming reduced but did not eliminate (~1 run in 4).
                if _NQ_PATH != my_path:
                    log.info(f"notify queue path changed to {_NQ_PATH} while holding the "
                             f"send lock - retiring WITHOUT sending")
                    return

                global _nq_last_outcome, _nq_inflight
                _nq_last_outcome = ""
                _nq_last_send_ts = time.time()
                _nq_inflight += 1
                try:
                    ok = _send_now(rec["text"], rec.get("chat_id", ""),
                                   rec.get("parse_mode", "HTML"))
                finally:
                    _nq_inflight -= 1
                outcome = _nq_last_outcome
                # Publish pacing so the OTHER process honours the same gap and, more
                # importantly, the same 429 cooldown (_send_now -> _nq_mark may have just
                # extended it).
                _nq_write_shared_pacing(_nq_last_send_ts, _nq_cooldown_until)

                # The read-decide-write POP must be ONE atomic file operation, so every
                # branch below stays inside this `with`.
                with _nq_lock, _nq_xlock(_NQ_PATH):
                    cur = _nq_load()
                    # Only pop if the head is still the record we just sent: another
                    # process or an inline fallback could have rewritten the file
                    # underneath us.
                    if cur and cur[0].get("queued_at") == rec.get("queued_at") \
                            and cur[0].get("text") == rec.get("text"):
                        head, rest = cur[0], cur[1:]
                    else:
                        head, rest = None, cur

                    if ok:
                        _nq_save(rest if head is not None else cur)
                    elif outcome == "throttled":
                        # Requeue at the FRONT: order matters for a bet log, and the
                        # message is fine, only the timing was wrong.
                        if head is not None:
                            head["attempts"] = int(head.get("attempts", 0)) + 1
                            _nq_save([head] + rest)
                    elif outcome == "uncertain":
                        # A read timeout means the POST was TRANSMITTED and very likely
                        # delivered. Resending duplicates it (the 2026-06-16 Turang/Olson
                        # double-send). This is the ONE case we drop, and we say so loudly.
                        log.error(
                            f"notify DELIVERY-UNCERTAIN, dropping to avoid a duplicate "
                            f"(chat={rec.get('chat_id')}): {str(rec.get('text'))[:80]}")
                        _nq_save(rest if head is not None else cur)
                    else:
                        # Permanent (400 chat-not-found, bot blocked, malformed):
                        # requeueing would spin forever; _send_now logged NOTIFY LOST.
                        _nq_save(rest if head is not None else cur)
        except Exception as e:
            log.error(f"notify queue drainer error: {e}")
            time.sleep(5)

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


def session_label(session_id, fallback_bookie: str = "") -> str:
    """Public alias for _session_label, for callers outside this module.

    v6.12 (Wilson): every operator-facing alert must name the ACCOUNT, not a session id.
    "sportsbet:118458" is unreadable at 3am; "Denzel Sportsbet (s118458)" is not. Three
    alert sites still rendered the raw pair: the sports ambiguous CRITICAL, and both
    deferred-verify CRITICALs (sports and racing). Those are exactly the messages that ask
    the operator to go and check an account by hand, so they are the worst place to print
    a number nobody remembers.
    """
    return _session_label(session_id, fallback_bookie)


# NOTE: the canonical implementation now lives in session_priority.session_label, which
# owns the metadata and, crucially, is never replaced by a test stub the way `notifier` is.
# This module keeps its own copy because _session_label predates it and is used by the
# BET PLACED / BET UNFILLED summaries; the two render identically.


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
    """Hand a message to the durable queue. Returns True once it is safely enqueued.

    v6.13. Wilson: "i rlly need the problem fixed as its crucial i dont miss this msgs".

    Every previous fix here tried harder to send RIGHT NOW, and each one still lost
    messages, because the failure is not ours to control. MEASURED on 2026-08-21: the very
    first 429 landed on a SINGLE message after a five-hour-thirteen-minute silence, peak
    traffic was 6/min against a ~20/min limit, zero minutes exceeded it, and refusals hit
    all four chats at once. The same message types both succeeded and failed that day, and
    there were zero 400s, so it was neither our volume nor our content. Telegram's limits
    are adaptive and undocumented, tightening on global load and per-bot reputation, so
    "send harder" can never be correct.

    So stop trying to win the race. A message that cannot go now is QUEUED and goes later.
    Late is acceptable for a bet log; lost is not. 731 NOTIFY LOST across the logs is what
    the old approach cost, including 69 BET FAILED alerts in a single day.

    The transport itself is unchanged and lives in _send_now.
    """
    # v6.08t: NEVER send a real Telegram from a test run. There is no legitimate case for
    # it, and the harm is concrete: test_saiyan_topup stubs the fan-out to return a
    # SUCCESSFUL top-up, so _saiyan_topup_tick fired a genuine "Saiyan top-up: $X more on
    # Patrick Dangerfield" alert on EVERY full-suite run, and the suite runs on every
    # deploy through the pre-commit gate. Wilson saw the spam and asked where it came
    # from. Same class as the 2026-08-10 incident where the suite wrote fake items into
    # the live top-up queue, so the guard belongs HERE at the single transport choke
    # point rather than in each test, where the next one will forget it.
    #
    # Returns True (the send "succeeded") so any test asserting on the return value keeps
    # its meaning; the message is logged instead of posted.
    if _blocked_by_test_guard():
        log.info(f"[test guard] notification suppressed: {str(text)[:160]}")
        return True
    return _nq_enqueue(text, chat_id or NOTIFY_CHAT_ID, parse_mode)


def _send_now(text: str, chat_id: str = "", parse_mode: str = "HTML") -> bool:
    """The actual Telegram transport. Called ONLY by the queue drainer.

    Return semantics matter to the drainer:
      True  -> delivered, drop from the queue
      False -> not delivered; the drainer decides whether to requeue, using
               _nq_last_outcome to tell "throttled, try again" from
               "delivery uncertain, must NOT resend" from "permanently rejected".
    """
    # v6.18: THE GUARD BELONGS HERE, and until now it was only on `_send`.
    #
    # v6.14 put the pytest/TIPBOT_TESTING check on the enqueue side and its comment claims
    # to be "at the single transport choke point". It was not: `_send_now` is. Since v6.13
    # split enqueue from transport, ANY record that reaches the drainer bypassed the guard
    # completely, and the drainer reads a FILE — so a leftover production
    # logs/notify_queue.jsonl, or a test that repoints _NQ_PATH while an earlier test's
    # daemon drainer is still running, POSTs real Telegrams from the suite.
    #
    # MEASURED: a `requests.post` spy over the full suite caught a live send to
    # api.telegram.org. Third time this class of bug has shipped (2026-08-10 fake top-up
    # queue items, v6.14's 13 real sends per run), so the check now sits on the call that
    # actually touches the wire. `_ALLOW_TEST_TRANSPORT` stays the opt-in for the tests
    # that deliberately exercise this function against a stubbed `requests`.
    if _blocked_by_test_guard():
        log.info(f"[test guard] transport suppressed: {str(text)[:160]}")
        return True

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
    # v6.10: give a RATE LIMIT more than one 2-second retry. A 429 is Telegram saying
    # "later", not "never", and it tells us exactly how much later in
    # parameters.retry_after. The old policy slept a fixed 2s and gave up on attempt 2,
    # so it retried while still throttled and discarded the message. MEASURED: 731
    # NOTIFY LOST across the logs, including 69 BET FAILED alerts on 2026-08-10 (the day
    # of 588 top-up re-asks) and SGM BET PLACED / MANUAL BET ALERT messages. On
    # 2026-08-21, 19 sends hit 429 and 9 were lost outright, one of them the Nick Daicos
    # top-up notice Wilson went looking for. A dropped alert is the silent-drop class
    # this project keeps paying for, so a rate limit now gets real attempts with the
    # bookie's, sorry, Telegram's OWN stated delay honoured.
    for attempt in (1, 2, 3, 4, 5):
        try:
            _payload = {"chat_id": target, "text": text}
            if parse_mode:
                # Omit parse_mode entirely when falsy ("" / None) so Telegram treats
                # the text as PLAIN — used by send_long_text for the ASCII daily
                # review (passing parse_mode="" previously would have been rejected).
                _payload["parse_mode"] = parse_mode
            resp = requests.post(
                url,
                json=_payload,
                # v5.71 (notifier dedup): (connect, read) timeouts. Fail fast on
                # connect (5s) but give Telegram 20s to respond — the 06-16
                # Turang double-send was a READ timeout at the old flat 10s
                # where the POST had actually delivered.
                timeout=(5, 20),
            )
        except ReadTimeout as e:
            # v5.71 (notifier dedup): a READ timeout means the request was
            # TRANSMITTED — Telegram very likely posted the message already, the
            # HTTP response just didn't return in time. Retrying re-POSTs the
            # identical message and DELIVERS A SECOND COPY (the 06-16 Turang BET
            # PLACED / Olson MANUAL double-sends: both the original and the
            # v5.56 retry read-timed-out, both actually delivered, while the bot
            # logged NOTIFY LOST). So do NOT retry a read timeout — log it as
            # delivery-uncertain (the bot genuinely cannot know if it landed).
            log.error(
                f"Telegram send DELIVERY-UNCERTAIN (read timeout, chat={target}): "
                f"{e} | preview: {preview} — request reached Telegram; NOT "
                f"retrying (a retry would duplicate the message)"
            )
            _nq_mark("uncertain")     # v6.13: drainer must NOT requeue this one
            return False
        except Exception as e:
            # Connect-level failures (ConnectTimeout / ConnectionError / DNS /
            # refused) mean the request did NOT reach Telegram -> safe to retry.
            # Connect-level retry stays at ONE extra attempt: unlike a 429 there is no
            # server-stated delay to honour, and a hard connect failure rarely clears in
            # seconds. Deliberately narrower than the rate-limit path above.
            log.error(f"Telegram send EXCEPTION (chat={target}, attempt "
                      f"{attempt}): {e} | preview: {preview}")
            if attempt == 1:
                time.sleep(2)
                continue
            log.error(f"NOTIFY LOST (final): NOT delivered after retry "
                      f"(chat={target}) | preview: {preview}")
            _nq_mark("throttled")    # connect failure: never reached Telegram, safe to requeue
            return False

        if resp.status_code == 200:
            log.info(f"Telegram send ok (chat={target}): {preview}")
            return True

        log.error(
            f"Telegram send FAILED (chat={target}, status={resp.status_code}, "
            f"attempt {attempt}/5): {resp.text[:300]} | preview: {preview}"
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
                    timeout=(5, 20),  # v5.71: match the main send timeout
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
        # A 429 gets the full budget; a 5xx keeps its single retry. Deliberately narrow:
        # a rate limit is a KNOWN-temporary condition that reports its own clearance time,
        # whereas a 503 is an opaque server fault with no stated recovery, so hammering it
        # five times buys nothing and delays the loud failure.
        _retryable = (resp.status_code == 429 and attempt < 5) or (
            resp.status_code >= 500 and attempt == 1)
        if _retryable:
            _wait = 2.0
            if resp.status_code == 429:
                # Honour Telegram's OWN number. It is authoritative and retrying sooner
                # is guaranteed to fail: on 2026-08-21 it said retry_after=8 twice and
                # both 2-second retries were refused.
                try:
                    _ra = ((resp.json() or {}).get("parameters") or {}).get("retry_after")
                    if _ra is not None:
                        _wait = min(60.0, max(1.0, float(_ra) + 0.5))
                except Exception:
                    pass
            log.warning(f"Telegram {resp.status_code}, waiting {_wait:.1f}s before "
                        f"attempt {attempt + 1}/5 (chat={target})")
            time.sleep(_wait)
            continue
        # v6.13: classify so the queue can decide. A 429 is temporary and the message is
        # fine, so it goes back on the queue; anything else is permanent and dropping it
        # is correct (requeueing a 400 would spin forever).
        if resp.status_code == 429:
            _nq_mark("throttled")
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


def send_long_text(text: str, chat_id: str = "", header: str = "") -> int:
    """Deliver a long PLAIN-TEXT report as a sequence of Telegram messages, split on
    LINE boundaries (never mid-line), each well under Telegram's 4096-char hard cap.
    No parse_mode, so ASCII renders verbatim — the daily review is ASCII-only and the
    old path delivered only a truncated 1024-char caption, which read as 'cut off'.
    Each message is prefixed '[i/n]'. Returns the number of chunks successfully sent.
    """
    target = chat_id or NOTIFY_MAINTENANCE_CHAT_ID or NOTIFY_CHAT_ID
    CHUNK = 3500  # < 4096, leaving room for the '[i/n] header' prefix
    chunks: list[str] = []
    cur = ""
    for ln in (text or "").split("\n"):
        # Hard-wrap any single line longer than a whole chunk.
        while len(ln) > CHUNK:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:CHUNK])
            ln = ln[CHUNK:]
        if cur and len(cur) + 1 + len(ln) > CHUNK:
            chunks.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur:
        chunks.append(cur)
    if not chunks:
        return 0
    n = len(chunks)
    sent = 0
    for i, ch in enumerate(chunks, 1):
        prefix = f"[{i}/{n}]" + (f" {header}" if header else "") + "\n"
        if _send(prefix + ch, chat_id=target, parse_mode=""):
            sent += 1
    return sent


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


# The ONLY failure reasons a top-up re-ask is allowed to swallow. Both mean "the market
# did not offer what we asked for", which is the precise condition the top-up exists to
# retry, so hitting them again is a duplicate of an alert the operator already received.
#
# Everything NOT on this list still pages, even on a retry. That distinction was the
# review finding on the first cut of this fix: `_place_afl_fanout` funnels FIVE different
# failures through one notify_bet_failed call, and gating on the tip marker alone also
# silenced "No active HyperBot sessions" (main.py:6922). A session, auth or proxy outage
# can start BETWEEN the original placement and a retry tick, so that one is genuinely new
# information and must never be suppressed as though it were the line moving.
_TOPUP_DUPLICATE_FAILURES = (
    "not carried",              # line moved off the board (the Bergman case)
    "no placeable accounts",    # the to-win cap has not risen yet
)


def _is_silent_retry(tip, reason: str = "") -> bool:
    """A RETRY of stake that already alerted once must not alert again FOR THE SAME REASON.

    2026-08-15. Miles Bergman's Saiyan tip filled $375.20 of $600.00 and the shortfall
    was queued for top-up. Sportsbet then moved his two-way disposals line from 20.5 to
    19.5, so the exact-line rule correctly refused every re-ask -- but each refusal ran
    the ordinary "route to manual" path and fired a FRESH manual-bet alert. Seven
    attempts, seven identical Telegrams for one tip that had already been reported.

    The alert is the bug, not the refusal: the top-up is a BONUS attempt on stake the
    operator was already told about, so a failed one is a no-op and must be silent. Only
    the successful top-up still notifies (the "cap rose" message), because that one
    carries new information -- money moved.

    Two conditions, BOTH required:
      1. the tip carries the marker `_saiyan_topup_tick` sets on its own synthetic tip,
         so an ORIGINAL tip failing to place is completely unaffected, and
      2. the failure is one the top-up is expected to hit (see the tuple above).

    Fails SAFE: an unrecognised reason, or no reason at all, alerts. Silence is the more
    expensive mistake here, because a bet nobody hears about is a bet nobody places.
    """
    if not getattr(tip, "_saiyan_topup", False):
        return False
    r = (reason or "").lower()
    return any(m in r for m in _TOPUP_DUPLICATE_FAILURES)


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
    if _is_silent_retry(tip, getattr(result, "error", "")):
        return True
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
    if _is_silent_retry(tip, getattr(tip, "alert_reason", "")):
        return True
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


def notify_betfair_bsp_placed(channel_name: str, event_str: str, placements: list) -> bool:
    """v5.93: Bet Log message for a Betfair BSP placement (Leroy). placements = list
    of {runner, market, size, units, bet_id} — each matched at the Starting Price."""
    lines = []
    total = 0.0
    for p in (placements or []):
        total += float(p.get("size", 0) or 0)
        lines.append(
            f"  #{p.get('saddle', '?')} {p.get('runner', '?')} "
            f"{str(p.get('market', 'win')).upper()} @BSP  "
            f"${float(p.get('size', 0) or 0):.2f} ({float(p.get('units', 0) or 0):g}u)  "
            f"[{p.get('bet_id') or 'no-id'}]"
        )
    body = "\n".join(lines) if lines else "  (none)"
    text = (
        f"<b>BET PLACED (Betfair BSP)</b>\n"
        f"<b>Tipster:</b> {_escape_html(channel_name)}\n"
        f"<b>Event:</b> {_escape_html(event_str)}\n"
        f"<b>Total staked:</b> ${total:.2f} at the Starting Price\n"
        f"<pre>{_escape_html(body)}</pre>\n"
        f"Backs match at the SP when the race jumps."
    )
    return _send_success(text)


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


def notify_image_no_tip(tipster: str, caption: str = "") -> bool:
    """An image was received but the vision parser found NO bettable tip (a
    recap/results graphic, or a tip the model couldn't read). Distinct from
    notify_image_alert, whose "Image bet detected" footer falsely implies a bet was
    found — here NONE was, so say so plainly (2026-06-28: the 23:33 Eddie recap
    image read as 'Image bet detected', which it was not)."""
    cap = (f"\n<b>Caption:</b> <pre>{_escape_html(caption[:200])}</pre>"
           if caption else "")
    text = (
        f"<b>IMAGE (no tip parsed)</b>\n"
        f"<b>Tipster:</b> {_escape_html(tipster)}{cap}\n"
        f"An image was received but no bettable tip was found — ignore unless you "
        f"can see a tip in it."
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
            f"${r.stake:.2f} @ {r.odds if r.odds else '?'} "
            f"[{r.bet_id if r.bet_id else 'reconcile-confirmed'}]"
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

    # v5.92 (Wilson): flag any account that hit a proxy-403 (or other transient
    # pre-placement reject) on the first attempt but was RECOVERED on the same-stake
    # re-bet — shown in the bet-log (and persisted to the ledger `note` column).
    recovery_tag = ""
    _rec = [getattr(r, "_recovered_note", "") for r in (placed_results or [])
            if getattr(r, "_recovered_note", "")]
    if _rec:
        recovery_tag = "\n<b>⚙️ Recovered on re-bet:</b> " + _escape_html("; ".join(_rec))

    text = (
        f"<b>BET PLACED</b>{sgm_tag}{fallback_tag}\n"
        f"<b>Tipster:</b> {_escape_html(tip.tipster)}\n"
        f"<b>Event:</b> {_escape_html(tip.event or 'N/A')}\n"
        f"<pre>{_escape_html(legs_str)}</pre>\n"
        f"<b>Total placed:</b> ${total_placed:.2f} of ${intended_stake:.2f} "
        f"@ avg {weighted_odds:.3f}"
        f"{fill_tag}"
        f"{recovery_tag}"
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
    # v5.92 (Wilson): flag accounts that FAILED TWICE on this message — a
    # pre-placement reject (e.g. the proxy 403) on BOTH the initial attempt AND the
    # re-bet — so the manual alert says which account still needs a hand-place.
    _twice = [getattr(r, "_retry_note", "") for r in (failed_results or [])
              if getattr(r, "_retry_failed_twice", False) and getattr(r, "_retry_note", "")]
    if _twice:
        parts.append(
            f"<b>🔁 Failed twice (same message):</b>\n<pre>"
            f"{_escape_html(chr(10).join(_twice))}</pre>"
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


# ── Racing notifications (Tip Titans + the standalone racing tipsters) ──
# v6.08e (Wilson 2026-08-03): these three alerts were hardcoded "TIP TITANS", but the
# SAME functions serve the standalone racing tipsters too — Zak Trussell and The Trial
# Sniper come from their own Telegram channels via the image/text auto-place path, and
# Leroy via Betfair BSP. None of them are Tip Titans, so every one of their bet-log
# messages was mislabelled (and the "Titan:" field with it).
#
# Derived from the `titan` code rather than switched per call site, and UNKNOWN CODES
# KEEP "TIP TITANS": Tip Titans is a service with several titans, so an unrecognised
# code is far more likely to be a real titan than a new standalone tipster. That way
# this can never relabel a genuine Titans alert — it only names the ones we know are not.
_RACING_SOURCE_LABELS = {
    "ZAK": "ZAK RACING",
    "TRIAL": "TRIAL SNIPER",
    "LEROY": "LATE MAIL LEROY",
}


def _racing_source(titan) -> tuple:
    """(header_label, field_label) for a racing alert, from the `titan` code.

    Returns ("TIP TITANS", "Titan") for anything not known to be standalone, so
    existing Titans messages are byte-identical.
    """
    code = str(titan or "").strip().upper()
    if code in _RACING_SOURCE_LABELS:
        return _RACING_SOURCE_LABELS[code], "Tipster"
    return "TIP TITANS", "Titan"

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
    _src, _srcfield = _racing_source(parsed.get("titan"))
    text = (
        f"<b>🏇 {_src} PLACED</b>{test_tag}\n"
        f"<b>{_srcfield}:</b> {_escape_html(parsed['titan'])}\n"
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
    failures, below_floor, tipster_odds, bookie_quotes=None, skipped=None,
) -> bool:
    """Unfilled stake alert - goes to Manual Bets (#1).

    `bookie_quotes` (optional) shows the price-shop snapshot inline so the
    user can see what was available without log lookup.
    `skipped` (v5.77, optional): in-range sessions skipped BEFORE any attempt
    (do-not-bet / cap=0) — surfaced so a partial unfill isn't a blank
    "(no failure details)" (the Minor Catastrophe 06-19 case).
    """
    saddle_str = f"{parsed.get('saddle')}. " if parsed.get("saddle") else ""
    failure_lines = []
    for f in failures[-3:]:  # last 3 placement failures (tried + error)
        err = (f.get("error") or "")[:160]
        failure_lines.append(
            f"  {f.get('bookie', '?')} (s{f.get('session_id', '?')}) "
            f"${f.get('stake_tried', 0):.0f}: {err}"
        )
    # v5.77 (Wilson 2026-06-20): explain WHY the in-range sessions didn't take the
    # rest of the stake, instead of a bare "(no failure details)". List the
    # in-range sessions that were DISABLED (do-not-bet) and summarise how many
    # priced-but-BELOW-FLOOR (value gone). Minor Catastrophe 06-19: 13/18 below
    # floor + 4 placeable accounts do-not-bet at Gloucester Park -> $1009 manual.
    for s in (skipped or [])[-4:]:
        failure_lines.append(
            f"  {s.get('bookie', '?')} (s{s.get('session_id', '?')}): "
            f"{(s.get('reason') or 'skipped (do-not-bet)')[:80]}"
        )
    _bf = [q for q in (bookie_quotes or []) if q.get("status") == "below_floor"]
    if _bf:
        _best = max((q.get("odds") or 0) for q in _bf)
        failure_lines.append(
            f"  {len(_bf)} bookie(s) below floor"
            + (f" (best {_best} < tipster {tipster_odds})" if _best else "")
        )
    failures_str = "\n".join(failure_lines) or "  (no failure details)"

    reason = ""
    if below_floor:
        reason = f"\n<b>⚠️ All bookies below 90% of tipster odds ({tipster_odds})</b>"

    quotes_block = _format_bookie_quotes(bookie_quotes, tipster_odds=tipster_odds)

    title = "BET UNFILLED" if placed_stake > 0 else "BET FAILED"
    _src, _srcfield = _racing_source(parsed.get("titan"))
    text = (
        f"<b>🏇 {_src} {title}</b>\n"
        f"<b>Tip ID:</b> {tip_id}\n"
        f"<b>{_srcfield}:</b> {_escape_html(parsed['titan'])}\n"
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

    _src, _srcfield = _racing_source(titan)
    text = (
        f"<b>🏇 {_src} MANUAL</b>\n"
        f"<b>Tip ID:</b> {tip_id}\n"
        f"<b>{_srcfield}:</b> {_escape_html(titan)}\n"
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
