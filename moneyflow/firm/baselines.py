"""
Baselines the firm-model must beat out-of-sample (the plan's B0–B2). Each takes the
train/test split and returns a firm-likelihood score per test row, so it's scored on
exactly the same footing as the model.

  B0 favourite      — back the market (score = implied prob). The bar to clear.
  B1 opening-price  — logistic on price / favouritism only.
  B2 tips           — the tipster/best-bet flag.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .schema import LABEL


def _b1_openprice(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    cols = ["log_price", "price_rank", "market_prob"]
    Xtr = train[cols].fillna(train[cols].median())
    Xte = test[cols].fillna(train[cols].median())
    if train[LABEL].nunique() < 2:                 # can't fit — fall back to market
        return test["market_prob"].to_numpy()
    scaler = StandardScaler().fit(Xtr)
    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    clf.fit(scaler.transform(Xtr), train[LABEL])
    return clf.predict_proba(scaler.transform(Xte))[:, 1]


def baseline_scores(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "B0_favourite": test["market_prob"].to_numpy(),
        "B1_openprice": _b1_openprice(train, test),
        "B2_tips": test["is_tipped"].astype(float).to_numpy(),
    }
