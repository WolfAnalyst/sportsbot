"""The Leg Up Australia: a racing tips EMAIL newsletter (EmailOctopus), read by
gmail_watcher.py and forwarded to Telegram as a compact text post that main.py places.

Two halves, both pure (no network, unit-tested):

1. Email side (gmail_watcher): html_to_text(), subject_race_date(), build_forward().
   The email carries every Pro Tip TWICE (the "Quick Take" list, then a long write-up
   per tip) and also lists Black Book runners. Only the FIRST "PRO TIPS:" block (the
   Quick Take) and the Quick Take "FUTURE TIPS:" block are forwarded.

       SATURDAY TIPS | Quick Take
       PRO TIPS:
       Randwick R2 #4 SAFFRON VEIL – WIN @ TO+ (Bookie: Sportsbet)(Stake: 9 units)
       Randwick R3 #7 POLYMNIA – WIN @ $4.40 (Bookie: Ladbrokes)(Stake: 6 units)
       Newcastle R6 #8 ALABAMA BLITZ – Scratched.
       BLACK BOOK RUNNERS:
       ...
       FUTURE TIPS:
       CAULFIELD CUP – Caulfield (Date: 17th October)
       SAINT GEORGE – WIN @ $17 (Bookie: Sportsbet)(Stake: 3 units)
       Sent: Friday, September 4th

   The tip lines carry no date: the race date comes from the email SUBJECT
   ("SATURDAY TIPS | OCT 17", "SUNDAY'S TIPS | OCT 11" sent the evening before).

2. main side: parse_forward(text) reads the forwarded post back into
   {"date", "subject", "received", "pro": [...], "future": [...], "unparsed": [...]}.

Wilson 2026-10-18: Pro Tips auto-place (our own price shop, the named bookie is
ignored); a "TO+" tip (best tote + 10%, no fixed price) goes to MANUAL; Future Tips
are manual only (they name a race, e.g. CAULFIELD CUP, not a track R#).
"""
import re
from datetime import date
from html.parser import HTMLParser

LEGUP_HEADER = "THE LEG UP"

_DASH = r"[–—-]"  # en dash, em dash, hyphen (the email uses the en dash)

# "Randwick R2 #4 SAFFRON VEIL – WIN @ TO+ (Bookie: Sportsbet)(Stake: 9 units)".
# The saddle is optional (the Black Book once printed "Bendigo R5 MISTY WHISPER").
# The runner/rest separator is a dash WITH spaces round it, so a hyphenated horse
# name ("ALL-TOO-HARD") stays whole.
_TIP_RE = re.compile(
    rf"^\s*(?P<track>[A-Za-z][A-Za-z .'’&-]*?)\s+R\s*(?P<race>\d{{1,2}})\s+"
    rf"(?:#\s*(?P<saddle>\d{{1,2}})\s+)?(?P<runner>\S.*?)\s+{_DASH}\s+(?P<rest>\S.*)$")
_MARKET_RE = re.compile(r"^\s*(WIN|PLACE)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"@\s*(\$?\s*\d+(?:\.\d+)?|[^\s(]+)")
# v6.79 (old emails): "(Stakes: 5 units)" (26 Sep) and "(Stake: 10) units" (4 Oct) too
# ... and "(6 units)" with no "Stake:" (the OLIVEANOTHERDAY Future Tip, 26 Sep): its units must
# reach the ledger, or the race-day Pro Tip would double up Wilson's manual bet
_STAKE_RE = re.compile(r"(?:Stakes?\s*:\s*|\(\s*)(\d+(?:\.\d+)?|\.\d+)\s*\)?\s*(?:units?|u)\b",
                       re.IGNORECASE)
_BOOKIE_RE = re.compile(r"Bookie\s*:\s*([^)]+?)\s*\)", re.IGNORECASE)
# v6.264 (Wilson 7 Nov 2026): the card's stake text "Stake: 7 units (4 locked in + 3 more at the
# current price)". Only a clear "<L> locked (in) + <A> more (at the current price)" gives
# locked_units (see parse_tip_line). Review fix: the forms "(4 locked + 3 more)", "4 locked in,
# 3 more", "(4 units locked in + 3 more units at current price)" and a trailing full stop read the
# same; any other stake text that says "locked" or "more" is not understood (no price, manual).
_STK_N = r"\d+(?:\.\d+)?"
_LOCKED_STAKE_RE = re.compile(
    rf"(?<![\d.])({_STK_N})\s*(?:units?|u)?\s*locked(?:\s+in)?\s*(?:\+|,|&|\band\b)\s*"
    rf"({_STK_N})\s*(?:units?|u)?\s*more(?:\s+units?)?(?:\s+at\s+(?:the\s+)?current\s+price)?"
    rf"(?=\s*[)\].,;]|\s*$)", re.IGNORECASE)
_LOCKED_WORD_RE = re.compile(r"\blocked\b|\bmore\b", re.IGNORECASE)
_SCRATCHED_RE = re.compile(r"^\s*scratch", re.IGNORECASE)

# Future Tips: a race header line "CAULFIELD CUP – Caulfield (Date: 17th October)", a
# runner line "SAINT GEORGE – WIN @ $17 (Bookie: Sportsbet)(Stake: 3 units)" and a
# "Sent: Friday, September 4th" line (when the tip was first given).
_FUT_RUNNER_RE = re.compile(rf"^\s*(?P<runner>\S.*?)\s+{_DASH}\s+(?P<rest>(?:WIN|PLACE)\b.*)$",
                            re.IGNORECASE)

_PRO_START_RE = re.compile(r"^\s*PRO TIPS\s*:?\s*$", re.IGNORECASE)
_FUT_START_RE = re.compile(r"^\s*FUTURE TIPS\s*:\s*$", re.IGNORECASE)
# Section ends. Anything here closes the Pro Tips or Future Tips block.
_SECTION_END_RE = re.compile(
    r"^\s*(BLACK BOOK|FUTURE TIPS|FULL PREVIEW|POST RACE|RESULTS\b|STAKING|"
    r"(?:[A-Z'’]+\s+){1,3}TIPS\s*\||PRO TIPS\s*:)", re.IGNORECASE)
# v6.79: a Future Tips block also ends at the feature-race previews that followed it on 3 Oct.
# Not used for Pro/Early blocks: a marker there must stay a visible (manual) line, never cut
# off the tips after it (review).
_FUT_END_RE = re.compile(
    _SECTION_END_RE.pattern[:-1] + r"|FEATURE RACE|🔍|📊|BOD\b)", re.IGNORECASE)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_SUBJECT_DATE_RE = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+"
                              r"(\d{1,2})\b", re.IGNORECASE)


# ── email side ──────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "td", "th", "li", "ul", "ol", "table", "h1", "h2",
              "h3", "h4", "h5", "h6", "section", "article", "header", "footer", "hr"}
    _SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _clean_lines(text: str) -> list:
    out = []
    for ln in (text or "").replace("\r", "").split("\n"):
        ln = " ".join(ln.replace("\xa0", " ").replace("​", "").split())
        if ln:
            out.append(ln)
    return out


def html_to_text(html: str) -> str:
    """Email HTML -> one logical line per block element, whitespace collapsed."""
    p = _TextExtractor()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    return "\n".join(_clean_lines("".join(p.parts)))


def subject_race_date(subject: str, received: date):
    """The race date in the subject ("SATURDAY TIPS | OCT 17") as a date, or None.
    The year is the received year, except across New Year (a December email about
    "JAN 1" is next year; a January email about "DEC 31" is last year)."""
    m = _SUBJECT_DATE_RE.search(subject or "")
    if not m or received is None:
        return None
    month = _MONTHS[m.group(1).upper()[:3]]
    year = received.year
    if month == 1 and received.month == 12:
        year += 1
    elif month == 12 and received.month == 1:
        year -= 1
    try:
        return date(year, month, int(m.group(2)))
    except ValueError:
        return None


def _block_after(lines: list, start_re, from_idx: int = 0, end_re=None):
    """Lines after the FIRST line (at or after from_idx) matching start_re, up to the next
    section header; None when there is no such line."""
    end_re = end_re or _SECTION_END_RE
    for i in range(from_idx, len(lines)):
        if start_re.match(lines[i]):
            block = []
            for nxt in lines[i + 1:]:
                if end_re.match(nxt):
                    break
                block.append(nxt)
            return block
    return None


# v6.77 (the 18 Oct email): the Quick Take now has TWO Pro Tips blocks, "TODAY'S TIPS | Quick
# Take" (today's races) and "EARLY TIPS | Quick Take" (tomorrow's, dated by the later
# "EARLY TIPS | OCT 19" header), and an empty block reads "No Pro Tips today." / "No Early
# Tips today.".
_QT_EARLY_RE = re.compile(r"^\s*(?:[A-Z]+(?:['’]S)?\s+){0,2}EARLY TIPS\s*\|\s*Quick Take", re.IGNORECASE)
_QT_TODAY_RE = re.compile(r"^\s*(?!(?:\S+\s+){0,2}(?:EARLY|FUTURE)\b)(?:[A-Z]+(?:['’]S)?\s+){1,3}TIPS\s*\|\s*Quick Take\b",
                          re.IGNORECASE)  # v6.79: 1-3 words (BJ's 26 Sep prose has 8), never EARLY/FUTURE
_EARLY_DATE_RE = re.compile(r"^\s*EARLY TIPS\s*\|\s*(\S.*)$", re.IGNORECASE)
_NONE_TODAY_RE = re.compile(r"^\s*No\s+(?:[A-Za-z]+\s+){0,3}(?:tips?|runners?|additions?)"
                            r"(?:\s+(?:today|pending))?\s*(?:\.\s*[^@()]*)?$", re.IGNORECASE)
_EARLY_FWD_RE = re.compile(r"^\s*EARLY TIPS:\s*(\S+)\s*$", re.IGNORECASE)  # the forwarded post


def _quick_take_pro(lines: list, header_re):
    """(block, header_seen) for the PRO TIPS block straight after a Quick Take header. block
    is None when the header is missing OR when it has no PRO TIPS line just below it (then
    header_seen is True: a changed layout the caller must alert on, review v6.77 #3)."""
    for i, ln in enumerate(lines):
        if header_re.match(ln):
            for j in range(i + 1, min(i + 4, len(lines))):
                if _PRO_START_RE.match(lines[j]):
                    return _block_after(lines, _PRO_START_RE, j), True
            return None, True
    return None, False


# The write-up's Future Tips header ("FUTURE TIPS | Pending"): the 18 Oct Quick Take had no
# "FUTURE TIPS:" block, so pending Future Tips are read from here when it is missing.
_FUT_PENDING_RE = re.compile(r"^\s*FUTURE TIPS\s*\|\s*Pending", re.IGNORECASE)
_FUT_BOILERPLATE_RE = re.compile(r"^[\s_*]*These Future Tips\b", re.IGNORECASE)  # _md_ too


def _drop_boilerplate(block: list) -> list:
    """The block without the "These Future Tips count towards ..." sentence, including its
    wrapped continuation lines in a text/plain part (up to the line ending the sentence, at
    most 2 lines, and never a race header or runner line: a sentence missing its full stop
    must not swallow the Future Tips after it)."""
    out, skip_left = [], 0
    ends = lambda s: s.rstrip().rstrip("_*").rstrip().endswith(".")
    for ln in block or []:
        if _FUT_BOILERPLATE_RE.match(ln):
            skip_left = 0 if ends(ln) else 2
            continue
        if skip_left and "(date:" not in ln.lower() and not _FUT_RUNNER_RE.match(ln):
            skip_left = 0 if ends(ln) else skip_left - 1
            continue
        skip_left = 0
        out.append(ln)
    return out


def _drop_none_lines(block):
    return [ln for ln in (block or []) if not _NONE_TODAY_RE.match(ln)]


def extract_sections(text: str) -> dict:
    """{"pro": today's Quick Take Pro Tip lines, "early": tomorrow's (EARLY TIPS) lines,
    "early_date_text": the "EARLY TIPS | OCT 19" header, "future": Future Tip lines,
    "found": whether any tips block was found at all (an email that says "No Pro Tips today"
    has found=True and nothing to bet; found=False means the format changed)}.
    Only the Quick Take blocks are read: the long write-up below repeats every tip."""
    lines = _clean_lines(text)
    pro, today_seen = _quick_take_pro(lines, _QT_TODAY_RE)
    early, early_seen = _quick_take_pro(lines, _QT_EARLY_RE)
    if pro is None and not today_seen and not early_seen:
        # neither Quick Take header (an older layout): the first PRO TIPS block. Never when
        # an EARLY Quick Take exists, or its tips would be read twice, as today's and as
        # tomorrow's (review v6.77 #1).
        pro = _block_after(lines, _PRO_START_RE)
    future = _block_after(lines, _FUT_START_RE, end_re=_FUT_END_RE)
    if future is None:
        future = _block_after(lines, _FUT_PENDING_RE, end_re=_FUT_END_RE)
    future = _drop_boilerplate(future) if future is not None else None
    early_date_text = next((m.group(1).strip() for m in map(_EARLY_DATE_RE.match, lines)
                            if m and "quick take" not in m.group(1).lower()), "")
    # a Quick Take header without its PRO TIPS line is a changed layout: not "found", so the
    # watcher alerts instead of calling it an empty email
    broken = (today_seen and pro is None) or (early_seen and early is None)
    return {"pro": _drop_none_lines(pro), "early": _drop_none_lines(early),
            "early_date_text": early_date_text, "future": _drop_none_lines(future),
            "found": (not broken) and (pro is not None or early is not None or future is not None)}


_WEEKDAYS = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
_WEEKDAY_RE = re.compile(r"^\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*:?\s*$",
                         re.IGNORECASE)


def early_groups(lines: list, received, header_date=None) -> list:
    """v6.79 (the old emails): EARLY TIPS lines grouped by their weekday label ("Saturday:",
    "Wednesday:") -> [(race date or None, [tip lines])]. A weekday is the NEXT such day after
    `received` (early tips are for a later day). The "EARLY TIPS | OCT 9" header is only the
    fallback for lines with no weekday above them: on 9 Oct it said OCT 9 for Saturday's
    (10 Oct) Rosehill tips."""
    from datetime import timedelta
    groups, cur_date, cur = [], header_date, []
    for ln in lines or []:
        wm = _WEEKDAY_RE.match(ln)
        if wm:
            if cur:
                groups.append((cur_date, cur))
            cur = []
            cur_date = None
            if received is not None:
                ahead = (_WEEKDAYS[wm.group(1).lower()] - received.weekday()) % 7 or 7
                cur_date = received + timedelta(days=ahead)
            continue
        cur.append(ln)
    if cur:
        groups.append((cur_date, cur))
    return groups


def build_forward(subject: str, received_text: str, race_date, sections: dict,
                  early_date=None, groups=None, subject_date=None) -> str:
    """The compact Telegram post main.py parses back with parse_forward(). `groups` (from
    early_groups) dates each early tip; without it they all take `early_date`."""
    out = [LEGUP_HEADER,
           f"date: {race_date.isoformat() if race_date else 'unknown'}",
           f"subject: {subject or ''}",
           f"received: {received_text or ''}"]
    if subject_date:  # v6.79: the TODAY typo rule moved the date; main checks the original too
        out.append(f"subject_date: {subject_date.isoformat()}")
    out.append("PRO TIPS:")
    out += _drop_none_lines(sections.get("pro"))
    early = _drop_none_lines(sections.get("early"))
    if early:
        for gdate, glines in (groups if groups is not None else [(early_date, early)]):
            glines = [ln for ln in _drop_none_lines(glines) if not _WEEKDAY_RE.match(ln)]
            if glines:
                out.append(f"EARLY TIPS: {gdate.isoformat() if gdate else 'unknown'}")
                out += glines
    fut = _drop_none_lines(sections.get("future"))
    if fut:
        out.append("FUTURE TIPS:")
        out += fut
    return "\n".join(out)


# ── tips page side (v6.102) ─────────────────────────────────────────────
# Since 19 Oct the email carries no tips, only a "SEE TODAY'S TIPS" button (an EmailOctopus
# tracking link) to a public page per race day, https://thelegupaustralia.com/tips-21oct-<slug>,
# with tabs to the next two days' pages. Tips are added to a page as they're ready ("We'll add
# any new tips as soon as they're ready"), so gmail_watcher polls the pages. A Pro Tip card:
#
#   <div class="ptx"><small>Pro Tip</small><b>Geelong R2 #18 ROYAL RENDEZVOUS – WIN $4</b>
#   <p class="meta"><strong>Bookie:</strong> TAB</p><p class="meta"><strong>Stake:</strong> 6 units</p>...
#
# It is rebuilt as the old Quick Take line, so main.py places it exactly as before.

PAGE_HOST = "thelegupaustralia.com"
_PAGE_PATH_RE = re.compile(r"^/tips-(\d{1,2})([a-z]{3})-[a-z0-9]+/?$", re.IGNORECASE)
# "21 October" / "21 Oct" / "21st Oct": whole month words only (review: "3 Decent" read as 3 Dec)
_PAGE_HEAD_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|"
    r"JULY?|AUG(?:UST)?|SEPT?(?:EMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)\b", re.IGNORECASE)
# the Pro Tips section title, with at most its date pill ("Pro Tips Wed 21 Oct"); "Pro Tips
# Plus" or anything else is an unknown tips section (manual)
_PRO_TITLE_RE = re.compile(r"^\s*PRO TIPS(?:\s+(?:[A-Z]{3,9}\s+)?\d{1,2}(?:st|nd|rd|th)?\s+[A-Z]{3,9})?\s*$",
                           re.IGNORECASE)
_PRO_LABEL_RE = re.compile(r"^\s*PRO TIPS?\s*$", re.IGNORECASE)


def tips_page_url(url: str):
    """The canonical https://thelegupaustralia.com/tips-<d><mon>-<slug> for a Leg Up tips page
    URL (absolute, or a path from the page's own day tabs), else None. Nothing else is ever
    fetched as a tips page."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme or host:
        if u.scheme != "https" or host not in (PAGE_HOST, "www." + PAGE_HOST):
            return None
    m = _PAGE_PATH_RE.match(u.path or "")
    if not m or m.group(2).upper() not in _MONTHS:
        return None
    return f"https://{PAGE_HOST}{u.path.rstrip('/').lower()}"


def _nearest_date(day: int, mon: str, today: date):
    """day + month as the date nearest `today` (a page is never more than days away)."""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, _MONTHS[mon.upper()[:3]], int(day))
        except (ValueError, KeyError):
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def tips_page_date(url: str, today: date):
    """The race day a tips page is for, from its address (tips-21oct-...), or None."""
    canon = tips_page_url(url)
    if not canon:
        return None
    m = _PAGE_PATH_RE.match(canon.split(PAGE_HOST, 1)[1])
    return _nearest_date(int(m.group(1)), m.group(2), today)


class _PageReader(HTMLParser):
    """Sections (h2 title, cards) and day-tab links of a tips page. A card is a <div> inside a
    <section>: its <small> label, its first <b> headline, its "<strong>Label:</strong> value"
    meta lines and all its text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections, self.tabs, self.hero = [], [], ""
        self._sec = None
        self._card = None
        self._card_depth = 0
        self._cap = None          # (kind, parts) while capturing h1 / h2 / small / b / strong / p
        self._nav_tabs = 0
        self._p_parts = None
        self._p_label = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if self._cap:
            self._cap[1].append(" ")  # "21 October<br>Today's" must not read "OctoberToday's"
        if tag == "nav" and "dtabs" in cls:
            self._nav_tabs += 1
        elif tag == "nav" and self._nav_tabs:
            self._nav_tabs += 1
        if tag == "a" and self._nav_tabs and a.get("href"):
            self.tabs.append(a["href"])
        if tag == "section":
            self._sec = {"title": "", "cards": []}
            self.sections.append(self._sec)
            self._card = None
        elif tag == "div" and self._card is not None:
            self._card_depth += 1
        elif tag == "div" and self._sec is not None:
            self._card = {"cls": cls, "label": "", "head": "", "metas": [], "text": []}
            self._sec["cards"].append(self._card)
            self._card_depth = 1
        if tag == "h1" and not self.hero:
            self._cap = ("h1", [])
        elif tag == "h2" and self._sec is not None and self._card is None:
            self._cap = ("h2", [])
        elif self._card is not None and tag == "small" and not self._card["label"]:
            self._cap = ("small", [])
        elif self._card is not None and tag == "b" and not self._card["head"] and self._p_parts is None:
            self._cap = ("b", [])
        elif self._card is not None and tag == "p":
            self._p_parts, self._p_label = [], None
        elif self._p_parts is not None and tag == "strong":
            self._cap = ("strong", [])

    def handle_endtag(self, tag):
        if tag == "nav" and self._nav_tabs:
            self._nav_tabs -= 1
        if self._cap and tag == self._cap[0]:
            kind, parts = self._cap
            txt = " ".join("".join(parts).split())
            self._cap = None
            if kind == "h1":
                self.hero = txt
            elif kind == "h2" and self._sec is not None:
                self._sec["title"] = txt
            elif kind == "small":
                self._card["label"] = txt
            elif kind == "b":
                self._card["head"] = txt
            elif kind == "strong":
                self._p_label = txt
        if tag == "p" and self._p_parts is not None:
            val = " ".join("".join(self._p_parts).split())
            if self._p_label is not None and self._card is not None:
                lab = self._p_label.rstrip(":").strip()
                v = val[len(self._p_label):].strip() if val.startswith(self._p_label) else val
                self._card["metas"].append((lab, v))
            self._p_parts, self._p_label = None, None
        if tag == "div" and self._card is not None:
            self._card_depth -= 1
            if self._card_depth <= 0:
                self._card = None
        if tag == "section":
            self._sec, self._card = None, None

    def handle_data(self, data):
        if self._cap:
            self._cap[1].append(data)
        if self._p_parts is not None:
            self._p_parts.append(data)
        if self._card is not None:
            self._card["text"].append(data)


def _meta(card: dict, *names) -> str:
    for lab, v in card["metas"]:
        if lab.lower() in names:
            return v
    return ""


# v6.262 (Wilson 7 Nov 2026): since 5 Nov the card head is a bare "Rosehill R9 #11 SHOW BUSINESS
# – WIN" and the price sits in meta sentences. Three strict readings, in this order. The page
# parser stores a whole-<strong> sentence as a meta whose LABEL is the sentence, so both the
# label and the value are searched.
_NUM = r"\d+(?:\.\d+)?"
_BK = r"[A-Za-z0-9][A-Za-z0-9 '&,.-]*?"
_ADD_RE = re.compile(
    rf"\bAdd\s+({_NUM})\s+more\s+units?\s+at\s+the\s+current\s+price\s*"
    rf"\(\s*\$({_NUM})(?:\s+with\s+({_BK}))?\s*\)", re.IGNORECASE)
_LOCKED_RE = re.compile(
    rf"\bMembers\s+locked\s+in\s+{_NUM}\s+units?\s+at\s+\$({_NUM})"
    rf"(?:\s+including\s+{_NUM}c\s+deductions)?"
    rf"(?:\s+with\s+([A-Za-z0-9][A-Za-z0-9 '&,-]*?))?\s*\.(?=\s|$)", re.IGNORECASE)
_RATED_RE = re.compile(
    rf"\bvs\s+\$({_NUM})\s+Available\s+with\s+([A-Za-z0-9][A-Za-z0-9 '&-]*)", re.IGNORECASE)
# v6.262 review fix: wording the reading does not fully understand is no price (manual), never
# a fall-through to a different price. A "mention" is the wording alone, readable or not.
_ADD_MENTION_RE = re.compile(r"\badd\b[^$]{0,40}?\bmore\b", re.IGNORECASE)
_LOCKED_MENTION_RE = re.compile(r"\bMembers\s+locked\s+in\b", re.IGNORECASE)
_LOCKED_ANY_RE = re.compile(r"\blocked\s+in\b", re.IGNORECASE)
_LOCKED_UNITS_RE = re.compile(rf"\bMembers\s+locked\s+in\s+({_NUM})\s+units?\b", re.IGNORECASE)
_TOTAL_RE = re.compile(rf"\bfor\s+({_NUM})\s+units?\s+in\s+total\b", re.IGNORECASE)
_TOTAL_MENTION_RE = re.compile(r"\bin\s+total\b", re.IGNORECASE)
_TOTE_RE = re.compile(r"TO\s*\+|\btote\b", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"scratch", re.IGNORECASE)
_LEAD_NUM_RE = re.compile(rf"^\s*({_NUM})\b")


def _meta_texts(card: dict) -> list:
    return [" ".join(x for x in (lab, v) if x) for lab, v in card["metas"]]


def _first_bookie(b: str) -> str:
    return re.split(r"\s*(?:,|&|\band\b)\s*", (b or "").strip(), maxsplit=1)[0].strip()


def _page_price(card: dict):
    """(price text "$4.20", bookie) from a bare-head card's meta sentences, or None. First the
    "Add N more units at the current price ($P with B)" play, then "Members locked in N units
    at $P ... with B", then "Rated vs Market: ... vs $P Available with B"; a bookie list
    ("TAB, Sportsbet", "TAB and Sportsbet") gives its first name. v6.262 review fix: a card the
    reading does not fully understand has NO price (it goes to the manual alert), it never falls
    through to another price: any scratch / TO+ / tote wording; an Add or Locked sentence that
    cannot be read (an Add one never falls to Locked, a Locked one never to Rated); two
    different prices; a price of 1 or less; and an Add play whose units do not agree with the
    Stake ("for N units in total" = the stake, else locked + add = the stake)."""
    texts = _meta_texts(card)
    if any(_SCRATCH_RE.search(t) or _TOTE_RE.search(t) for t in texts):
        return None
    add_n = sum(len(_ADD_MENTION_RE.findall(t)) for t in texts)
    lock_n = sum(len(_LOCKED_MENTION_RE.findall(t)) for t in texts)
    if add_n:
        found = [m for t in texts for m in _ADD_RE.finditer(t)]
        if len(found) != add_n or add_n != 1:
            return None
        m = found[0]
        price, bookie = m.group(2), _first_bookie(m.group(3))
        if float(price) <= 1.0 or not _add_units_agree(card, texts, float(m.group(1))):
            return None
        return f"${price}", bookie
    if lock_n:
        found = [m for t in texts for m in _LOCKED_RE.finditer(t)]
        if len(found) != lock_n or len({m.group(1) for m in found}) != 1:
            return None
        price = found[0].group(1)
        if float(price) <= 1.0:
            return None
        return f"${price}", _first_bookie(found[0].group(2))
    if any(_LOCKED_ANY_RE.search(t) for t in texts):
        return None  # "locked in" wording that is not the members sentence: not understood
    pool = [t for (lab, _), t in zip(card["metas"], texts) if lab.lower().startswith("rated vs market")]
    found = [m for t in pool for m in _RATED_RE.finditer(t)]
    if not found or len({m.group(1) for m in found}) != 1:
        return None
    price = found[0].group(1)
    if float(price) <= 1.0:
        return None
    return f"${price}", _first_bookie(found[0].group(2))


def _add_units_agree(card: dict, texts: list, add_units: float) -> bool:
    """The Stake units must equal locked + add, and the Add sentence's own total when it has one.
    v6.264 review fix: the locked-in units MUST be read (a total alone is not enough): the bet
    is only the added units, so an Add price with no readable locked-in number is no price."""
    sm = _LEAD_NUM_RE.match(_meta(card, "stake", "stakes"))
    if not sm:
        return False
    stake = float(sm.group(1))
    totals = [float(m.group(1)) for t in texts for m in _TOTAL_RE.finditer(t)]
    if sum(len(_TOTAL_MENTION_RE.findall(t)) for t in texts) != len(totals) or len(totals) > 1:
        return False
    locked = {float(m.group(1)) for t in texts for m in _LOCKED_UNITS_RE.finditer(t)}
    if len(locked) != 1:
        return False
    if totals and abs(totals[0] - stake) > 1e-9:
        return False
    return abs(next(iter(locked)) + add_units - stake) <= 1e-9


def _page_add_locked(card: dict):
    """(locked, added) units when the card's single Add sentence and its single Members locked
    in sentence both read and add up to the Stake, else None."""
    texts = _meta_texts(card)
    found = [m for t in texts for m in _ADD_RE.finditer(t)]
    if len(found) != 1:
        return None
    add = float(found[0].group(1))
    locked = {float(m.group(1)) for t in texts for m in _LOCKED_UNITS_RE.finditer(t)}
    if len(locked) != 1 or not _add_units_agree(card, texts, add):
        return None
    lk = next(iter(locked))
    return (lk, add) if lk > 0 and add > 0 else None


def page_card_line(card: dict) -> str:
    """A Pro Tip card as the Quick Take line parse_tip_line() reads:
    "Geelong R2 #18 ROYAL RENDEZVOUS – WIN @ $4 (Bookie: TAB)(Stake: 6 units)".
    A card that doesn't fit stays readable (main sends any line it can't parse to manual)."""
    head = card.get("head") or ""
    if not head:
        return " ".join("".join(card.get("text") or []).split())
    parts = re.split(rf"\s+{_DASH}\s+", head, maxsplit=1)
    page_bookie = ""
    add_lk = None
    if len(parts) == 2:
        rest = parts[1].strip()
        price = _meta(card, "price", "odds")
        if (not price and not _meta(card, "bookie") and "@" not in rest
                and re.fullmatch(r"(?i)(WIN|PLACE)", rest)):
            pp = _page_price(card)  # v6.262: the price moved out of the head (5 Nov)
            if pp:
                price, page_bookie = pp
        # v6.264 re-check: read for a priced head too, or a plain "7 units" bets all 7u at the head's price
        add_lk = _page_add_locked(card)
        if "@" not in rest:
            if re.fullmatch(r"(?i)(WIN|PLACE)", rest) and price:
                rest = f"{rest} {price}"
            rest = re.sub(r"(?i)^(WIN|PLACE)\s+(?=\S)", lambda m: f"{m.group(1)} @ ", rest, count=1)
        head = f"{parts[0].strip()} – {rest}"
    bookie = _meta(card, "bookie") or page_bookie
    stake = _meta(card, "stake", "stakes")
    if add_lk and re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)(?:\s*units?)?", stake or "", re.IGNORECASE):
        # v6.264 review fix: the Add sentence gave the price and the locked-in and added units
        # agree with a plain Stake, so write the stake in the card's own form and main sees
        # locked_units (a plain "7 units" would have bet all 7u at the added price)
        stake = (f"{add_lk[0] + add_lk[1]:g} units ({add_lk[0]:g} locked in + {add_lk[1]:g} more "
                 f"at the current price)")
    if stake and re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", stake):
        stake += " units"
    return (head + (f" (Bookie: {bookie})" if bookie else "")
            + (f"(Stake: {stake})" if stake else ""))


_KNOWN_SECTION_RE = re.compile(r"^\s*(FUTURE TIPS|BLACK BOOK|POST RACE)", re.IGNORECASE)


def _head_date_ok(text: str, d: date) -> bool:
    """A page heading's "21 October" / pill's "21 Oct" agrees with d (no date in it: True)."""
    found = list(_PAGE_HEAD_DATE_RE.finditer(text or ""))
    return all(int(m.group(1)) == d.day and _MONTHS[m.group(2).upper()[:3]] == d.month for m in found)


def parse_tips_page(html: str, url: str, today: date) -> dict:
    """A tips page -> {"date": its race day (from the address), "pro": Pro Tip lines (Quick
    Take format), "future": Future Tip lines, "unparsed": card lines from an unknown tips
    section, "tabs": canonical addresses of the other day pages, "found": a Pro Tips section
    exists (False = the layout changed), "date_problem": "" or why the page's own date
    headings disagree with its address (then nothing on it may auto-place)}."""
    rd = _PageReader()
    try:
        rd.feed(html or "")
        rd.close()
    except Exception:
        pass
    d = tips_page_date(url, today)
    res = {"date": d, "pro": [], "future": [], "unparsed": [], "tabs": [], "found": False,
           "date_problem": "" if d else f"no date in the page address {url!r}"}
    for t in rd.tabs:
        c = tips_page_url(t)
        if c and c not in res["tabs"]:
            res["tabs"].append(c)
    if d and not _head_date_ok(rd.hero, d):
        res["date_problem"] = f"the page heading {rd.hero!r} is not {d.isoformat()}"
    for sec in rd.sections:
        title = sec["title"]
        cards = [c for c in sec["cards"] if "empty" not in c["cls"] and (c["head"] or "".join(c["text"]).strip())]
        if _PRO_TITLE_RE.match(title):
            res["found"] = True
            if d and not _head_date_ok(title, d):
                res["date_problem"] = res["date_problem"] or f"the Pro Tips heading {title!r} is not {d.isoformat()}"
            for c in cards:
                if _PRO_LABEL_RE.match(c.get("label") or ""):
                    res["pro"].append(page_card_line(c))
                else:
                    # a card not labelled "Pro Tip" (a Black Book card moved here?): manual
                    res["unparsed"].append(f"[{title}, card {c.get('label') or 'without a label'!r}] "
                                           f"{page_card_line(c)}")
        elif re.match(r"^\s*FUTURE TIPS\b", title, re.IGNORECASE):
            for c in cards:
                if c.get("label") and "(date:" in c["label"].lower():
                    res["future"].append(c["label"])
                res["future"].append(page_card_line(c))
        elif not _KNOWN_SECTION_RE.match(title) and re.search(r"\bTIPS?\b", title, re.IGNORECASE):
            # a new kind of tips section: shown to Wilson (manual), never placed
            res["unparsed"] += [f"[{title}] {page_card_line(c)}" for c in cards]
    return res


# ── main side ───────────────────────────────────────────────────────────

def parse_tip_line(line: str):
    """One Quick Take line -> dict, or None if it doesn't look like a tip line.

    {"track", "race_num", "saddle", "runner", "market" (win|place|None),
     "odds" (float|None), "price_text" ("$4.40" / "TO+" / ...), "fixed_price" (bool),
     "units" (float|None), "locked_units" (float|None), "locked_unreadable" (bool), "bookie",
     "scratched" (bool), "raw"}

    v6.264: locked_units is L only when the stake text clearly says "<L> locked (in) + <A> more
    (at the current price)" exactly once and L + A equals the units read. Review fix: stake text
    that mentions "locked" or "more" in any other way is NOT understood: locked_unreadable is set
    and the tip has no price (manual), never a silent bet of the full units."""
    m = _TIP_RE.match(line or "")
    if not m:
        return None
    rest = m.group("rest").strip()
    tip = {
        "track": m.group("track").strip(),
        "race_num": int(m.group("race")),
        "saddle": int(m.group("saddle")) if m.group("saddle") else None,
        "runner": m.group("runner").strip(),
        "market": None, "odds": None, "price_text": "", "fixed_price": False,
        "units": None, "locked_units": None, "locked_unreadable": False, "bookie": "",
        "scratched": False, "raw": line.strip(),
    }
    if _SCRATCHED_RE.match(rest):
        tip["scratched"] = True
        return tip
    mk = _MARKET_RE.match(rest)
    if mk:
        tip["market"] = mk.group(1).lower()
    pm = _PRICE_RE.search(rest)
    if pm:
        ptxt = pm.group(1).strip()
        tip["price_text"] = ptxt
        num = re.fullmatch(r"\$?\s*(\d+(?:\.\d+)?)", ptxt)
        if num:
            tip["odds"] = float(num.group(1))
            tip["fixed_price"] = True
    sm = _STAKE_RE.search(rest)
    if sm:
        tip["units"] = float(sm.group(1))
        tail = rest[sm.start():]
        words = len(_LOCKED_WORD_RE.findall(tail))
        if words:
            lk = list(_LOCKED_STAKE_RE.finditer(tail))
            if len(lk) == 1 and words == 2:
                held, more = float(lk[0].group(1)), float(lk[0].group(2))
                if held > 0 and more > 0 and abs(held + more - tip["units"]) <= 1e-9:
                    tip["locked_units"] = held
            if tip["locked_units"] is None:
                tip["locked_unreadable"] = True
                tip["odds"], tip["fixed_price"] = None, False
    bm = _BOOKIE_RE.search(rest)
    if bm:
        tip["bookie"] = bm.group(1).strip()
    return tip


def _parse_future(lines: list):
    """Future Tip lines -> ([{"race", "runner", "rest", "sent", "raw"}], [unparsed])."""
    out, unparsed = [], []
    race = ""
    for ln in lines:
        if "(date:" in ln.lower():  # the race header; a runner line never carries one
            race = ln.strip()
            continue
        if ln.lower().startswith("sent:"):
            if out:
                out[-1]["sent"] = ln.split(":", 1)[1].strip()
            continue
        fm = _FUT_RUNNER_RE.match(ln)
        if fm:
            out.append({"race": race, "runner": fm.group("runner").strip(),
                        "rest": fm.group("rest").strip(), "sent": "", "raw": ln.strip(),
                        "note": ""})
            continue
        if out and not race_since_runner(out, race):
            # v6.79: "Bet will be refunded - he didn't nominate." / "*Anything above $3 is a
            # fair price" belong to the tip above, not to an unreadable-lines list
            out[-1]["note"] = (out[-1].get("note", "") + " " + ln.strip()).strip()
            continue
        unparsed.append(ln.strip())
    return out, unparsed


def race_since_runner(out: list, race: str) -> bool:
    """True when a new race header came after the last runner (a note then has no tip)."""
    return bool(out) and race != out[-1]["race"]


def parse_forward(text: str):
    """The Telegram post built by build_forward() -> dict, or None if it isn't one."""
    lines = _clean_lines(text)
    if not lines or lines[0].upper() != LEGUP_HEADER:
        return None
    res = {"date": None, "subject_date": None, "subject": "", "received": "", "pro": [], "future": [],
           "early": [], "early_date": None, "unparsed": [], "future_unparsed": []}
    section = None
    cur_early = None
    fut_lines = []
    for ln in lines[1:]:
        low = ln.lower()
        if section is None and low.startswith("date:"):
            v = ln.split(":", 1)[1].strip()
            res["date"] = v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None
            continue
        if section is None and low.startswith("subject:"):
            res["subject"] = ln.split(":", 1)[1].strip()
            continue
        if section is None and low.startswith("received:"):
            res["received"] = ln.split(":", 1)[1].strip()
            continue
        if section is None and low.startswith("subject_date:"):
            v = ln.split(":", 1)[1].strip()
            res["subject_date"] = v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None
            continue
        if _PRO_START_RE.match(ln):
            section = "pro"
            continue
        em = _EARLY_FWD_RE.match(ln)
        if em:
            section = "early"
            v = em.group(1)
            cur_early = v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None
            if res["early_date"] is None:
                res["early_date"] = cur_early
            continue
        if _FUT_START_RE.match(ln):
            section = "future"
            continue
        if _NONE_TODAY_RE.match(ln):
            continue  # "No Pro Tips today."
        if section in ("pro", "early"):
            tip = parse_tip_line(ln)
            if tip is not None:
                if section == "early":
                    tip["race_date"] = cur_early  # v6.79: per weekday group
                res[section].append(tip)
            else:
                # Every other line in the Quick Take block goes to the manual alert: a
                # format change ("Randwick Race 2 ...") must never vanish silently (review M4).
                res["unparsed"].append(ln.strip())
        elif section == "future":
            fut_lines.append(ln)
    res["future"], res["future_unparsed"] = _parse_future(fut_lines)
    return res


def future_key(f: dict) -> str:
    """Stable identity of a Future Tip across the daily emails that repeat it. v6.74: the
    units are part of it, so a Future Tip re-sent with MORE units reaches main.py, whose
    units ledger turns it into a top-up (same units = the daily repeat, dropped)."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    race = (f.get("race") or "").split("(")[0]
    race = re.split(rf"\s+{_DASH}\s+", race)[0]
    sm = _STAKE_RE.search(f.get("rest") or "")
    units = f"{float(sm.group(1)):g}u" if sm else ""
    return f"{norm(race)}|{norm(f.get('runner'))}|{norm(f.get('sent'))}|{units}"
