"""
Business-metric backtest: the number that actually decides whether the model is
worth serving. Given firm-likelihood scores on a held-out test set, "back the top
picks" with a half-Kelly stake and measure:

  ROI / P&L  — staked at the H price, paid at the H price when the runner WINS.
  strike     — of the picks, how many actually firmed (the thing we predicted).
  CLV        — did we beat the close (H price vs jump price); the cleanest edge proxy.

The model must beat the baselines on these, not just on AUC.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..kelly import kelly_stake
from .schema import LABEL

BANKROLL = 100.0
KELLY_FRACTION = 0.5
KELLY_CAP = 0.25


def backtest(test: pd.DataFrame, scores: np.ndarray, top_frac: float = 0.2) -> dict:
    """Back the top `top_frac` of runners by score. Half-Kelly staked; p = score."""
    n = len(test)
    if n == 0:
        return {"picks": 0}
    k = max(1, round(top_frac * n))
    order = np.argsort(-scores)
    pick_idx = order[:k]
    picks = test.iloc[pick_idx].copy()
    p = np.clip(scores[pick_idx], 1e-3, 0.999)

    staked = profit = 0.0
    for (_, row), pi in zip(picks.iterrows(), p):
        price = row["price_at_h"]
        stake = kelly_stake(BANKROLL, float(pi), float(price), KELLY_FRACTION, KELLY_CAP)
        if stake <= 0:
            continue
        staked += stake
        if row["won"] == 1:
            profit += stake * (price - 1.0)
        else:
            profit -= stake
    clv = (picks["price_at_h"] / picks["jump_price"] - 1.0)
    return {
        "picks": int(k),
        "strike_firm": round(float(picks[LABEL].mean()), 3),   # fraction that firmed
        "win_rate": round(float((picks["won"] == 1).mean()), 3),
        "staked": round(staked, 2),
        "profit": round(profit, 2),
        "roi": round(100 * profit / staked, 1) if staked else None,
        "clv_pct": round(float(clv.mean()) * 100, 2),
    }
