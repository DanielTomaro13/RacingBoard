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
        """Compare each runner to its first observation to derive firm/drift,
        plus a recent-window delta to capture live momentum (moving NOW)."""
        first = self.history[0] if self.history else None
        opens = {r.number: r for r in first.runners} if first else {}

        # Reference snapshot ~recent_window seconds ago, for live momentum.
        recent_ref = None
        if self.history:
            target = snap.ts - settings.recent_window
            for h in self.history:            # oldest → newest
                if h.ts <= target:
                    recent_ref = h
                else:
                    break
            if recent_ref is None:
                recent_ref = self.history[0]
        recents = {r.number: r for r in recent_ref.runners} if recent_ref else {}

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
            # recent-window momentum (how fast it's moving right now)
            rr = recents.get(r.number)
            if rr is not None and rr.tote_pool_share is not None and r.tote_pool_share is not None:
                r.share_delta_recent = r.tote_pool_share - rr.tote_pool_share
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

    def sparklines(self, field: str = "tote_pool_share", target: int = 80) -> dict[int, list[float | None]]:
        """All runners' sparklines in ONE pass (was O(history × runners²) when
        called per-runner). Downsampled to ~`target` points so the payload and
        cost stay bounded as history fills, while preserving the trend shape."""
        hist = list(self.history)
        if len(hist) > target:
            step = len(hist) / target
            hist = [hist[min(int(i * step), len(hist) - 1)] for i in range(target)]
        nums: set[int] = set()
        for snap in hist:
            for r in snap.runners:
                nums.add(r.number)
        out: dict[int, list[float | None]] = {n: [] for n in nums}
        for snap in hist:
            by_num = {r.number: getattr(r, field, None) for r in snap.runners}
            for n in nums:
                out[n].append(by_num.get(n))
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
            fav = pick = None
            n_conf = 0
            if snap:
                active = [r for r in snap.runners if not r.scratched]
                if active:
                    fav = max(active, key=lambda r: (r.tote_pool_share or r.bf_implied or 0))
                    pick = _pick(active)
                    n_conf = sum(1 for r in active if _confirmed(r))
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
                    "favourite": _runner_brief(fav),
                    "pick": pick,
                    "confirmed_count": n_conf,
                    "result_winner": snap.results[0] if snap and snap.results else None,
                }
            )
        rows.sort(key=lambda r: r["start_time"])
        return rows

    def movers(self, limit: int = 24) -> list[dict[str, Any]]:
        """Runners whose price is SHORTENING (money coming in) across all races.

        Drifters are intentionally excluded — we only care where money is going.
        """
        out = []
        for st in self.races.values():
            snap = st.latest
            if not snap:
                continue
            for r in snap.runners:
                # firming == pool share rising == price shortening == money in.
                if r.scratched or r.direction != "firming" or (r.share_delta or 0) < 0.006:
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
                        "share_delta_recent": r.share_delta_recent,
                        "live": bool(r.share_delta_recent and r.share_delta_recent > 0.006),
                        "confirm": _confirm_count(r), "confirmed": _confirmed(r),
                        "price_move_pct": r.price_move_pct,
                        "fair_price": r.fair_price,
                        "corp_best": r.corp_best,
                        "value_pct": r.value_pct,
                    }
                )
        # Live movers (moving now) first, then by cumulative move.
        out.sort(key=lambda x: (x["live"], x.get("share_delta_recent") or 0, x["share_delta"]), reverse=True)
        return out[:limit]

    def value(self, limit: int = 24) -> list[dict[str, Any]]:
        """Runners whose best book price is longer than fair (overlays), across
        all races — ranked by edge. Same idea as the firmers list, for value."""
        out = []
        for st in self.races.values():
            snap = st.latest
            if not snap:
                continue
            for r in snap.runners:
                if r.scratched or not r.value_pct or r.value_pct <= 0:
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
                        "value_pct": r.value_pct,
                        "corp_best": r.corp_best,
                        "corp_best_book": r.corp_best_book,
                        "fair_price": r.fair_price,
                        "direction": r.direction,
                        "share_delta": r.share_delta,
                    }
                )
        out.sort(key=lambda x: x["value_pct"], reverse=True)
        return out[:limit]

    def race_detail(self, race_key: str) -> dict[str, Any] | None:
        st = self.races.get(race_key)
        if st is None or st.latest is None:
            return None
        snap = st.latest
        active = [r for r in snap.runners if not r.scratched]
        runners = sorted(
            snap.runners,
            key=lambda r: (r.tote_pool_share or r.bf_implied or 0),
            reverse=True,
        )
        bf_flow = _betfair_flow(st)   # estimated Betfair $ per runner since open
        sparks = st.sparklines("tote_pool_share")   # all runners in one history pass
        return {
            "ref": st.ref.to_dict(),
            "status": snap.status,
            "ts": snap.ts,
            "bf_total_matched": snap.bf_total_matched,
            "tote_win_pool": snap.tote_win_pool,
            "tips": snap.tips,
            "comment": snap.comment,
            "results": snap.results,
            "pick": _pick(active),
            "runners": [
                {
                    **r.to_dict(),
                    "share_spark": sparks.get(r.number, []),
                    "bf_money_est": bf_flow.get(r.number),
                    "confirm": _confirm_count(r), "confirmed": _confirmed(r),
                }
                for r in runners
            ],
        }


def _confirm_count(r: Any) -> int:
    """How many independent markets agree this runner is shortening — each counts
    separately: the tote (pool share rising), the Betfair exchange (weight of money
    >= 55% backing), each corporate book shortening since open (Sportsbet,
    Pointsbet), and Betr's book-wide mover feed. More markets = realer steam."""
    c = 0
    if r.direction == "firming":
        c += 1
    if r.bf_wom is not None and r.bf_wom >= 0.55:
        c += 1
    c += len(r.corp_short or [])
    if r.betr_short:
        c += 1
    return c


def _confirmed(r: Any) -> bool:
    return _confirm_count(r) >= 2


def _betfair_flow(st: "RaceState") -> dict[int, float]:
    """Estimate where the Betfair money has gone, per runner, since we started
    watching this market.

    Betfair's public feed hides per-runner matched volume — but it gives the
    market-level total matched and every runner's price. So: the extra money
    matched since open (ΔM = matched_now − matched_open) must have gone to the
    runners whose price SHORTENED, in proportion to how much their implied
    probability rose. It's an estimate (total-matched counts churn/both sides,
    and assumes moves are money-driven), but it's directionally honest.
    """
    latest = st.latest
    if latest is None:
        return {}
    # "Open" = the earliest snapshot that actually has Betfair data. history[0] is
    # usually pre-Betfair (the market gets stamped/enriched a poll or two later),
    # so keying off it would leave this permanently blank.
    first = next((h for h in st.history if h.bf_total_matched), None)
    if first is None:
        return {}
    m_now, m_open = latest.bf_total_matched, first.bf_total_matched
    if not m_now or not m_open:
        return {}
    delta_matched = m_now - m_open
    if delta_matched <= 0:
        return {}
    opens = {r.number: r.bf_implied for r in first.runners if r.bf_implied}
    gains: dict[int, float] = {}
    for r in latest.runners:
        o, n = opens.get(r.number), r.bf_implied
        if o and n and n > o:          # implied prob rose == price shortened
            gains[r.number] = n - o
    total_gain = sum(gains.values())
    if total_gain <= 0:
        return {}
    return {num: delta_matched * g / total_gain for num, g in gains.items()}


def _pick(active: list[Any]) -> dict[str, Any] | None:
    """Recommended runner: the one with the most money coming in (biggest pool-
    share gain = shortening hardest). Falls back to the market favourite, clearly
    flagged, before any real move has happened."""
    if not active:
        return None
    movers = [r for r in active if (r.share_delta or 0) > 0.008]
    if movers:
        r = max(movers, key=lambda r: r.share_delta)
        if r.share_delta > 0.05:
            conf = "STRONG"
        elif r.share_delta > 0.02:
            conf = "FIRMING"
        else:
            conf = "EDGING IN"
        reason = "money in"
    else:
        r = max(active, key=lambda r: (r.tote_pool_share or 0))
        conf = "NO MOVER YET"
        reason = "market fav"
    brief = _runner_brief(r) or {}
    brief.update({
        "reason": reason,
        "confidence": conf,
        "fair_price": r.fair_price,
        "corp_best": r.corp_best,
        "corp_best_book": r.corp_best_book,
        "value_pct": r.value_pct,
        "price_move_pct": r.price_move_pct,
        "share_delta_recent": r.share_delta_recent,
        "live": bool(r.share_delta_recent and r.share_delta_recent > 0.006),
        "confirm": _confirm_count(r), "confirmed": _confirmed(r),
    })
    return brief


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
