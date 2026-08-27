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
from .datalog import DataLogger
from .db import DB
from .engine import SportsDataEngine
from .follow import FollowLedger
from .form import FormSource
from .notify import DiscordNotifier
from .scorer import Scorer
from .sources import (
    BetfairMatcher,
    apply_betfair_market,
    betfair_bootstrap_snapshot,
    discover_races_betfair,
    discover_races,
    finalize_snapshot,
    tab_snapshot,
)
from .store import Store


class Poller:
    def __init__(self, store: Store, broadcast=None, subscribed=None) -> None:
        self.store = store
        self.broadcast = broadcast  # async callable(dict) or None
        self.subscribed = subscribed  # callable()->set[race_key] of races clients view
        self.engine = SportsDataEngine()
        self.betfair = BetfairClient() if settings.enable_betfair else None
        self.matcher = BetfairMatcher(self.betfair) if self.betfair else None
        self.corporate = CorporateSource() if settings.enable_corporate else None
        self.form = FormSource()
        self.betr = BetrMovers() if settings.enable_corporate else None
        self.best_bets = BestBets() if settings.enable_corporate else None
        self.scorer = Scorer(settings.scores_path)
        self.follow = FollowLedger(settings.follow_path)
        self.notifier = DiscordNotifier(settings.discord_webhook) if settings.discord_webhook else None
        if self.notifier:
            self.follow.on_event = self.notifier.enqueue
        self.db = DB(settings.db_path) if settings.enable_datalog else None
        self.datalog = DataLogger(self.db) if self.db else None
        # Slow-changing training/model status for the site — refreshed on the
        # discovery cadence, never per-broadcast (it queries the whole DB).
        self.training: dict | None = self.datalog.overview() if self.datalog else None
        self.next_up: dict | None = None   # earliest race beyond the horizon
        self._active_keys: list[str] = []
        self._running = False

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    async def start(self) -> None:
        self._running = True
        if self.notifier:
            self.notifier.start()   # queue worker needs the running loop
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
        if self.notifier:
            await self.notifier.stop()
        if self.db:
            self.db.close()

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
        res = await discover_races(self.engine, date)
        if res is None:
            # TAB refused us. The exchange is public and keyless — fall back to
            # Betfair's navigation graph so the board stays alive (money-only:
            # no tote, no corporate prices, until the spine returns).
            if self.betfair:
                try:
                    bf_races = await discover_races_betfair(
                        self.betfair, settings.horizon_minutes)
                except Exception as exc:
                    bf_races = []
                    print(f"[discovery] betfair fallback error: {exc}")
                if bf_races:
                    print(f"[discovery] TAB down — Betfair fallback tracking {len(bf_races)} races")
                    races, self.next_up = bf_races, None
                else:
                    print("[discovery] fetch failed; keeping tracked races (dropping long-jumped)")
                    self._prune_stale()
                    return
            else:
                print("[discovery] fetch failed; keeping tracked races (dropping long-jumped)")
                self._prune_stale()
                return
        else:
            races, self.next_up = res
        for ref in races:
            self.store.upsert_ref(ref)

        # Track the nearest-to-jump races at full cadence — PLUS any already-jumped
        # race whose result hasn't arrived yet. Those must stay polled or the
        # ledger/scorecard/outcomes never settle (results post minutes after the off).
        races.sort(key=lambda r: r.start_time)
        now = time.time()

        def _ep(r) -> float:
            try:
                return datetime.fromisoformat(r.start_time.replace("Z", "+00:00")).timestamp()
            except Exception:
                return now

        upcoming = [r for r in races if _ep(r) > now - 120]
        awaiting = []
        for r in races:
            if _ep(r) > now - 120:
                continue
            st = self.store.races.get(r.race_key)
            if st is None or st.latest is None or not st.latest.results:
                awaiting.append(r)   # jumped, no result yet — keep polling
        active = awaiting[:15] + upcoming[: settings.max_active_races]
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
        if self.datalog:
            self.datalog.prune(keep)
            try:
                self.training = self.datalog.overview()
            except Exception as exc:
                print(f"[datalog] overview error: {exc}")
        # Settle any ledger entry whose race resolved after it left the board.
        try:
            await self.follow.reconcile(self.engine)
        except Exception as exc:
            print(f"[follow] reconcile error: {exc}")
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
        if self.datalog:
            self.datalog.prune(keep)

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
        # return_exceptions: one race's fault must not abort the others' snapshots
        # nor the board broadcast for this cycle.
        results = await asyncio.gather(*(self._poll_race(k) for k in keys), return_exceptions=True)
        for k, res in zip(keys, results):
            if isinstance(res, Exception):
                print(f"[price] {k} poll error: {res}")
        if self.broadcast:
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers(), "value": self.store.value(),
                                  "firm": self.store.firm(), "next_up": self.next_up,
                                  "scores": self.scorer.stats(), "follows": self.follow.stats()})

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

        # Carry Betfair forward from the previous snapshot instead of making a
        # per-race exchange call here — the fast Betfair loop (every ~3s) already
        # batches ALL markets in one call and refreshes st.latest. This drops N
        # un-batched Betfair HTTP calls every price cycle with no visible gap.
        if self.betfair and ref.betfair_market_id and st.latest is not None:
            prev = {r.number: r for r in st.latest.runners}
            snap.bf_total_matched = st.latest.bf_total_matched
            for r in snap.runners:
                p = prev.get(r.number)
                if p:
                    r.bf_back, r.bf_lay, r.bf_last = p.bf_back, p.bf_lay, p.bf_last
                    r.bf_wom, r.bf_implied = p.bf_wom, p.bf_implied

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
            # Bookkeeping consumers must never break the poll cycle for a race.
            for name, obs in (("scorer", self.scorer), ("follow", self.follow),
                              ("datalog", self.datalog)):
                if obs is None:
                    continue
                try:
                    obs.observe(race_key, detail)
                except Exception as exc:
                    print(f"[{name}] observe error for {race_key}: {exc}")
            if self.broadcast and self._is_viewed(race_key):
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
            # latest may be None in TAB-down mode — the loop bootstraps those
            if st and st.ref.betfair_market_id:
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
                    if not st:
                        continue
                    if not st.latest:
                        # TAB-down mode: no tote snapshot exists — bootstrap one
                        # from the exchange alone so the race has a board row.
                        boot = betfair_bootstrap_snapshot(mkt)
                        if boot is None:
                            continue
                        finalize_snapshot(boot)
                        st.add(boot)
                        updated.add(key)
                        continue
                    apply_betfair_market(st.latest, mkt)
                    finalize_snapshot(st.latest)   # fair/value depend on bf mids
                    updated.add(key)

        if self.broadcast and updated:
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers(), "value": self.store.value(),
                                  "firm": self.store.firm(), "next_up": self.next_up,
                                  "scores": self.scorer.stats(), "follows": self.follow.stats()})
            # Only build+send the heavy per-runner detail for races clients are
            # actually viewing — not all ~24 active markets every 3s.
            for key in updated:
                if not self._is_viewed(key):
                    continue
                detail = self.store.race_detail(key)
                if detail:
                    await self.broadcast({"type": "race", "race_key": key, "detail": detail})

    def _is_viewed(self, race_key: str) -> bool:
        if self.subscribed is None:      # no hub (e.g. capture script) → send all
            return True
        return race_key in self.subscribed()
