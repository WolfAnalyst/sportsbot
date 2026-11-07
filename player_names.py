"""v6.267 (Wilson 7 Nov 2026: "fix up the name mismatches for sportmarket and b365 for future"): ONE rule for a
player the tip spells one way and a book spells another (bet365's card says "Alexandre Sarr", the roster and
Sportsbet say "Alex Sarr"). No imports from the rest of tipbot, so every book and the resulter can use it.

match(names, player) looks `player` up among the `names` a book lists, in this order:
  1. exact: the same name once accents, case, punctuation, spaces and a Jr / Sr / II / III / IV / V suffix are
     dropped (so PJ = P.J., Ja'Kobi = Jakobi). Exact always wins.
  2. the alias table below (both directions), for names a prefix cannot catch (Moe = Moritz Wagner).
  3. a shortened first name: the same words after the first, the same number of words, and one first name a prefix
     of the other with the shorter one at least 3 letters (Alex / Alexandre, Cam / Cameron, Nic / Nicolas). A
     hyphen in either spelling and a middle name never match; a different first name (Jalen / Jaylin) never does.
2 and 3 count only when exactly ONE distinct player on the list fits (an alias and a prefix hit on two different
players is two fits: no match), and never when the two spellings are a NEVER_SAME pair (James Williams is not
Jameson Williams). Step 3 (a shortened first name) also needs tipbot's roster for the sport (match(...,
roster=roster_keys(sport))): it counts only when the tip's own spelling IS on that roster and the list's spelling is
NOT a different player on it (Alex Sarr is rostered, Alexandre Sarr is not: a match; Joshua Allen is not rostered, so
he never finds Josh Allen; a rostered Josh Uche finds Joshua Uche). No roster (None, empty, unreadable) = no step 3:
a doubtful name is no match. The alias table is by hand, the same person, so step 2 needs no roster and ignores it
(a roster that lists both spellings of one man must not break his alias); it still needs exactly one fit and
NEVER_SAME.
A Jr never binds to a Sr: when both names carry a suffix and they differ, even an exact name is no match (a suffix on
one side only still matches). `near` lists the names on the list with the same surname that the rule did NOT
accept, so a caller can warn about a new spelling.

ALIASES holds only pairs that are the same real person; add a pair here and every book and the resulter
(auto_result_leagues.PLAYER_ALIAS is built from it) picks it up."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, NamedTuple

_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}

# Same real person, two spellings a prefix cannot join. Each pair works in both directions.
ALIASES = [
    ("Marquise Brown", "Hollywood Brown"),       # ESPN box: Hollywood Brown (v6.261)
    ("Moe Wagner", "Moritz Wagner"),
    ("Bub Carrington", "Carlton Carrington"),
    ("Chig Okonkwo", "Chigoziem Okonkwo"),
    ("Bones Hyland", "Nahshon Hyland"),
    # found by scripts/audit_b365_names.py on 7 Nov (bet365 card spelling / tipbot roster spelling)
    ("Ahmad Gardner", "Sauce Gardner"),
    ("Andres Borregales", "Andy Borregales"),
    ("Andrew Ogletree", "Drew Ogletree"),
    ("Kenneth Gainwell", "Kenny Gainwell"),
    ("JT Tuimoloau", "Jaylahn Tuimoloau"),
    ("Jacob Bates", "Jake Bates"),
]


# Two different men whose spellings a prefix would join across teams (the bridge session's Sportmarket board, 7 Nov):
# James Williams Sr. (Titans) is not Jameson Williams (Lions); Jay Higgins IV (Ravens) is not Jayden Higgins (Texans).
# The roster check in match() covers most such pairs; this list covers one the roster lacks.
NEVER_SAME = [
    ("James Williams", "Jameson Williams"),
    ("Jay Higgins", "Jayden Higgins"),
]


def tokens(name) -> list:
    """Lower-case words of a name: accents dropped, punctuation = a space, suffix words removed."""
    s = unicodedata.normalize("NFD", str(name or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [w for w in s.split() if w not in _SUFFIX]


def suffix(name) -> str:
    """The generational suffix word of a name (jr / sr / ii / iii / iv / v), or ''."""
    s = unicodedata.normalize("NFD", str(name or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    for w in re.sub(r"[^a-z0-9]+", " ", s).split():
        if w in _SUFFIX:
            return w
    return ""


def squash(name) -> str:
    """The name with every space and punctuation mark squashed out (P.J. = PJ, Ja'Kobi = Jakobi)."""
    return "".join(tokens(name))


def _build_alias_map() -> dict:
    out: dict = {}
    for a, b in ALIASES:
        ka, kb = squash(a), squash(b)
        if ka and kb and ka != kb:
            out.setdefault(ka, set()).add(kb)
            out.setdefault(kb, set()).add(ka)
    return out


ALIAS_MAP = _build_alias_map()           # {squashed name: {squashed other spellings}}


_NEVER_KEYS = {frozenset((squash(a), squash(b))) for a, b in NEVER_SAME}


def alias_keys(name) -> set:
    """The squashed alias spellings of `name` (empty when it has none)."""
    return set(ALIAS_MAP.get(squash(name), ()))


class Match(NamedTuple):
    kind: str          # "exact" | "alias" | "first_name" | "none"
    name: str          # the list's own spelling ("" for none)
    near: tuple        # same-surname names the rule did not accept (only for kind "none")


def match(names: Iterable, player: str, roster=None) -> Match:
    """The list's spelling of `player` under the rule in the module doc. Never raises on odd input.
    `roster` (a set of squashed full names, see roster_keys): the shortened-first-name step needs it, counts only when
    the tip's `player` is on it and the list's spelling is not (a different player on the roster), and the alias step
    ignores it. NEVER_SAME pairs are refused by both steps whatever the roster says. An exact name is never touched
    by either."""
    key = squash(player)
    toks = tokens(player)
    if not key:
        return Match("none", "", ())
    sfx = suffix(player)

    def _clash(v) -> bool:               # a Jr never binds to a Sr
        return bool(sfx and suffix(v) and sfx != suffix(v))
    distinct: dict = {}                  # squashed -> the list's first spelling
    exact = ""
    for v in names or ():
        if isinstance(v, str) and v.strip():
            k = squash(v)
            if not k:
                continue
            distinct.setdefault(k, v.strip())
            if k == key and not exact and not _clash(v):
                exact = v.strip()
    if exact:
        return Match("exact", exact, ())

    def _never(k) -> bool:
        return frozenset((key, k)) in _NEVER_KEYS
    fits: dict = {}                      # squashed -> (kind, spelling)
    for k in ALIAS_MAP.get(key, ()):
        if k in distinct and not _clash(distinct[k]) and not _never(k):
            fits[k] = ("alias", distinct[k])
    # step 3 needs the roster: the tip's spelling is a rostered player and the list's is not another rostered one
    if (roster and key in roster and len(toks) >= 2 and len(toks[0]) >= 3 and "-" not in str(player)):
        for k, v in distinct.items():
            t = tokens(v)
            if (k != key and k not in roster and not _clash(v) and "-" not in v and len(t) == len(toks)
                    and t[1:] == toks[1:] and len(t[0]) >= 3 and t[0] != toks[0]
                    and (t[0].startswith(toks[0]) or toks[0].startswith(t[0])) and not _never(k)):
                fits.setdefault(k, ("first_name", v))
    if len(fits) == 1:
        kind, spelling = next(iter(fits.values()))
        return Match(kind, spelling, ())
    near = ()
    if len(toks) >= 2:
        near = tuple(v for k, v in distinct.items() if len(tokens(v)) >= 2 and tokens(v)[-1] == toks[-1])
    return Match("none", "", near)


def suffix_clash(a, b) -> bool:
    """True when both names carry a generational suffix and the two differ (a Jr is not a Sr)."""
    sa, sb = suffix(a), suffix(b)
    return bool(sa and sb and sa != sb)


import os as _os

_ROSTER_DIR = _os.path.dirname(_os.path.abspath(__file__))     # the roster_<sport>.json files sit next to this module
_ROSTER_CACHE: dict = {}
_ROSTER_WARNED: set = set()


def _warn_once(tag: str, msg: str) -> None:
    """One WARNING line per distinct `tag` (a roster the shortened-first-name step cannot use must show in the log)."""
    if tag in _ROSTER_WARNED:
        return
    _ROSTER_WARNED.add(tag)
    import logging
    logging.getLogger("tipbot").warning(msg)


def roster_keys(*sports) -> frozenset:
    """Squashed FULL names (two words or more) of tipbot's roster_<sport>.json files, read next to this module and
    cached by file time (a changed file is read again, a bad parse is not kept past the next change). FAIL CLOSED: no
    sport (or an empty one), a missing file, a file that is not JSON or not an object = nothing added for it and ONE
    WARNING line, never a crash; match() then finds no shortened first name for that sport."""
    import json
    import os
    use = tuple(str(s).strip().lower() for s in sports if s and str(s).strip())
    if not use:
        _warn_once("roster:no-sport", "player_names: roster_keys called with no sport: no shortened first-name "
                                      "match (the caller must pass the tip's sport)")
        return frozenset()
    out: set = set()
    for sp in use:
        if not re.fullmatch(r"[a-z0-9_]+", sp):
            _warn_once(f"roster:bad-sport:{sp}", f"player_names: {sp!r} is not a sport name: no roster")
            continue
        path = os.path.join(_ROSTER_DIR, f"roster_{sp}.json")
        try:
            mt = os.stat(path).st_mtime_ns
        except OSError as e:
            _warn_once(f"roster:missing:{path}", f"player_names: no roster file for {sp!r} ({e}): no shortened "
                                                 f"first-name match for it")
            continue
        hit = _ROSTER_CACHE.get(path)
        if not hit or hit[0] != mt:
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    raise ValueError("not a JSON object")
                keys = frozenset(squash(k) for k in d if isinstance(k, str) and len(tokens(k)) >= 2)
            except Exception as e:
                keys = frozenset()
                _warn_once(f"roster:bad:{path}:{mt}", f"player_names: roster_{sp}.json is unreadable ({type(e).__name__}: "
                                                      f"{e}): no shortened first-name match for {sp!r} until it is fixed")
            hit = _ROSTER_CACHE[path] = (mt, keys)
        out |= hit[1]
    return frozenset(out)
