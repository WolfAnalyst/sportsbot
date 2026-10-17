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
_STAKE_RE = re.compile(r"Stake\s*:\s*(\d+(?:\.\d+)?|\.\d+)\s*(?:units?|u)\b", re.IGNORECASE)
_BOOKIE_RE = re.compile(r"Bookie\s*:\s*([^)]+?)\s*\)", re.IGNORECASE)
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
    r"[A-Z'’ ]*TIPS\s*\||PRO TIPS\s*:)", re.IGNORECASE)

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


def _block_after(lines: list, start_re) -> list:
    """Lines after the FIRST line matching start_re, up to the next section header."""
    for i, ln in enumerate(lines):
        if start_re.match(ln):
            block = []
            for nxt in lines[i + 1:]:
                if _SECTION_END_RE.match(nxt):
                    break
                block.append(nxt)
            return block
    return []


def extract_sections(text: str) -> dict:
    """{"pro": [quick-take Pro Tip lines], "future": [quick-take Future Tip lines]}.
    The FIRST "PRO TIPS:" is the Quick Take; the long write-up repeats it further down
    and must not be read (every tip would be seen twice)."""
    lines = _clean_lines(text)
    return {"pro": _block_after(lines, _PRO_START_RE),
            "future": _block_after(lines, _FUT_START_RE)}


def build_forward(subject: str, received_text: str, race_date, sections: dict) -> str:
    """The compact Telegram post main.py parses back with parse_forward()."""
    out = [LEGUP_HEADER,
           f"date: {race_date.isoformat() if race_date else 'unknown'}",
           f"subject: {subject or ''}",
           f"received: {received_text or ''}",
           "PRO TIPS:"]
    out += sections.get("pro") or []
    fut = [ln for ln in (sections.get("future") or [])
           if not re.match(r"^\s*No Future Tips", ln, re.IGNORECASE)]
    if fut:
        out.append("FUTURE TIPS:")
        out += fut
    return "\n".join(out)


# ── main side ───────────────────────────────────────────────────────────

def parse_tip_line(line: str):
    """One Quick Take line -> dict, or None if it doesn't look like a tip line.

    {"track", "race_num", "saddle", "runner", "market" (win|place|None),
     "odds" (float|None), "price_text" ("$4.40" / "TO+" / ...), "fixed_price" (bool),
     "units" (float|None), "bookie", "scratched" (bool), "raw"}"""
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
        "units": None, "bookie": "", "scratched": False, "raw": line.strip(),
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
                        "rest": fm.group("rest").strip(), "sent": "", "raw": ln.strip()})
            continue
        unparsed.append(ln.strip())
    return out, unparsed


def parse_forward(text: str):
    """The Telegram post built by build_forward() -> dict, or None if it isn't one."""
    lines = _clean_lines(text)
    if not lines or lines[0].upper() != LEGUP_HEADER:
        return None
    res = {"date": None, "subject": "", "received": "", "pro": [], "future": [],
           "unparsed": [], "future_unparsed": []}
    section = None
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
        if _PRO_START_RE.match(ln):
            section = "pro"
            continue
        if _FUT_START_RE.match(ln):
            section = "future"
            continue
        if section == "pro":
            tip = parse_tip_line(ln)
            if tip is not None:
                res["pro"].append(tip)
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
