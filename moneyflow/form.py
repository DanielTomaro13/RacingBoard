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
        self._cache: dict[str, dict[int, str]] = {}  # race_key -> {number: comment}

    async def enrich(self, engine: SportsDataEngine, race: RaceRef, snapshot: RaceSnapshot) -> None:
        comments = self._cache.get(race.race_key)
        if comments is None:
            comments = await self._fetch(engine, race)
            self._cache[race.race_key] = comments   # cache even if empty (one attempt)
        if comments:
            for r in snapshot.runners:
                c = comments.get(r.number)
                if c:
                    r.comment = c

    async def _fetch(self, engine: SportsDataEngine, race: RaceRef) -> dict[int, str]:
        data = await engine.try_call(
            "tab_racing_race_form",
            date=race.date,
            raceType=race.code,
            venueMnemonic=race.venue_mnem,
            raceNumber=race.race_no,
            jurisdiction=settings.jurisdiction,
        )
        out: dict[int, str] = {}
        if not data:
            return out
        for f in data.get("form", []) or []:
            num = f.get("runnerNumber")
            comment = f.get("formComment")
            if num is not None and comment:
                out[int(num)] = comment.strip()
        return out

    def prune(self, keep_keys: set[str]) -> None:
        for key in list(self._cache):
            if key not in keep_keys:
                self._cache.pop(key, None)
