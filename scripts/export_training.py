#!/usr/bin/env python
"""
Export the point-in-time firm training table to Parquet for reproducible modelling.

    python scripts/export_training.py --horizon 60
    python scripts/export_training.py --all      # one file per standard horizon

Reads the local SQLite store; writes data/training/firm_h{H}.parquet. Also prints
per-horizon coverage so you can see how much trainable data has accrued.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path

from moneyflow.config import settings
from moneyflow.firm.features import build_dataset, coverage

HORIZONS = (120, 90, 60, 45, 30, 20, 15, 10, 5, 2)


def export(horizon: int, threshold: float, out_dir: Path) -> None:
    df = build_dataset(settings.db_path, horizon=horizon, threshold=threshold)
    if df.empty:
        print(f"  H={horizon:>3}: no data yet — skipped")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"firm_h{horizon}.parquet"
    df.to_parquet(path, index=False)
    print(f"  H={horizon:>3}: {len(df):>5} rows, firm-rate {df['firmed'].mean():.1%} -> {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--threshold", type=float, default=0.08)
    ap.add_argument("--all", action="store_true", help="export every standard horizon")
    a = ap.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "data" / "training"
    print("coverage:")
    print(coverage(settings.db_path, threshold=a.threshold).to_string(index=False))
    print("\nexporting:")
    for h in (HORIZONS if a.all else (a.horizon,)):
        export(h, a.threshold, out_dir)
