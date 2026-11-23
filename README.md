# xtracker-v2

Local options-signal tracker fed by a Chrome extension that scrapes X (Twitter).

Posts flow **extension → localhost ingest → parser → Robinhood/Yahoo quotes → watchlist UI**.

Research / education only. **Not financial advice. Never places trades.**

## Watchlist UI

Local desk at **http://127.0.0.1:8081** — Live / Top filters, Stocks vs ETFs split, TraderGrader highlights, and scored contracts:

![xtracker-v2 watchlist](docs/watchlist.png)

## What it does

- Scrapes `x.com/home` or `x.com/notifications` from whoever is logged into Chrome (no X API token required in plugin mode)
- Parses option calls vs flagged notes (trade ideas, conditionals, no real strike)
- Scores contracts with live quotes (Robinhood MCP or Yahoo)
- Serves a watchlist at **http://127.0.0.1:8081**
- Reloads today’s CSV on restart so a refresh is not a blank “fresh sync”
- Highlights TraderGrader handles from `tradergrader.json` (green / orange / yellow)
- Splits the table into **Stocks** vs **ETFs** (SPY, QQQ, IWM, SPX, XSP, …)

This repo is the **v2** app (port **8081**). The older X-API tracker on port **8080** is a separate tree and is not required.

## Requirements

- Windows / macOS / Linux with Python 3.11+
- Chrome (for the scrape extension)
- Optional: Robinhood Trading MCP token for live option quotes

## Setup

```powershell
cd xtracker-v2
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
cd xtracker-v2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Robinhood quotes (optional)

Token files are **not** in git. Use the redacted examples, then put real values locally:

```powershell
copy robinhood_token.json.example robinhood_token.json
copy robinhood_client.json.example robinhood_client.json
# replace XXXXXXX with your tokens, or restore from .local-secrets\ (gitignored backup)
```

Or log in once:

```powershell
python robinhood_mcp.py
```

Without Robinhood, start with `--quotes yahoo` (delayed).

**Do not commit** `robinhood_token.json`, `robinhood_client.json`, `x_token.json`, or `.local-secrets/`.

## Chrome extension

1. Chrome → `chrome://extensions` → Developer mode → **Load unpacked**
2. Select the `extension/` folder in this repo
3. Keep an **x.com/home** or **x.com/notifications** tab open (logged in)
4. Open the extension viewer → **Start**
5. Leave **Push to xtracker-v2** on (default `http://127.0.0.1:8081/api/ingest`)

The extension follows the X account signed into that Chrome profile. Switch login, and the feed changes with it.

## Run

```powershell
python xtracker.py --plugin --now
```

| | |
|---|---|
| Watchlist | http://127.0.0.1:8081 |
| Ingest | `POST http://127.0.0.1:8081/api/ingest` |
| Default refresh | 30s |

`--now` keeps researching outside the weekday 09:00–16:00 ET window. Without `--now`, ingest still accepts; the research loop follows that window.

Useful flags:

| Flag | Meaning |
|---|---|
| `--plugin` | Extension ingest only (default path for v2) |
| `--now` | Ignore market hours |
| `--port 8081` | UI + ingest port |
| `--interval 30` | Table refresh seconds |
| `--quotes robinhood\|yahoo` | Quote source |
| `--fresh` | Wipe today’s CSV and start empty |
| `--ingest-secret` | Optional shared secret (`XTRACKER_INGEST_SECRET`) |

Do **not** use `--fresh` for a normal start; the default reloads today’s `logs/calls_*.csv` and `flagged_*.csv`.

### Optional X API poll

```powershell
python xtracker.py --with-x-api --plugin --now
```

Needs `x_token.json` (gitignored). Prefer plugin-only.

## Watchlist tips

- **Live / Top / Flagged / All** — scope chips
- **Both / Stocks / ETFs** — ticker kind split
- **Complete list / Group by ticker** — layout
- **TG** column — TraderGrader rank when the poster is on the list
- Edit `tradergrader.json` to add handles; the tracker reloads it on the next refresh (no code change)

## Layout

| Path | Role |
|---|---|
| `xtracker.py` | Main app, CSV persistence, research loop |
| `webview.py` | Local UI + `/api/ingest` (127.0.0.1 only) |
| `parser.py` | Live vs flagged parsing |
| `research.py` | Quotes + confidence score |
| `robinhood_mcp.py` | Robinhood MCP quote client |
| `tradergrader.json` | Green / orange / yellow highlight roster |
| `extension/` | MV3 scrape + push to localhost |
| `logs/` | Daily CSV (gitignored) |
| `test_parser.py` | Parser checks: `python test_parser.py` |
| `smoke_ingest.py` | Sample ingest without Chrome |

## Smoke test (no Chrome)

With the tracker running:

```powershell
python smoke_ingest.py
```

Or POST JSON `{ "posts": [ { "id", "handle", "text", "time", "url" } ] }` to `/api/ingest`, then open the watchlist.

## Security notes

- Server binds **127.0.0.1** only
- Real credentials stay on this machine only:
  - Working copies: `robinhood_token.json`, `robinhood_client.json`, `x_token.json` (gitignored)
  - Local backup: `.local-secrets/` (gitignored)
  - Repo templates: `*.json.example` with `XXXXXXX` placeholders
- Optional ingest auth: set the same secret in the env/flag and in the extension viewer
- Before every push, confirm `git status` does **not** list token JSON files

## Disclaimer

Public social posts are not verified fills. Quotes can be delayed or missing. This tool does not place orders and is not financial advice.
