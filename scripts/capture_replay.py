#!/usr/bin/env python3
"""
Capture a sequence of real money-flow frames for the static (GitHub Pages) demo.

Runs the poller in-process against the live upstreams, samples the store every
few seconds, and writes a replay file the frontend animates when there's no
backend. Frame shape matches what app.js expects:

    [ { "board": [...], "movers": [...], "races": { race_key: detail, ... } }, ... ]

Usage:
    python scripts/capture_replay.py [frames] [spacing_seconds] [out_path]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moneyflow.poller import Poller           # noqa: E402
from moneyflow.store import Store             # noqa: E402


async def capture(n_frames: int, spacing: float, out: Path) -> None:
    store = Store()
    poller = Poller(store, broadcast=None)
    task = asyncio.create_task(poller.start())

    # Wait until at least a few races have their first snapshot.
    for _ in range(40):
        await asyncio.sleep(1)
        if sum(1 for s in store.races.values() if s.latest) >= 3:
            break
    print(f"primed: {sum(1 for s in store.races.values() if s.latest)} races have data")

    frames = []
    for f in range(n_frames):
        await asyncio.sleep(spacing)
        races = {}
        for key, st in store.races.items():
            if st.latest is not None:
                d = store.race_detail(key)
                if d:
                    races[key] = d
        frames.append({
            "board": store.board(),
            "movers": store.movers(),
            "value": store.value(),
            "scores": poller.scorer.stats(),
            "races": races,
        })
        print(f"frame {f + 1}/{n_frames}: {len(frames[-1]['board'])} races, "
              f"{len(races)} with detail, {len(frames[-1]['movers'])} movers")

    poller._running = False
    task.cancel()
    await poller.stop()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(frames, separators=(",", ":"), default=str))
    kb = out.stat().st_size / 1024
    print(f"wrote {len(frames)} frames -> {out} ({kb:.0f} KB)")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    sp = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else \
        Path(__file__).resolve().parents[1] / "moneyflow" / "static" / "data" / "replay.json"
    asyncio.run(capture(n, sp, out))
