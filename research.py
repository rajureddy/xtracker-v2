"""Market-data research + 0-100 confidence score for a grouped contract.

Data: yfinance (delayed ~15 min for options; 1-minute bars for the underlying).
Weights (sum 100) -- see README "Confidence score":
  moneyness 20 | trend vs VWAP/open 20 | strike reach vs day high/low 10 | relative volume 10
  option quality (spread/liquidity/price vs entry) 20 | consensus (# posters) 10 | poster credibility 10
Components with no data (e.g. expired chain) are dropped and the score is rescaled.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yfinance as yf

YF_SYMBOL = {"SPX": "^SPX", "NDX": "^NDX", "VIX": "^VIX", "XSP": "^XSP", "RUT": "^RUT"}
CHAIN_SYMBOL = {"SPX": "^SPX", "NDX": "^NDX", "VIX": "^VIX", "RUT": "^RUT"}
# Robinhood has no equity instrument for these indexes (404 missing_instruments).
# Weeklies and 0DTE SPX/NDX/RUT trade on the *W / *P chain; monthlies stay on the index symbol.
RH_OPTION_CHAINS = {
    "SPX": ("SPXW", "SPX"),
    "NDX": ("NDXP", "NDX"),
    "RUT": ("RUTW", "RUT"),
}


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


@dataclass
class Underlying:
    price: float
    open: float
    high: float
    low: float
    vwap: float
    rvol: Optional[float]


@dataclass
class OptQuote:
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    volume: Optional[float]
    oi: Optional[float]
    iv: Optional[float]

    @property
    def mid(self):
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2
        return None


class MarketData:
    """Caches quotes. source='robinhood' uses the Trading MCP; 'yahoo' uses yfinance."""

    def __init__(self, ttl=55, source="yahoo"):
        self.ttl = ttl
        self.source = source
        self.note = ""
        self._bars: Dict[str, tuple] = {}
        self._avgvol: Dict[str, float] = {}
        self._chains: Dict[tuple, tuple] = {}
        self._rh = None
        self._rh_eq: Dict[str, tuple] = {}
        self._rh_opt: Dict[tuple, tuple] = {}
        self._opt_err: Dict[tuple, str] = {}

    def _bars_for(self, ticker: str, day):
        sym = YF_SYMBOL.get(ticker, ticker)
        k = (sym, day)
        hit = self._bars.get(k)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        try:
            df = yf.Ticker(sym).history(start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(),
                                        interval="1m", prepost=False)
        except Exception:
            df = None
        self._bars[k] = (time.time(), df)
        return df

    def _avg_volume(self, ticker: str):
        sym = YF_SYMBOL.get(ticker, ticker)
        if sym in self._avgvol:
            return self._avgvol[sym]
        v = None
        try:
            d = yf.Ticker(sym).history(period="1mo", interval="1d")
            if d is not None and len(d) > 5:
                v = float(d["Volume"].iloc[:-1].tail(20).mean()) or None
        except Exception:
            pass
        self._avgvol[sym] = v
        return v

    def _robinhood(self):
        if self._rh is None:
            from robinhood_mcp import Client
            self._rh = Client()
        return self._rh

    def _rh_underlying(self, ticker: str) -> Optional[Underlying]:
        hit = self._rh_eq.get(ticker)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        try:
            row = self._robinhood().equity(ticker)
        except Exception as e:
            self.note = str(e)
            row = None
        u = None
        if row and row.get("price"):
            # Equity quote is last/bid/ask. There is no session open, high, low, or VWAP,
            # so those fields stay equal to the last price and the score skips trend.
            px = row["price"]
            u = Underlying(px, px, px, px, px, None)
        self._rh_eq[ticker] = (time.time(), u)
        return u

    def _rh_option(self, ticker: str, expiry, opt_type: str, strike: float) -> Optional[OptQuote]:
        key = (ticker, expiry.isoformat(), opt_type, strike)
        hit = self._rh_opt.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        row = None
        err = None
        for sym in RH_OPTION_CHAINS.get(ticker, (ticker,)):
            try:
                row = self._robinhood().option(sym, expiry.isoformat(), opt_type, strike)
            except Exception as e:
                err = str(e)
                row = None
            if row:
                err = None
                break
        if row is None and err is None:
            err = f"no Robinhood contract {ticker} {expiry.isoformat()} {strike:g}{opt_type}"
        q = OptQuote(row["last"], row["bid"], row["ask"], row["volume"], row["oi"], row["iv"]) if row else None
        if q is None and err:
            self._opt_err[key] = err
        else:
            self._opt_err.pop(key, None)
        self._rh_opt[key] = (time.time(), q)
        return q

    def option_error(self, ticker: str, expiry, opt_type: str, strike: float) -> str:
        if expiry is None:
            return ""
        return self._opt_err.get((ticker, expiry.isoformat(), opt_type, strike), "")

    def underlying(self, ticker: str, asof: datetime) -> Optional[Underlying]:
        # Index symbols are not Robinhood equities; a quote request 404s and the
        # error used to stick onto every later contract.
        if self.source == "robinhood" and ticker not in YF_SYMBOL:
            return self._rh_underlying(ticker)
        df = self._bars_for(ticker, asof.date())
        if df is None or len(df) == 0:
            return None
        df = df[df.index <= asof]
        if len(df) == 0:
            return None
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        vol = df["Volume"]
        vwap = float((tp * vol).sum() / vol.sum()) if vol.sum() > 0 else float(tp.mean())
        rvol = None
        avg = self._avg_volume(ticker)
        if avg and vol.sum() > 0:
            frac = clamp(len(df) / 390.0, 1 / 390, 1)
            rvol = float(vol.sum()) / (avg * frac)
        return Underlying(float(df["Close"].iloc[-1]), float(df["Open"].iloc[0]), float(df["High"].max()),
                          float(df["Low"].min()), vwap, rvol)

    def option(self, ticker: str, expiry, opt_type: str, strike: float) -> Optional[OptQuote]:
        if expiry is None:
            return None
        if self.source == "robinhood":
            return self._rh_option(ticker, expiry, opt_type, strike)
        sym = CHAIN_SYMBOL.get(ticker, ticker)
        k = (sym, expiry.isoformat())
        hit = self._chains.get(k)
        if not hit or time.time() - hit[0] > self.ttl:
            chain = None
            try:
                t = yf.Ticker(sym)
                if expiry.isoformat() in (t.options or ()):
                    chain = t.option_chain(expiry.isoformat())
            except Exception:
                chain = None
            self._chains[k] = hit = (time.time(), chain)
        chain = hit[1]
        if chain is None:
            return None
        df = chain.calls if opt_type == "C" else chain.puts
        row = df[(df["strike"] - strike).abs() < 1e-6]
        if len(row) == 0:
            return None
        r = row.iloc[0]
        f = lambda c: (None if c not in r or r[c] is None or (isinstance(r[c], float) and math.isnan(r[c])) else float(r[c]))
        return OptQuote(f("lastPrice"), f("bid"), f("ask"), f("volume"), f("openInterest"), f("impliedVolatility"))


@dataclass
class Score:
    total: int
    parts: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


def poster_cred(user: Optional[dict], now: datetime) -> Optional[float]:
    """0..1 from account age (5y = full) and followers (log scale, 100k = full)."""
    if not user:
        return None
    age = 0.0
    if user.get("created_at"):
        try:
            c = datetime.fromisoformat(user["created_at"].replace("Z", "+00:00"))
            age = clamp((now - c).days / 365 / 5)
        except ValueError:
            pass
    f = (user.get("public_metrics") or {}).get("followers_count", 0)
    fol = clamp(math.log10(max(f, 1)) / 5)
    return 0.5 * age + 0.5 * fol


def score_contract(opt_type: str, strike: float, entry: Optional[float], u: Optional[Underlying],
                   q: Optional[OptQuote], n_posters: int, cred: Optional[float], is_0dte: bool) -> Score:
    W = {"moneyness": 20, "trend": 20, "reach": 10, "rvol": 10, "option": 20, "consensus": 10, "poster": 10}
    got, avail, why = {}, {}, []
    sign = 1 if opt_type == "C" else -1
    if u:
        otm = sign * (strike - u.price) / u.price * 100      # >0 = OTM %
        tol = 1.5 if is_0dte else 4.0
        got["moneyness"] = W["moneyness"] * clamp(1 - max(otm, 0) / tol)
        why.append(f"{'ITM' if otm <= 0 else 'OTM'} {abs(otm):.2f}%")
        quote_only = u.high == u.low == u.open == u.vwap == u.price
        if quote_only:
            avail["moneyness"] = W["moneyness"]
            u = None
        t = 0.0
    if u:
        if sign * (u.price - u.vwap) > 0:
            t += 0.5
        if sign * (u.price - u.open) > 0:
            t += 0.5
        got["trend"] = W["trend"] * t
        why.append(("with" if t == 1 else "mixed" if t == .5 else "against") + " trend (VWAP/open)")
        extreme = u.high if opt_type == "C" else u.low
        gap = sign * (strike - extreme) / u.price * 100
        got["reach"] = W["reach"] * clamp(1 - max(gap, 0) / 1.5)
        if u.rvol is not None:
            got["rvol"] = W["rvol"] * clamp(u.rvol / 2)
            why.append(f"RVOL {u.rvol:.1f}x")
        for k in ("moneyness", "trend", "reach"):
            avail[k] = W[k]
        if u.rvol is not None:
            avail["rvol"] = W["rvol"]
    if q and (q.last or q.mid):
        cur = q.mid or q.last
        s = 0.0
        why.append(f"mark {cur:.2f}")
        if q.mid and q.ask:
            spr = (q.ask - q.bid) / q.mid
            s += 8 * clamp(1 - (spr - 0.05) / 0.25)
            why.append(f"spread {spr*100:.0f}%")
        liq = (q.volume or 0) + (q.oi or 0)
        s += 6 * clamp(math.log10(max(liq, 1)) / 3.5)
        if entry:
            ch = (cur - entry) / entry
            s += 6 * (1.0 if -0.3 <= ch <= 0.3 else 0.5 if ch < -0.3 else clamp(1 - (ch - 0.3)))
            why.append(f"now {ch*100:+.0f}% vs entry")
        else:
            s += 3
        if q.iv and q.iv > 1.0:
            s -= 3
            why.append(f"IV {q.iv*100:.0f}% high")
        got["option"] = max(0.0, s)
        avail["option"] = W["option"]
    got["consensus"] = W["consensus"] * {1: .3, 2: .6}.get(n_posters, 1.0)
    avail["consensus"] = W["consensus"]
    if n_posters > 1:
        why.append(f"{n_posters} posters agree")
    if cred is not None:
        got["poster"] = W["poster"] * cred
        avail["poster"] = W["poster"]
    tot = sum(got.values()) / sum(avail.values()) * 100 if avail else 0
    return Score(int(round(tot)), got, why)
