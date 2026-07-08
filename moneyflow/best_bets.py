"""
Sportsbet best-bets — an expert-tipster layer.

`sportsbet_racing_best_bets_with_events` returns, in one call, tipsters' "best
bet" selections across today's racing with event context. We index them by
(venue, race number, runner number) and flag matching runners so the board shows
where the experts agree with (or diverge from) the money.

Refreshed on the discovery loop (it changes slowly) and applied per-race as a
cached lookup — no per-race API call.
"""

from __future__ import annotations

from .engine import SportsDataEngine
from .models import RaceRef, RaceSnapshot
from .sources import _norm_venue


class BestBets:
    def __init__(self) -> None:
        self._idx: dict[tuple, str] = {}  # (venue_norm, race_no, runner_no) -> tipster

    async def refresh(self, engine: SportsDataEngine) -> None:
        data = await engine.try_call("sportsbet_racing_best_bets_with_events")
        idx: dict[tuple, str] = {}
        if data:
            for mod in data.get("tipsModules", []):
                venue = _norm_venue(mod.get("meeting", ""))
                tipster = mod.get("tipsterName") or "Sportsbet"
                for t in mod.get("tips", []):
                    rno = t.get("raceNumber")
                    num = t.get("runnerNumber")
                    if rno is not None and num is not None:
                        idx[(venue, int(rno), int(num))] = tipster
        if idx:                 # keep the last good set if a refresh comes back empty
            self._idx = idx

    def enrich(self, race: RaceRef, snapshot: RaceSnapshot) -> None:
        if not self._idx:
            return
        vnorm = _norm_venue(race.venue)
        for r in snapshot.runners:
            tipster = self._idx.get((vnorm, race.race_no, r.number))
            if tipster:
                r.best_bet = tipster
