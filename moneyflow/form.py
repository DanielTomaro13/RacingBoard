"""
Runner form comments from TAB.

The inline TAB race response (in `tab_snapshot`) already gives last-5, jockey,
trainer, barrier, weight and speed band for free. This adds the per-runner form
*comment* narrative from `tab_racing_race_form`. Form is static, so it's fetched
once per race and cached for the session.
"""

from __future__ import annotations

from .config import settings
from .engine import SportsDataEngine
from .models import RaceRef, RaceSnapshot


class FormSource:
    def __init__(self) -> None:
        # race_key -> {number: {comment, best_time, career}}
        self._cache: dict[str, dict[int, dict]] = {}

    async def enrich(self, engine: SportsDataEngine, race: RaceRef, snapshot: RaceSnapshot) -> None:
        form = self._cache.get(race.race_key)
        if form is None:
            form = await self._fetch(engine, race)
            self._cache[race.race_key] = form   # cache even if empty (one attempt)
        if form:
            for r in snapshot.runners:
                info = form.get(r.number)
                if not info:
                    continue
                r.comment = info.get("comment") or r.comment
                r.best_time = info.get("best_time")
                r.career = info.get("career")

    async def _fetch(self, engine: SportsDataEngine, race: RaceRef) -> dict[int, dict]:
        data = await engine.try_call(
            "tab_racing_race_form",
            date=race.date,
            raceType=race.code,
            venueMnemonic=race.venue_mnem,
            raceNumber=race.race_no,
            jurisdiction=settings.jurisdiction,
        )
        out: dict[int, dict] = {}
        if not data:
            return out
        for f in data.get("form", []) or []:
            num = f.get("runnerNumber")
            if num is None:
                continue
            info: dict = {}
            if f.get("formComment"):
                info["comment"] = f["formComment"].strip()
            bt = f.get("bestTime")
            if bt and str(bt) not in ("0", "0.00", ""):
                info["best_time"] = str(bt)
            overall = ((f.get("runnerStarts") or {}).get("startSummaries") or {}).get("overall") or {}
            starts = overall.get("numberOfStarts")
            if starts:
                info["career"] = f"{starts}: {overall.get('numberOfWins', 0)}-{overall.get('numberOfPlacings', 0)}"
            if info:
                out[int(num)] = info
        return out

    def prune(self, keep_keys: set[str]) -> None:
        for key in list(self._cache):
            if key not in keep_keys:
                self._cache.pop(key, None)
