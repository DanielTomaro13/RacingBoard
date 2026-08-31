#!/usr/bin/env python3
"""Entry point: launch the dashboard server.

    python run.py           # serve on http://127.0.0.1:8000
"""

from __future__ import annotations

import uvicorn

from moneyflow.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "moneyflow.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        # The placer polls /api/race for every open race on every pass, so the
        # access log was tens of lines a second of pure noise -- and it buried the
        # board's own [fast] and [discovery] lines, which are the ones worth
        # reading. Errors still surface; only the 200s are silenced.
        access_log=False,
    )
