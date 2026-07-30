"""
Follower ledger — an append-only, IMMUTABLE record of committed selections.

When a runner clears a strict bar (§ criteria below) it is *committed*: a real,
half-Kelly-staked, permanent entry. Once committed it can never be removed or
re-priced — the only mutation allowed is the one-time pending→settled transition
when the race resolves. Because losers can't be pruned, the running P&L is an honest
track record, which is the whole point. Immutability is why the entry bar is strict.

Entry criteria (must clear ALL, evaluated on the race's own recommended pick):
  * the race hasn't jumped yet (no minimum time-to-jump — strong multi-market steam
    is a near-jump event, so gating it an hour out simply never triggers);
  * the pick has ≥ follow_min_confirm markets confirming the steam;
  * the pick shows a positive value edge (best price > fair);
  * best price within a sane band (skip odds-on and roughies);
  * one selection per race, ever (de-duped by race_key).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .kelly import kelly_stake


def _minutes_to_jump(start_time: str | None) -> float | None:
    if not start_time:
        return None
    try:
        jump = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except Exception:
        return None
    return (jump - datetime.now(timezone.utc)).total_seconds() / 60.0


class FollowLedger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.bankroll = settings.bankroll
        self.kelly_fraction = settings.kelly_fraction
        self.kelly_cap = settings.kelly_cap
        self.entries: list[dict] = []
        self._committed: set[str] = set()   # race_keys already committed (dedup)
        self._reconciled: dict[str, float] = {}   # race_key -> last backfill attempt
        # Optional observer called as on_event(kind, entry, stats) after every
        # commit ('commit') and settlement ('won'/'lost'/'void') — e.g. Discord.
        self.on_event = None
        self._load()

    def _emit(self, kind: str, entry: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, entry, self.stats(limit=0))
        except Exception as exc:
            print(f"[follow] on_event error: {exc}")

    # ---- persistence (must be wipe-proof: this file IS the permanent record) ----
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d.get("entries"), list):
                self.entries = d["entries"]
            self._committed = {e["race_key"] for e in self.entries}
        except Exception:
            # A corrupt/unreadable ledger must never be silently replaced by an
            # empty one — preserve the evidence and refuse to write over it.
            bad = self.path.with_suffix(f".bad-{int(time.time())}")
            try:
                os.replace(self.path, bad)
                print(f"[follow] ledger unreadable — preserved as {bad.name}")
            except OSError:
                self._frozen = True   # can't even move it: stop persisting entirely
                print("[follow] ledger unreadable and could not be preserved — "
                      "persistence FROZEN to avoid overwriting it")

    def _save(self) -> None:
        if getattr(self, "_frozen", False):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: write a temp file, then rename over the old one — a crash
            # mid-write can never leave a truncated/partial ledger behind.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": 1, "entries": self.entries}))
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[follow] save failed: {exc}")

    def _persist(self) -> None:
        try:
            asyncio.get_running_loop().run_in_executor(None, self._save)
        except RuntimeError:
            self._save()

    @property
    def _realized(self) -> float:
        return sum(e["pnl"] for e in self.entries if e.get("pnl") is not None)

    # ---- observe / commit / settle ----
    def observe(self, race_key: str, detail: dict[str, Any]) -> None:
        """Called with each race_detail: commit while OPEN if the pick clears the
        bar (idempotent per race), settle once the race resolves."""
        status = detail.get("status")
        results = detail.get("results")
        if status == "RESULTED" and results:
            self._settle(race_key, detail, results)
        elif status == "OPEN" and race_key not in self._committed:
            self._maybe_commit(race_key, detail)

    def _maybe_commit(self, race_key: str, detail: dict[str, Any]) -> None:
        pick = detail.get("pick")
        if not pick or pick.get("number") is None:
            return
        mins = _minutes_to_jump((detail.get("ref") or {}).get("start_time"))
        if mins is None or mins < settings.follow_min_minutes:
            return
        if (pick.get("confirm") or 0) < settings.follow_min_confirm:
            return
        value = pick.get("value_pct")
        if value is None or value <= settings.follow_min_value:
            return
        fair = pick.get("fair_price")
        price = pick.get("corp_best")
        if not fair or not price:
            return
        if price < settings.follow_price_min or price > settings.follow_price_max:
            return
        stake = kelly_stake(self.bankroll + self._realized, 1.0 / fair, price,
                            self.kelly_fraction, self.kelly_cap)
        if stake <= 0:
            return

        ref = detail.get("ref") or {}
        self.entries.append({
            "race_key": race_key,
            "number": pick["number"],
            "selection": pick.get("name"),
            "code": ref.get("code"),
            "venue": ref.get("venue"),
            "race_no": ref.get("race_no"),
            "jump_time": ref.get("start_time"),
            "committed_at": detail.get("ts"),
            "minutes_out": round(mins),
            "trigger": "confirmed+value",
            "confirm": pick.get("confirm"),
            "edge_pct": round(value, 1),
            "entry_price": price,
            "fair_price": fair,
            "p_firm": round(1.0 / fair, 3),
            "kelly_fraction": self.kelly_fraction,
            "stake": stake,
            "status": "pending",
            "settled_price": None,
            "settled_at": None,
            "pnl": None,
        })
        self._committed.add(race_key)
        self._persist()
        self._emit("commit", self.entries[-1])

    def _settle(self, race_key: str, detail: dict[str, Any], results: list[int]) -> None:
        pending = [e for e in self.entries if e["race_key"] == race_key and e["status"] == "pending"]
        if not pending:
            return
        winners = set(detail.get("winners") or results[:1])   # dead-heat aware
        runners = {r.get("number"): r for r in detail.get("runners", [])}
        for e in pending:
            num = e["number"]
            r = runners.get(num) or {}
            # Closing price (for CLV): last known backable price at resolution.
            close = r.get("corp_best") or r.get("fixed_win") or r.get("tote_win")
            e["settled_price"] = close
            e["settled_at"] = detail.get("ts")
            if r.get("scratched"):
                e["status"], e["pnl"] = "void", 0.0     # refund — never delete
            elif num in winners:
                e["status"] = "won"
                e["pnl"] = round(e["stake"] * (e["entry_price"] - 1), 2)
            else:
                e["status"], e["pnl"] = "lost", round(-e["stake"], 2)
        self._persist()
        for e in pending:
            self._emit(e["status"], e)

    # ---- reconcile: settle entries whose race left the board unresolved ----
    async def reconcile(self, engine) -> None:
        """Backfill settlement for pending entries whose races are no longer being
        polled (result posted after the race was pruned, or across a restart).
        Fetches the race once per discovery cycle until the result appears."""
        from .models import RaceRef
        from .sources import tab_snapshot

        now = datetime.now(timezone.utc).timestamp()
        for e in self.entries:
            if e["status"] != "pending":
                continue
            mins = _minutes_to_jump(e.get("jump_time"))
            if mins is None or mins > -5:      # give the live path 5 min to settle it
                continue
            last = self._reconciled.get(e["race_key"], 0)
            if now - last < 120:               # at most one fetch per 2 min per race
                continue
            self._reconciled[e["race_key"]] = now
            try:
                code, mnem, no, date = e["race_key"].split(":")
                ref = RaceRef(race_key=e["race_key"], code=code, venue=e.get("venue") or mnem,
                              venue_mnem=mnem, race_no=int(no), race_name="",
                              start_time=e.get("jump_time") or "", date=date)
                snap = await tab_snapshot(engine, ref)
            except Exception:
                continue
            if snap is None or not snap.results:
                continue
            self._settle(e["race_key"], {
                "ts": snap.ts,
                "winners": snap.winners,
                "runners": [r.to_dict() for r in snap.runners],
            }, snap.results)
            print(f"[follow] reconciled {e['race_key']} -> {e['status']}")

    # ---- view ----
    def stats(self, limit: int = 40) -> dict[str, Any]:
        settled = [e for e in self.entries if e["status"] in ("won", "lost")]
        staked = sum(e["stake"] for e in settled)
        profit = sum(e["pnl"] for e in settled if e.get("pnl") is not None)
        # CLV: a back bet beats the close when entry price > closing price.
        clvs = [100 * (e["entry_price"] / e["settled_price"] - 1)
                for e in self.entries
                if e["status"] in ("won", "lost") and e.get("settled_price")]
        counts = {k: 0 for k in ("pending", "won", "lost", "void")}
        for e in self.entries:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        recent = sorted(self.entries, key=lambda e: e.get("committed_at") or 0, reverse=True)[:limit]
        return {
            "bankroll": self.bankroll,
            "kelly_fraction": self.kelly_fraction,
            "n": len(self.entries),
            "pending": counts["pending"],
            "won": counts["won"],
            "lost": counts["lost"],
            "void": counts["void"],
            "staked": round(staked, 2),
            "profit": round(profit, 2),
            "roi": round(100 * profit / staked, 1) if staked else None,
            "current_bankroll": round(self.bankroll + profit, 2),
            "clv_pct": round(sum(clvs) / len(clvs), 1) if clvs else None,
            "entries": recent,
        }
