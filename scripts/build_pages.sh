#!/usr/bin/env bash
# Build the static GitHub Pages site into ./docs from the shared frontend.
# Pages can't run the Python backend, so the site ships a captured replay and
# forces replay mode. (Point a static page at a live backend with ?api=wss://…)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/moneyflow/static"
DOCS="$ROOT/docs"

mkdir -p "$DOCS/data"
cp "$SRC/index.html" "$DOCS/index.html"
cp "$SRC/styles.css" "$DOCS/styles.css"
cp "$SRC/app.js"     "$DOCS/app.js"
cp "$SRC/data/replay.json" "$DOCS/data/replay.json"

# Pages config: force replay.
cat > "$DOCS/config.js" <<'EOF'
// GitHub Pages build — no backend, so replay the captured sequence.
// Connect a deployed backend instead with ?api=wss://your-host
window.MF_CONFIG = {
  forceReplay: true,
  replayUrl: "data/replay.json",
  apiBase: null,
};
EOF

# Tell Pages not to run Jekyll (serve files verbatim).
touch "$DOCS/.nojekyll"

echo "built docs/ ($(du -sh "$DOCS" | cut -f1)):"
ls -1 "$DOCS"
