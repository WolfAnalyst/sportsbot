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
import re
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


# v6.148 (2026-10-29, found by the limits poller review): HyperBot's /api/pending_bets reports
# the SPORT FAMILY ("basketball", "american_football", "Racing", "soccer" on 29 Oct; the May
# self-test sample said "NBA"), while callers pass tipbot's league ("nba", "nfl"). The exact
# compare rejected every NBA / NFL landing, so a slow 538 that LANDED read as not placed and was
# rebet at the max on the same account (a double stake). Both sides map to one family; only two
# KNOWN, different families reject. An unknown label never rejects: a false "found" only
# under-fills, a false "absent" double-stakes.
_SPORT_FAMILY = {
    "nba": "basketball", "nbl": "basketball", "wnba": "basketball", "ncaab": "basketball",
    "basketball": "basketball",
    "nfl": "americanfootball", "ncaaf": "americanfootball", "americanfootball": "americanfootball",
    "afl": "aussierules", "aussierules": "aussierules", "australianrules": "aussierules",
    "australianfootball": "aussierules",
    "mlb": "baseball", "baseball": "baseball",
    "nhl": "icehockey", "icehockey": "icehockey",
    "soccer": "soccer", "epl": "soccer",      # not "football": AFL might be labelled that way
    "racing": "racing", "horseracing": "racing", "thoroughbreds": "racing", "harness": "racing",
    "greyhounds": "racing",
    "tennis": "tennis", "mma": "mma", "ufc": "mma", "nrl": "rugbyleague", "rugbyleague": "rugbyleague",
}


def _sport_mismatch(pending_sport, sport) -> bool:
    """True only when both sides are known sport families and they differ."""
    a = _SPORT_FAMILY.get(_norm(str(pending_sport or "")))
    b = _SPORT_FAMILY.get(_norm(str(sport or "")))
    return bool(a and b and a != b)


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _line_mismatch(bet_text, line) -> bool:
    """v6.148 review: with the sport gate fixed, another single on the same player (a rebounds
    bet beside the points attempt) passed every gate. True only when the attempt has a line, the
    pending bet text carries numbers, and none of them is within 1 of the line (30.5 in "30.5+
    Points", 16.5 in "Over 16.5", a milestone's 6 in "6+ Rebounds" for line 5.5, a snapped rung).
    No line, a non-finite line or no number in the text: no reject (a false reject would
    double-stake)."""
    if line is None or line == "":
        return False
    try:
        ln = abs(float(line))
    except (TypeError, ValueError):
        return False
    if ln != ln or ln in (float("inf"),):
        return False
    nums = []
    for x in _NUM_RE.findall(str(bet_text or "")):
        try:
            nums.append(float(x))
        except ValueError:
            pass
    if not nums:
        return False
    # within 1 of the posted line (review): a milestone's "N+" is line + 0.5, and HyperBot can snap a
    # whole-number post to the next rung ("Line moved 23.0 -> 22.5"); a different stat's line
    # (rebounds 5.5 beside points 30.5) is still rejected
    return not any(abs(n - ln) <= 1.0 + 1e-6 for n in nums)


_SCORE_RE = re.compile(r"(?<![\d.:\-])(\d)\s?-\s?(\d)(?![\d.\-:]|\s?[ap]m\b)")


def _score_tokens(text) -> set:
    """v6.180: set-score tokens ("2-0") in a bet text, as "N-M". Single digits joined by a dash
    only (a colon is a label: "Set 1: 2-0" is the score 2-0, not 1-2), never inside a longer run
    (a date 1-2-2026, a time 7-8pm), so a handicap (-5.5), a date or a line never reads as one."""
    return {f"{a}-{b}" for a, b in _SCORE_RE.findall(str(text or "").replace("\u2212", "-").replace("\u2013", "-"))}


def _score_mismatch(bet_text, selection_text) -> bool:
    """v6.180 (Wilson via the bridge chat, 2026-10-31: "2-0 vs 2-1 can match the wrong pending bet"):
    True when BOTH the attempt's selection text and the pending bet text carry exactly one set
    score and they differ. Tennis set betting only in practice; no score on either side = no
    reject (a false reject would double-stake)."""
    a, b = _score_tokens(selection_text), _score_tokens(bet_text)
    return len(a) == 1 and len(b) == 1 and a != b


def _line_distance(bet_text, line):
    """v6.180: how far the pending bet text's nearest number is from the attempt's line (None when
    there is no line or no number). Used to RANK matches, never to reject: among several pending
    bets that pass every gate (an 8.5 and a 9.5 on the same player, a -7.0 and a -7.5 on the same
    team once whole rungs exist), the exact line is ours."""
    if line is None or line == "":
        return None
    try:
        ln = abs(float(line))
    except (TypeError, ValueError):
        return None
    if ln != ln or ln == float("inf"):
        return None
    try:
        signed_line = float(line)
    except (TypeError, ValueError):
        signed_line = None
    best = None
    text = str(bet_text or "").replace("\u2212", "-").replace("\u2013", "-")
    for m in re.finditer(r"([+-]?)(\d+(?:\.\d+)?)", text):
        try:
            n = float(m.group(2))
        except ValueError:
            continue
        # review: a signed text number ("+7.5" / "-7.5") is compared with its sign when the
        # attempt's line is NEGATIVE (its sign is then certain), so the two sides of one spread on
        # the same account do not tie; a positive attempt line may be an unsigned magnitude, and an
        # unsigned text number has none, so those compare by magnitude
        if (m.group(1) and signed_line is not None and signed_line < 0
                and (m.start(1) == 0 or not text[m.start(1) - 1].isalnum())):
            d = abs((n if m.group(1) == "+" else -n) - signed_line)
        else:
            d = abs(n - ln)
        if best is None or d < best:
            best = d
    return best


# words that follow a name in a pending bet text without being a name (the side, the market, the
# racing market tag): a pending "Anna K. -5.5 games" must not read as the surname "games"
_NOT_A_NAME = frozenset({
    "over", "under", "win", "place", "line", "total", "totals", "points", "point", "rebounds",
    "assists", "threes", "blocks", "steals", "games", "game", "sets", "set", "asian", "handicap",
    "spread", "margin", "yards", "receiving", "rushing", "passing", "receptions", "touchdowns",
    "touchdown", "disposals", "marks", "tackles", "goals", "kicks", "handballs", "clearances",
    "hitouts", "first", "last", "anytime", "alternate", "pick", "own", "team", "match", "the",
    "and", "with", "each", "way", "quinella", "exacta", "trifecta", "odds", "boost", "promo",
    "multi", "same", "sgm", "leg", "legs", "h2h", "head", "moneyline", "winner", "draw", "tie",
    "plus", "minus", "half", "quarter", "1st", "2nd", "3rd", "4th", "correct", "score", "scorer",
    "betting", "bet", "double", "triple", "pts", "reb", "ast", "pra", "trb", "stl", "blk",
})


def _ordered_toks(s):
    """_toks as a list in text order (duplicates kept), accents folded."""
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in _fold(s)).split()
            if len(t) >= 3]


def _looks_like(a: str, b: str) -> bool:
    """v6.180 (review): two surname spellings that are one name after folding (Schroder / Schroeder,
    Doncic / Donchich): a close string ratio, or one a prefix of the other."""
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _same_first_name_other_player(bet_text, selection_text) -> bool:
    """v6.180 (tipbot-f5's set_betting reviewers, 2026-10-31): "Anna Kalinina" shares one token with
    the opponent's pending "Anna Blinkova (Anna Blinkova)" on the same match, so the one-shared-token
    gate read the opponent's bet as ours. True only when: the selection has at least two name
    tokens, the ONLY shared token is the selection's first word, and in the pending text that word
    is followed by a name-like word (alphabetic, not a market word) that the selection does not
    carry. A single-token selection, two or more shared tokens, a shortened pending name ("Anna K."),
    a market word after the name, or a surname that is the tip's with another spelling (accents
    folded, then a close string ratio) never reject (a false reject double-stakes)."""
    sel = _ordered_toks(selection_text)
    bet = _ordered_toks(bet_text)
    if len(sel) < 2 or len(bet) < 2:
        return False
    # mutation lens (G1): market words shared by both sides ("under", "over", "win") are not names,
    # so "Paul Reed Under" vs the foreign "Paul Walker 9.5 Rebounds (Paul Walker Under)" still has
    # exactly one shared NAME, paul
    shared = (set(sel) & set(bet)) - _NOT_A_NAME
    if shared != {sel[0]} or sel[0] in _NOT_A_NAME:
        return False
    first = sel[0]
    for i, t in enumerate(bet[:-1]):
        if t != first:
            continue
        nxt = bet[i + 1]
        if (nxt.isalpha() and nxt not in _NOT_A_NAME and nxt not in sel
                and not any(_looks_like(nxt, x) for x in sel[1:])):
            # review: a surname that merely SPELLS differently from the tip's (a transliteration
            # the fold did not catch) is the same player, never a reject
            return True
    return False


def _debit_floor(chosen: dict, cands: list, stake):
    """v6.180 refutation round (M4): when several records passed the gates and the chosen one's stake
    is below another candidate's, the caller's debit (`match["stake"]`) is raised to the largest
    candidate stake, capped at the attempt: our own bet may be the larger record (HyperBot snapped
    the line, a foreign exact-line bet was chosen), and a debit that is too SMALL leaves the
    difference to be re-placed (a double stake), while one too large only under-reports. The
    record's id / odds / text are the chosen record's; the copy carries `stake_floor_from`."""
    if not chosen or len(cands) <= 1:
        return chosen
    try:
        mx = max(float(c.get("stake") or 0) for c in cands)
        cur = float(chosen.get("stake") or 0)
        cap = float(stake) if stake is not None else mx
    except (TypeError, ValueError):
        return chosen
    floor = min(mx, cap)
    if floor <= cur + 0.005:
        return chosen
    out = dict(chosen)
    out["stake"] = round(floor, 2)
    out["stake_floor_from"] = cur
    log.warning(f"reconcile: debit raised from ${cur:.2f} (the chosen record) to ${floor:.2f} (the "
                f"largest of {len(cands)} matching records, capped at the attempt ${stake}): a smaller "
                f"debit could leave our own stake to be re-placed")
    return out


def _best_match(cands: list, line, selection_text=None, stake=None):
    """v6.180: the candidate nearest the attempt's line (exact first; no number last), then the one
    at the attempt's exact stake, then the one sharing MORE selection tokens; ties keep the
    pending_bets order. `cands` are records that already passed every gate, so the choice never
    changes landed, only which record's stake / odds / id are recorded; a chosen stake that is not
    the attempt's is logged as a WARNING (review: a snapped line beside a foreign exact-line bet)."""
    if len(cands) <= 1:
        return cands[0] if cands else None
    sel_toks = _toks(selection_text) if selection_text else set()

    def _stake_off(c) -> int:
        try:
            return 0 if stake is not None and abs(float(c.get("stake")) - float(stake)) <= 0.01 else 1
        except (TypeError, ValueError):
            return 1

    ranked = sorted(enumerate(cands),
                    key=lambda ic: (_line_distance(ic[1].get("bet"), line) is None,
                                    _line_distance(ic[1].get("bet"), line) or 0.0,
                                    _stake_off(ic[1]),
                                    -len(sel_toks & _toks(ic[1].get("bet"))), ic[0]))
    chosen = ranked[0][1]
    others = [f"{str(c.get('bet'))[:40]} ${c.get('stake')}" for c in cands if c is not chosen]
    stakes = set()
    for c in cands:
        try:
            stakes.add(round(float(c.get("stake")), 2))
        except (TypeError, ValueError):
            pass
    if _stake_off(chosen) or len(stakes) > 1:
        # re-check: the records carry different stakes, so one of them may be OURS auto-capped
        # beside a foreign bet at the attempt's stake; the ledger row needs a look either way
        why = (f"its stake is NOT the attempt's ${stake}" if _stake_off(chosen)
               else f"the matching records carry different stakes {sorted(stakes)}")
        log.warning(f"reconcile: {len(cands)} pending bets matched every gate; chose "
                    f"{str(chosen.get('bet'))[:60]!r} ${chosen.get('stake')} (nearest line {line}) over "
                    f"{others}; {why}: check the ledger row")
    else:
        log.info(f"reconcile: {len(cands)} pending bets matched every gate; chose "
                 f"{str(chosen.get('bet'))[:60]!r} (nearest line {line}, the attempt's stake) over {others}")
    return chosen


_FOLD_TABLE = str.maketrans({
    # letters NFKD does not decompose (refutation round: "Đokić" vs the book's "Djokic", "Łukasz")
    "\u0111": "dj", "\u0110": "Dj", "\u00f0": "d", "\u00d0": "D", "\u0142": "l", "\u0141": "L",
    "\u00f8": "o", "\u00d8": "O", "\u0131": "i", "\u00fe": "th", "\u00de": "Th", "\u00e6": "ae",
    "\u00c6": "Ae", "\u0153": "oe", "\u0152": "Oe", "\u00df": "ss",
})


def _fold(s) -> str:
    """v6.180 (review): accents and other marks folded away, so the tip's "Jokić" and the book's
    "Jokic" are one token (NFKD, combining marks dropped), and the letters NFKD leaves whole are
    mapped by hand (đ -> dj, ł -> l, ø -> o, ı -> i, þ -> th, æ -> ae, œ -> oe, ß -> ss)."""
    import unicodedata
    out = unicodedata.normalize("NFKD", str(s or "").translate(_FOLD_TABLE))
    return "".join(c for c in out if not unicodedata.combining(c))


def _toks(s):
    """Significant (>=3 alnum char) lowercased tokens of s (split on non-alnum,
    exact-token equality). Shared by pending_bet_matches and the startup orphan
    sweep for fuzzy selection comparison. v6.180: accents folded first."""
    return {t for t in "".join(
        c.lower() if c.isalnum() else " " for c in _fold(s)
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
                        stake_tol=DEFAULT_STAKE_TOL, market=None, line=None,
                        selection_exact=True):
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

    if sport and _sport_mismatch(pending.get("sport"), sport):
        return False

    if _line_mismatch(pending.get("bet"), line):
        log.info(f"reconcile: dt/event/stake matched but the pending bet "
                 f"{str(pending.get('bet'))[:60]!r} is not on line {line}: rejecting (not our bet)")
        return False

    # v6.180 review: the two text rules below read the selection as THE posted selection (a player,
    # a runner, a team, a set score). The deferred sports verify can only offer a tipster's raw
    # message when the result carried no posted selection (SGM, MLB): fractional odds "3-1" or a
    # "5-2 record" there would read as a score and reject our own bet, so it passes
    # selection_exact=False and the rules stand aside (the one-shared-token gate still applies).
    if selection_exact and _score_mismatch(pending.get("bet"), selection_text):
        log.info(f"reconcile: dt/event/stake matched but the pending bet "
                 f"{str(pending.get('bet'))[:60]!r} is another set score than "
                 f"{str(selection_text)[:40]!r}: rejecting (not our bet)")
        return False

    if selection_exact and _same_first_name_other_player(pending.get("bet"), selection_text):
        log.info(f"reconcile: dt/event/stake matched but the pending bet "
                 f"{str(pending.get('bet'))[:60]!r} names another player with the same first name as "
                 f"{str(selection_text)[:40]!r}: rejecting (not our bet)")
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
                      market=None, exclude_bet_ids=(), line=None, selection_exact=True,
                      line_gate=True):
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

    _excl = {str(x) for x in (exclude_bet_ids or ()) if x}
    deadline = time.time() + max_wait_sec
    api_ok_once = False
    last_ok_ts = None   # v6.115: when the latest SUCCESSFUL read STARTED (its snapshot's age)
    while True:
        t_req = time.time()
        resp = hb.get_pending_bets(account_id)
        if resp.get("success") is False:
            log.warning(f"reconcile: get_pending_bets failed: {resp.get('error')}")
        else:
            api_ok_once = True
            last_ok_ts = t_req
            _matched = []
            for b in resp.get("bets", []):
                # v6.128: a bet already known on this account (the same account's main-row bet
                # seconds before its milestone twin) is never read as this one
                if _excl and (str(b.get("id")) in _excl or str(b.get("bookie_bet_id")) in _excl):
                    continue
                # v6.180 refutation round: the deferred sports verify's "absent" ends in a "place it
                # by hand" alert, so there the line only RANKS (line_gate=False); the slow-538
                # path keeps the within-1 gate (its "absent" only debits)
                if pending_bet_matches(b, event=event, stake=stake, sport=sport,
                                       selection_text=selection_text, after_ts=after_ts,
                                       market=market, line=(line if line_gate else None),
                                       selection_exact=selection_exact):
                    _matched.append(b)
            if _matched:
                # v6.180: several records can pass every gate (an 8.5 and a 9.5 on the same
                # player within the line tolerance): the one on the exact line is ours
                b = _best_match(_matched, line, selection_text, stake)
                b = _debit_floor(b, _matched, stake)
                log.info(
                    f"reconcile: LANDED — matched pending bet id={b.get('id')} "
                    f"bookie_bet_id={b.get('bookie_bet_id')} stake=${b.get('stake')} "
                    f"odds={b.get('odds')} event='{b.get('event')}'"
                )
                return {"landed": True, "match": b, "reconcile_failed": False}
        if time.time() >= deadline:
            break
        time.sleep(poll_interval_sec)

    # v6.115 (review F1): absence is PROVEN only by a successful read that STARTED in the window's
    # last poll interval. One early good read followed by failures (a stalled HyperBot) proves
    # nothing, and neither does a slow read that finished late but asked early: the bet may have
    # appeared after that snapshot, and "not found" lets the caller re-place it.
    if last_ok_ts is not None and last_ok_ts >= deadline - poll_interval_sec:
        log.info(
            f"reconcile: NOT FOUND after {max_wait_sec}s — bet did not land "
            f"(event='{event}' stake=${stake}); caller may spill"
        )
        return {"landed": False, "match": None, "reconcile_failed": False}
    if api_ok_once:
        log.warning("reconcile: pending_bets failed at the end of the window (the last good read "
                    f"was {deadline - last_ok_ts:.0f}s before it) — reconcile_failed, absence "
                    "not proven")
        return {"landed": None, "match": None, "reconcile_failed": True}
    log.warning("reconcile: pending_bets unreachable all round — reconcile_failed")
    return {"landed": None, "match": None, "reconcile_failed": True}


def decide_ambiguous(hb, account_id, *, event, stake, sport, selection,
                     submit_ts, reconcile_enabled, spill_enabled, market=None,
                     exclude_bet_ids=(), line=None):
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
            market=market, exclude_bet_ids=exclude_bet_ids, line=line,
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


# ── Late-landing check after a re-placement (v6.117) ────────────────────────
# Wilson 2026-10-24 ("whatever is best ... please do"): every re-placement that follows a
# PROVEN absence (a same-account rebet at Sportsbet's stated max, a spill to another account
# or bookie) carries one residual risk: the original bet lands AFTER the ~30 s pending_bets
# window. One deferred look at the ORIGINAL account turns that silent double into a CRITICAL
# while there is still time to cash out. ALERT-ONLY: it never places, re-places or re-stakes,
# so it cannot make anything worse.
LATE_LANDING_CHECK_DELAY_SEC = float(os.getenv("LATE_LANDING_CHECK_DELAY_SEC", "180"))
LATE_LANDING_CHECK_ENABLED = os.getenv(
    "LATE_LANDING_CHECK_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def find_late_landing(pending_bets, *, event, stake, sport, selection, submit_ts,
                      exclude_bet_ids=(), exclude_stakes=(), market=None, now_ts=None,
                      never_bet_ids=(), line=None, selection_exact=True):
    """The pending bet that is the ORIGINAL re-placed bet, or None. Pure, so it is testable.

    Matches the usual gates (pending_bet_matches: an auto-capped, SMALLER landing of the original
    still counts) and leaves out the re-placement itself: a same-account rebet is on the same
    event and selection, so it is excluded by its bet id AND by its stake (review: the reply's
    bet id may not be the pending record's id, and a rebet's stake is exact)."""
    excl = {str(x) for x in (exclude_bet_ids or ()) if x}
    # v6.128: another known bet on this account (a milestone twin's main bet) is simply never the
    # original; it does not use up the re-placement's stake slot
    never = {str(x) for x in (never_bet_ids or ()) if x}
    excl_st = []
    for x in exclude_stakes or ():
        try:
            excl_st.append(float(x))
        except (TypeError, ValueError):
            pass
    # Each re-placement excludes ONE record (review: an original auto-capped to exactly the max
    # must not hide behind its rebet). A record matched by bet id uses up its stake slot.
    cands = []
    for b in pending_bets or []:
        try:
            st = float(b.get("stake") or 0)
        except (TypeError, ValueError):
            continue
        ids = {str(b.get("bookie_bet_id") or ""), str(b.get("id") or "")} - {""}
        if ids & never:
            continue
        if ids & excl:
            for i, x in enumerate(excl_st):
                if abs(st - x) <= 0.01:
                    del excl_st[i]
                    break
            continue
        cands.append((b, st))
    _matched = []
    for b, st in cands:
        hit_i = next((i for i, x in enumerate(excl_st) if abs(st - x) <= 0.01), None)
        if hit_i is not None:
            del excl_st[hit_i]
            continue
        if pending_bet_matches(b, event=event, stake=stake, sport=sport,
                               selection_text=selection, after_ts=submit_ts,
                               now_ts=now_ts, market=market, line=line,
                               selection_exact=selection_exact):
            _matched.append(b)
    return _best_match(_matched, line, selection, stake)      # v6.180: the exact line first


def schedule_late_landing_check(hb, account_id, *, event, stake, sport, selection, submit_ts,
                                label, notify, exclude_bet_ids=(), exclude_stakes=(), market=None,
                                never_bet_ids=(), line=None,
                                delay_sec=None, retry_sec=30.0, selection_exact=True):
    """Fire-and-forget: after `delay_sec`, look at the ORIGINAL account once more; if the
    original bet is now there, call notify(message) (a CRITICAL). Never raises into the
    caller and never places anything. Returns the thread (tests join it), or None."""
    if not LATE_LANDING_CHECK_ENABLED or not account_id:
        return None
    delay = LATE_LANDING_CHECK_DELAY_SEC if delay_sec is None else float(delay_sec)
    excl = tuple(str(x) for x in (exclude_bet_ids or ()) if x)

    def _read():
        # review: a 200 with a non-JSON or error body must count as unreadable, never as "absent"
        try:
            r = hb.get_pending_bets(account_id)
        except Exception as e:
            log.warning(f"late-landing check {label}: pending_bets raised ({e})")
            return None
        if (not isinstance(r, dict) or r.get("success") is False
                or not isinstance(r.get("bets"), list)):
            return None
        return r

    def _worker():
        try:
            time.sleep(max(0.0, delay))
            resp = _read()
            if resp is None:                       # one retry, then say it could not run (review)
                time.sleep(max(0.0, float(retry_sec)))
                resp = _read()
            if resp is None:
                msg = (f"Late-landing check could NOT run: {label}. pending_bets was unreadable "
                       f"twice, so it is not known whether the original ${float(stake):.2f} bet "
                       f"landed late beside its re-placement. Check the account by hand.")
                log.error(f"late-landing check: {msg}")
                notify(msg)
                return
            hit = find_late_landing(resp.get("bets") or [], event=event, stake=stake,
                                    sport=sport, selection=selection, submit_ts=submit_ts,
                                    exclude_bet_ids=excl, exclude_stakes=exclude_stakes,
                                    market=market, never_bet_ids=never_bet_ids, line=line,
                                    selection_exact=selection_exact)
            if hit is None:
                log.info(f"late-landing check {label}: the original ${float(stake):.2f} is "
                         f"still absent after {int(delay)}s - no double")
                return
            msg = (f"POSSIBLE DOUBLE BET: {label}. The original ${float(stake):.2f} bet was "
                   f"proven absent and the stake was placed again, but it has now APPEARED "
                   f"on the account ({hit.get('bet')} @ {hit.get('odds')}, "
                   f"${hit.get('stake')}, bet id {hit.get('bookie_bet_id') or hit.get('id')}). "
                   f"Check the account and cash out one of them if the event has not started.")
            log.error(f"late-landing check: {msg}")
            notify(msg)
        except Exception as e:  # never escape: alert-only
            log.warning(f"late-landing check {label} failed: {e}")

    try:
        import threading
        t = threading.Thread(target=_worker, name=f"late-landing-{label[:40]}", daemon=True)
        t.start()
        return t
    except Exception as e:
        log.warning(f"could not schedule the late-landing check {label}: {e}")
        return None


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


# ── Player-prop EXPOSURE sweep (v6.08b, 2026-08-02) ─────────────────────────
# WHY THIS EXISTS. The DisposalsModel exposure ledger caps cumulative stake per
# selection, but it is keyed on THAT TIPSTER'S OWN record of its own placements, so it
# is structurally blind to the same selection being backed from anywhere else: another
# tipster's fan-out, or Wilson by hand. A live probe on 2026-08-02 found 29 pending
# records across 5 Sportsbet accounts, almost all DISPOSALS UNDERS at liability-capped
# fan-out shares -- i.e. another tipster already holding the exact positions the model
# would tip. A model bet on one of those would see $0 committed and add a full stake on
# top. `/api/pending_bets` is the only source that observes what is ACTUALLY on rather
# than what either side believes it did.
#
# SNAPSHOT SEMANTICS, agreed with the model side: each sweep returns the CURRENT FULL
# SET of unsettled bets, so a later observation for a selection REPLACES the earlier
# one. It is never a delta. Summing successive sweeps would inflate exposure on every
# poll and progressively suppress real bets.

# Sportsbet labels a player-prop line as "<Player> <N>+ <Stat>" and puts the SIDE in
# the PARENTHETICAL, not the label:
#     "Jayden Short 22.5+ Disposals (jayden Short Under)"   -> UNDER 22.5
#     "Tom Mccarthy 23.5+ Disposals (tom Mccarthy)"         -> OVER  23.5
# So the "N+" prefix looks like an over and is NOT one. Anything that reads the side
# off the label gets EVERY under backwards -- a wrong-bet class, which is why this
# parse lives in exactly one tested place. Verified against 29 live records.
_PROP_BET_RE = _re_compile = None
try:
    import re as _re
    _PROP_BET_RE = _re.compile(
        r"^\s*(?P<player>.+?)\s+(?P<line>\d+(?:\.\d+)?)\s*\+\s*(?P<stat>[A-Za-z_ ]+?)\s*"
        r"\((?P<sel>[^)]*)\)\s*$"
    )
    _UNDER_TOKEN_RE = _re.compile(r"\bunder\b", _re.IGNORECASE)
except Exception:  # pragma: no cover
    _PROP_BET_RE = None
    _UNDER_TOKEN_RE = None


def parse_player_prop_bet(bet_text: str):
    """Parse a Sportsbet player-prop `bet` string from /api/pending_bets.

    Returns {player, line, side, stat} or None when the text is not a player prop we
    understand (an h2h, a line bet, a multi, an unknown shape). None means "do not
    count this", never "assume something".

    The side comes from the PARENTHETICAL via a word-boundary token test, not from the
    "N+" label and not from a substring: `"over" in "Overton"` is the bug this repo
    already fixed once in the bet ledger.
    """
    if not bet_text or _PROP_BET_RE is None:
        return None
    m = _PROP_BET_RE.match(str(bet_text))
    if not m:
        return None
    player = (m.group("player") or "").strip()
    sel = m.group("sel") or ""
    stat = (m.group("stat") or "").strip().lower().replace(" ", "_")
    try:
        line = float(m.group("line"))
    except (TypeError, ValueError):
        return None
    if not player or line <= 0 or not stat:
        return None
    # A multi/SGM leg string would carry several props; refuse rather than half-read it.
    if "/" in str(bet_text) or " and " in str(bet_text).lower():
        return None
    side = "under" if (_UNDER_TOKEN_RE and _UNDER_TOKEN_RE.search(sel)) else "over"
    return {"player": player, "line": line, "side": side, "stat": stat}


def collect_prop_exposure(hb, sessions, *, stat="disposals", sport="aussie_rules"):
    """Sweep /api/pending_bets across `sessions` and total the money ON per selection.

    Returns (exposure, errors):
      exposure: {(player_lower, line, side): {"total": float, "accounts": [...],
                                              "events": [...], "player": <as seen>}}
      errors:   [(session_id, reason), ...] -- an API failure is NOT zero exposure.

    The caller MUST treat a non-empty `errors` as "unknown", never as "nothing is on":
    a failed sweep that reads as zero would re-arm exactly the double-stake this
    function exists to prevent.
    """
    exposure = {}
    errors = []
    for s in (sessions or []):
        sid = str(s.get("session_id") or "")
        acct = s.get("account_id")
        if not acct:
            errors.append((sid, "no account_id"))
            continue
        try:
            resp = hb.get_pending_bets(str(acct))
        except Exception as e:
            errors.append((sid, f"exception: {e}"))
            continue
        if not isinstance(resp, dict):
            errors.append((sid, f"unexpected response type {type(resp).__name__}"))
            continue
        if resp.get("success") is False:
            errors.append((sid, f"api error: {resp.get('error')}"))
            continue
        bets = resp.get("pending_bets") or resp.get("bets") or []
        if not isinstance(bets, list):
            errors.append((sid, "pending list missing"))
            continue
        for b in bets:
            if not isinstance(b, dict):
                continue
            if b.get("result") is not None:
                continue  # settled; only unsettled money is exposure
            # v6.148: by sport family; an unknown label is COUNTED (over-counted exposure only
            # under-stakes; a skipped landing would over-stake)
            if sport and _sport_mismatch(b.get("sport"), sport):
                continue
            parsed = parse_player_prop_bet(b.get("bet"))
            if not parsed:
                continue
            if stat and parsed["stat"] != stat:
                continue
            try:
                stake = float(b.get("stake") or 0)
            except (TypeError, ValueError):
                continue
            if stake <= 0:
                continue
            k = (parsed["player"].strip().casefold(), parsed["line"], parsed["side"])
            slot = exposure.setdefault(
                k, {"total": 0.0, "accounts": [], "events": [], "player": parsed["player"]})
            slot["total"] = round(slot["total"] + stake, 2)
            slot["accounts"].append({
                "session_id": sid, "stake": round(stake, 2),
                "odds": b.get("odds"), "bet_id": b.get("id"),
            })
            ev = str(b.get("event") or "")
            if ev and ev not in slot["events"]:
                slot["events"].append(ev)
    return exposure, errors
