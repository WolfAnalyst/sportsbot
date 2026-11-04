"""v6.241: the edited tipster post alert (store, judge, alert). Ops only: it NEVER places a bet.

Wilson 4 Nov 2026: tipbot never saw a tipster edit a post (4th and EV edited a Murray 325+ post after it
was parsed). The Telethon MessageEdited handler in main.py hands every edit on a watched tipster chat to
handle_edit() on a worker thread, which sends ONE info alert (notifier.notify_info, the maintenance chat,
through the durable queue) when the TEXT of the post really changed: the tipster, the old text, the new
text, how long after posting, and a line saying the bot did not change any bet.

  * Telegram fires edit events for reactions, view counts and link previews, so the NORMALISED text is
    compared and an unchanged one stays silent. A screenshot tip counts a changed caption; a changed
    image with the same caption counts too (Telethon gives the new photo's id for free).
  * The original text comes from a small store (record_post() from the NewMessage handler), kept per
    (chat, message id) in logs/edited_tip_posts.json: atomic write, a corrupt or missing file reads as
    empty (it holds no money state; a corrupt one is kept as .bak), an unreadable one is never saved
    over (v6.241 fix round), at most EDITED_TIP_STORE_MAX_POSTS posts, none older than
    EDITED_TIP_STORE_MAX_AGE_SEC.
  * No stored original (an older post, or a lost store): ONE alert saying the original is unknown, only
    when the post is within EDITED_TIP_UNKNOWN_RECENT_SEC and Telegram marks it as really edited.
  * Never twice in a row for the same (chat, message id, new content hash), across a restart too. A
    real change back to an earlier text alerts again (v6.241 fix round), so the last alert always
    matches the post.

Nothing here touches seen_bets, the resend guard, a parser or any placement queue. Every public call
swallows its own errors: an edit alert must never be able to stop a tip.
"""
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata

import config

log = logging.getLogger("tipbot.edit_alert")

_LOCK = threading.Lock()
_POSTS: dict = {}            # "chat:msg" -> {"t", "name", "text", "h", "img", "alerted": [hash, ...]}
_LOADED = [False]
_READ_FAILED = [False]       # v6.241 fix round: the last read failed (not a missing file): never save over it
_CORRUPT = [False]           # v6.241 fix round: the file failed to parse: copy it to .bak before overwriting

_TEXT_KEEP = 2000            # chars of a post kept in the store
_ALERT_TEXT_MAX = 1500       # chars of the old / new text shown in the alert (Telegram's cap is 4096)
_ALERTED_KEEP = 20           # alerted hashes remembered per post
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def _cfg(name, default):
    return getattr(config, name, default)


def _num(name, default) -> float:
    try:
        v = float(_cfg(name, default))
        return v if math.isfinite(v) and v >= 0 else float(default)
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return bool(_cfg("EDITED_TIP_ALERT_ENABLED", True))


def watched(channel_cfg) -> bool:
    """A tipster chat whose edits are alerted. Machine feeds (the AFL / NBA models) and Wilson's own
    self-bet channel are not tipster posts and are left out."""
    if not isinstance(channel_cfg, dict) or not channel_cfg:
        return False
    return not (channel_cfg.get("machine_feed") or channel_cfg.get("nba_model")
                or channel_cfg.get("self_bet"))


# ── text ─────────────────────────────────────────────────────────────────────────────────────────

def normalise(text) -> str:
    """The text as compared: NFKC, zero-width characters dropped, every run of whitespace one space,
    trimmed. Case and punctuation are kept (a changed number or name must count)."""
    s = unicodedata.normalize("NFKC", str(text or "")).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", s).strip()


def content_hash(text, img=None) -> str:
    raw = normalise(text) + ("\x00img:" + str(img) if img else "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _key(chat_id, msg_id) -> str:
    return f"{chat_id}:{msg_id}"


def _epoch(dt):
    """A Telethon datetime (UTC aware) or a number -> epoch seconds, else None."""
    if dt is None:
        return None
    try:
        if hasattr(dt, "timestamp"):
            v = float(dt.timestamp())
        else:
            v = float(dt)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return v if math.isfinite(v) and v > 1e9 else None


def _ago(sec) -> str:
    if sec is None:
        return "an unknown time"
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} s"
    m = sec // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min"
    return f"{h // 24} days {h % 24} h"


def _clip(text) -> str:
    s = str(text or "").strip()
    if not s:
        return "(no text)"
    return s if len(s) <= _ALERT_TEXT_MAX else s[:_ALERT_TEXT_MAX] + " ...(cut)"


# ── persistence ──────────────────────────────────────────────────────────────────────────────────

def _path() -> str:
    if os.getenv("TIPBOT_TESTING") or "pytest" in sys.modules:
        import tempfile
        return os.path.join(tempfile.gettempdir(), f"edited_tip_posts_{os.getpid()}.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "edited_tip_posts.json")


def _prune_locked(now) -> None:
    max_age = _num("EDITED_TIP_STORE_MAX_AGE_SEC", 604800.0)
    for k in [k for k, v in _POSTS.items() if now - float(v.get("t") or 0) > max_age]:
        _POSTS.pop(k, None)
    cap = int(_num("EDITED_TIP_STORE_MAX_POSTS", 2000))
    if len(_POSTS) > cap:
        for k in sorted(_POSTS, key=lambda k: float(_POSTS[k].get("t") or 0))[:len(_POSTS) - cap]:
            _POSTS.pop(k, None)


def _load_locked() -> None:
    """Tolerant load: a missing or corrupt file (or an odd-shaped entry) reads as nothing stored.

    v6.241 fix round (review finding 4 Nov 2026): any other read error (a PermissionError, a locked file)
    used to read as an empty store too, and the next save overwrote the good file. Now such an error
    leaves the store NOT loaded: nothing is saved (_save_locked) and the read is tried again on the next
    call. A corrupt file (a real parse failure) is copied to a .bak before the first overwrite."""
    if _LOADED[0]:
        return
    path = _path()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except (FileNotFoundError, NotADirectoryError):
        # no such file (a path part that is not a directory means the same: nothing there to lose)
        _LOADED[0] = True
        _READ_FAILED[0] = False
        return
    except Exception as e:
        if not _READ_FAILED[0]:
            log.warning(f"edit_alert: could not read {path} ({e}); the file is left untouched and not saved "
                        f"over until a read succeeds (alerts carry on from memory)")
        _READ_FAILED[0] = True
        return
    _LOADED[0] = True
    if _READ_FAILED[0]:
        log.info(f"edit_alert: {path} read again after a read error")
    _READ_FAILED[0] = False
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"top level is {type(data).__name__}, not an object")
    except Exception as e:
        _CORRUPT[0] = True
        log.warning(f"edit_alert: {path} is corrupt ({e}); starting with no stored posts, the file is "
                    f"copied to {path}.bak before it is overwritten (it carries no money state)")
        return
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(v.get("h"), str):
            try:
                v["t"] = float(v.get("t"))
            except (TypeError, ValueError):
                continue
            if not isinstance(v.get("alerted"), list):
                v["alerted"] = []
            # a post recorded or alerted in memory while the file could not be read is newer: keep it
            _POSTS.setdefault(str(k), v)
    _prune_locked(time.time())


def _save_locked() -> None:
    """Atomic tmp + replace. A failure is logged, never raised: the in-memory copy still holds.
    Never saves over a file that has not been read (v6.241 fix round), and backs a corrupt one up first."""
    path = _path()
    tmp = path + ".tmp"
    if not _LOADED[0]:
        log.warning(f"edit_alert: not saving {path}: it has not been read yet (an earlier read failed)")
        return
    if _CORRUPT[0]:
        try:
            if os.path.exists(path):
                shutil.copyfile(path, path + ".bak")
            _CORRUPT[0] = False
        except Exception as e:
            log.error(f"edit_alert: could not back up the corrupt {path} to .bak ({e}); not saving over it")
            return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_POSTS, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.error(f"edit_alert: could not save the post store ({e}); it holds in memory until a restart")
        try:
            os.unlink(tmp)
        except Exception:
            pass


def reset(clear_file: bool = True) -> None:
    """Forget the in-memory store (tests; a restart does the same). Optionally delete the file."""
    with _LOCK:
        _POSTS.clear()
        _LOADED[0] = False
        _READ_FAILED[0] = False
        _CORRUPT[0] = False
    if clear_file:
        for p in (_path(), _path() + ".bak"):
            try:
                os.remove(p)
            except OSError:
                pass


# ── the two entry points (both run on a worker thread, never on the event loop) ──────────────────

def record_post(chat_id, msg_id, text, posted=None, img=None, name="") -> None:
    """Keep a new tipster post's text as the original for a later edit. A post already stored is left
    alone (an edit handled first must not be overwritten by its own original). Never raises."""
    try:
        if not enabled() or msg_id is None:
            return
        now = time.time()
        t = _epoch(posted) or now
        with _LOCK:
            _load_locked()
            k = _key(chat_id, msg_id)
            if k in _POSTS:
                return
            _POSTS[k] = {"t": t, "name": str(name or ""), "text": str(text or "")[:_TEXT_KEEP],
                         "h": content_hash(text, img), "img": str(img) if img else None,
                         "alerted": []}
            _prune_locked(now)
            _save_locked()
    except Exception as e:
        log.error(f"edit_alert: record_post failed ({e})")


def judge(chat_id, msg_id, text, posted=None, edited=None, img=None, name="", edit_hide=False,
          now=None):
    """Decide one edit event and update the store. Returns the alert text, or None for silence.
    Pure of any send: handle_edit() sends what this returns."""
    now = time.time() if now is None else now
    t_post = _epoch(posted)
    t_edit = _epoch(edited)
    h = content_hash(text, img)
    k = _key(chat_id, msg_id)
    with _LOCK:
        _load_locked()
        old = _POSTS.get(k)
        if old is not None and old.get("h") == h:
            return None                      # a reaction, a view count, a link preview: same content
        # v6.241 fix round (review finding 4 Nov 2026): compared with the LAST alerted content only, not
        # every content ever alerted. 325+ -> 300+ -> 280+ -> 300+ is a real change back and alerts (else
        # Wilson's last alert says 280+ while the post says 300+); a stale re-delivery of the content
        # last alerted stays silent.
        if old is not None and (old.get("alerted") or [None])[-1] == h:
            log.info(f"edit_alert: {name} {k} edit re-delivered with the content last alerted, no second alert")
            return None
        if old is None:
            if t_edit is None:
                # Telegram says the post was never edited (a reaction or view count on an unstored
                # post): its text IS the original, so keep it silently for a later real edit
                if t_post is not None:
                    _POSTS[k] = {"t": t_post, "name": str(name or ""),
                                 "text": str(text or "")[:_TEXT_KEEP], "h": h,
                                 "img": str(img) if img else None, "alerted": []}
                    _prune_locked(now)
                    _save_locked()
                return None
            # No stored original: alert once, only for a recent post Telegram marks as edited
            if t_post is None or now - t_post > _num("EDITED_TIP_UNKNOWN_RECENT_SEC", 86400.0):
                log.info(f"edit_alert: {name} {k} edited, original not stored and post not recent "
                         f"(or undated): no alert")
                return None
            if edit_hide:
                return None                  # a hidden edit (Telegram's own, not the tipster's text)
            _POSTS[k] = {"t": t_post, "name": str(name or ""), "text": str(text or "")[:_TEXT_KEEP],
                         "h": h, "img": str(img) if img else None, "alerted": [h]}
            _prune_locked(now)
            _save_locked()
            before = ("(original unknown: the post was not in the edit store, e.g. it came before a "
                      "restart or the store was lost; this post is alerted once)")
            changed = ""
        else:
            before = _clip(old.get("text"))
            same_text = normalise(old.get("text")) == normalise(text)
            changed = (" (the IMAGE changed, the caption is the same)" if same_text else "")
            alerted = (list(old.get("alerted") or []) + [h])[-_ALERTED_KEEP:]
            old.update({"text": str(text or "")[:_TEXT_KEEP], "h": h, "img": str(img) if img else None,
                        "alerted": alerted})
            if t_post is None:
                t_post = _epoch(old.get("t"))
            _save_locked()
    when = (t_edit or now) - t_post if t_post is not None else None
    return (f"EDITED TIP POST: {name or 'unknown tipster'} edited a post{changed}, "
            f"{_ago(when)} after posting.\n"
            f"BEFORE:\n{before}\n"
            f"AFTER:\n{_clip(text)}\n"
            f"The bot did NOT place, change or cancel any bet for this edit. "
            f"Check any bet on this post by hand.")


def handle_edit(chat_id, msg_id, text, posted=None, edited=None, img=None, name="",
                edit_hide=False) -> bool:
    """Judge an edit and enqueue its one alert. True when an alert was handed to the notifier.
    The store marks the new content as alerted BEFORE the send, so a failed send is never repeated
    (one missed info alert beats two). Never raises."""
    try:
        if not enabled():
            return False
        msg = judge(chat_id, msg_id, text, posted=posted, edited=edited, img=img, name=name,
                    edit_hide=edit_hide)
        if not msg:
            return False
        log.info(f"edit_alert: {name} {_key(chat_id, msg_id)} text changed, alerting")
        import notifier
        if not notifier.notify_info(msg):
            log.error(f"edit_alert: the alert for {name} {_key(chat_id, msg_id)} was not queued")
            return False
        return True
    except Exception as e:
        log.error(f"edit_alert: handle_edit failed ({e})")
        return False
