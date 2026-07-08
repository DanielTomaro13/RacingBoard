# RacingBoard 📈

A **Bloomberg-style live terminal** for racing money flow across Thoroughbred,
Greyhound & Harness — it surfaces the runners **shortening in price (money coming
in)**, gives a **recommended pick per race** that updates live, and shows a
**fair price** (de-vigged) with the **value edge** of the best available book price.

> Per runner: tote pool share + how much it's shortening · fair price (de-vigged
> Betfair·tote) · value vs best book · Sportsbet/Pointsbet/Betfair prices. Scrolling
> "money-in" tape, firmers panel, and a per-race pick. Only shortening runners are
> shown as movers — drifters are ignored on purpose. (Ladbrokes/Neds and Dabble
> aren't wired: Entain's public racecard 404s without auth, Dabble's per-race
> fixture matching is too heavy for fast polling.)

### 🔴 [Live demo](https://danieltomaro13.github.io/RacingBoard/)

![RacingBoard](assets/dashboard-replay.png)

## Live vs replay

- **Local (`python run.py`)** — fully live: polls Betfair + TAB every few seconds.
- **The GitHub Pages demo** — Pages only hosts static files, and a browser can't
  call TAB/Betfair directly, so it **replays a recording of real data**. To make
  the public page live, deploy the backend (below) and point the page at it.

## Run it (live, local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SPORTSDATA_MCP_SRC="/path/to/sportsdata-mcp/src"   # the data-layer engine
python run.py            # http://127.0.0.1:8000
```

No API keys needed — TAB runs off its public feed, Betfair off public read-only
endpoints. (No TAB secrets are used.)

## Make the public page live

Deploy the backend anywhere that runs Python + WebSockets, then connect the page:

- **Render** — New → Blueprint → this repo (uses `render.yaml`), or
- **Docker** — `docker build -t racingboard . && docker run -p 8000:8000 racingboard`, or
- **Local + tunnel** — `cloudflared tunnel --url http://localhost:8000` (most
  reliable: keeps your home IP, which TAB likes).

Then set `apiBase` in `docs/config.js` to the backend URL and push — or just open
`…/RacingBoard/?api=wss://your-host`.

## Common settings (env vars)

`MF_PRICE_INTERVAL` (8s) · `MF_CODES` (`R,G,H`) · `MF_JURISDICTION` (`NSW`) ·
`MF_MAX_ACTIVE_RACES` (12) · `SPORTSDATA_MCP_SRC` · `PORT`

## Rebuild the demo data

```bash
python scripts/capture_replay.py 16 5 && bash scripts/build_pages.sh && git add docs && git commit -m "refresh demo" && git push
```

MIT © Daniel Tomaro
