"""Shared data models for races, runners and money-flow snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class RaceRef:
    """Canonical identity of a race, sourced from the TAB meetings spine."""

    race_key: str          # stable id: "{code}:{venue_mnem}:{race_no}:{date}"
    code: str              # R | G | H
    venue: str             # display name, e.g. "BATHURST"
    venue_mnem: str        # TAB mnemonic, e.g. "BAT"
    race_no: int
    race_name: str
    start_time: str        # ISO8601
    date: str              # YYYY-MM-DD

    # Optional cross-book handles filled in during enrichment.
    betfair_market_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunnerFlow:
    """Per-runner money picture at one point in time, merged across sources."""

    number: int
    name: str
    scratched: bool = False

    # TAB tote (pari-mutuel) — the pool-money signal (all codes).
    tote_win: float | None = None          # tote dividend (decimal)
    tote_pool_share: float | None = None   # normalised share of win pool [0..1]

    # TAB fixed odds.
    fixed_win: float | None = None

    # Corporate fixed odds (Sportsbet, Pointsbet, …) — book -> win price, plus
    # the best (highest) price on offer across books.
    corp: dict[str, float] = field(default_factory=dict)
    corp_best: float | None = None
    corp_best_book: str | None = None
    corp_short: list[str] = field(default_factory=list)   # corporate books shortening since open

    # Fair price (de-vigged from the sharpest market — Betfair, else tote) and the
    # value edge of the best available book price vs that fair price (%; >0 = value).
    fair_price: float | None = None
    value_pct: float | None = None

    # Betfair exchange (horses/greys).
    bf_back: float | None = None
    bf_lay: float | None = None
    bf_last: float | None = None
    bf_wom: float | None = None            # weight of money, back$/(back$+lay$) [0..1]
    bf_implied: float | None = None        # implied prob from mid price [0..1]

    # Independent fixed-odds confirmation (Betr book-wide movers).
    betr_short: bool = False               # Betr flags this runner's price shortening

    # Static form / runner info (from TAB — doesn't change during the race).
    last5: str | None = None               # last-5 finishing positions, e.g. "809x8"
    jockey: str | None = None
    trainer: str | None = None
    barrier: int | None = None
    weight: float | None = None            # handicap weight (kg)
    speed_band: str | None = None          # early-speed style, e.g. "LEADER"
    form_rating: float | None = None       # TAB form rating
    comment: str | None = None             # per-runner form comment (narrative)
    best_time: str | None = None           # greyhounds: best time over the trip
    career: str | None = None              # career record "starts: W-P" (all codes)
    best_bet: str | None = None            # Sportsbet expert best-bet tipster, if any

    # Derived movement (filled by the store from history).
    share_open: float | None = None        # first observed pool share
    share_delta: float | None = None       # current - open (pool share pts)
    share_delta_recent: float | None = None  # change over the recent window (live momentum)
    price_move_pct: float | None = None     # fixed/tote drift since open (%; <0 = firming)
    direction: str = "flat"                # firming | drifting | flat

    def to_dict(self) -> dict[str, Any]:
        # Hand-built (not dataclasses.asdict) — asdict deep-copies every field and
        # this runs for ~288 runners every 3s in the Betfair loop. corp / corp_short
        # are referenced directly (serialized immediately, never mutated in place).
        return {
            "number": self.number, "name": self.name, "scratched": self.scratched,
            "tote_win": self.tote_win, "tote_pool_share": self.tote_pool_share,
            "fixed_win": self.fixed_win,
            "corp": self.corp, "corp_best": self.corp_best, "corp_best_book": self.corp_best_book,
            "corp_short": self.corp_short,
            "fair_price": self.fair_price, "value_pct": self.value_pct,
            "bf_back": self.bf_back, "bf_lay": self.bf_lay, "bf_last": self.bf_last,
            "bf_wom": self.bf_wom, "bf_implied": self.bf_implied,
            "betr_short": self.betr_short,
            "last5": self.last5, "jockey": self.jockey, "trainer": self.trainer,
            "barrier": self.barrier, "weight": self.weight, "speed_band": self.speed_band,
            "form_rating": self.form_rating, "comment": self.comment,
            "best_time": self.best_time, "career": self.career, "best_bet": self.best_bet,
            "share_open": self.share_open, "share_delta": self.share_delta,
            "share_delta_recent": self.share_delta_recent, "price_move_pct": self.price_move_pct,
            "direction": self.direction,
        }


@dataclass
class RaceSnapshot:
    """One timestamped observation of a whole race."""

    ts: float                              # epoch seconds
    runners: list[RunnerFlow] = field(default_factory=list)

    # Race-level money aggregates.
    tote_win_pool: float | None = None     # gross win pool ($) if TAB reports it
    bf_total_matched: float | None = None  # Betfair matched on the WIN market ($)
    status: str = "OPEN"

    # Race context (from TAB).
    tips: dict | None = None               # {tipster, numbers:[...]}
    comment: str | None = None             # race preview comment
    results: list[int] | None = None       # finishing order (runner numbers) once run

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "status": self.status,
            "tote_win_pool": self.tote_win_pool,
            "bf_total_matched": self.bf_total_matched,
            "runners": [r.to_dict() for r in self.runners],
        }
