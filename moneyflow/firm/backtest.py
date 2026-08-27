"""
Business-metric backtest: given firm-likelihood scores on a held-out test set,
"back the top picks" and measure what a bettor would care about:

  CLV        — H price vs jump price on the picks: did the picks beat the close?
               The cleanest edge proxy and THE headline verdict — a score that
               keeps buying prices the market then shortens is finding steam.
  strike     — of the picks, how many actually firmed (the thing predicted).
  flat ROI   — $1 level stakes at the H price settled on the WIN market. Kept as
               colour only: p(firm) is not p(win), so no stake sizing is derived
               from it (the old half-Kelly sized stakes as if it were, which
               made the ROI a fiction of systematic overstaking).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .schema import LABEL


def backtest(test: pd.DataFrame, scores: np.ndarray, top_frac: float = 0.2) -> dict:
    """Back the top `top_frac` of runners by score, flat-staked."""
    n = len(test)
    if n == 0:
        return {"picks": 0}
    k = max(1, round(top_frac * n))
    order = np.argsort(-scores)
    picks = test.iloc[order[:k]].copy()

    stake = 1.0
    staked = stake * len(picks)
    returns = np.where(picks["won"] == 1, stake * (picks["price_at_h"] - 1.0), -stake)
    clv = (picks["price_at_h"] / picks["jump_price"] - 1.0)
    return {
        "picks": int(k),
        "clv_pct": round(float(clv.mean()) * 100, 2),               # the verdict
        "strike_firm": round(float(picks[LABEL].mean()), 3),
        "win_rate": round(float((picks["won"] == 1).mean()), 3),
        "staked": round(staked, 2),
        "profit": round(float(returns.sum()), 2),
        "flat_roi": round(100 * float(returns.sum()) / staked, 1) if staked else None,
    }
