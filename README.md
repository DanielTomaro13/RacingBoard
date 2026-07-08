# RacingBoard 📈

**Real-time "where's the money going" board for Thoroughbred, Greyhound & Harness racing.**
A live movers leaderboard across every upcoming race, plus a per-race drill-in
showing tote **pool share**, Betfair **weight of money**, and who's **firming vs
drifting** — updating on a WebSocket as the money moves.

### 🔴 [Live demo (GitHub Pages)](https://danieltomaro13.github.io/RacingBoard/) · replays a captured sequence of real race data

![RacingBoard replay](assets/dashboard-replay.png)

---

## What "money" means here

Three signals are merged per runner:

| Signal | Source | Codes | Meaning |
|---|---|:--:|---|
| **Tote pool share** | TAB pari-mutuel | R · G · H | Each runner's normalised share of the win pool — the purest "money is here" signal, and how **harness** is covered. |
| **Weight of money** | Betfair exchange | R · G | Back-side $ vs lay-side $ pressure at top of book. |
| **Firm / drift** | Betfair + TAB | R · G · H | Money coming in (firming ▲) vs leaving (drifting ▼) since first observation. |

The **movers board** ranks the biggest pool-share shifts across every tracked race;
the **drill-in** shows pool-share bars, a share sparkline over time, Betfair
weight-of-money, tote/fixed odds, and firm/drift direction with a hover tooltip.

## Two ways to run it

RacingBoard is one frontend with two data sources:

- **🟢 Live** — a FastAPI + WebSocket backend polls Betfair and TAB in real time.
- **🟠 Replay** — the static site steps through a captured JSON sequence of real
  data. This is what **GitHub Pages** serves (Pages can't run a backend, and
  browsers can't reach TAB/Betfair directly — CORS + Akamai). The replay *is* real
  data; it just animates a recording.

The page auto-detects: it tries the WebSocket, and falls back to replay if there
isn't one. You can also point the static page at a deployed backend:
`…github.io/RacingBoard/?api=wss://your-host`.

## Architecture

```
TAB meetings (spine) ─┐
Betfair nav + prices ─┼─►  Poller  ─►  Store (time-series)  ─►  FastAPI + WebSocket  ─►  dashboard
corporate flucs      ─┘       │              │                        │
                          discovery      derives movement         capture_replay.py
                          + price loops  (firm/drift, movers)      → docs/ (Pages)
```

- **Betfair** is hit directly (public read-only endpoints, no auth).
- **TAB + corporate books** go through a local **[sportsdata-mcp](https://github.com/DanielTomaro13) HTTP engine imported as a library** —
  not the MCP server/protocol, just its vetted `HTTPClient` + specs. This is what
  handles TAB's Akamai `bm_*` cookie handshake that a naive scraper (HTTP 000)
  can't get past.

## Run it live

```bash
git clone https://github.com/DanielTomaro13/RacingBoard
cd RacingBoard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# point at your sportsdata-mcp checkout's src/ (for the TAB + corporate data layer)
export SPORTSDATA_MCP_SRC="/path/to/sportsdata-mcp/src"

python run.py      # http://127.0.0.1:8000
```

### Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `SPORTSDATA_MCP_SRC` | `~/Documents/Projects/sportsdata-mcp/src` | Data-layer engine location |
| `MF_PRICE_INTERVAL` | `8` | Seconds between price polls |
| `MF_DISCOVERY_INTERVAL` | `60` | Seconds between race-list refreshes |
| `MF_HORIZON_MINUTES` | `45` | Track races jumping within this window |
| `MF_MAX_ACTIVE_RACES` | `12` | Races polled at full cadence at once |
| `MF_CODES` | `R,G,H` | Codes to track |
| `MF_JURISDICTION` | `NSW` | TAB jurisdiction for the meetings spine |
| `MF_BETFAIR` / `MF_TAB` / `MF_CORPORATE` | `1` | Toggle sources |
| `PORT` / `MF_PORT` | `8000` | HTTP port |

## Rebuild the Pages demo

```bash
python scripts/capture_replay.py 16 5     # record 16 frames, 5s apart -> static/data/replay.json
bash scripts/build_pages.sh               # copy frontend + replay into ./docs
git add docs && git commit -m "refresh demo" && git push
```

GitHub Pages is served from the **`/docs` folder on `main`** (Settings → Pages).

## Deploy a real live backend (optional)

To make the hosted page truly live, deploy the FastAPI app anywhere that runs
Python + WebSockets (Render, Railway, Fly.io) with `SPORTSDATA_MCP_SRC` available,
then open `…github.io/RacingBoard/?api=wss://your-host`. `run.py` honours `$PORT`.

## Gotchas

- **Markets form ~15–20 min before the jump.** Tote pools and Betfair matched are
  `null`/noisy until then — the race still lists, the money numbers fill in.
- **Betfair's public feed has no per-runner matched volume** (runner `totalMatched`
  is 0 even on liquid markets) — per-runner flow is reconstructed by polling and
  diffing over time.
- Cross-book Betfair↔TAB matching is best-effort (venue name + race number + runner
  name). Unmatched tracks (internationals) simply show tote-only.
- Be gentle on cadence — the upstreams rate-limit (TAB via Akamai especially).

## License

MIT © Daniel Tomaro
