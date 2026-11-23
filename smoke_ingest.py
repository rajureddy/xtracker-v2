#!/usr/bin/env python3
"""POST sample plugin payloads to a running xtracker-v2 ingest endpoint."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/api/ingest"
SECRET = sys.argv[2] if len(sys.argv) > 2 else ""
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

samples = [
    {
        "id": "smoke_potential_1",
        "handle": "@smoke",
        "time": now,
        "text": "$QQQ Trade Idea: Nov 5 700C. Trigger: 693. Target: 700. Stop: 690. If QQQ can hold 685 and reclaim 700 a bottom should form.",
        "url": "https://x.com/smoke/status/smoke_potential_1",
    },
    {
        "id": "smoke_live_1",
        "handle": "@smoke",
        "time": now,
        "text": "QQQ 694P 0DTE @1.14",
        "url": "https://x.com/smoke/status/smoke_live_1",
    },
]


def post(posts):
    body = json.dumps({"posts": posts}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-Ingest-Secret": SECRET} if SECRET else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    print("POST potential →", post([samples[0]]))
    print("POST live call →", post([samples[1]]))
    print("Open http://127.0.0.1:8081 — expect one Flagged (potential) and one Live QQQ 694P row.")
