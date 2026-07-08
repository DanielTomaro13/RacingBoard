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
    )
