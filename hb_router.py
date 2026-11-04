"""One HyperBot-shaped client in front of HyperBot and the Sportmarket bridge.

v6.154 (2026-10-29): Sportmarket is reached through a local bridge process that answers HyperBot's
calls for ONE account (C:/repos/sportmarket-bridge, contract in its docs/tipbot-contract.md). tipbot
treats it as one more HyperBot-style session. main.hb is a RoutedHyperBotClient: every call that
names a session id / account id goes to the client that owns it, everything else goes to HyperBot
(the primary). With SPORTMARKET_BRIDGE_URL empty there are no bridges and the router is a pure
pass-through, so behaviour is identical to a bare HyperBotClient.

Dependency-light on purpose: hyperbot_client and config are imported inside the functions that need
them, so check_session_health.py / hb_launch_sessions.py (their own interpreters) can import the
constants and helpers here cheaply.

Money-path rules kept: the bridge client is a plain HyperBotClient, so the test guard, the
SPORTS_PLACEMENT_PAUSED refusal, the event guard, the placement governor, the cid polling and the
ambiguous envelopes all apply to it exactly as to HyperBot. A session-less call never reaches the
bridge.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, NamedTuple

log = logging.getLogger("tipbot")

# A yaml `bookmaker:` that is NEVER a HyperBot session, whether or not the bridge is switched on:
# HyperBot cannot launch / restart it (the bridge is a local process Wilson starts and stops).
NON_HYPERBOT_BOOKMAKERS = frozenset({"sportmarket"})

# The session list of a bridge is a cheap local read: one short attempt, never the 2 s + 5 s retry
# sleeps of the HyperBot default (a dead bridge must not slow every tip's session fetch).
_BRIDGE_SESSION_TIMEOUT_SEC = 4

# Calls whose first positional argument (or `session_id=` keyword) names the session that owns them.
_BY_SESSION = (
    "get_transactions", "get_transactions_or_none", "get_balance", "start_session", "restart_session",
    "price_check_sports", "place_single_sports_bet", "place_sgm_bet",
    "price_check_racing", "racing_promo_tokens", "place_racing_bet", "place_betfair_bsp",
)


class Bridge(NamedTuple):
    client: Any
    session_id: str
    account_id: str
    bookie: str
    label: str


class RoutedHyperBotClient:
    """One HyperBot-shaped client in front of HyperBot and the Sportmarket bridge. Every call that
    names a session_id / session_ids / account_id goes to the client that owns it; everything else
    goes to HyperBot (primary). Built by build_client(); main.hb is an instance."""

    def __init__(self, primary, bridges=()):
        self._primary = primary
        self._bridges = tuple(bridges)
        self._bridge_state: dict = {}          # session_id -> last logged state, for change-only logs
        self._state_lock = threading.Lock()

    # ── plumbing ────────────────────────────────────────────────────

    @property
    def primary(self):
        return self._primary

    @property
    def bridges(self):
        return self._bridges

    @property
    def event_guard(self):
        return self._primary.event_guard

    @event_guard.setter
    def event_guard(self, guard):
        # main.py does `hb.event_guard = _nba_wrong_fixture_reason`; a bet on the bridge needs the
        # same wrong-fixture check as a bet on HyperBot.
        self._primary.event_guard = guard
        for b in self._bridges:
            b.client.event_guard = guard

    def __getattr__(self, name):
        # Only reached when normal lookup fails: api_key, base_url, headers, _post, ...
        if name in ("_primary", "_bridges", "_bridge_state", "_state_lock"):
            raise AttributeError(name)         # not built yet (copy / pickle): no recursion
        return getattr(self._primary, name)

    def _owner(self, session_id):
        """The client that owns this session id (the primary unless a bridge claims it)."""
        sid = "" if session_id is None else str(session_id)
        for b in self._bridges:
            if sid and sid == b.session_id:
                return b.client
        return self._primary

    def _by_session(self, name: str, args: tuple, kwargs: dict):
        sid = kwargs["session_id"] if "session_id" in kwargs else (args[0] if args else None)
        owner = self._owner(sid)
        if owner is not self._primary:
            refusal = _bridge_refusal(name, args, kwargs, sid)
            if refusal is not None:
                return refusal
        res = getattr(owner, name)(*args, **kwargs)
        if name == "price_check_sports" and owner is not self._primary:
            _note_bridge_price(res)         # v6.223: the bridge's own board, for the caller that asked
        return res

    # ── sessions ────────────────────────────────────────────────────

    def _note_bridge_state(self, b: Bridge, state: str) -> None:
        with self._state_lock:
            if self._bridge_state.get(b.session_id) == state:
                return
            self._bridge_state[b.session_id] = state
        log.info(f"{b.label}: session {state}")

    def _bridge_sessions(self) -> list:
        """The active session of each bridge. A bridge that is unreachable, or answers without its
        own session, contributes nothing (never raises); state changes are logged once."""
        out: list = []
        for b in self._bridges:
            data = _quiet_session_list(b)
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
                self._note_bridge_state(b, "unreachable")
                continue
            mine = [s for s in data["sessions"]
                    if isinstance(s, dict) and s.get("active") and str(s.get("session_id")) == b.session_id]
            if not mine:
                self._note_bridge_state(b, "inactive")
                continue
            self._note_bridge_state(b, "active")
            for s in mine:
                s = dict(s)
                s["bookie"] = b.bookie               # the configured identity is authoritative
                if not s.get("account_id"):
                    s["account_id"] = b.account_id
                out.append(s)
        return out

    def get_sessions(self) -> list:
        return list(self._primary.get_sessions() or []) + self._bridge_sessions()

    def get_sessions_or_none(self):
        """None only when HyperBot itself is unreachable (the watchdog's signal); a dead bridge
        just contributes no session."""
        base = self._primary.get_sessions_or_none()
        if base is None:
            return None
        return list(base) + self._bridge_sessions()

    def get_pending_bets(self, account_id):
        acct = "" if account_id is None else str(account_id)
        for b in self._bridges:
            if acct and acct == b.account_id:
                return _dict_records(b.client.get_pending_bets(account_id))
        return self._primary.get_pending_bets(account_id)

    # ── bulk price check across owners ──────────────────────────────

    def price_check_multi_session(self, session_ids, sport, event, player=None):
        ids = list(session_ids or [])
        buckets: list = []                       # [(client, [ids])], primary first
        primary_ids = []
        by_bridge: dict = {}
        for sid in ids:
            owner = self._owner(sid)
            if owner is self._primary:
                primary_ids.append(sid)
            else:
                by_bridge.setdefault(id(owner), (owner, []))[1].append(sid)
        if not by_bridge:                         # no bridge session asked: exactly the old call
            return self._primary.price_check_multi_session(session_ids, sport, event, player)
        if primary_ids:
            buckets.append((self._primary, primary_ids))
        buckets.extend(by_bridge.values())
        if len(buckets) == 1:                     # only one owner: its answer, untouched
            c, sub = buckets[0]
            return c.price_check_multi_session(sub, sport, event, player)

        answers = []                              # [(ok, sub_ids, answer)] in bucket order
        for c, sub in buckets:
            try:
                r = c.price_check_multi_session(sub, sport, event, player)
            except Exception as e:
                log.warning(f"price_check_multi_session failed for {sub}: {e}")
                r = {"success": False, "error": str(e)}
            if not isinstance(r, dict):
                r = {"success": False, "error": "bad answer"}
            answers.append((r.get("success") is not False and "selections" in r, sub, r))
        good = [r for ok, _sub, r in answers if ok]
        if not good:
            return answers[0][2]                  # every call failed: the primary's envelope
        base = dict(good[0])                      # other top-level keys of the first good answer
        merged: dict = {}
        order: list = []
        errors: list = []
        for ok, sub, r in answers:
            if not ok:
                errors.extend({"session_id": s, "siteName": "", "error": r.get("error") or "failed"}
                              for s in sub)
                continue
            for e in (r.get("errors") or []):
                errors.append(e)
            for sel in (r.get("selections") or []):
                if not isinstance(sel, dict):
                    continue
                key = _selection_key(sel)
                if key in merged:                 # same selection priced by several owners: one entry
                    merged[key]["prices"] = list(merged[key].get("prices") or []) + list(sel.get("prices") or [])
                else:
                    merged[key] = dict(sel)
                    order.append(key)
        base["success"] = True
        base["selections"] = [merged[k] for k in order]
        base["errors"] = errors or None
        return base


def _quiet_session_list(b: Bridge):
    """The bridge's /api/session_ids, read directly: one short attempt, no retries and no ERROR line
    per call (the bridge is a local process Wilson starts and stops, so "down" is its normal state
    for hours; HyperBotClient._post would log an ERROR on every tip's session fetch). The suite's
    test guard applies as to every HyperBot call."""
    try:
        import hyperbot_client as _hc
        if type(b.client).__module__ != _hc.__name__:
            # a stand-in client (tests) is used as given: its own _post answers (one short attempt)
            return b.client._post("/api/session_ids", {}, timeout=_BRIDGE_SESSION_TIMEOUT_SEC,
                                  max_attempts=1)
        if _hc._blocked_by_test_guard():
            return None          # a real client under pytest: never a real HTTP call
        import requests
        resp = requests.post(f"{b.client.base_url}/api/session_ids", headers=b.client.headers,
                             json={}, timeout=_BRIDGE_SESSION_TIMEOUT_SEC)
        if resp.status_code != 200:
            log.debug(f"{b.label}: session list answered {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        log.debug(f"{b.label}: session list failed: {e}")
        return None


# Bridge calls that carry a sport (first positional after the session id, or sport=): only the
# sports on the bridge's list may go to it; anything else is a clean, local, nothing-sent refusal.
_SPORT_ARG_INDEX = {"price_check_sports": 1, "place_single_sports_bet": 1, "place_sgm_bet": 1}


# v6.223: the last successful Sportmarket bridge /v3/price answer on THIS thread (event echo, the bridge's
# own event_id, start_time_iso), so the resolver's caller can read the board the resolver just used without
# the resolver having to return it. Cleared by the caller before the resolve; nothing else reads it.
_PRICE_TLS = threading.local()


def _note_bridge_price(res) -> None:
    try:
        if isinstance(res, dict) and res.get("success") is not False and res.get("event"):
            _PRICE_TLS.last = {"event": str(res.get("event")), "event_id": res.get("event_id"),
                               "start_time_iso": res.get("start_time_iso")}
    except Exception:
        pass


def clear_bridge_price() -> None:
    _PRICE_TLS.last = None


def last_bridge_price():
    """{event, event_id, start_time_iso} of the last successful bridge price read on this thread since
    clear_bridge_price(), else None."""
    return getattr(_PRICE_TLS, "last", None)


def _bridge_refusal(name: str, args: tuple, kwargs: dict, sid):
    """None to send to the bridge, else a clean failure envelope (nothing was sent). The bridge
    carries one sport list (config.SPORTMARKET_SPORTS) and no SGMs / racing."""
    try:
        import config
        allowed = {str(x).lower() for x in (getattr(config, "SPORTMARKET_SPORTS", None) or ())}
    except Exception:
        allowed = set()
    idx = _SPORT_ARG_INDEX.get(name)
    if idx is None:
        return None
    sport = kwargs.get("sport") if "sport" in kwargs else (args[idx] if len(args) > idx else None)
    if sport is None:
        return None          # no sport named: the bridge's own checks decide (SGMs and racing too)
    if str(sport).strip().lower() not in allowed:
        return {"success": False, "session_id": str(sid),
                "error": f"[event_not_carried] sport {sport!r} is not on the Sportmarket bridge's "
                         f"list {sorted(allowed)} (refused locally, nothing sent)"}
    return None


# tipbot's reconcile reads pending bets as dicts (HyperBot's shape). The bridge's first version
# answered positional lists in this field order (live 2026-10-30, test bet 2); normalise them.
_PENDING_FIELDS = ("account_id", "bet", "bet_type", "bookie_bet_id", "dt", "event", "id", "odds",
                   "result", "session_id", "sport", "stake", "verified")


def _dict_records(resp):
    """A bridge pending_bets answer with every record as a dict. A list record of the contract's
    13 fields is zipped into one; anything else is left as it is (the reconcile skips a
    non-dict record rather than crashing on it)."""
    try:
        bets = resp.get("bets") if isinstance(resp, dict) else None
        if not isinstance(bets, list):
            return resp
        out = []
        for b in bets:
            if isinstance(b, (list, tuple)) and len(b) == len(_PENDING_FIELDS):
                out.append(dict(zip(_PENDING_FIELDS, b)))
            else:
                out.append(b)
        return dict(resp, bets=out)
    except Exception as e:
        log.warning(f"pending_bets normalisation skipped ({e})")
        return resp


def _selection_key(sel: dict):
    line = sel.get("line")
    try:
        line = float(line) if line is not None else None
    except (TypeError, ValueError):
        pass
    return (str(sel.get("market") or "").lower(), str(sel.get("player") or "").lower(),
            str(sel.get("selection") or "").lower(), line)


def _make_by_session(name: str):
    def method(self, *args, **kwargs):
        return self._by_session(name, args, kwargs)
    method.__name__ = name
    method.__qualname__ = f"RoutedHyperBotClient.{name}"
    method.__doc__ = f"Routed by session id (first argument or session_id=) to the owning client: {name}."
    return method


for _name in _BY_SESSION:
    setattr(RoutedHyperBotClient, _name, _make_by_session(_name))
del _name


# ── module state + helpers ──────────────────────────────────────────

_CLIENT: RoutedHyperBotClient | None = None   # the client build_client() last registered


def build_client(register: bool = True) -> RoutedHyperBotClient:
    """The routing client main.hb is. Always a router (no bridges = pure pass-through), so main.hb
    has one type. The bridge is added only when config.SPORTMARKET_BRIDGE_URL is non-empty."""
    global _CLIENT
    import config
    from hyperbot_client import HyperBotClient
    primary = HyperBotClient()
    bridges = []
    url = (getattr(config, "SPORTMARKET_BRIDGE_URL", "") or "").strip()
    if url:
        bridges.append(Bridge(
            HyperBotClient(api_key=config.SPORTMARKET_BRIDGE_API_KEY, base_url=url),
            str(config.SPORTMARKET_SESSION_ID), str(config.SPORTMARKET_ACCOUNT_ID),
            str(config.SPORTMARKET_BOOKIE), "Sportmarket bridge"))
    router = RoutedHyperBotClient(primary, bridges)
    if register:
        _CLIENT = router
    return router


def _bridge_specs() -> list:
    """[(session_id, account_id, bookie)] of the bridges: the registered client's, else config's."""
    if _CLIENT is not None:
        return [(b.session_id, b.account_id, b.bookie) for b in _CLIENT.bridges]
    try:
        import config
        if (getattr(config, "SPORTMARKET_BRIDGE_URL", "") or "").strip():
            return [(str(config.SPORTMARKET_SESSION_ID), str(config.SPORTMARKET_ACCOUNT_ID),
                     str(config.SPORTMARKET_BOOKIE))]
    except Exception:
        pass
    return []


def bridge_session_ids() -> frozenset:
    return frozenset(s for s, _a, _b in _bridge_specs())


def bridge_bookies() -> frozenset:
    return frozenset(b for _s, _a, b in _bridge_specs())


def is_bridge_session(session_id) -> bool:
    return session_id is not None and str(session_id) in bridge_session_ids()
