"""
Discord notifications for the follower ledger.

Every ledger event posts a rich embed to the configured webhook channel:
  🔒 commit  — runner, race, jump time, entry price vs fair, edge, confirmations,
               ½-Kelly stake, and the ledger's running bank/record at commit time.
  ✅/❌/⚪ settle — result, P&L, closing price + this bet's CLV, and the updated
               bankroll, ROI, CLV and W-L record.

Delivery is a small async queue + worker so a slow/unreachable Discord can never
block or crash the poller; on 429 the worker honours retry_after once, then drops.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

AMBER, GREEN, RED, GREY = 0xFFB000, 0x21D16B, 0xFF4D4F, 0x6A6A76
CODE_NAME = {"R": "Thoroughbred", "G": "Greyhounds", "H": "Harness"}


def _jump_str(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        mins = (dt - datetime.now(timezone.utc).astimezone()).total_seconds() / 60
        rel = f"in {mins:.0f}m" if mins > 0 else f"{-mins:.0f}m ago"
        return f"{dt.strftime('%H:%M')} ({rel})"
    except Exception:
        return iso


def _race_line(e: dict) -> str:
    return f"{CODE_NAME.get(e.get('code'), e.get('code'))} · **{e.get('venue')} R{e.get('race_no')}**"


def _ledger_line(s: dict) -> str:
    roi = f"{s['roi']:+.1f}%" if s.get("roi") is not None else "–"
    clv = f"{s['clv_pct']:+.1f}%" if s.get("clv_pct") is not None else "–"
    return (f"bank **${s['current_bankroll']}** · {s['won']}W-{s['lost']}L"
            f"{'-' + str(s['void']) + 'V' if s.get('void') else ''} · "
            f"{s['pending']} live · ROI **{roi}** · CLV {clv}")


def build_embed(kind: str, e: dict, s: dict) -> dict:
    """kind: 'commit' | 'won' | 'lost' | 'void'."""
    sel = f"#{e.get('number')} {e.get('selection')}"
    if kind == "commit":
        return {
            "title": f"🔒 COMMITTED — {sel}",
            "color": AMBER,
            "description": _race_line(e) + f" · jumps {_jump_str(e.get('jump_time'))}",
            "fields": [
                {"name": "Entry", "value": f"**${e.get('entry_price')}** best (fair {e.get('fair_price')})", "inline": True},
                {"name": "Edge", "value": f"+{e.get('edge_pct')}%", "inline": True},
                {"name": "Steam", "value": f"{e.get('confirm')}✓ markets", "inline": True},
                {"name": "Stake", "value": f"**${e.get('stake')}** (½-Kelly, p≈{e.get('p_firm')})", "inline": True},
                {"name": "Committed", "value": f"{e.get('minutes_out')}m before jump", "inline": True},
                {"name": "Ledger", "value": _ledger_line(s), "inline": False},
            ],
        }
    icon, colour = {"won": ("✅ WON", GREEN), "lost": ("❌ LOST", RED)}.get(kind, ("⚪ VOID", GREY))
    pnl = e.get("pnl")
    pnl_s = f"{'+' if (pnl or 0) >= 0 else '−'}${abs(pnl or 0):.2f}"
    clv_bet = None
    if e.get("settled_price") and e.get("entry_price"):
        clv_bet = (e["entry_price"] / e["settled_price"] - 1) * 100
    return {
        "title": f"{icon} — {sel}  {pnl_s}",
        "color": colour,
        "description": _race_line(e),
        "fields": [
            {"name": "Backed", "value": f"${e.get('stake')} @ **{e.get('entry_price')}**", "inline": True},
            {"name": "Close", "value": f"{e.get('settled_price') or '–'}"
                     + (f" (CLV {clv_bet:+.1f}%)" if clv_bet is not None else ""), "inline": True},
            {"name": "P&L", "value": f"**{pnl_s}**", "inline": True},
            {"name": "Ledger", "value": _ledger_line(s), "inline": False},
        ],
    }


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self.webhook = webhook_url
        self._q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self._client = httpx.AsyncClient(timeout=10)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await self._client.aclose()

    # sync + non-blocking: safe to call from the ledger inside any loop
    def enqueue(self, kind: str, entry: dict, stats: dict) -> None:
        try:
            self._q.put_nowait(build_embed(kind, entry, stats))
        except asyncio.QueueFull:
            print("[discord] queue full — dropping notification")

    def announce(self, text: str) -> None:
        try:
            self._q.put_nowait({"description": text, "color": GREY})
        except asyncio.QueueFull:
            pass

    async def _worker(self) -> None:
        while True:
            embed = await self._q.get()
            try:
                await self._send(embed)
            except Exception as exc:
                print(f"[discord] send failed: {exc}")
            await asyncio.sleep(0.5)   # stay far under webhook rate limits

    async def _send(self, embed: dict) -> None:
        payload = {"username": "RacingBoard", "embeds": [embed]}
        r = await self._client.post(self.webhook, json=payload)
        if r.status_code == 429:       # rate limited — honour retry_after once
            delay = float((r.json() or {}).get("retry_after", 1.0))
            await asyncio.sleep(min(delay, 10.0))
            await self._client.post(self.webhook, json=payload)
        elif r.status_code >= 400:
            print(f"[discord] HTTP {r.status_code}: {r.text[:200]}")
