"""
Build the point-in-time feature+label table from the SQLite training store.

For a chosen horizon H (minutes before jump), each row is one runner observed at
offset_min == H, labelled by whether it then shortened to the jump. The leakage
guard is structural: the query only ever selects the H snapshot, never a later one,
and only `jump_price` from outcomes (to form the label) — no post-H feature leaks in.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from .schema import CATEGORICAL, FEATURES, LABEL, META, NUMERIC


def build_dataset(db_path: str, horizon: int = 60, threshold: float = 0.08) -> pd.DataFrame:
    """Return a DataFrame of FEATURES + LABEL + META for runners seen at `horizon`.
    `threshold` = min fractional shortening (H→jump) to count as firmed."""
    con = sqlite3.connect(db_path)
    try:
        raw = pd.read_sql_query(
            """
            SELECT s.race_key, s.number, s.offset_min,
                   s.best_price, s.tote_share, s.implied, s.fair_price,
                   s.price_rank, s.n_confirm, s.value_pct, s.bf_wom, s.is_tipped,
                   s.direction,
                   o.jump_price, o.won, o.finish_pos,
                   r.date, r.jump_time, r.code, r.field_size,
                   rn.barrier, rn.weight
            FROM snapshots s
            JOIN outcomes o ON s.race_key = o.race_key AND s.number = o.number
            JOIN races    r ON s.race_key = r.race_key
            LEFT JOIN runners rn ON s.race_key = rn.race_key AND s.number = rn.number
            WHERE s.offset_min = ?
              AND (r.country IS NULL OR r.country != 'BF')  -- exchange-only fallback races: different regime
              AND s.best_price IS NOT NULL
              AND o.jump_price IS NOT NULL
              AND o.jump_price > 0
            """,
            con, params=(horizon,))
    finally:
        con.close()

    if raw.empty:
        return raw

    df = raw.copy()
    df["price_at_h"] = df["best_price"]
    df["log_price"] = np.log(df["best_price"].clip(lower=1.01))
    df["market_prob"] = 1.0 / df["best_price"]
    # implied_share: market prob normalised within each race (field-relative favour).
    race_sum = df.groupby("race_key")["market_prob"].transform("sum")
    df["implied_share"] = df["market_prob"] / race_sum.replace(0, np.nan)
    df["is_tipped"] = df["is_tipped"].fillna(0).astype(int)

    # Label: did it shorten from the H price to the jump price by ≥ threshold?
    df[LABEL] = ((df["jump_price"] / df["best_price"] - 1.0) <= -threshold).astype(int)

    # Force numeric dtype — an all-null column (e.g. bf_wom far out) otherwise reads
    # back from SQLite as `object`, which LightGBM rejects.
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in CATEGORICAL:
        df[c] = df[c].astype("category")

    keep = FEATURES + [LABEL] + META
    return df[keep].reset_index(drop=True)


def coverage(db_path: str, horizons=(120, 90, 60, 45, 30, 20, 15, 10, 5, 2),
             threshold: float = 0.08) -> pd.DataFrame:
    """Diagnostic: usable rows and firm-rate per horizon — how much trainable data
    exists right now, and how balanced the label is."""
    rows = []
    for h in horizons:
        d = build_dataset(db_path, horizon=h, threshold=threshold)
        rows.append({
            "horizon": h,
            "rows": len(d),
            "races": d["race_key"].nunique() if len(d) else 0,
            "firm_rate": round(d[LABEL].mean(), 3) if len(d) else None,
        })
    return pd.DataFrame(rows)
