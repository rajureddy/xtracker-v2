#!/usr/bin/env python3
"""Read-only Robinhood Trading MCP client for live option and stock quotes.

Uses https://agent.robinhood.com/mcp/trading (OAuth PKCE). Quote tools only.
This module never places, reviews, or cancels orders.

  python robinhood_mcp.py     # one-time browser login -> robinhood_token.json
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
MCP_URL = "https://agent.robinhood.com/mcp/trading"
REGISTER_URL = "https://agent.robinhood.com/oauth/trading/register"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
AUTH_URL = "https://robinhood.com/oauth"
REDIRECT = os.environ.get("RH_REDIRECT_URI", "http://127.0.0.1:8766/callback")
CLIENT_FILE = HERE / "robinhood_client.json"
TOKEN_FILE = Path(os.environ.get("RH_TOKEN_FILE") or HERE / "robinhood_token.json")
READ_TOOLS = {"get_option_instruments", "get_option_quotes", "get_equity_quotes"}


class RobinhoodError(Exception):
    pass


def _num(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _parse_sse(text: str):
    data = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data.append(line[5:].strip())
    if not data:
        return None
    return json.loads(data[-1])


class Client:
    def __init__(self):
        self.s = requests.Session()
        self.session_id = None
        self._rpc = 0
        self.note = ""
        self.tok = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else {}

    def logged_in(self) -> bool:
        return bool(self.tok.get("access_token"))

    def _save(self):
        TOKEN_FILE.write_text(json.dumps({k: self.tok.get(k) for k in ("access_token", "refresh_token", "expires_at", "token_type")}))
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

    def _refresh(self) -> bool:
        rt = self.tok.get("refresh_token")
        cid = _client_id()
        if not (rt and cid):
            return False
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": rt, "client_id": cid,
            "resource": MCP_URL,
        }, timeout=20)
        if r.status_code != 200:
            return False
        self.tok.update(r.json())
        self.tok["expires_at"] = time.time() + float(self.tok.get("expires_in") or 3600) - 60
        self._save()
        return True

    def _bearer(self) -> str:
        if not self.logged_in():
            raise RobinhoodError("Robinhood login needed: python robinhood_mcp.py")
        if self.tok.get("expires_at") and time.time() > self.tok["expires_at"]:
            if not self._refresh():
                raise RobinhoodError("Robinhood login expired: python robinhood_mcp.py")
        return self.tok["access_token"]

    def _rpc_call(self, method: str, params: dict | None, note=False):
        self._rpc += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not note:
            body["id"] = self._rpc
        if params is not None:
            body["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self._bearer(),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        r = self.s.post(MCP_URL, headers=headers, json=body, timeout=30)
        if r.status_code == 401 and self._refresh():
            headers["Authorization"] = "Bearer " + self.tok["access_token"]
            r = self.s.post(MCP_URL, headers=headers, json=body, timeout=30)
        if r.status_code >= 400:
            raise RobinhoodError(f"Robinhood MCP {r.status_code}: {r.text[:180]}")
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if note or not r.content:
            return None
        ctype = r.headers.get("Content-Type", "")
        msg = _parse_sse(r.text) if "text/event-stream" in ctype else r.json()
        if isinstance(msg, dict) and msg.get("error"):
            raise RobinhoodError(str(msg["error"])[:180])
        return msg

    def _ensure_session(self):
        if self.session_id:
            return
        self._rpc_call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "xtracker", "version": "1"},
        })
        self._rpc_call("notifications/initialized", None, note=True)

    def call(self, name: str, arguments: dict):
        if name not in READ_TOOLS:
            raise RobinhoodError(f"refusing non-quote tool {name}")
        self._ensure_session()
        msg = self._rpc_call("tools/call", {"name": name, "arguments": arguments})
        result = (msg or {}).get("result") or {}
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            raise RobinhoodError(text[:180] or f"{name} failed")
        chunks = []
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("text"):
                chunks.append(c["text"])
        raw = "\n".join(chunks).strip()
        if not raw:
            return result
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}

    def equity(self, symbol: str) -> dict | None:
        data = self.call("get_equity_quotes", {"symbols": [symbol]})
        for row in _walk(data):
            sym = str(row.get("symbol") or row.get("ticker") or "").upper()
            if sym and sym != symbol.upper():
                continue
            last = _num(row, "last_trade_price", "last_price", "last", "mark_price", "adjusted_mark_price")
            if last is None:
                continue
            return {
                "price": last,
                "open": _num(row, "open_price", "open") or _num(row, "adjusted_previous_close", "previous_close", "prev_close"),
                "high": _num(row, "high_price", "high") or last,
                "low": _num(row, "low_price", "low") or last,
            }
        return None

    def option(self, symbol: str, expiry: str, opt_type: str, strike: float) -> dict | None:
        side = "call" if opt_type == "C" else "put"
        inst = self.call("get_option_instruments", {
            "chain_symbol": symbol,
            "expiration_dates": expiry,
            "strike_price": f"{strike:.4f}",
            "type": side,
            "state": "active",
        })
        iid = None
        for row in _walk(inst):
            if row.get("id") or row.get("instrument_id"):
                iid = row.get("id") or row.get("instrument_id")
                break
        if not iid:
            self.note = f"no Robinhood contract {symbol} {expiry} {strike:g}{opt_type}"
            return None
        q = self.call("get_option_quotes", {"instrument_ids": [iid]})
        for row in _walk(q):
            bid = _num(row, "bid_price", "bid")
            ask = _num(row, "ask_price", "ask")
            mark = _num(row, "adjusted_mark_price", "mark_price", "mark", "last_trade_price")
            if bid is None and ask is None and mark is None:
                continue
            return {
                "last": mark,
                "bid": bid,
                "ask": ask,
                "volume": _num(row, "volume"),
                "oi": _num(row, "open_interest", "openInterest"),
                "iv": _num(row, "implied_volatility", "impliedVolatility"),
            }
        self.note = f"no Robinhood quote {symbol} {expiry} {strike:g}{opt_type}"
        return None


def _client_id() -> str:
    if CLIENT_FILE.exists():
        return json.loads(CLIENT_FILE.read_text()).get("client_id") or ""
    r = requests.post(REGISTER_URL, json={
        "client_name": "xtracker",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "internal",
    }, timeout=20)
    if r.status_code >= 400:
        raise RobinhoodError(f"Robinhood client registration {r.status_code}: {r.text[:200]}")
    CLIENT_FILE.write_text(r.text)
    return r.json()["client_id"]


def login():
    cid = _client_id()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "internal",
        "resource": MCP_URL,
    })
    result = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Robinhood login done. You can close this tab.")
            threading.Thread(target=self.server.shutdown).start()

        def log_message(self, *a):
            pass

    u = urllib.parse.urlparse(REDIRECT)
    srv = http.server.HTTPServer((u.hostname, u.port), H)
    print("Opening browser for Robinhood login.\nIf it does not open, visit:\n" + url)
    webbrowser.open(url)
    srv.serve_forever()
    if result.get("state") != state or "code" not in result:
        raise SystemExit(f"Login failed: {result}")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": cid, "resource": MCP_URL,
    }, timeout=20)
    if r.status_code != 200:
        raise SystemExit(f"Token exchange failed {r.status_code}: {r.text[:300]}")
    tok = r.json()
    tok["expires_at"] = time.time() + float(tok.get("expires_in") or 3600) - 60
    TOKEN_FILE.write_text(json.dumps({k: tok.get(k) for k in ("access_token", "refresh_token", "expires_at", "token_type")}))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"Saved Robinhood token to {TOKEN_FILE} (gitignored). Quotes only; no orders.")


if __name__ == "__main__":
    login()
