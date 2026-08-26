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


def _discord_webhook() -> str:
    """MF_DISCORD_WEBHOOK env wins; else reuse the sportsdata-agents stack's
    webhook from its .env (same Discord channel) so the secret lives in one place."""
    url = os.environ.get("MF_DISCORD_WEBHOOK", "")
    if url:
        return url
    env = Path.home() / "Documents" / "Projects" / "sportsdata-agents" / ".env"
    try:
        for line in env.read_text().splitlines():
            if line.startswith("DISCORD_WEBHOOK_URL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


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

    # How far ahead to track races for the board (minutes to jump). Set to 120 so the
    # DataLogger's earliest training buckets (T‑120/T‑90) actually get captured —
    # with a 60-min horizon a race isn't tracked until 60 min out, leaving them empty.
    horizon_minutes: int = int(os.environ.get("MF_HORIZON_MINUTES", "120"))
    # Max races polled at full cadence at once (protects the upstreams). The board
    # lists every race in the horizon; the nearest this-many get live money data.
    # Sized with the 120-min horizon in mind: too small and far-out races never get
    # polled, so the DataLogger's T-120/90 training buckets are missed exactly when
    # the schedule is dense.
    max_active_races: int = int(os.environ.get("MF_MAX_ACTIVE_RACES", "40"))

    # TAB jurisdiction for the meetings spine.
    jurisdiction: str = os.environ.get("MF_JURISDICTION", "NSW")

    # Racing codes to track: R=thoroughbred, G=greyhound, H=harness.
    codes: tuple[str, ...] = tuple(os.environ.get("MF_CODES", "R,G,H").split(","))

    # Only track meetings in these locations (TAB meeting `location` code).
    # Default AU/NZ: the firm-prediction model is scoped to AU/NZ, and dropping
    # thin overnight international pools (USA/GBR/FRA/…) de-noises the board.
    # MF_COUNTRIES=* tracks every meeting TAB lists; the model can re-scope at
    # training time via races.country, so collecting wide costs nothing there.
    countries: frozenset[str] = frozenset(
        os.environ.get("MF_COUNTRIES", "NSW,VIC,QLD,SA,WA,TAS,NT,ACT,NZL").split(","))

    @property
    def all_countries(self) -> bool:
        return "*" in self.countries

    # --- source toggles ---
    enable_betfair: bool = os.environ.get("MF_BETFAIR", "1") == "1"
    enable_tab: bool = os.environ.get("MF_TAB", "1") == "1"
    enable_corporate: bool = os.environ.get("MF_CORPORATE", "1") == "1"

    # Time-series retention per race (snapshots kept in memory). Sized to cover the
    # full 120-min horizon at price_interval (120·60/8 ≈ 900) so the "since open"
    # baseline for a long-tracked race doesn't silently roll off and drift.
    history_len: int = int(os.environ.get("MF_HISTORY_LEN", "1000"))
    # Window (seconds) for "recent" momentum — how fast a runner is shortening
    # right now, vs cumulatively since we started watching.
    recent_window: float = float(os.environ.get("MF_RECENT_WINDOW", "90"))

    # Where the signal scoreboard persists its running hit-rate across sessions.
    scores_path: str = os.environ.get(
        "MF_SCORES_PATH", os.path.join(os.path.expanduser("~"), ".racingboard", "scores.json"))
    # Scorecard P&L: stakes are sized by fractional Kelly off the running bankroll,
    # using the tool's de-vig fair price as the win-probability estimate and betting
    # at the best available fixed price. Half-Kelly (0.5) by default; only positive-
    # edge selections get a stake. ROI/profit tell you if a signal makes money.
    bankroll: float = float(os.environ.get("MF_BANKROLL", "100"))
    kelly_fraction: float = float(os.environ.get("MF_KELLY_FRACTION", "0.5"))
    # Safety cap on the per-bet bankroll fraction after the Kelly multiplier.
    kelly_cap: float = float(os.environ.get("MF_KELLY_CAP", "0.25"))

    # Follower ledger — an append-only, IMMUTABLE record of committed, Kelly-staked
    # selections. Once committed a selection can never be removed (only settled), so
    # the entry bar is strict: a selection auto-commits only when it clears ALL of
    # the criteria below. This keeps the permanent track record honest.
    follow_path: str = os.environ.get(
        "MF_FOLLOW_PATH", os.path.join(os.path.expanduser("~"), ".racingboard", "follows.json"))

    # Local training store (SQLite). The DataLogger forward-collects point-in-time
    # snapshots + outcomes here so a firm-prediction model can be trained once weeks
    # of AU/NZ data have accrued (the warehouse never captured any — audited empty).
    db_path: str = os.environ.get(
        "MF_DB_PATH", os.path.join(os.path.expanduser("~"), ".racingboard", "racingboard.db"))
    enable_datalog: bool = os.environ.get("MF_DATALOG", "1") == "1"
    # Minutes-before-jump at which to capture a training snapshot of each runner.
    datalog_buckets: tuple[int, ...] = tuple(
        int(x) for x in os.environ.get("MF_DATALOG_BUCKETS", "120,90,60,45,30,20,15,10,5,2").split(","))
    # Firm label threshold: open→jump shortened by ≥ this fraction ⇒ firmed.
    firm_threshold: float = float(os.environ.get("MF_FIRM_THRESHOLD", "0.08"))
    # No time gate: strong multi-market steam is inherently a near-jump event, so a
    # minimum time-to-jump never triggers (0 snapshots ever reach ≥3 confirms ≥60min
    # out). The only invariant is immutability — once committed, an entry can't change.
    follow_min_minutes: float = float(os.environ.get("MF_FOLLOW_MIN_MINUTES", "0"))     # commit any time pre-jump
    follow_min_confirm: int = int(os.environ.get("MF_FOLLOW_MIN_CONFIRM", "3"))         # ≥3 markets agree
    follow_min_value: float = float(os.environ.get("MF_FOLLOW_MIN_VALUE", "0.0"))       # value_pct must exceed this
    follow_price_min: float = float(os.environ.get("MF_FOLLOW_PRICE_MIN", "1.5"))       # skip odds-on (firming meaningless)
    follow_price_max: float = float(os.environ.get("MF_FOLLOW_PRICE_MAX", "26.0"))      # skip roughies

    # Discord notifications for ledger commits/settlements (empty = disabled).
    discord_webhook: str = field(default_factory=_discord_webhook)

    # HTTP server.
    host: str = os.environ.get("MF_HOST", "127.0.0.1")
    # Honour a harness-assigned PORT (preview/hosting) before MF_PORT/default.
    port: int = int(os.environ.get("PORT") or os.environ.get("MF_PORT") or "8000")


settings = Settings()

CODE_LABEL = {"R": "Thoroughbred", "G": "Greyhound", "H": "Harness"}
