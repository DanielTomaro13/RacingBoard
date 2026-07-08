"""
Betr book-wide market movers — an independent fixed-odds money-flow signal.

`betr_market_movers` returns, in ONE call, the runners whose Betr fixed price is
moving, each with its full fluctuation curve. We keep the ones that are
SHORTENING (open → current) as a third confirmation source alongside the tote and
Betfair.

Runs on its own slow loop (see poller) and the per-race apply is a cached dict
lookup — it never touches or slows the Betfair fast loop.
"""

from __future__ import annotations

from .engine import SportsDataEngine
from .models import RaceRef, RaceSnapshot
from .sources import _norm_runner, _norm_venue

# Betr EventTypeID -> our code (verified live: 1=Darwin thoroughbred, 3=Taree greys).
BETR_TYPE = {1: "R", 2: "H", 3: "G"}


class BetrMovers:
    def __init__(self) -> None:
        # (code, venue_norm, race_no, runner_norm) -> move_pct (negative = shortening)
        self._short: dict[tuple, float] = {}

    async def refresh(self, engine: SportsDataEngine) -> None:
        data = await engine.try_call("betr_market_movers")
        idx: dict[tuple, float] = {}
        if data:
            for x in data.get("Items", []):
                if x.get("Scratched"):
                    continue
                code = BETR_TYPE.get(x.get("EventTypeID"))
                rno = x.get("RaceNumber")
                if not code or rno is None:
                    continue
                flucs = (x.get("OutcomeFluc") or {}).get("Flucs") or []
                cur = x.get("Win")
                open_p = flucs[0]["Price"] if flucs else None  # first = earliest offset
                if open_p and cur and cur < open_p:            # shortening only
                    key = (code, _norm_venue(x.get("Venue", "")), int(rno),
                           _norm_runner(x.get("OutcomeName", "")))
                    idx[key] = round((cur - open_p) / open_p * 100.0, 1)
        self._short = idx

    def enrich(self, race: RaceRef, snapshot: RaceSnapshot) -> None:
        if not self._short:
            return
        vnorm = _norm_venue(race.venue)
        for r in snapshot.runners:
            if (race.code, vnorm, race.race_no, _norm_runner(r.name)) in self._short:
                r.betr_short = True
