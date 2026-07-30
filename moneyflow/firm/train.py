"""
Train the firm-prediction model (LightGBM) and judge it against the baselines.

Run offline:  python -m moneyflow.firm.train --horizon 60

Pipeline: build the point-in-time dataset → time-based split by race (never random —
that leaks the future) → LightGBM binary classifier → isotonic calibration (so
p_firm is a real probability, which the Kelly stake needs) → evaluate AUC/log-loss/
Brier and the Kelly ROI/CLV backtest vs B0–B2 → save the artifact only if asked.

On a small dataset the numbers are noisy and NOT meaningful — this exists so the
whole pipeline is verified end-to-end now and just needs data to become real.
"""

from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

import lightgbm as lgb

from ..config import settings
from .backtest import backtest
from .baselines import baseline_scores
from .features import build_dataset
from .schema import CATEGORICAL, FEATURES, LABEL


def _races_by_time(df: pd.DataFrame) -> list[str]:
    """Race keys strictly ordered by jump time (ISO strings sort chronologically).
    Sorting on race_key would be alphabetical-by-venue — a venue split, not a time
    split — so jump_time is the only valid ordering."""
    order = (df[["race_key", "jump_time"]].drop_duplicates("race_key")
             .sort_values("jump_time", na_position="first"))
    return order["race_key"].tolist()


def time_split(df: pd.DataFrame, test_frac: float = 0.3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split by race in jump-time order — all runners of a race stay together, and
    every test race jumps strictly later than every train race (walk-forward)."""
    races = _races_by_time(df)
    cut = int(len(races) * (1 - test_frac))
    train_races = set(races[:cut])
    tr = df[df["race_key"].isin(train_races)]
    te = df[~df["race_key"].isin(train_races)]
    return tr.reset_index(drop=True), te.reset_index(drop=True)


def _fit(tr: pd.DataFrame):
    """Fit LightGBM on the earlier part of `tr` and the isotonic calibrator on the
    LAST ~25% of train races (a held-out temporal slice) — never on the model's own
    training predictions, which are overfit and would make live p_firm overconfident.
    Falls back to no calibration when the slice is too small/one-class."""
    races = _races_by_time(tr)
    cut = int(len(races) * 0.75)
    core_races = set(races[:cut])
    core = tr[tr["race_key"].isin(core_races)]
    calib = tr[~tr["race_key"].isin(core_races)]
    if core[LABEL].nunique() < 2:            # tiny data — train on everything, skip cal
        core, calib = tr, tr.iloc[0:0]

    ytr = core[LABEL]
    pos = max(1, int((ytr == 1).sum()))
    neg = int((ytr == 0).sum())
    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=200, learning_rate=0.05,
        num_leaves=15, min_child_samples=5, subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=neg / pos, verbose=-1)
    model.fit(core[FEATURES], ytr, categorical_feature=CATEGORICAL)

    iso = None
    if len(calib) >= 20 and calib[LABEL].nunique() == 2:
        try:
            iso = IsotonicRegression(out_of_bounds="clip").fit(
                model.predict_proba(calib[FEATURES])[:, 1], calib[LABEL])
        except Exception:
            iso = None
    return model, iso


def rolling_eval(df: pd.DataFrame, folds: int = 4) -> dict:
    """Rolling-origin walk-forward: split the race timeline into folds+1 chunks;
    fold i trains on chunks[0..i] and tests on chunk i+1. The average across folds
    is a far stabler model-vs-baseline verdict than one split."""
    races = _races_by_time(df)
    if len(races) < (folds + 1) * 3:
        return {"error": f"too few races ({len(races)}) for {folds} rolling folds"}
    chunks = np.array_split(np.array(races), folds + 1)
    rows = []
    for i in range(folds):
        train_races = set(np.concatenate(chunks[: i + 1]))
        test_races = set(chunks[i + 1])
        tr = df[df["race_key"].isin(train_races)]
        te = df[df["race_key"].isin(test_races)]
        if te.empty or tr[LABEL].nunique() < 2 or te[LABEL].nunique() < 2:
            continue
        model, iso = _fit(tr)
        p = model.predict_proba(te[FEATURES])[:, 1]
        if iso is not None:
            p = iso.transform(p)
        m = _metrics(te[LABEL], p)
        b1 = _metrics(te[LABEL], baseline_scores(tr, te)["B1_openprice"])
        rows.append({"fold": i + 1, "test_races": len(test_races),
                     "model_auc": m["auc"], "b1_auc": b1["auc"]})
    if not rows:
        return {"error": "no valid folds"}
    aucs = [r["model_auc"] for r in rows if r["model_auc"] is not None]
    b1s = [r["b1_auc"] for r in rows if r["b1_auc"] is not None]
    return {"folds": rows,
            "mean_model_auc": round(float(np.mean(aucs)), 3) if aucs else None,
            "mean_b1_auc": round(float(np.mean(b1s)), 3) if b1s else None}


def _metrics(y, p) -> dict:
    y = np.asarray(y); p = np.asarray(p)
    out = {"auc": None, "logloss": None, "brier": None}
    if len(np.unique(y)) == 2:                       # AUC/log-loss need both classes
        out["auc"] = round(roc_auc_score(y, p), 3)
        out["logloss"] = round(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6)), 4)
        out["brier"] = round(brier_score_loss(y, p), 4)
    return out


def train(horizon: int = 60, threshold: float = 0.08, save: bool = False) -> dict:
    df = build_dataset(settings.db_path, horizon=horizon, threshold=threshold)
    if len(df) < 20 or df[LABEL].nunique() < 2:
        return {"error": f"insufficient data at H={horizon}: {len(df)} rows, "
                         f"{int(df[LABEL].sum()) if len(df) else 0} firmers"}

    tr, te = time_split(df)
    if te.empty or tr[LABEL].nunique() < 2:
        return {"error": "degenerate split (too little data / one class in train)"}

    model, iso = _fit(tr)
    p_raw = model.predict_proba(te[FEATURES])[:, 1]
    p_cal = iso.transform(p_raw) if iso is not None else p_raw
    yte = te[LABEL]

    result = {
        "horizon": horizon, "threshold": threshold,
        "rows": len(df), "train": len(tr), "test": len(te),
        "firm_rate": round(float(df[LABEL].mean()), 3),
        "model": {**_metrics(yte, p_cal), "backtest": backtest(te, p_cal)},
        "baselines": {}, "importance": {},
    }
    for name, sc in baseline_scores(tr, te).items():
        result["baselines"][name] = {**_metrics(yte, sc), "backtest": backtest(te, sc)}

    imp = sorted(zip(FEATURES, model.feature_importances_), key=lambda x: -x[1])
    result["importance"] = {k: int(v) for k, v in imp[:8]}

    # Publish a small metrics file every run — the site's DATA & MODEL panel reads
    # it, so training progress is visible without the server touching the ML stack.
    models_dir = Path(__file__).parent.parent / "models"
    models_dir.mkdir(exist_ok=True)
    (models_dir / f"metrics_h{horizon}.json").write_text(json.dumps({
        "horizon": horizon, "rows": len(df), "firm_rate": result["firm_rate"],
        "model_auc": result["model"]["auc"],
        "b1_auc": result["baselines"]["B1_openprice"]["auc"],
        "beats_baseline": (result["model"]["auc"] or 0) > (result["baselines"]["B1_openprice"]["auc"] or 1),
        "top_features": list(result["importance"])[:5],
        "trained_at": time.time(),
    }))

    if save:
        # Versioned artifacts live in the package's models/ dir (plan §7); *.pkl is
        # gitignored so they never land in the repo.
        models_dir = Path(__file__).parent.parent / "models"
        models_dir.mkdir(exist_ok=True)
        path = models_dir / f"firm_v1_h{horizon}.pkl"
        with open(path, "wb") as f:
            pickle.dump({"model": model, "isotonic": iso, "features": FEATURES,
                         "categorical": CATEGORICAL, "horizon": horizon,
                         "threshold": threshold, "trained_at": time.time()}, f)
        result["saved"] = str(path)
    return result


def _print(r: dict) -> None:
    if "error" in r:
        print("⚠ ", r["error"]); return
    print(f"\nfirm-model @ H={r['horizon']}min  (firm if ≥{int(r['threshold']*100)}% shorten to jump)")
    print(f"  data: {r['rows']} rows ({r['train']} train / {r['test']} test), "
          f"firm-rate {r['firm_rate']:.1%}")
    def line(name, d):
        b = d["backtest"]
        print(f"  {name:14} AUC {str(d['auc']):>5}  logloss {str(d['logloss']):>7}  "
              f"| picks {b.get('picks')}  ROI {str(b.get('roi')):>6}%  "
              f"strike {b.get('strike_firm')}  CLV {b.get('clv_pct')}%")
    print("  " + "-" * 76)
    line("MODEL", r["model"])
    for n, d in r["baselines"].items():
        line(n, d)
    print(f"  top features: {', '.join(r['importance'])}")
    if r.get("saved"):
        print(f"  saved: {r['saved']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--threshold", type=float, default=0.08)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--folds", type=int, default=0,
                    help="also run rolling-origin walk-forward with N folds")
    a = ap.parse_args()
    _print(train(horizon=a.horizon, threshold=a.threshold, save=a.save))
    if a.folds:
        df = build_dataset(settings.db_path, horizon=a.horizon, threshold=a.threshold)
        r = rolling_eval(df, folds=a.folds)
        print("\nrolling walk-forward:")
        if "error" in r:
            print("  ⚠", r["error"])
        else:
            for f in r["folds"]:
                print(f"  fold {f['fold']}: model AUC {f['model_auc']}  vs  B1 {f['b1_auc']}"
                      f"  ({f['test_races']} test races)")
            print(f"  mean: model {r['mean_model_auc']}  vs  B1 {r['mean_b1_auc']}")
