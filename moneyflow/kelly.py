"""Shared Kelly-staking maths for the scorecard and the follower ledger.

Half-Kelly by default: stake a fraction of the Kelly-optimal bet, sized off the
running bankroll, and only when there's a positive edge. `p` is our win-probability
estimate (typically 1/fair_price), `price` is the odds we'd actually back at.
"""

from __future__ import annotations


def kelly_full_fraction(p: float | None, price: float | None) -> float:
    """Full-Kelly fraction of bankroll: f* = (b·p − q) / b, with b = price − 1.
    Returns 0 when there's no positive edge (price ≤ fair) or inputs are unusable."""
    if not p or not price or price <= 1:
        return 0.0
    b = price - 1.0
    f = (p * price - 1.0) / b          # == (b·p − (1−p)) / b
    return f if f > 0 else 0.0


def kelly_stake(bankroll: float, p: float | None, price: float | None,
                fraction: float, cap: float) -> float:
    """Fractional-Kelly stake in $, capped at `cap`·bankroll per bet. 0 = no bet."""
    f = kelly_full_fraction(p, price)
    if f <= 0 or bankroll <= 0:
        return 0.0
    frac = min(fraction * f, cap)
    return round(frac * bankroll, 2)
