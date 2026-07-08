"""
In-memory time-series store.

Holds, per race: the RaceRef, a bounded history of RaceSnapshots, and the latest
snapshot with derived movement fields filled in. Also computes the cross-race
"movers" board (biggest money shifts across all tracked races).
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .config import settings, CODE_LABEL
from .models import RaceRef, RaceSnapshot


class RaceState:
    def __init__(self, ref: RaceRef) -> None:
        self.ref = ref
        self.history: deque[RaceSnapshot] = deque(maxlen=settings.history_len)
        self.latest: RaceSnapshot | None = None

    def add(self, snap: RaceSnapshot) -> None:
        self._fill_movement(snap)
        self.history.append(snap)
        self.latest = snap

    def _fill_movement(self, snap: RaceSnapshot) -> None:
        """Compare each runner to its first observation to derive firm/drift."""
        first = self.history[0] if self.history else None
        opens = {r.number: r for r in first.runners} if first else {}
        for r in snap.runners:
            o = opens.get(r.number)
            # pool share movement
            if o is not None and o.tote_pool_share is not None:
                r.share_open = o.tote_pool_share
                if r.tote_pool_share is not None:
                    r.share_delta = r.tote_pool_share - o.tote_pool_share
            elif r.tote_pool_share is not None:
                r.share_open = r.tote_pool_share
                r.share_delta = 0.0
            # price movement (prefer tote, fall back to fixed): <0 == firming
            cur = r.tote_win or r.fixed_win
            base = None
            if o is not None:
                base = o.tote_win or o.fixed_win
            if cur and base:
                r.price_move_pct = (cur - base) / base * 100.0
            # direction from whichever signal we have
            move = r.share_delta if r.share_delta is not None else (
                -(r.price_move_pct or 0) / 100.0
            )
            if move is None:
                r.direction = "flat"
            elif move > 0.004:
                r.direction = "firming"
            elif move < -0.004:
                r.direction = "drifting"
            else:
                r.direction = "flat"

    def sparkline(self, runner_number: int, field: str = "tote_pool_share") -> list[float | None]:
        out: list[float | None] = []
        for snap in self.history:
            val = None
            for r in snap.runners:
                if r.number == runner_number:
                    val = getattr(r, field, None)
                    break
            out.append(val)
        return out


class Store:
    def __init__(self) -> None:
        self.races: dict[str, RaceState] = {}

    def upsert_ref(self, ref: RaceRef) -> RaceState:
        st = self.races.get(ref.race_key)
        if st is None:
            st = RaceState(ref)
            self.races[ref.race_key] = st
        else:
            # keep betfair market id / freshest metadata
            if ref.betfair_market_id:
                st.ref.betfair_market_id = ref.betfair_market_id
        return st

    def add_snapshot(self, race_key: str, snap: RaceSnapshot) -> None:
        st = self.races.get(race_key)
        if st is not None:
            st.add(snap)

    def prune(self, keep_keys: set[str]) -> None:
        for key in list(self.races):
            if key not in keep_keys:
                del self.races[key]

    # ---- views for the API ----

    def board(self) -> list[dict[str, Any]]:
        """One row per race for the overview board, sorted by start time."""
        rows = []
        for st in self.races.values():
            snap = st.latest
            top = None
            top_mover = None
            if snap:
                active = [r for r in snap.runners if not r.scratched]
                if active:
                    top = max(
                        active,
                        key=lambda r: (r.tote_pool_share or r.bf_implied or 0),
                    )
                    movers = [r for r in active if r.share_delta is not None]
                    if movers:
                        top_mover = max(movers, key=lambda r: abs(r.share_delta))
            rows.append(
                {
                    "race_key": st.ref.race_key,
                    "code": st.ref.code,
                    "code_label": CODE_LABEL.get(st.ref.code, st.ref.code),
                    "venue": st.ref.venue,
                    "race_no": st.ref.race_no,
                    "race_name": st.ref.race_name,
                    "start_time": st.ref.start_time,
                    "status": snap.status if snap else "PENDING",
                    "has_betfair": bool(st.ref.betfair_market_id),
                    "bf_total_matched": snap.bf_total_matched if snap else None,
                    "tote_win_pool": snap.tote_win_pool if snap else None,
                    "favourite": _runner_brief(top),
                    "top_mover": _runner_brief(top_mover),
                }
            )
        rows.sort(key=lambda r: r["start_time"])
        return rows

    def movers(self, limit: int = 20) -> list[dict[str, Any]]:
        """Biggest pool-share shifts across every tracked race."""
        out = []
        for st in self.races.values():
            snap = st.latest
            if not snap:
                continue
            for r in snap.runners:
                if r.scratched or r.share_delta is None or abs(r.share_delta) < 0.005:
                    continue
                out.append(
                    {
                        "race_key": st.ref.race_key,
                        "venue": st.ref.venue,
                        "code": st.ref.code,
                        "race_no": st.ref.race_no,
                        "start_time": st.ref.start_time,
                        "runner": r.name,
                        "number": r.number,
                        "direction": r.direction,
                        "share": r.tote_pool_share,
                        "share_delta": r.share_delta,
                        "price_move_pct": r.price_move_pct,
                    }
                )
        out.sort(key=lambda x: abs(x["share_delta"]), reverse=True)
        return out[:limit]

    def race_detail(self, race_key: str) -> dict[str, Any] | None:
        st = self.races.get(race_key)
        if st is None or st.latest is None:
            return None
        snap = st.latest
        runners = sorted(
            snap.runners,
            key=lambda r: (r.tote_pool_share or r.bf_implied or 0),
            reverse=True,
        )
        return {
            "ref": st.ref.to_dict(),
            "status": snap.status,
            "ts": snap.ts,
            "bf_total_matched": snap.bf_total_matched,
            "tote_win_pool": snap.tote_win_pool,
            "runners": [
                {
                    **r.to_dict(),
                    "share_spark": st.sparkline(r.number, "tote_pool_share"),
                }
                for r in runners
            ],
        }


def _runner_brief(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "number": r.number,
        "name": r.name,
        "share": r.tote_pool_share,
        "share_delta": r.share_delta,
        "direction": r.direction,
    }
