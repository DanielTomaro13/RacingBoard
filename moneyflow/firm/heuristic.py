"""
Heuristic firm-score (v1, no ML) — a transparent, rule-based prediction of which
runners will SHORTEN before the jump.

It ships before the model so there's something predictive on screen immediately, and
it is the baseline the LightGBM model must beat out-of-sample (B-heuristic in the
plan). Every input is knowable ≥1hr out; the score is deliberately simple and
explainable — a weighted blend of the firming precursors the tool already trusts:

  market respect  (favouritism — money already rates it)   w=0.45
  recent form     (TAB form rating, field-relative)         w=0.35
  consensus       (tipped / expert best-bet)                w=0.20
  × momentum adj  (already firming ↑, already drifting ↓)

Connections (jockey/trainer strike-rate) are intentionally omitted in v1 — no data
join yet — and are the first thing to add. Returns a 0–1 score + a tier + the factor
breakdown, so the panel and any later evaluation can see exactly why.
"""

from __future__ import annotations

from typing import Any

W_MARKET = 0.45
W_FORM = 0.35
W_CONSENSUS = 0.20

# Momentum adjustment: a runner already firming is more likely to keep firming; one
# already drifting against the market is less likely to turn.
ADJ_FIRMING = 1.10
ADJ_DRIFTING = 0.75

TIERS = ((0.66, "STRONG"), (0.48, "WARM"), (0.32, "LEAN"))


def _price(r: dict[str, Any]) -> float | None:
    return r.get("corp_best") or r.get("fixed_win") or r.get("tote_win")


def _tier(score: float) -> str:
    for cut, label in TIERS:
        if score >= cut:
            return label
    return "—"


def firm_scores(runners: list[dict[str, Any]], tip_numbers: set[int] | None = None
                ) -> dict[int, dict[str, Any]]:
    """Score every (active) runner's firm-likelihood, field-relative. Returns
    {number: {score, tier, factors}}. Pure — safe to call live and for logging."""
    tip_numbers = tip_numbers or set()
    active = [r for r in runners if not r.get("scratched")]
    if not active:
        return {}

    # Market respect: implied prob from best price, normalised so the favourite = 1.
    implied = {r["number"]: (1.0 / _price(r)) for r in active if _price(r)}
    max_imp = max(implied.values(), default=0.0)

    # Recent form: field-relative min-max of the TAB form rating (missing → neutral).
    ratings = {r["number"]: r["form_rating"] for r in active
               if isinstance(r.get("form_rating"), (int, float))}
    lo = min(ratings.values(), default=0.0)
    hi = max(ratings.values(), default=0.0)
    span = hi - lo

    out: dict[int, dict[str, Any]] = {}
    for r in active:
        num = r["number"]
        m = (implied.get(num, 0.0) / max_imp) if max_imp > 0 else 0.0
        if num in ratings and span > 0:
            f = (ratings[num] - lo) / span
        else:
            f = 0.5
        c = min(1.0, (0.6 if num in tip_numbers else 0.0) + (0.4 if r.get("best_bet") else 0.0))

        base = W_MARKET * m + W_FORM * f + W_CONSENSUS * c
        direction = r.get("direction")
        adj = ADJ_FIRMING if direction == "firming" else ADJ_DRIFTING if direction == "drifting" else 1.0
        score = max(0.0, min(1.0, base * adj))

        out[num] = {
            "score": round(score, 3),
            "tier": _tier(score),
            "factors": {"market": round(m, 2), "form": round(f, 2),
                        "consensus": round(c, 2), "momentum": adj},
        }
    return out
