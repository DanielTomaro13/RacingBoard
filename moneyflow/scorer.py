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
from pathlib import Path
from typing import Any

from .config import settings

CATS = ["pick", "confirmed", "value", "favourite"]


def _blank() -> dict:
    return {"races": 0, **{c: {"n": 0, "won": 0, "placed": 0, "staked": 0.0, "returned": 0.0} for c in CATS}}


class Scorer:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.stake = settings.bet_stake
        self.bankroll = settings.bankroll
        self.scores = _blank()
        self._graded: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            loaded = d.get("scores")
            if isinstance(loaded, dict) and "races" in loaded:
                self.scores = loaded
            self._graded = set(d.get("graded", []))
        except Exception:
            pass
        # migrate older files that predate the P&L fields
        self.scores.setdefault("races", 0)
        for c in CATS:
            cat = self.scores.setdefault(c, {"n": 0, "won": 0, "placed": 0})
            cat.setdefault("staked", 0.0)
            cat.setdefault("returned", 0.0)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"scores": self.scores, "graded": list(self._graded)}))
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
            self._grade(sig, results)
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
        # Best available fixed price per runner — what you could realistically back at.
        prices = {}
        for r in active:
            p = r.get("corp_best") or r.get("fixed_win") or r.get("tote_win")
            if p:
                prices[r["number"]] = p
        return {
            "pick": pick.get("number") if pick else None,
            "confirmed": [r["number"] for r in active if r.get("confirmed")],
            "value": [r["number"] for r in active if (r.get("value_pct") or 0) > 0],
            "fav": active[0]["number"] if active else None,  # runners are share-sorted
            "prices": prices,
        }

    def _grade(self, sig: dict, results: list[int]) -> None:
        winner = results[0]
        placed = set(results[:3])
        prices = sig.get("prices", {})
        self.scores["races"] += 1

        def rec(cat: str, num: int | None) -> None:
            if num is None:
                return
            s = self.scores[cat]
            s["n"] += 1
            if num == winner:
                s["won"] += 1
            if num in placed:
                s["placed"] += 1
            price = prices.get(num)   # flat-stake P&L at best available price
            if price:
                s["staked"] += self.stake
                if num == winner:
                    s["returned"] += self.stake * price

        rec("pick", sig["pick"])
        rec("favourite", sig["fav"])
        for n in set(sig["confirmed"]):
            rec("confirmed", n)
        for n in set(sig["value"]):
            rec("value", n)

    # ---- view ----
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"races": self.scores["races"], "bankroll": self.bankroll, "stake": self.stake}
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
                "bets": round(staked / self.stake) if self.stake else 0,
                "roi": round(100 * profit / staked, 1) if staked else None,
                "profit": round(profit, 2),
                "bankroll": round(self.bankroll + profit, 2),
            }
        return out
