"""Runtime configuration for the racing money-flow tool.

Everything is overridable via environment variables so the same code runs on a
laptop or a box. The one path that matters is SPORTSDATA_MCP_SRC — the `src`
directory of your local sportsdata-mcp checkout, whose vetted HTTP engine we
import as a library to reach TAB (Akamai-gated) and the corporate books.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_mcp_src() -> str:
    # Sensible default for this machine; override with SPORTSDATA_MCP_SRC.
    guess = Path.home() / "Documents" / "Projects" / "sportsdata-mcp" / "src"
    return os.environ.get("SPORTSDATA_MCP_SRC", str(guess))


@dataclass
class Settings:
    # --- sportsdata-mcp engine (TAB + corporate data layer) ---
    sportsdata_mcp_src: str = field(default_factory=_default_mcp_src)

    # --- polling cadence (seconds) ---
    # Board (all upcoming races) is discovered less often than prices are polled.
    discovery_interval: float = float(os.environ.get("MF_DISCOVERY_INTERVAL", "60"))
    price_interval: float = float(os.environ.get("MF_PRICE_INTERVAL", "8"))
    # Betfair is public + cheap and batches every active market in one call, so
    # refresh it on a fast loop of its own for near-real-time exchange moves.
    betfair_interval: float = float(os.environ.get("MF_BETFAIR_INTERVAL", "3"))
    # Corporate books rate-limit, so price them on a slower cadence than the tote.
    corp_interval: float = float(os.environ.get("MF_CORP_INTERVAL", "20"))
    # Betr book-wide movers refresh (one call, its own loop — never blocks Betfair).
    betr_interval: float = float(os.environ.get("MF_BETR_INTERVAL", "15"))

    # How far ahead to track races for the board (minutes to jump).
    horizon_minutes: int = int(os.environ.get("MF_HORIZON_MINUTES", "60"))
    # Max races polled at full cadence at once (protects the upstreams). The board
    # lists every race in the horizon; the nearest this-many get live money data.
    max_active_races: int = int(os.environ.get("MF_MAX_ACTIVE_RACES", "24"))

    # TAB jurisdiction for the meetings spine.
    jurisdiction: str = os.environ.get("MF_JURISDICTION", "NSW")

    # Racing codes to track: R=thoroughbred, G=greyhound, H=harness.
    codes: tuple[str, ...] = tuple(os.environ.get("MF_CODES", "R,G,H").split(","))

    # --- source toggles ---
    enable_betfair: bool = os.environ.get("MF_BETFAIR", "1") == "1"
    enable_tab: bool = os.environ.get("MF_TAB", "1") == "1"
    enable_corporate: bool = os.environ.get("MF_CORPORATE", "1") == "1"

    # Time-series retention per race (snapshots kept in memory). Sized to cover
    # the full horizon at price_interval so the "since open" baseline for a race
    # tracked up to ~90 min doesn't silently roll forward and drift.
    history_len: int = int(os.environ.get("MF_HISTORY_LEN", "700"))
    # Window (seconds) for "recent" momentum — how fast a runner is shortening
    # right now, vs cumulatively since we started watching.
    recent_window: float = float(os.environ.get("MF_RECENT_WINDOW", "90"))

    # HTTP server.
    host: str = os.environ.get("MF_HOST", "127.0.0.1")
    # Honour a harness-assigned PORT (preview/hosting) before MF_PORT/default.
    port: int = int(os.environ.get("PORT") or os.environ.get("MF_PORT") or "8000")


settings = Settings()

CODE_LABEL = {"R": "Thoroughbred", "G": "Greyhound", "H": "Harness"}
