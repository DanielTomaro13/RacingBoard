"""
Score logged predictions against real outcomes — the live track record.

The DataLogger writes every heuristic (and later, model) prediction to the
`predictions` table at each capture bucket. Joining those to `outcomes` answers,
per model version and horizon: of the runners we scored high, how many actually
firmed? This is the number Phase 4's decision gate compares the ML model against.

    python -m moneyflow.firm.evaluate            # all versions, all horizons
    python -m moneyflow.firm.evaluate --horizon 60
"""

from __future__ import annotations

import argparse
import sqlite3

import pandas as pd

from ..config import settings

from .heuristic import TIERS  # single source of truth for the tier cuts

STRONG = TIERS[0][0]
WARM = TIERS[1][0]


def evaluate(db_path: str, horizon: int | None = None,
             threshold: float = 0.08) -> pd.DataFrame:
    """Each prediction is graded against movement FROM ITS OWN HORIZON'S PRICE
    to the jump — the same label the model trains on. Grading against
    outcomes.firmed (open→jump) flattered late buckets absurdly: a T-2
    prediction was scored on a move that had already happened, and its
    `direction` input all but contained the answer."""
    con = sqlite3.connect(db_path)
    try:
        q = """
            SELECT p.model_version, p.horizon_min, p.p_firm,
                   s.best_price AS h_price, o.jump_price, o.won
            FROM predictions p
            JOIN outcomes o ON p.race_key = o.race_key AND p.number = o.number
            JOIN snapshots s ON s.race_key = p.race_key AND s.number = p.number
                            AND s.offset_min = p.horizon_min
            WHERE s.best_price IS NOT NULL AND o.jump_price > 0
        """
        params: tuple = ()
        if horizon is not None:
            q += " AND p.horizon_min = ?"
            params = (horizon,)
        df = pd.read_sql_query(q, con, params=params)
    finally:
        con.close()
    if df.empty:
        return df
    df["firmed"] = ((df["jump_price"] / df["h_price"] - 1.0) <= -threshold).astype(int)

    def tier(p: float) -> str:
        return "STRONG" if p >= STRONG else "WARM" if p >= WARM else "lean/-"

    df["tier"] = df["p_firm"].map(tier)
    g = (df.groupby(["model_version", "horizon_min", "tier"])
           .agg(n=("firmed", "size"),
                firm_rate=("firmed", "mean"),
                win_rate=("won", "mean"))
           .reset_index())
    base = (df.groupby(["model_version", "horizon_min"])["firmed"].mean()
              .rename("base_firm_rate").reset_index())
    out = g.merge(base, on=["model_version", "horizon_min"])
    out["lift"] = (out["firm_rate"] / out["base_firm_rate"]).round(2)
    for c in ("firm_rate", "win_rate", "base_firm_rate"):
        out[c] = out[c].round(3)
    return out.sort_values(["model_version", "horizon_min", "tier"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=None)
    a = ap.parse_args()
    res = evaluate(settings.db_path, horizon=a.horizon)
    if res.empty:
        print("no settled predictions yet — accrues as logged races resolve")
    else:
        print(res.to_string(index=False))
        print("\nlift = tier firm-rate ÷ overall firm-rate at that horizon "
              "(>1 = the score finds firmers better than chance)")
