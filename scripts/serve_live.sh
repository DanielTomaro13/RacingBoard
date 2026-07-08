#!/usr/bin/env bash
# Put RacingBoard live for free: run the backend locally + expose it through a
# Cloudflare quick tunnel (no account, no domain). Polling happens from your own
# IP, which TAB trusts. Ctrl+C stops both.
#
#   bash scripts/serve_live.sh
#
# Prints two live URLs:
#   • the tunnel URL itself  — a complete live dashboard (backend serves the UI)
#   • the GitHub Pages page wired to your tunnel backend via ?api=
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${MF_PORT:-8000}"
PAGES_URL="https://danieltomaro13.github.io/RacingBoard"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
TUN_LOG="$(mktemp)"

command -v cloudflared >/dev/null || { echo "cloudflared not found → brew install cloudflared"; exit 1; }

cleanup() { echo; echo "stopping…"; kill "${BACK_PID:-}" "${TUN_PID:-}" 2>/dev/null || true; rm -f "$TUN_LOG"; }
trap cleanup EXIT INT TERM

echo "▶ starting backend on :$PORT …"
MF_PORT="$PORT" MF_HOST=127.0.0.1 "$PY" run.py > "$ROOT/backend.log" 2>&1 &
BACK_PID=$!

# wait for the backend health endpoint
for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
  [ "$i" = 60 ] && { echo "backend didn't come up — see backend.log"; exit 1; }
done
echo "✓ backend up (priming races in the background)"

echo "▶ opening Cloudflare tunnel …"
cloudflared tunnel --url "http://localhost:$PORT" > "$TUN_LOG" 2>&1 &
TUN_PID=$!

# grab the public URL cloudflared prints
URL=""
for i in $(seq 1 40); do
  URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUN_LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || { echo "tunnel URL not found — see $TUN_LOG"; cat "$TUN_LOG"; exit 1; }

WSS="wss://${URL#https://}"
echo
echo "══════════════════════════════════════════════════════════════════"
echo "  🟢 LIVE — open either:"
echo
echo "  Full app (simplest):   $URL"
echo "  Via GitHub Pages:      $PAGES_URL/?api=$WSS"
echo "══════════════════════════════════════════════════════════════════"
echo "  Leave this running. Ctrl+C to stop. (URL changes each run.)"
echo

wait "$BACK_PID"
