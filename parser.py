"""Options-call parser: turns a post's text into zero or more OptionCall records.

Live rows need a real contract: ticker + strike price + call/put.
Also accepted: multi-line dated bare strikes like ``$TBBK / 20 NOV 26 $50 / $1.10``
(no C/P — defaults to call unless the post says put).
Expiry and entry are extracted when present; when missing they are flagged
(expiry is assumed 0DTE for daily-expiry index ETFs, entry shows as '-').
Recaps and previous-gain posts are dropped and never shown. Matches that are
not a real strike (a year, a percent, "put 2026 revenue"), two-outcome
setups, and trade ideas that were never entered are returned as flagged
notes so they stay out of the live table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional

DAILY_EXPIRY = {"SPY", "QQQ", "SPX", "SPXW", "IWM", "XSP", "NDX"}
KNOWN_BARE = DAILY_EXPIRY | {"TSLA", "NVDA", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "GOOG", "AMD",
                             "INTC", "MU", "SMCI", "PLTR", "COIN", "MSTR", "NFLX", "AVGO", "DIA", "TLT", "VIX",
                             "IBIT", "TSM", "HOOD", "BABA", "CRWD", "TQQQ", "SQQQ", "UVXY", "VXX"}
NOT_TICKERS = {"ITM", "OTM", "ATM", "DTE", "EXP", "AM", "PM", "ET", "EST", "EDT", "OI", "IV", "NEW", "AVG",
               "LOTTO", "CALL", "CALLS", "PUT", "PUTS", "THE", "AND", "FOR", "ALERT", "ENTRY", "EXIT", "GM",
               "HOD", "LOD", "NHOD", "USD", "CPI", "FOMC", "SL", "TP", "PT", "I", "A", "DAY", "TRADE",
               "INCASE", "WANTS", "WAKEUP", "SIZE", "LIGHT", "HEAVY", "RISKY"}
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
WEEKDAYS = {d: i for i, d in enumerate(["MON", "TUE", "WED", "THU", "FRI"])}

NUM = r"\d{1,5}(?:\.\d{1,2})?"
# strike then type:  694P | 758 puts | $768 put | 912.50 CALL | 277.5p
RE_STRIKE_TYPE = re.compile(rf"(?<![\w/.:%])\$?({NUM})\s?(calls?|puts?|c|p)\b", re.I)
# type letter glued before strike: C7720 | P450
RE_TYPE_STRIKE = re.compile(rf"(?<![\w$])([cp])({NUM})\b", re.I)
# type word then strike: CALL 695 | puts 450
RE_WORD_STRIKE = re.compile(rf"\b(calls?|puts?)\s+\$?({NUM})\b(?!\s*%)", re.I)
# dated bare strike (no C/P): 20 NOV 26 $50   |  09 NOV 26 $716
RE_DATED_BARE = re.compile(
    rf"\b(\d{{1,2}})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{{2,4}})\s+\$?({NUM})\b"
    rf"(?!\s*(?:calls?|puts?|[cp])\b)",
    re.I,
)
RE_DOLLAR_TICKER = re.compile(r"\$([A-Za-z]{1,5})\b(?![\d.])")
RE_BARE_TICKER = re.compile(r"\b([A-Z]{2,5})\b")
RE_URL = re.compile(r"https?://\S+")

# "closed at 84.99" is a stock close, not an exit. "entry and exit" is not a recap either.
RE_RECAP = re.compile(
    r"(→|->|➡|\bgainer\b|\bprofit\b|\bexited\b|\bexit alert\b|\bsold\b|\btrim(med|ming)?\b|"
    r"\bclos(?:ed|ing)\s+out\b|\bclosed\s+(?:out|the|my|this|half|position)\b|"
    r"\+\s?\d[\d,]*%|\d[\d,]*%\+|\bfrom\s+\$?\.?\d|\bout on\b|\bhigh of\b|\blocked in\b)",
    re.I,
)
RE_PRIOR_GAIN = re.compile(r"(\brecaps?\b|\bgains?\b|\bin the past\b|\bpast\s+(week|day|month)\b|\bhighlight trades\b)", re.I)
RE_CALL_FOR_PCT = re.compile(r"\b(calls?|puts?)\s+for\s+\d[\d,]*(?:\.\d+)?\s*%", re.I)
RE_COMMA_PCT = re.compile(r"\d{1,3},\d{3}\s*%")
# Day / session scoreboard — not a live entry (e.g. "MU 780c 1351%" lists, win ratio)
RE_DAY_PERF = re.compile(
    r"(win\s*ratio|ideas?\s+so\s+far\s+today|so\s+far\s+today|"
    r"solid\s+day|main\s+ideas\s*:|failed\s*:|"
    r"\btoday'?s?\s+(results?|performance|recap|pnl|p/?l)\b|"
    r"\boverall\s+(performance|results?)\b|"
    r"\b\d{1,3}\s*%\s+win\b)",
    re.I,
)
RE_PCT_RESULT_LINE = re.compile(
    r"(?:\$)?[A-Za-z]{1,5}\s+\d+(?:\.\d+)?\s*[cCpP]\s+\d[\d,]*(?:\.\d+)?\s*%",
)
RE_FLOW = re.compile(r"(at the ask|at the bid|\bsweep\b|\bblock\b|\bprint\b|OI confirmed|\bwhale\b|\$\d+(\.\d+)?M\b|unusual)", re.I)


@dataclass
class OptionCall:
    ticker: str
    opt_type: str            # 'C' or 'P'
    strike: float
    expiry: Optional[date]
    expiry_label: str        # e.g. '0DTE', '11/02', '0DTE?' (assumed)
    entry: Optional[float]
    flags: List[str] = field(default_factory=list)
    handle: str = ""
    author_id: str = ""
    post_id: str = ""
    posted_at: Optional[datetime] = None
    text: str = ""

    @property
    def key(self):
        return (self.ticker, self.strike, self.opt_type, self.expiry.isoformat() if self.expiry else self.expiry_label)

    @property
    def url(self):
        return f"https://x.com/{self.handle or 'i'}/status/{self.post_id}" if self.post_id else ""


@dataclass
class FlaggedNote:
    """A post that is not a live call: no real strike, two outcomes, or only a potential setup."""
    reason: str              # 'no-strike', 'conditional', or 'potential'
    detail: str
    handle: str = ""
    author_id: str = ""
    post_id: str = ""
    posted_at: Optional[datetime] = None
    text: str = ""

    @property
    def url(self):
        return f"https://x.com/{self.handle or 'i'}/status/{self.post_id}" if self.post_id else ""


def _norm_ticker(t: str) -> str:
    t = t.upper()
    return "SPX" if t == "SPXW" else t


def _find_ticker(text: str, pos: int) -> Optional[str]:
    """Nearest $TICKER before the contract; else bare symbol just before strike; else known bare; else after.

    Prefer the symbol next to the contract so later commentary like ``incase $BTC wants to wakeup``
    cannot steal the ticker from ``Trade Alert IBIT $34C``.
    """
    before = [(m.start(), m.group(1)) for m in RE_DOLLAR_TICKER.finditer(text) if m.start() < pos]
    before = [b for b in before if b[1].upper() not in NOT_TICKERS]
    if before:
        return _norm_ticker(before[-1][1])
    # Immediate bare ticker right before the strike (within ~24 chars)
    near = text[max(0, pos - 24):pos]
    m = re.search(r"\b([A-Za-z]{1,5})\s*$", near)
    if m and m.group(1).upper() not in NOT_TICKERS:
        return _norm_ticker(m.group(1))
    bare = [(m.start(), m.group(1)) for m in RE_BARE_TICKER.finditer(text.upper())
            if m.group(1) in KNOWN_BARE]
    bb = [b for b in bare if b[0] < pos]
    if bb:
        return _norm_ticker(bb[-1][1])
    after = [m.group(1) for m in RE_DOLLAR_TICKER.finditer(text) if m.start() > pos and m.group(1).upper() not in NOT_TICKERS]
    after += [b[1] for b in bare if b[0] > pos]
    return _norm_ticker(after[0]) if after else None


def _next_weekday(today: date, wd: int) -> date:
    days = (wd - today.weekday()) % 7
    return today + timedelta(days=days or 7)


def _parse_expiry(text: str, ticker: str, today: date):
    t = text.upper()
    # 0DTE / ODTE (letter O) / same-day → expiry is today
    if re.search(r"\b[0O]\s?DTE\b|\bSAME[- ]DAY\b|\bEXP(IRES|IRING)? TODAY\b", t):
        return today, "0DTE"
    m = re.search(r"\b1\s?DTE\b", t)
    if m:
        d = today + timedelta(days=3 if today.weekday() == 4 else 1)
        return d, "1DTE"
    m = re.search(r"(?<![\d.$])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d%])", t)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12 and 1 <= dy <= 31:
            y = int(yr) + (2000 if yr and len(yr) == 2 else 0) if yr else today.year
            try:
                d = date(y, mo, dy)
                if not yr and (today - d).days > 60:
                    d = date(y + 1, mo, dy)
                return d, d.strftime("%m/%d" if d.year == today.year else "%m/%d/%y")
            except ValueError:
                pass
    m = re.search(r"\b(\d{1,2})(?:ST|ND|RD|TH)?\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*(\d{2,4})?\b", t) or \
        re.search(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{1,2})(?:ST|ND|RD|TH)?(?:,?\s*(\d{4}))?\b", t)
    if m:
        g = m.groups()
        if g[0].isdigit():
            dy, mo, yr = int(g[0]), MONTHS[g[1][:3]], g[2]
        else:
            mo, dy, yr = MONTHS[g[0][:3]], int(g[1]), g[2]
        y = (int(yr) + (2000 if len(yr) == 2 else 0)) if yr else today.year
        try:
            d = date(y, mo, dy)
            if not yr and (today - d).days > 60:
                d = date(y + 1, mo, dy)
            return d, d.strftime("%m/%d" if d.year == today.year else "%m/%d/%y")
        except ValueError:
            pass
    # Month + year only (e.g. "March 2027 1130 put") → standard monthly expiry (3rd Friday)
    m = re.search(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{4})\b", t)
    if m:
        mo, y = MONTHS[m.group(1)[:3]], int(m.group(2))
        d = date(y, mo, 15)
        d = d + timedelta(days=(4 - d.weekday()) % 7)  # first Friday on/after the 15th ≈ 3rd Friday
        if d.day > 21:
            d -= timedelta(days=7)
        return d, d.strftime("%b %y")
    m = re.search(r"\b(?:(EXP(?:IRY|IRES)?|NEXT)\s+)(MON|TUE|WED|THU|FRI)[A-Z]*\b", t) or \
        re.search(r"\b()(MON|TUE|WED|THU|FRI)[A-Z]*\s+(?:CALLS?|PUTS?|EXP)", t)
    if m:
        wd = WEEKDAYS[m.group(2)]
        d = _next_weekday(today, wd) if (wd != today.weekday() or "NEXT" in t) else today
        return d, ("0DTE" if d == today else d.strftime("%a %m/%d"))
    if re.search(r"\bWEEKL(Y|IES)\b|\bTHIS WEEK\b", t):
        d = today + timedelta(days=(4 - today.weekday()) % 7)
        return d, "weekly " + d.strftime("%m/%d")
    # No specific date (and daily names like SPY/QQQ) → treat as today / ODTE
    return today, "0DTE?"


RE_ENTRY_EXPLICIT = re.compile(r"(?:@|(?<!stock )(?<!spot )(?<!price )\bat\b|\bentry(?:\s+mid)?\b|\bfill(?:ed)?\b|\bavg\b|\bfor\b|\bpaid\b)\s*:?\s*\$?\s*(\d*\.\d{1,3}|\d{1,4})(\$)?", re.I)
RE_ENTRY_BARE = re.compile(r"(?<![\w/$%])\$?(\d*\.\d{1,3})\$?(?![\d%/])")
# "from the 12.70 entry" / ".16 entry" — number tagged as entry (may sit after commentary)
RE_ENTRY_TAGGED = re.compile(
    r"(?:\bfrom\s+(?:the\s+)?|@)?\$?\s*(\d*\.\d{1,3}|\d{1,4})\s+entr(?:y|ies)\b",
    re.I,
)


def _entry_val(raw: str, flags: list) -> float:
    val = float(raw)
    if "." not in raw and val >= 10:
        # "$63" style quotes usually mean cents (0.63)
        val = val / 100.0
        flags.append("cents?")
    return val


def _parse_entry(seg: str):
    """seg = text right after the contract (up to next contract)."""
    s = re.sub(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", " ", seg)          # strip dates
    s = re.sub(r"\b\d\s?DTE\b", " ", s, flags=re.I)
    # Underlying levels, TP/stop trails, and option marks — not the fill
    s = re.sub(
        r"\b(stock|spot|price|underlying|pt|target|trigger|stop'?s?|trail(?:ing)?|tp\d*|option)\b"
        r"\W{0,3}(is|at|of|:)?\s*~?\$?\d[\d.,]*",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(r"\b\d+m\s+clos(?:e|ed|ing)\s+at\s*~?\$?\d[\d.,]*", " ", s, flags=re.I)
    s = re.sub(r"\bclos(?:e|ed|ing)\s+at\s*~?\$?\d[\d.,]*", " ", s, flags=re.I)
    s = re.sub(r"\bthrough\s+tp\d*\s*~?\$?\d[\d.,]*", " ", s, flags=re.I)
    flags = []
    # Prefer an explicitly tagged entry anywhere in the segment (handles long mid-sentence fills)
    m = RE_ENTRY_TAGGED.search(s)
    if m:
        return _entry_val(m.group(1), flags), flags
    m = RE_ENTRY_EXPLICIT.search(s)
    if m and m.start() < 40:
        return _entry_val(m.group(1), flags), flags
    m = RE_ENTRY_BARE.search(s)
    if m and m.start() < 30:
        return float(m.group(1)), flags
    return None, flags


def _conditional_why(clean: str, hits) -> Optional[str]:
    """A post that offers two outcomes based on where price goes, not one live call."""
    call = put = False
    for _s, _e, _strike, typ, _kind in hits:
        if typ[0].lower() == "c":
            call = True
        else:
            put = True
    bull = re.search(r"\bbullish\b", clean, re.I)
    bear = re.search(r"\bbearish\b", clean, re.I)
    held = re.search(r"\bif price\b.{0,90}\b(defend|holds?|bought|above)\b", clean, re.I)
    off = re.search(r"\bhands off\b|\bstay out\b", clean, re.I)
    two = re.search(r"\btwo plays\b|\blong/short\b", clean, re.I)
    up_down = re.search(r"\bupside\b", clean, re.I) and re.search(r"\bdownside\b", clean, re.I)
    split = (re.search(r"\babove\b", clean, re.I) and re.search(r"\bbelow\b", clean, re.I)
             and re.search(r"\blongs?\b", clean, re.I) and re.search(r"\b(shorts?|fade|puts?)\b", clean, re.I))
    if (call and put) or (bull and bear) or (held and off) or two or up_down or split:
        return "two outcomes depending on the price move"
    return None


def _potential_why(clean: str) -> Optional[str]:
    """A trade idea with a trigger is a plan, not a call someone has entered."""
    if not re.search(r"\btrade idea\b", clean, re.I):
        return None
    if re.search(r"(?:@|(?:\bentry(?:\s+mid)?|\bfill(?:ed)?|\bbought|\badding|\badded|\bpaid|\bavg)\b)\s*:?\s*\$?\s*\d", clean, re.I):
        return None
    return "trade idea with a trigger, not an entered call"


def _is_prior_gain(clean: str) -> bool:
    """Past results / day performance boards — omit entirely (not live, not flagged)."""
    if RE_PRIOR_GAIN.search(clean) or RE_CALL_FOR_PCT.search(clean) or RE_COMMA_PCT.search(clean):
        return True
    if RE_DAY_PERF.search(clean):
        return True
    # Scoreboard: several "TICKER 780c 1351%" style result lines
    if len(RE_PCT_RESULT_LINE.findall(clean)) >= 3:
        return True
    if RE_RECAP.search(clean) and not re.search(r"\bentry alert\b|\btrade alert\b", clean, re.I):
        return True
    return False


def _reject_strike(clean: str, start: int, end: int, strike: float, kind: str) -> Optional[str]:
    """None when this is a strike price. Otherwise a short reason to flag, not trade."""
    if re.match(r"\s*%", clean[end:end + 6]):
        return "percent, not a strike"
    # "4 put prints" / "3 call prints" — count of flow prints, not a strike
    if re.match(r"\s*prints?\b", clean[end:end + 12], re.I):
        return "flow print count, not a strike"
    if strike == int(strike) and 1990 <= int(strike) <= 2040:
        window = clean[max(0, start - 40):min(len(clean), end + 40)]
        if re.search(r"\b(revenue|estimates?|earnings|annual)\b", window, re.I):
            return f"{int(strike)} is not an option strike"
        if kind != "strike-type" and "$" not in clean[start:end]:
            return f"{int(strike)} looks like a year, not a strike"
    return None


def _note(reason: str, detail: str, raw: str, posted_at, handle, author_id, post_id) -> FlaggedNote:
    return FlaggedNote(reason, detail, handle, author_id, post_id, posted_at, raw)


def _infer_side(clean: str) -> str:
    """Default call when C/P omitted (challenge / adding lines). Put only if puts mentioned alone."""
    has_put = bool(re.search(r"\bputs?\b", clean, re.I))
    has_call = bool(re.search(r"\bcalls?\b", clean, re.I))
    if has_put and not has_call:
        return "P"
    return "C"


def analyze_post(text: str, posted_at: datetime, handle: str = "", author_id: str = "", post_id: str = ""):
    """Return (live calls, flagged notes). Flagged notes stay out of the live table."""
    if not text:
        return [], []
    raw = text.replace("&amp;", "&")
    if raw.startswith("RT @"):
        return [], []
    clean = RE_URL.sub(" ", raw)
    clean = re.sub(r"[·|]", " ", clean)
    if _is_prior_gain(clean):
        return [], []
    today = posted_at.date()
    hits = []
    for m in RE_STRIKE_TYPE.finditer(clean):
        hits.append((m.start(), m.end(), m.group(1), m.group(2), "strike-type"))
    for m in RE_TYPE_STRIKE.finditer(clean):
        hits.append((m.start(), m.end(), m.group(2), m.group(1), "type-strike"))
    for m in RE_WORD_STRIKE.finditer(clean):
        if not any(h[0] <= m.start() < h[1] or m.start() <= h[0] < m.end() for h in hits):
            hits.append((m.start(), m.end(), m.group(2), m.group(1), "word"))
    # "$TBBK / 20 NOV 26 $50 / $1.10" — date + strike, no C/P
    side = _infer_side(clean)
    for m in RE_DATED_BARE.finditer(clean):
        s, e = m.start(), m.end()
        if any(h[0] <= s < h[1] or s <= h[0] < e for h in hits):
            continue
        hits.append((s, e, m.group(4), side, "dated-bare"))
    if not hits:
        return [], []
    hits.sort()
    why_two = _conditional_why(clean, hits)
    if why_two:
        return [], [_note("conditional", why_two, raw, posted_at, handle, author_id, post_id)]
    why_potential = _potential_why(clean)
    if why_potential:
        return [], [_note("potential", why_potential, raw, posted_at, handle, author_id, post_id)]
    is_flow = bool(RE_FLOW.search(clean))
    out, seen, flagged = [], set(), []
    for i, (s, e, strike_s, typ, kind) in enumerate(hits):
        strike = float(strike_s)
        if strike <= 0.5:
            continue
        why = _reject_strike(clean, s, e, strike, kind)
        if why:
            flagged.append(_note("no-strike", f"{clean[s:e].strip()}: {why}", raw, posted_at, handle, author_id, post_id))
            continue
        ticker = _find_ticker(clean, s)
        if not ticker:
            flagged.append(_note("no-strike", f"{clean[s:e].strip()}: no ticker with this strike", raw, posted_at, handle, author_id, post_id))
            continue
        opt = "C" if typ[0].lower() == "c" else "P"
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(clean)
        seg = clean[e:nxt] if nxt - e > 3 else clean[e:]
        entry, eflags = _parse_entry(seg)
        if entry is None and i + 1 < len(hits):     # "700C 699C 1.00-1.30" shared entry
            entry, eflags = _parse_entry(clean[hits[-1][1]:])
        if entry is None:  # entry written before contract, e.g. "$INTC 0dte $68 C .84" handled; "@1.2 for 450c"
            pass
        prev_end = hits[i - 1][1] if i > 0 else 0
        exp, label = _parse_expiry(clean[s:nxt], ticker, today)          # text after the contract
        # "weekly" in later commentary must not hide an explicit date written before the strike
        if "?" in label or label.startswith("weekly"):
            exp, label = _parse_expiry(clean[prev_end:nxt], ticker, today)
        if "?" in label or label.startswith("weekly"):
            exp, label = _parse_expiry(clean, ticker, today)
        # dated-bare match already embeds "20 NOV 26 $50" — prefer that window
        if kind == "dated-bare":
            exp, label = _parse_expiry(clean[s:e], ticker, today)
        flags = list(eflags)
        if kind == "dated-bare" and "side-assumed" not in flags:
            flags.append("side-assumed")
        if "?" in label:
            flags.append("exp-assumed")
        if exp and exp < today:
            flags.append("expired")
        if entry is None:
            flags.append("no-entry")
        if is_flow:
            flags.append("flow")
        if re.search(r"\b(sell|selling|sold to open|short)\b", clean, re.I):
            flags.append("short?")
        c = OptionCall(ticker, opt, strike, exp, label, entry, flags, handle, author_id, post_id, posted_at, raw)
        if c.key in seen:
            continue
        seen.add(c.key)
        out.append(c)
    return out, flagged


def parse_post(text: str, posted_at: datetime, handle: str = "", author_id: str = "", post_id: str = "") -> List[OptionCall]:
    return analyze_post(text, posted_at, handle, author_id, post_id)[0]
