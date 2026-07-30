"""
Signal scoreboard — grades the tool's signals against actual results.

As each race resolves, the runners it flagged just before the jump (the pick, the
✓-confirmed steamers, the value bets) are graded against the finishing order, and
a running win/place hit-rate accumulates — persisted across sessions. The market
favourite is graded too as a baseline: a signal only "works" if it beats the fav.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .config import settings
from .kelly import kelly_stake

CATS = ["pick", "confirmed", "value", "favourite"]


def _blank() -> dict:
    return {"races": 0, **{c: {"n": 0, "won": 0, "placed": 0, "bets": 0, "staked": 0.0, "returned": 0.0} for c in CATS}}


class Scorer:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.kelly_fraction = settings.kelly_fraction
        self.kelly_cap = settings.kelly_cap
        self.bankroll = settings.bankroll
        self.scores = _blank()
        self._graded: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._load()

    # ---- persistence ----
    SCHEMA = 2  # v2 = half-Kelly staking (v1 P&L was flat-stake, not comparable)

    def _load(self) -> None:
        ver = self.SCHEMA
        try:
            d = json.loads(self.path.read_text())
            loaded = d.get("scores")
            if isinstance(loaded, dict) and "races" in loaded:
                self.scores = loaded
            self._graded = set(d.get("graded", []))
            ver = d.get("v", 1)
        except Exception:
            pass
        self.scores.setdefault("races", 0)
        for c in CATS:
            cat = self.scores.setdefault(c, {"n": 0, "won": 0, "placed": 0})
            cat.setdefault("bets", 0)
            cat.setdefault("staked", 0.0)
            cat.setdefault("returned", 0.0)
            # Pre-Kelly P&L was accumulated at flat $1 and can't be recomputed under
            # Kelly (fair prices weren't stored). Reset the money fields so ROI/P&L
            # reflect pure half-Kelly; keep the staking-independent hit-rate history.
            if ver < 2:
                cat["bets"] = 0
                cat["staked"] = 0.0
                cat["returned"] = 0.0

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic temp-file + rename, same as the follow ledger — a crash
            # mid-write must not truncate the running history.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"v": self.SCHEMA, "scores": self.scores, "graded": list(self._graded)}))
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- observe / grade ----
    def observe(self, race_key: str, detail: dict[str, Any]) -> None:
        """Called with each race_detail. Captures pre-jump signals while OPEN and
        grades once the race resolves (idempotent per race)."""
        status = detail.get("status")
        results = detail.get("results")
        if status == "RESULTED" and results:
            if race_key in self._graded:
                return
            sig = self._pending.get(race_key) or self._extract(detail)
            self._grade(sig, results, detail.get("winners"))
            self._graded.add(race_key)
            self._pending.pop(race_key, None)
            # Keep the (blocking) disk write off the event loop.
            try:
                asyncio.get_running_loop().run_in_executor(None, self._save)
            except RuntimeError:
                self._save()
        elif status == "OPEN":
            self._pending[race_key] = self._extract(detail)

    def _extract(self, detail: dict[str, Any]) -> dict:
        runners = detail.get("runners", [])
        active = [r for r in runners if not r.get("scratched")]
        pick = detail.get("pick")
        # Best available fixed price per runner (what you could back at) + the tool's
        # de-vig fair price (its win-probability estimate) — both needed to size the
        # Kelly stake at grade time.
        prices, fair = {}, {}
        for r in active:
            p = r.get("corp_best") or r.get("fixed_win") or r.get("tote_win")
            if p:
                prices[r["number"]] = p
            fp = r.get("fair_price")
            if fp:
                fair[r["number"]] = fp
        return {
            "pick": pick.get("number") if pick else None,
            "confirmed": [r["number"] for r in active if r.get("confirmed")],
            "value": [r["number"] for r in active if (r.get("value_pct") or 0) > 0],
            "fav": active[0]["number"] if active else None,  # runners are share-sorted
            "prices": prices,
            "fair": fair,
        }

    def _kelly_stake(self, cat_stats: dict, price: float, fair: float) -> float:
        """Half-Kelly stake off the running per-category bankroll. p = 1/fair (our
        estimate), odds = best available price. 0 when there's no positive edge."""
        bank = self.bankroll + (cat_stats["returned"] - cat_stats["staked"])
        p = (1.0 / fair) if fair else None
        return kelly_stake(bank, p, price, self.kelly_fraction, self.kelly_cap)

    def _grade(self, sig: dict, results: list[int], win_group: list[int] | None = None) -> None:
        # Whole first group wins — >1 runner on a dead-heat (flat stake pays in full;
        # good enough for a signal scorecard).
        winners = set(win_group) if win_group else {results[0]}
        placed = set(results[:3])
        prices = sig.get("prices", {})
        fair = sig.get("fair", {})
        self.scores["races"] += 1

        def rec(cat: str, num: int | None) -> None:
            if num is None:
                return
            s = self.scores[cat]
            s["n"] += 1
            if num in winners:
                s["won"] += 1
            if num in placed:
                s["placed"] += 1
            # Half-Kelly P&L: stake sized by edge (fair vs best price), win at price.
            stake = self._kelly_stake(s, prices.get(num), fair.get(num))
            if stake > 0:
                s["bets"] += 1
                s["staked"] += stake
                if num in winners:
                    s["returned"] += stake * prices[num]

        rec("pick", sig["pick"])
        rec("favourite", sig["fav"])
        for n in set(sig["confirmed"]):
            rec("confirmed", n)
        for n in set(sig["value"]):
            rec("value", n)

    # ---- view ----
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "races": self.scores["races"],
            "bankroll": self.bankroll,
            "staking": "half-kelly",
            "kelly_fraction": self.kelly_fraction,
        }
        for c in CATS:
            s = self.scores[c]
            staked = s.get("staked", 0.0)
            returned = s.get("returned", 0.0)
            profit = returned - staked
            out[c] = {
                "n": s["n"],
                "won": s["won"],
                "placed": s["placed"],
                "win_pct": round(100 * s["won"] / s["n"], 1) if s["n"] else None,
                "place_pct": round(100 * s["placed"] / s["n"], 1) if s["n"] else None,
                "bets": s.get("bets", 0),
                "staked": round(staked, 2),
                "roi": round(100 * profit / staked, 1) if staked else None,
                "profit": round(profit, 2),
                "bankroll": round(self.bankroll + profit, 2),
            }
        return out
