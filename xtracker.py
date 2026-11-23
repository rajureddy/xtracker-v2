#!/usr/bin/env python3
"""xtracker-v2 — options-signal tracker fed by Chrome extension scrape (or optional X API).

Research/education tool only. Not financial advice. It never places trades.

  python xtracker.py --plugin --now          # plugin-only (no X API token)
  python xtracker.py --plugin --port 8081
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from parser import FlaggedNote, OptionCall, analyze_post, _is_prior_gain
from research import MarketData, poster_cred, score_contract

ET = ZoneInfo("America/New_York")
API = "https://api.x.com/2"
HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"


# ----------------------------------------------------------------- plugin normalize
def _strip_at(handle: str) -> str:
    return (handle or "").lstrip("@").strip()


def _row_key(c: OptionCall) -> tuple:
    """One live table row per contract. Multiple posts (same or different people) stay on that row."""
    return c.key


def _status_id_from_url(url: str) -> str:
    m = re.search(r"/status/([^/?#]+)", url or "")
    return m.group(1) if m else ""


def normalize_plugin_record(rec: dict) -> dict | None:
    """Map a Chrome-extension scrape row into the X API post shape Tracker.ingest expects."""
    if not isinstance(rec, dict):
        return None
    text = (rec.get("text") or "").strip()
    if not text:
        return None
    # Prefer status id from url; fall back to extension id; else hash text+handle+time
    pid = str(rec.get("id") or "") or _status_id_from_url(rec.get("url") or "")
    handle = _strip_at(rec.get("handle") or "")
    created = rec.get("time") or rec.get("created_at") or rec.get("seen")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created / (1000 if created > 1e12 else 1), tz=timezone.utc).isoformat()
    if not created:
        created = datetime.now(timezone.utc).isoformat()
    if isinstance(created, str) and not created.endswith("Z") and "+" not in created[10:]:
        # bare ISO without tz → treat as UTC
        try:
            datetime.fromisoformat(created)
            created = created + ("Z" if "T" in created else "")
        except ValueError:
            created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not pid:
        pid = "p" + hashlib.sha1(f"{handle}|{created}|{text}".encode()).hexdigest()[:16]
    author_id = str(rec.get("author_id") or handle or pid)
    post = {
        "id": pid,
        "author_id": author_id,
        "created_at": created if isinstance(created, str) else datetime.now(timezone.utc).isoformat(),
        "text": text,
    }
    avatar = (rec.get("avatar") or rec.get("profile_image_url") or "").strip()
    user = {
        "id": author_id,
        "username": handle or author_id,
        "name": rec.get("name") or handle or "",
        "created_at": rec.get("user_created_at"),
        "public_metrics": rec.get("public_metrics") or {},
        "profile_image_url": avatar,
    }
    return {"post": post, "user": user}


def _num_or_none(raw) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _posted_et(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    except ValueError:
        return None


def _expiry_or_none(raw) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _expiry_display(c: OptionCall) -> str:
    """Show 0DTE/ODTE (and assumed same-day) as today's date so the desk reads a real expiry."""
    label = (c.expiry_label or "").strip()
    lab_u = label.upper().replace(" ", "")
    if c.expiry and (lab_u in ("0DTE", "ODTE", "0DTE?", "ODTE?") or lab_u.startswith("WKLY?")):
        day = c.expiry.strftime("%m/%d")
        if lab_u in ("0DTE", "ODTE"):
            return f"0DTE {day}"
        return f"0DTE? {day}"
    return label or (c.expiry.strftime("%m/%d") if c.expiry else "-")


def _call_from_csv(row: dict) -> OptionCall | None:
    """Rebuild one logged call. Same columns the original xtracker writes."""
    if not row:
        return None
    ticker = (row.get("ticker") or "").strip().upper()
    opt = (row.get("type") or "").strip().upper()[:1]
    strike = _num_or_none(row.get("strike"))
    posted = _posted_et(row.get("posted_et") or "")
    if not ticker or opt not in ("C", "P") or strike is None or posted is None:
        return None
    url = (row.get("url") or "").strip()
    handle = (row.get("handle") or "").strip().lstrip("@")
    post_id = _status_id_from_url(url)
    flags = [f for f in (row.get("flags") or "").split(";") if f]
    exp = _expiry_or_none(row.get("expiry"))
    label = (row.get("expiry_label") or "").strip()
    # ODTE / 0DTE / old wkly? with no real dated expiry → poster's session day (today when logged)
    lab_u = label.upper().replace(" ", "")
    if (not exp) or lab_u.startswith("WKLY?") or lab_u in ("0DTE?", "ODTE?", "0DTE", "ODTE") or "ODTE" in lab_u or "0DTE" in lab_u:
        if not exp:
            exp = posted.date()
        if lab_u.startswith("WKLY?") or not label:
            label = "0DTE?"
        elif lab_u in ("ODTE", "0DTE") or re.fullmatch(r"[0O]DTE", lab_u):
            label = "0DTE"
    return OptionCall(ticker, opt, strike, exp, label, _num_or_none(row.get("entry")),
                      flags, handle, handle, post_id, posted, row.get("text") or "")


def _flag_from_csv(row: dict) -> FlaggedNote | None:
    if not row:
        return None
    reason = (row.get("reason") or "").strip()
    posted = _posted_et(row.get("posted_et") or "")
    if not reason or posted is None:
        return None
    url = (row.get("url") or "").strip()
    handle = (row.get("handle") or "").strip().lstrip("@")
    return FlaggedNote(reason, row.get("detail") or "", handle, handle, _status_id_from_url(url),
                       posted, row.get("text") or "")


# ----------------------------------------------------------------- optional X client (legacy poll)
class XClient:
    def __init__(self, token_file: str | None = None):
        import requests
        self.token_file = Path(token_file or os.environ.get("X_TOKEN_FILE") or HERE / "x_token.json")
        self.tok = {}
        if os.environ.get("X_USER_ACCESS_TOKEN"):
            self.tok = {"access_token": os.environ["X_USER_ACCESS_TOKEN"]}
        elif self.token_file.exists():
            self.tok = json.loads(self.token_file.read_text())
        else:
            sys.exit("No X token. Use --plugin for extension-only mode, or run auth / set X_USER_ACCESS_TOKEN.")
        self.s = requests.Session()

    def _refresh(self) -> bool:
        import requests
        cid = os.environ.get("X_CLIENT_ID")
        if not (cid and self.tok.get("refresh_token")):
            return False
        data = {"grant_type": "refresh_token", "refresh_token": self.tok["refresh_token"], "client_id": cid}
        auth = (cid, os.environ["X_CLIENT_SECRET"]) if os.environ.get("X_CLIENT_SECRET") else None
        r = requests.post(f"{API}/oauth2/token", data=data, auth=auth, timeout=20)
        if r.status_code != 200:
            return False
        self.tok.update(r.json())
        self.token_file.write_text(json.dumps(self.tok))
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        return True

    def get(self, path, params=None):
        for attempt in range(3):
            r = self.s.get(f"{API}{path}", params=params, timeout=20,
                           headers={"Authorization": f"Bearer {self.tok['access_token']}"})
            if r.status_code == 401 and attempt == 0 and self._refresh():
                continue
            if r.status_code == 429:
                reset = int(r.headers.get("x-rate-limit-reset", time.time() + 60))
                time.sleep(max(5, min(reset - time.time(), 900)))
                continue
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def me(self):
        return self.get("/users/me", {"user.fields": "username"})["data"]

    def timeline(self, uid, since_id=None, start_time=None):
        params = {"max_results": 100, "tweet.fields": "created_at,author_id,public_metrics,note_tweet",
                  "expansions": "author_id", "user.fields": "created_at,public_metrics,verified,username"}
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time
        posts, users, newest = [], {}, None
        token = None
        for _ in range(10):
            if token:
                params["pagination_token"] = token
            js = self.get(f"/users/{uid}/timelines/reverse_chronological", params)
            posts += js.get("data", [])
            for u in js.get("includes", {}).get("users", []):
                users[u["id"]] = u
            meta = js.get("meta", {})
            newest = newest or meta.get("newest_id")
            token = meta.get("next_token")
            if not token:
                break
        return posts, users, newest


# ----------------------------------------------------------------- tracker
class Tracker:
    def __init__(self, md: MarketData, log_day=None, session_start: datetime | None = None, fresh=False,
                 quote_ttl: float | None = None):
        self.md = md
        self.quote_ttl = md.ttl if quote_ttl is None else quote_ttl
        self.session_start = session_start  # ET; posts before this are ignored
        self.groups: "OrderedDict[tuple, list[OptionCall]]" = OrderedDict()
        self._quotes: dict[tuple, dict] = {}
        self._peaks: dict[tuple, float] = {}   # contract key -> high-water option mark
        self._marks: dict[tuple, float] = {}   # contract key -> last seen mark (for arrows)
        self.quotes_fetched = 0
        self.quotes_cached = 0
        self.users: dict = {}
        self.rows = []
        self.flagged = []
        self._flagged_keys = set()
        self._seen_post_ids: set[str] = set()
        self.stats = {"posts": 0, "calls": 0, "ingested": 0, "duplicates": 0, "stale_skipped": 0}
        day = (log_day or datetime.now(ET).date()).isoformat()
        LOG_DIR.mkdir(exist_ok=True)
        self.calls_csv = LOG_DIR / f"calls_{day}.csv"
        self.scores_csv = LOG_DIR / f"scores_{day}.csv"
        self.flagged_csv = LOG_DIR / f"flagged_{day}.csv"
        self.load_note = ""
        self._lock = threading.Lock()
        if fresh:
            self.reset(clear_logs=True)
        else:
            self._load_logs()

    def reset(self, clear_logs=True):
        """Drop in-memory state (and optionally today's CSV) so the session starts clean."""
        with self._lock:
            self.groups.clear()
            self._quotes.clear()
            self._peaks.clear()
            self._marks.clear()
            self.users.clear()
            self.rows = []
            self.quotes_fetched = 0
            self.quotes_cached = 0
            self.flagged = []
            self._flagged_keys.clear()
            self._seen_post_ids.clear()
            self.stats = {"posts": 0, "calls": 0, "ingested": 0, "duplicates": 0, "stale_skipped": 0}
        if clear_logs:
            for f in (self.calls_csv, self.scores_csv, self.flagged_csv):
                if f.exists():
                    f.unlink()
            self.load_note = ""

    def _load_logs(self):
        """Rebuild today's table from the CSV logs so a restart is not a fresh sync."""
        try:
            n_calls = self._load_calls()
            n_flags = self._load_flagged()
            self._load_peaks()
        except OSError as e:
            self.load_note = f"could not read logs: {e}"
            return
        if n_calls or n_flags:
            self.load_note = f"loaded {n_calls} calls, {n_flags} flagged from {self.calls_csv.name}"

    def _remember_loaded_post(self, post_id: str):
        if not post_id or post_id in self._seen_post_ids:
            return
        self._seen_post_ids.add(post_id)
        self.stats["posts"] += 1

    def _load_calls(self) -> int:
        if not self.calls_csv.exists():
            return 0
        n = 0
        seen = set()
        with self.calls_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                c = _call_from_csv(row)
                if c is None:
                    continue
                if _is_prior_gain(c.text or ""):
                    continue  # day-performance / recap — omit from live
                sig = (c.post_id, c.key, c.handle, c.entry)
                if sig in seen:
                    continue
                seen.add(sig)
                self.groups.setdefault(_row_key(c), []).append(c)
                self.stats["calls"] += 1
                self._remember_loaded_post(c.post_id)
                n += 1
        return n

    def _load_flagged(self) -> int:
        if not self.flagged_csv.exists():
            return 0
        n = 0
        with self.flagged_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                note = _flag_from_csv(row)
                if note is None:
                    continue
                key = (note.post_id, note.reason)
                if key in self._flagged_keys:
                    continue
                self._flagged_keys.add(key)
                self.flagged.append(note)
                self._remember_loaded_post(note.post_id)
                n += 1
        return n

    def ingest(self, posts, users):
        with self._lock:
            self.users.update(users)
            for p in sorted(posts, key=lambda p: p.get("created_at") or ""):
                pid = str(p.get("id") or "")
                if pid and pid in self._seen_post_ids:
                    self.stats["duplicates"] += 1
                    continue
                u = users.get(p.get("author_id"), {})
                raw_t = p.get("created_at") or datetime.now(timezone.utc).isoformat()
                try:
                    dt = datetime.fromisoformat(str(raw_t).replace("Z", "+00:00")).astimezone(ET)
                except ValueError:
                    dt = datetime.now(ET)
                if self.session_start and dt < self.session_start:
                    self.stats["stale_skipped"] += 1
                    if pid:
                        self._seen_post_ids.add(pid)  # don't reprocess forever
                    continue
                if pid:
                    self._seen_post_ids.add(pid)
                self.stats["posts"] += 1
                handle = u.get("username", p.get("author_id", ""))
                calls, notes = analyze_post(_post_text(p), dt, handle, p.get("author_id", ""), p.get("id", ""))
                for note in notes:
                    self._add_flag(note)
                for c in calls:
                    self.groups.setdefault(_row_key(c), []).append(c)
                    self.stats["calls"] += 1
                    self._log_call(c)

    def ingest_plugin_batch(self, records: list) -> dict:
        posts, users = [], {}
        rejected = 0
        for rec in records:
            norm = normalize_plugin_record(rec)
            if not norm:
                rejected += 1
                continue
            posts.append(norm["post"])
            users[norm["user"]["id"]] = norm["user"]
        before_dup = self.stats["duplicates"]
        before_posts = self.stats["posts"]
        before_stale = self.stats["stale_skipped"]
        self.ingest(posts, users)
        accepted = self.stats["posts"] - before_posts
        dups = self.stats["duplicates"] - before_dup
        stale = self.stats["stale_skipped"] - before_stale
        self.stats["ingested"] += accepted
        return {"accepted": accepted, "duplicates": dups, "rejected": rejected, "stale_skipped": stale}

    def _add_flag(self, note):
        key = (note.post_id, note.reason)
        if key in self._flagged_keys:
            return
        self._flagged_keys.add(key)
        self.flagged.append(note)
        self._log_flag(note)

    def _append_csv(self, path: Path, header: list, row: list):
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)

    def _log_call(self, c: OptionCall):
        self._append_csv(self.calls_csv,
                         ["posted_et", "handle", "ticker", "type", "strike", "expiry", "expiry_label", "entry",
                          "flags", "url", "text"],
                         [c.posted_at.strftime("%Y-%m-%d %H:%M:%S"), c.handle, c.ticker, c.opt_type, c.strike,
                          c.expiry or "", c.expiry_label, "" if c.entry is None else c.entry, ";".join(c.flags),
                          c.url, c.text.replace("\n", " ")])

    def _log_flag(self, note):
        posted = note.posted_at.strftime("%Y-%m-%d %H:%M:%S") if note.posted_at else ""
        self._append_csv(self.flagged_csv,
                         ["posted_et", "handle", "reason", "detail", "url", "text"],
                         [posted, note.handle, note.reason, (note.detail or "").replace("\n", " "),
                          note.url, (note.text or "").replace("\n", " ")])

    def _contract_inputs(self, calls, asof: datetime):
        # Earliest post owns Entry / score baseline; later posts still show in Poster(s).
        first = min(calls, key=lambda c: c.posted_at or datetime.max.replace(tzinfo=ET))
        handles = list(OrderedDict.fromkeys(c.handle for c in calls))
        entries = [c.entry for c in calls if c.entry is not None]
        entry = first.entry if first.entry is not None else (entries[0] if entries else None)
        creds = [x for x in (poster_cred(self.users.get(c.author_id), asof) for c in calls) if x is not None]
        cred = max(creds) if creds else None
        base_flags = tuple(sorted({f for c in calls for f in c.flags} - ({"no-entry"} if entry else set())))
        sig = (tuple(handles), entry, base_flags, cred, first.expiry == asof.date())
        return first, handles, entry, cred, base_flags, sig

    @staticmethod
    def _opt_mark(q) -> float | None:
        if q is None:
            return None
        if q.mid is not None:
            return float(q.mid)
        if q.last is not None:
            return float(q.last)
        return None

    def _note_mark(self, key: tuple, q, entry: float | None) -> tuple[float | None, float | None, str | None]:
        """Track high-water peak. Green up at/near peak; red down once the mark falls off the high."""
        mark = self._opt_mark(q)
        if mark is None:
            return None, self._peaks.get(key), None
        peak = self._peaks.get(key)
        if peak is None or mark > peak:
            peak = mark
            self._peaks[key] = peak
        self._marks[key] = mark
        # At the captured peak → ▲; anything under the peak → ▼
        move = "up" if mark >= peak - 1e-9 else "down"
        return mark, peak, move

    def _score_row(self, calls, asof: datetime, u, q, with_quotes: bool):
        first, handles, entry, cred, base_flags, sig = self._contract_inputs(calls, asof)
        posters = len(handles)
        sc = score_contract(first.opt_type, first.strike, entry, u, q, posters, cred, first.expiry == asof.date())
        if with_quotes and self.md.source == "robinhood" and q is None and first.expiry and first.expiry >= asof.date():
            note = self.md.option_error(first.ticker, first.expiry, first.opt_type, first.strike)
            if note:
                sc.reasons.append(note)
        flags = list(base_flags)
        if u and abs(first.strike - u.price) / u.price > 0.5:
            flags.append("strike-far")
            sc.total = min(sc.total, 10)
        if "expired" in flags:
            sc.total = 0
        mark = peak = None
        move = None
        if with_quotes:
            mark, peak, move = self._note_mark(first.key, q, entry)
        elif first.key in self._peaks:
            peak = self._peaks[first.key]
            mark = self._marks.get(first.key)
        row = dict(first=first, calls=calls, handles=handles, entry=entry, u=u, q=q, score=sc,
                   flags=flags, posters=posters, mark=mark, peak=peak, move=move)
        return row, sig

    def _fetch_quotes(self, first, asof: datetime, und_now: dict):
        ticker = first.ticker
        if ticker not in und_now:
            und_now[ticker] = self.md.underlying(ticker, asof)
        q = None if (first.expiry and first.expiry < asof.date()) else \
            self.md.option(first.ticker, first.expiry, first.opt_type, first.strike)
        return und_now[ticker], q

    def _load_peaks(self):
        """Restore today's peak marks from scores CSV so a restart keeps the high-water print."""
        if not self.scores_csv.exists():
            return
        with self.scores_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                raw = (row.get("opt_peak") or "").strip()
                if not raw:
                    continue
                try:
                    peak = float(raw)
                except ValueError:
                    continue
                ticker = (row.get("ticker") or "").upper()
                typ = (row.get("type") or "").upper()[:1]
                try:
                    strike = float(row.get("strike") or "")
                except ValueError:
                    continue
                exp_label = row.get("expiry") or ""
                # Match live group keys: prefer ISO expiry from calls group if present.
                key = None
                for k in self.groups:
                    if k[0] == ticker and k[1] == strike and k[2] == typ:
                        key = k
                        break
                if key is None:
                    key = (ticker, strike, typ, exp_label)
                prev = self._peaks.get(key)
                if prev is None or peak > prev:
                    self._peaks[key] = peak
                mark_raw = (row.get("opt_mid") or row.get("opt_last") or "").strip()
                if mark_raw:
                    try:
                        self._marks[key] = float(mark_raw)
                    except ValueError:
                        pass

    def _set_rows(self, by_key: dict, items):
        rows = [by_key[k] for k, _ in items if k in by_key]
        rows.sort(key=lambda r: (-r["score"].total, r["first"].posted_at or datetime.min.replace(tzinfo=ET)))
        self.rows = rows

    def research(self, asof: datetime, with_quotes: bool = True, on_progress=None):
        """Score one row per contract. A quote younger than quote_ttl is reused."""
        self.quotes_fetched = 0
        self.quotes_cached = 0
        with self._lock:
            items = list(self.groups.items())
        by_key: dict = {}
        fetch_keys = []
        now_ts = time.time()
        for key, calls in items:
            _first, _handles, _entry, _cred, _flags, sig = self._contract_inputs(calls, asof)
            hit = self._quotes.get(key)
            fresh = bool(with_quotes and hit and now_ts - hit["at"] < self.quote_ttl)
            if fresh:
                self.quotes_cached += 1
                if hit.get("sig") == sig and hit.get("row"):
                    row = dict(hit["row"], calls=calls)
                    row["peak"] = self._peaks.get(key, row.get("peak"))
                    row["mark"] = self._marks.get(key, row.get("mark"))
                    by_key[key] = row
                    continue
                row, sig = self._score_row(calls, asof, hit["u"], hit["q"], True)
                self._quotes[key] = dict(hit, sig=sig, row=row)
                by_key[key] = row
                continue
            if not with_quotes:
                row, _sig = self._score_row(calls, asof, None, None, False)
                by_key[key] = row
                continue
            fetch_keys.append(key)
            if hit and hit.get("row"):
                row = dict(hit["row"], calls=calls)
                row["peak"] = self._peaks.get(key, row.get("peak"))
                row["mark"] = self._marks.get(key, row.get("mark"))
                by_key[key] = row
        calls_by = dict(items)
        for key in fetch_keys:
            if key not in by_key:
                row, _sig = self._score_row(calls_by[key], asof, None, None, False)
                by_key[key] = row
        self._set_rows(by_key, items)
        if on_progress and fetch_keys:
            on_progress()
        und_now: dict = {}
        for key in fetch_keys:
            calls = calls_by[key]
            u, q = self._fetch_quotes(calls[0], asof, und_now)
            row, sig = self._score_row(calls, asof, u, q, True)
            self._quotes[key] = {"at": time.time(), "u": u, "q": q, "sig": sig, "row": row}
            by_key[key] = row
            self.quotes_fetched += 1
            self._set_rows(by_key, items)
            if on_progress:
                on_progress()
        live = {k for k, _ in items}
        for key in list(self._quotes):
            if key not in live:
                del self._quotes[key]
        if with_quotes:
            self._write_scores(asof)
        return self.rows

    def _write_scores(self, asof):
        with self.scores_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["asof_et", "ticker", "type", "strike", "expiry", "entry", "opt_last", "opt_mid", "opt_peak",
                        "underlying", "dist_pct", "posters", "confidence", "reasons", "flags", "handles", "first_url"])
            for r in self.rows:
                c, u, q = r["first"], r["u"], r["q"]
                w.writerow([asof.strftime("%Y-%m-%d %H:%M"), c.ticker, c.opt_type, c.strike, c.expiry_label, r["entry"],
                            q.last if q else "", q.mid if q else "",
                            "" if r.get("peak") is None else r["peak"],
                            f"{u.price:.2f}" if u else "",
                            f"{(c.strike-u.price)/u.price*100:.2f}" if u else "", r.get("posters", len(r["handles"])), r["score"].total,
                            "; ".join(r["score"].reasons), ";".join(r["flags"]), " ".join(r["handles"]), c.url])


# ----------------------------------------------------------------- rendering
def fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


GREY_TAGS = {"expired", "flow", "no-entry", "exp-assumed", "side-assumed", "cents?", "short?", "strike-far"}
FLAG_LABEL = {"prior-gain": "previous gain", "no-strike": "no real strike",
              "conditional": "depends on price", "potential": "potential setup"}


def _post_text(p: dict) -> str:
    note = p.get("note_tweet") or {}
    if isinstance(note, dict) and note.get("text"):
        return note["text"]
    return p.get("text") or ""


def conf_style(v):
    return "bold bright_green" if v >= 75 else "yellow" if v >= 50 else "dim red"


# TraderGrader list lives in tradergrader.json so a new chart can be added
# without editing code. Reloaded when the file changes; a bad edit keeps the last good list.
TG_FILE = HERE / "tradergrader.json"
_TG: dict[str, tuple[int, str]] = {}
_TG_MTIME: float | None = None
RICH_BAND = {"green": "bold bright_green", "orange": "bold orange1", "yellow": "bold yellow"}


def _reload_grades() -> None:
    global _TG, _TG_MTIME
    try:
        mtime = TG_FILE.stat().st_mtime
    except OSError:
        return
    if _TG_MTIME == mtime:
        return
    try:
        data = json.loads(TG_FILE.read_text(encoding="utf-8"))
        bands = data.get("bands") if isinstance(data, dict) else None
        if not isinstance(bands, dict):
            raise ValueError("bands must be an object")
        table: dict[str, tuple[int, str]] = {}
        rank = 1
        for band in ("green", "orange", "yellow"):
            names = bands.get(band) or []
            if not isinstance(names, list):
                continue
            for name in names:
                key = str(name).lstrip("@").strip().lower()
                if not key or key in table:
                    continue
                table[key] = (rank, band)
                rank += 1
        aliases = data.get("aliases") if isinstance(data, dict) else None
        if isinstance(aliases, dict):
            for alias, target in aliases.items():
                a = str(alias).lstrip("@").strip().lower()
                t = str(target).lstrip("@").strip().lower()
                if a and t in table and a not in table:
                    table[a] = table[t]
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as e:
        print(f"tradergrader.json not loaded ({e}); keeping the previous list", file=sys.stderr)
        _TG_MTIME = mtime
        return
    _TG = table
    _TG_MTIME = mtime


def trader_grade(handle: str) -> tuple[int, str] | None:
    """Rank and color band for a TraderGrader handle, or None if they are not on the list."""
    _reload_grades()
    return _TG.get((handle or "").lstrip("@").lower())


def _grade_marks(handles: list[str]) -> tuple[int | None, str | None, list[dict]]:
    marks = []
    best: tuple[int, str] | None = None
    for h in handles:
        g = trader_grade(h)
        marks.append(dict(handle=h, rank=g[0] if g else None, band=g[1] if g else None))
        if g and (best is None or g[0] < best[0]):
            best = g
    return (best[0], best[1], marks) if best else (None, None, marks)


def _avatar_by_handle(users: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for u in (users or {}).values():
        if not isinstance(u, dict):
            continue
        h = _strip_at(u.get("username") or "").lower()
        av = (u.get("profile_image_url") or u.get("avatar") or "").strip()
        if h and av:
            out[h] = av
    return out


def row_view(r, avatars: dict[str, str] | None = None):
    c, u, q, sc = r["first"], r["u"], r["q"], r["score"]
    grade, band, marks = _grade_marks(r["handles"])
    latest_by: dict[str, OptionCall] = {}
    entry_by: dict[str, OptionCall] = {}
    for call in r["calls"]:
        prev = latest_by.get(call.handle)
        if prev is None or (call.posted_at and prev.posted_at and call.posted_at >= prev.posted_at):
            latest_by[call.handle] = call
        if call.entry is not None:
            prev_e = entry_by.get(call.handle)
            if prev_e is None or (call.posted_at and prev_e.posted_at and call.posted_at >= prev_e.posted_at):
                entry_by[call.handle] = call
    avatars = avatars or {}
    for m in marks:
        own = latest_by.get(m["handle"])
        with_entry = entry_by.get(m["handle"])
        m["entry"] = with_entry.entry if with_entry else (own.entry if own else None)
        m["url"] = own.url if own else ""
        m["avatar"] = avatars.get((m["handle"] or "").lstrip("@").lower(), "")
    latest = max(r["calls"], key=lambda x: x.posted_at or c.posted_at or datetime.min.replace(tzinfo=ET))
    peak = r.get("peak")
    move = r.get("move")
    peak_pct = None
    if peak is not None and r["entry"]:
        peak_pct = round((peak - r["entry"]) / r["entry"] * 100, 1)
    return dict(time=latest.posted_at.strftime("%H:%M") if latest.posted_at else "",
                ticker=c.ticker, type=c.opt_type, strike=c.strike,
                expiry=_expiry_display(c), entry=r["entry"], opt="-" if not q else f"{fmt(q.last)}/{fmt(q.mid)}",
                peak=None if peak is None else round(peak, 2),
                peak_pct=peak_pct, move=move,
                underlying=round(u.price, 2) if u else None,
                dist=round((c.strike - u.price) / u.price * 100, 2) if u else None,
                posters=r.get("posters", len(r["handles"])), conf=sc.total, handles=r["handles"], marks=marks,
                grade=grade, band=band, why=", ".join(sc.reasons),
                flags=r["flags"], url=latest.url or c.url)


def flag_view(n, avatars: dict[str, str] | None = None):
    g = trader_grade(n.handle)
    av = (avatars or {}).get((n.handle or "").lstrip("@").lower(), "")
    return dict(time=n.posted_at.strftime("%H:%M") if n.posted_at else "", reason=n.reason,
                label=FLAG_LABEL.get(n.reason, n.reason), detail=n.detail, handle=n.handle,
                grade=g[0] if g else None, band=g[1] if g else None, avatar=av,
                text=n.text.replace("\n", " "), url=n.url)


def web_payload(tr, asof, status=""):
    avatars = _avatar_by_handle(getattr(tr, "users", {}) or {})
    rows = [row_view(r, avatars) for r in tr.rows]
    # Group-friendly order: ticker A–Z, then best conf within ticker.
    rows.sort(key=lambda r: (r["ticker"] or "", -(r["conf"] or 0)))
    flagged = [flag_view(n, avatars) for n in tr.flagged]
    summ = dict(posts=tr.stats["posts"], calls=tr.stats["calls"], contracts=len(rows),
                call_contracts=sum(r["type"] == "C" for r in rows), put_contracts=sum(r["type"] == "P" for r in rows),
                high=sum(r["conf"] >= 75 for r in rows), medium=sum(50 <= r["conf"] < 75 for r in rows),
                low=sum(r["conf"] < 50 for r in rows), multi=sum(r["posters"] >= 2 for r in rows),
                flagged=len(flagged))
    return dict(asof=f"{asof:%a %b %d %H:%M}", status=status, summary=summ, rows=rows, flagged=flagged)


def build_table(tr: Tracker, asof: datetime, title_extra="", limit=40):
    n_c = sum(r["first"].opt_type == "C" for r in tr.rows)
    t = Table(title=f"X OPTIONS SIGNALS v2  |  as of {asof:%a %b %d %H:%M} ET  {title_extra}",
              caption=f"{tr.stats['posts']} posts read, {tr.stats['calls']} option calls, {len(tr.rows)} contracts "
                      f"([green]{n_c} calls[/green] / [red]{len(tr.rows) - n_c} puts[/red])  |  "
                      + ("Robinhood live quotes  |  " if getattr(tr.md, "source", "") == "robinhood"
                         else "option quotes via yfinance (delayed)  |  ")
                      + "NOT financial advice",
              header_style="bold cyan", expand=False, show_lines=False)
    for col, j in [("Time ET", "left"), ("Ticker", "left"), ("C/P", "center"), ("Strike", "right"), ("Expiry", "left"),
                   ("Entry", "right"), ("Opt last/mid", "right"), ("Peak", "right"), ("Underlying", "right"), ("Dist", "right"),
                   ("#Post", "right"), ("Conf", "right"), ("TG", "right"), ("Poster(s)", "left"), ("Why / flags", "left")]:
        t.add_column(col, justify=j, no_wrap=col not in ("Why / flags",))
    for r in tr.rows[:limit]:
        v = row_view(r)
        is_call = v["type"] == "C"
        base = "green" if is_call else "red"
        cp = Text("CALL" if is_call else "PUT", style=f"bold white on {'dark_green' if is_call else 'dark_red'}")
        why = Text(v["why"])
        if v["flags"]:
            why.append(" [", style="grey50")
            for i, fl in enumerate(v["flags"]):
                why.append(("," if i else "") + fl, style="grey50" if fl in GREY_TAGS else "magenta")
            why.append("]", style="grey50")
        hs = Text()
        shown = v["marks"][:2]
        for i, p in enumerate(shown):
            if i:
                hs.append(", ", style="grey50")
            hs.append("@" + p["handle"], style=RICH_BAND.get(p["band"] or "") or None)
            if p.get("entry") is not None:
                hs.append(f" {p['entry']:g}", style="grey50")
        if len(v["marks"]) > 2:
            hs.append(f" +{len(v['marks']) - 2}", style="grey50")
        grade = Text(f"#{v['grade']}", style=RICH_BAND[v["band"]]) if v["grade"] else Text("-", style="grey50")
        opt = Text(v["opt"])
        if v.get("move") == "up":
            opt.append(" ▲", style="bold bright_green")
        elif v.get("move") == "down":
            opt.append(" ▼", style="bold bright_red")
        peak_txt = fmt(v.get("peak"))
        if v.get("peak_pct") is not None:
            peak_txt = f"{peak_txt} ({v['peak_pct']:+.0f}%)"
        t.add_row(v["time"], Text(v["ticker"], style=f"bold {base}"), cp, f"{v['strike']:g}", v["expiry"],
                  fmt(v["entry"]), opt, peak_txt, fmt(v["underlying"]),
                  "-" if v["dist"] is None else f"{v['dist']:+.2f}%",
                  str(v["posters"]), Text(str(v["conf"]), style=conf_style(v["conf"])), grade, hs, why,
                  style=f"{base}{' bold' if v['posters'] >= 2 else ''}")
    if not tr.flagged:
        return t
    f = Table(title="FLAGGED — not live calls", header_style="bold grey50", expand=False,
              caption="No real strike, two outcomes, or a trade idea that was never entered. Recaps are left out. Not scored.")
    for col, j in [("Time ET", "left"), ("Why", "left"), ("Poster", "left"), ("Note", "left")]:
        f.add_column(col, justify=j, no_wrap=col != "Note")
    for n in tr.flagged[:20]:
        v = flag_view(n)
        who = Text("@" + v["handle"], style=RICH_BAND.get(v["band"] or "", "grey50"))
        f.add_row(v["time"], v["detail"] or v["label"], who, v["text"][:140], style="grey50")
    return Group(t, f)


# ----------------------------------------------------------------- modes
def run_plugin(interval=30, start="09:00", end="16:00", ignore_hours=False, web=True, port=8081,
               secret="", md_source="robinhood", fresh=False):
    import webview

    dirty = threading.Event()
    s_h, e_h = dtime.fromisoformat(start), dtime.fromisoformat(end)
    now0 = datetime.now(ET)
    # Session starts at today's --start (ET). Older Chrome backlog / smoke / prior-day
    # posts are skipped so the table only reflects the current market day.
    session_start = now0.replace(hour=s_h.hour, minute=s_h.minute, second=0, microsecond=0)
    tr = Tracker(MarketData(ttl=max(interval - 5, 10), source=md_source),
                 session_start=session_start, fresh=fresh, quote_ttl=interval)
    pending: queue.Queue = queue.Queue()

    def on_ingest(records: list) -> dict:
        result = tr.ingest_plugin_batch(records)
        if result.get("accepted"):
            dirty.set()
            pending.put(True)
        return result

    def on_clear() -> dict:
        tr.reset(clear_logs=True)
        dirty.set()
        pub(web_payload(tr, datetime.now(ET), "cleared — waiting for fresh ingest"))
        return {"ok": True, "session_start": session_start.isoformat()}

    pub = lambda *a: None
    if web:
        webview.set_ingest_handler(on_ingest, secret=secret, clear_cb=on_clear)
        webview.serve(port, interval)
        pub = webview.publish

    def rescore(now: datetime) -> str | None:
        try:
            def progress():
                pub(web_payload(tr, now, f"quoting {tr.quotes_fetched} fetched, {tr.quotes_cached} cached"))
            tr.research(now, on_progress=progress)
            return None
        except Exception as e:
            return f"research error at {now:%H:%M:%S}: {e}"

    now = datetime.now(ET)
    in_hours = ignore_hours or (now.weekday() < 5 and s_h <= now.time() <= e_h)
    session_over = (not ignore_hours) and (not in_hours) and now.time() > e_h
    # Show the restored CSV immediately, then fill quotes. A refresh during quoting
    # still has the contracts instead of an empty "fresh sync" table.
    if tr.groups or tr.flagged:
        tr.research(now, with_quotes=False)
        pub(web_payload(tr, now, f"restored · {tr.load_note or 'CSV'} · loading quotes"))
    err = rescore(now) if (tr.groups or tr.flagged) else None
    held_close = bool(session_over and not err)
    if err:
        status = err
    elif session_over:
        status = (f"market window over — kept {len(tr.rows)} contracts / {len(tr.flagged)} flagged "
                  f"from today's CSV · logs in {LOG_DIR}")
    else:
        status = (f"plugin-only · {tr.load_note or 'no CSV yet'} · "
                  f"session≥{session_start:%Y-%m-%d %H:%M} ET · "
                  f"ingest http://127.0.0.1:{port}/api/ingest")
    pub(web_payload(tr, now, status))

    con = Console()
    with Live(Text("waiting for extension posts..."), console=con, refresh_per_second=1, screen=False) as live:
        while True:
            now = datetime.now(ET)
            in_hours = ignore_hours or (now.weekday() < 5 and s_h <= now.time() <= e_h)
            session_over = (not ignore_hours) and (not in_hours) and now.time() > e_h
            if in_hours or dirty.is_set() or (session_over and not held_close):
                while not pending.empty():
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break
                # Wake early on the next ingest; a post that arrives during research sets this again.
                dirty.clear()
                err = rescore(now)
                if err:
                    status = err
                elif session_over:
                    held_close = True
                    status = (f"market window over — kept {len(tr.rows)} contracts / {len(tr.flagged)} flagged "
                              f"from today's CSV · logs in {LOG_DIR}")
                else:
                    status = (f"plugin  session≥{session_start:%H:%M}  "
                              f"last refresh {now:%H:%M:%S} ET  "
                              f"{tr.load_note + '  ' if tr.load_note else ''}"
                              f"ingested={tr.stats['ingested']} posts={tr.stats['posts']} "
                              f"calls={tr.stats['calls']} dups={tr.stats['duplicates']} "
                              f"stale={tr.stats['stale_skipped']}  "
                              f"quotes {tr.quotes_fetched} fetched, {tr.quotes_cached} cached  "
                              f"next in {interval}s")
            elif session_over:
                status = (f"market window over — kept {len(tr.rows)} contracts / {len(tr.flagged)} flagged "
                          f"from today's CSV · logs in {LOG_DIR}")
            else:
                status = f"waiting for {start} ET (now {now:%a %H:%M}) — ingest still accepting"
            if web:
                status += f"  |  browser: http://127.0.0.1:{port}"
            live.update(Group(build_table(tr, now, "[PLUGIN]"), Text(status, style="dim")))
            pub(web_payload(tr, now, status))
            dirty.wait(timeout=interval)


def run_live_x(token_file=None, interval=30, start="09:00", end="16:00", ignore_hours=False,
               web=True, port=8081, also_plugin=False, secret=""):
    """Optional legacy X API poll; can also accept plugin ingest on the same port."""
    import webview

    x = XClient(token_file)
    tr = Tracker(MarketData(ttl=max(interval - 5, 10), source="robinhood"), quote_ttl=interval)
    dirty = threading.Event()

    def on_ingest(records: list) -> dict:
        result = tr.ingest_plugin_batch(records)
        if result.get("accepted"):
            dirty.set()
        return result

    pub = lambda *a: None
    if web:
        if also_plugin:
            webview.set_ingest_handler(on_ingest, secret=secret)
        webview.serve(port, interval)
        pub = webview.publish
    me = x.me()
    s_h, e_h = dtime.fromisoformat(start), dtime.fromisoformat(end)
    since_id = None
    con = Console()
    status = "starting"
    held_close = False
    close_note = "market window over — table kept; logs in " + str(LOG_DIR)
    with Live(Text("loading..."), console=con, refresh_per_second=1, screen=False) as live:
        while True:
            now = datetime.now(ET)
            in_hours = ignore_hours or (now.weekday() < 5 and s_h <= now.time() <= e_h)
            session_over = (not ignore_hours) and (not in_hours) and now.time() > e_h
            if session_over:
                if not held_close:
                    try:
                        if tr.groups or tr.flagged:
                            tr.research(now, with_quotes=False)
                            pub(web_payload(tr, now, f"restored · {tr.load_note or 'CSV'} · loading quotes"))
                            tr.research(now)
                        close_note = "market window over — table kept; logs in " + str(LOG_DIR)
                    except Exception as e:
                        close_note = f"research error at {now:%H:%M:%S}: {e}"
                    held_close = True
                status = close_note
                if web:
                    status += f"  |  browser: http://127.0.0.1:{port}"
                live.update(Group(build_table(tr, now, ""), Text(status, style="dim")))
                pub(web_payload(tr, now, status))
                time.sleep(interval)
                continue
            if in_hours:
                try:
                    st = None
                    if since_id is None:
                        st = now.replace(hour=s_h.hour, minute=s_h.minute, second=0, microsecond=0) \
                            .astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
                    posts, users, newest = x.timeline(me["id"], since_id=since_id, start_time=st)
                    since_id = newest or since_id
                    tr.ingest(posts, users)
                    tr.research(now, on_progress=lambda: pub(web_payload(
                        tr, now, f"quoting {tr.quotes_fetched} fetched, {tr.quotes_cached} cached")))
                    status = f"@{me['username']}  last poll {now:%H:%M:%S} ET: +{len(posts)} posts  next in {interval}s"
                except Exception as e:
                    status = f"poll error at {now:%H:%M:%S}: {e}"
            else:
                status = f"waiting for {start} ET (now {now:%a %H:%M})"
            if web:
                status += f"  |  browser: http://127.0.0.1:{port}"
            live.update(Group(build_table(tr, now, ""), Text(status, style="dim")))
            pub(web_payload(tr, now, status))
            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="xtracker-v2: Chrome-plugin (or optional X API) options tracker.")
    ap.add_argument("--plugin", "--no-x-api", dest="plugin", action="store_true",
                    help="plugin-only mode: no X API token; accept posts via /api/ingest")
    ap.add_argument("--with-x-api", action="store_true",
                    help="also poll X API (requires token); can combine with plugin ingest on the same port")
    ap.add_argument("--interval", type=int, default=30, help="table refresh seconds (default 30)")
    ap.add_argument("--web", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--port", type=int, default=8081, help="local UI + ingest port (default 8081; old app uses 8080)")
    ap.add_argument("--start", default="09:00")
    ap.add_argument("--end", default="16:00")
    ap.add_argument("--now", action="store_true", help="ignore the 9-4 ET window (testing)")
    ap.add_argument("--token-file", help="X token JSON (only with --with-x-api)")
    ap.add_argument("--ingest-secret", default=os.environ.get("XTRACKER_INGEST_SECRET", ""),
                    help="optional shared secret; extension sends X-Ingest-Secret header")
    ap.add_argument("--quotes", choices=("robinhood", "yahoo"), default="robinhood")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe today's CSV and start empty (default reloads today's calls and flagged notes)")
    a = ap.parse_args()

    if a.plugin or not a.with_x_api:
        # Default for v2 is plugin-only when --with-x-api is not set
        if a.with_x_api:
            run_live_x(a.token_file, a.interval, a.start, a.end, a.now, web=a.web, port=a.port,
                       also_plugin=True, secret=a.ingest_secret)
        else:
            run_plugin(a.interval, a.start, a.end, a.now, web=a.web, port=a.port,
                       secret=a.ingest_secret, md_source=a.quotes, fresh=a.fresh)
    else:
        run_live_x(a.token_file, a.interval, a.start, a.end, a.now, web=a.web, port=a.port,
                   also_plugin=False, secret=a.ingest_secret)


if __name__ == "__main__":
    main()
