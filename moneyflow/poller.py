"""
Async orchestrator: discover races, poll money flow, update the store, broadcast.

Two loops run concurrently:
  * discovery loop  — every `discovery_interval`, refresh the race list from the
    TAB spine and (re)build the Betfair market index for the tracked venues.
  * price loop      — every `price_interval`, snapshot the N nearest-to-jump races
    across all sources and push updates to connected WebSocket clients.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from .betfair import BetfairClient
from .config import settings
from .best_bets import BestBets
from .betr_movers import BetrMovers
from .corporate import CorporateSource
from .engine import SportsDataEngine
from .form import FormSource
from .scorer import Scorer
from .sources import (
    BetfairMatcher,
    apply_betfair_market,
    betfair_enrich,
    discover_races,
    finalize_snapshot,
    tab_snapshot,
)
from .store import Store


class Poller:
    def __init__(self, store: Store, broadcast=None) -> None:
        self.store = store
        self.broadcast = broadcast  # async callable(dict) or None
        self.engine = SportsDataEngine()
        self.betfair = BetfairClient() if settings.enable_betfair else None
        self.matcher = BetfairMatcher(self.betfair) if self.betfair else None
        self.corporate = CorporateSource() if settings.enable_corporate else None
        self.form = FormSource()
        self.betr = BetrMovers() if settings.enable_corporate else None
        self.best_bets = BestBets() if settings.enable_corporate else None
        self.scorer = Scorer(settings.scores_path)
        self._active_keys: list[str] = []
        self._running = False

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    async def start(self) -> None:
        self._running = True
        await self._discover_once()  # prime before serving
        loops = [self._discovery_loop(), self._price_loop()]
        if self.betfair:
            loops.append(self._betfair_loop())
        if self.betr:
            loops.append(self._betr_loop())
        await asyncio.gather(*loops)

    async def stop(self) -> None:
        self._running = False
        if self.betfair:
            await self.betfair.aclose()

    # ---- discovery ----

    async def _discovery_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.discovery_interval)
            try:
                await self._discover_once()
            except Exception as exc:  # keep the loop alive
                print(f"[discovery] error: {exc}")

    async def _discover_once(self) -> None:
        date = self._today()
        races = await discover_races(self.engine, date)
        if races is None:
            # Discovery fetch failed — keep the board rather than wiping it, but
            # still drop races well past the jump so a sustained outage can't grow
            # the tracked set unbounded.
            print("[discovery] fetch failed; keeping tracked races (dropping long-jumped)")
            self._prune_stale()
            return
        for ref in races:
            self.store.upsert_ref(ref)

        # Track the nearest-to-jump races at full cadence.
        races.sort(key=lambda r: r.start_time)
        active = races[: settings.max_active_races]
        self._active_keys = [r.race_key for r in active]

        # Build / refresh Betfair market index and stamp market ids onto refs.
        if self.matcher and settings.enable_betfair:
            try:
                await self.matcher.refresh_for(active)
                for r in active:
                    mid = self.matcher.market_id_for(r)
                    if mid:
                        self.store.races[r.race_key].ref.betfair_market_id = mid
            except Exception as exc:
                print(f"[discovery] betfair index error: {exc}")

        # Refresh corporate-book indices (Sportsbet / Pointsbet) for the day.
        if self.corporate:
            await self.corporate.refresh_indices(self.engine, date)
        if self.best_bets:
            await self.best_bets.refresh(self.engine)

        # Drop races that are well past the jump to keep memory bounded.
        keep = {r.race_key for r in races}
        self.store.prune(keep)
        if self.corporate:
            self.corporate.prune(keep)
        self.form.prune(keep)
        print(f"[discovery] {len(races)} races tracked, {len(active)} active @ {time.strftime('%H:%M:%S')}")

    def _prune_stale(self) -> None:
        """Drop races more than 5 min past their jump — used when discovery can't
        refresh the list (fetch failure) so the tracked set still shrinks."""
        now = datetime.now(timezone.utc).timestamp()
        keep = set()
        for key, st in self.store.races.items():
            try:
                ep = datetime.fromisoformat(st.ref.start_time.replace("Z", "+00:00")).timestamp()
            except Exception:
                ep = None
            if ep is None or ep > now - 300:
                keep.add(key)
        self.store.prune(keep)
        if self.corporate:
            self.corporate.prune(keep)
        self.form.prune(keep)

    # ---- prices ----

    async def _price_loop(self) -> None:
        while self._running:
            try:
                await self._poll_active()
            except Exception as exc:
                print(f"[price] error: {exc}")
            await asyncio.sleep(settings.price_interval)

    async def _poll_active(self) -> None:
        keys = list(self._active_keys)
        # Snapshot each active race concurrently (bounded by upstream rate limits
        # inside the engine / Betfair client).
        await asyncio.gather(*(self._poll_race(k) for k in keys))
        if self.broadcast:
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers(), "value": self.store.value(), "scores": self.scorer.stats()})

    async def _poll_race(self, race_key: str) -> None:
        st = self.store.races.get(race_key)
        if st is None:
            return
        ref = st.ref

        snap = None
        if settings.enable_tab:
            snap = await tab_snapshot(self.engine, ref)
        if snap is None:
            return

        if self.betfair and ref.betfair_market_id:
            try:
                await betfair_enrich(self.betfair, ref.betfair_market_id, snap)
            except Exception:
                pass

        if self.corporate:
            try:
                await self.corporate.enrich(self.engine, ref, snap)
            except Exception:
                pass

        try:
            await self.form.enrich(self.engine, ref, snap)
        except Exception:
            pass

        if self.betr:
            self.betr.enrich(ref, snap)   # cached dict lookup — no API call here
        if self.best_bets:
            self.best_bets.enrich(ref, snap)

        finalize_snapshot(snap)
        self.store.add_snapshot(race_key, snap)

        detail = self.store.race_detail(race_key)
        if detail:
            self.scorer.observe(race_key, detail)   # grade signals as races resolve
            if self.broadcast:
                await self.broadcast({"type": "race", "race_key": race_key, "detail": detail})

    # ---- Betr movers loop (independent, slow, never blocks Betfair) ----

    async def _betr_loop(self) -> None:
        while self._running:
            try:
                await self.betr.refresh(self.engine)
            except Exception as exc:
                print(f"[betr] error: {exc}")
            await asyncio.sleep(settings.betr_interval)

    # ---- fast Betfair loop ----

    async def _betfair_loop(self) -> None:
        """Refresh Betfair prices on the latest snapshots far faster than the tote,
        in one batched call for every active exchange market."""
        while self._running:
            await asyncio.sleep(settings.betfair_interval)
            try:
                await self._refresh_betfair()
            except Exception as exc:
                print(f"[betfair] error: {exc}")

    async def _refresh_betfair(self) -> None:
        # Map every active race that has an exchange market to its latest snapshot.
        id_to_key: dict[str, str] = {}
        for key in list(self._active_keys):
            st = self.store.races.get(key)
            if st and st.latest and st.ref.betfair_market_id:
                id_to_key[st.ref.betfair_market_id] = key
        if not id_to_key:
            return

        blocks = await self.betfair.market_prices(list(id_to_key))
        updated: set[str] = set()
        for et in blocks:
            for ev in et.get("eventNodes", []):
                for mkt in ev.get("marketNodes", []):
                    key = id_to_key.get(mkt.get("marketId"))
                    st = self.store.races.get(key) if key else None
                    if not st or not st.latest:
                        continue
                    apply_betfair_market(st.latest, mkt)
                    finalize_snapshot(st.latest)   # fair/value depend on bf mids
                    updated.add(key)

        if self.broadcast and updated:
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers(), "value": self.store.value(), "scores": self.scorer.stats()})
            for key in updated:
                detail = self.store.race_detail(key)
                if detail:
                    await self.broadcast({"type": "race", "race_key": key, "detail": detail})
