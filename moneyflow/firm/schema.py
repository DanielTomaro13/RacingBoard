"""
Single source of truth for the firm-model's features and label.

A prediction is made at a fixed horizon H (minutes before the jump). The label is
whether the runner shortens from its price AT H to its starting price — so the model
answers "given what we know at H, will this one firm from here?". Every feature is
taken from the snapshot at offset_min == H (or the static race/runner context); no
field derived from anything after H is allowed in — the leakage guard lives in
features.py (it only ever reads the H snapshot).
"""

from __future__ import annotations

# Numeric features knowable at the horizon.
NUMERIC = [
    "log_price",       # ln(best backable price) — favouritism, the strongest signal
    "price_rank",      # 1 = shortest in the field
    "market_prob",     # 1 / best_price
    "implied_share",   # market_prob normalised within the field (sums ~1)
    "fair_price",      # de-vig fair (sharpest market) — may be null far out
    "value_pct",       # best price vs fair (overlay %) — may be null far out
    "n_confirm",       # markets already shortening at H (usually 0 far out)
    "bf_wom",          # Betfair weight of money
    "tote_share",      # tote pool share
    "is_tipped",       # tipster / best-bet flag (0/1)
    "field_size",
    "barrier",
    "weight",
]

# Categorical features (LightGBM native categoricals).
CATEGORICAL = ["code", "direction"]

FEATURES = NUMERIC + CATEGORICAL

LABEL = "firmed"          # 1 if price shortened from H → jump by ≥ threshold

# Carried through for backtesting / joins / the chronological split — NOT fed to
# the model.
META = ["race_key", "date", "jump_time", "number", "price_at_h", "jump_price",
        "won", "finish_pos"]
