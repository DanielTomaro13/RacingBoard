# Firm-Prediction Pipeline — forecasting which AU/NZ horses shorten before it happens

**Goal:** for a race **≥1 hour from the jump**, predict which runners will **firm**
(shorten from opening price to starting price) — anticipate the money *before* it
arrives, rather than detecting it after (which RacingBoard does today). Turn strong
predictions into **committed, staked, immutable** entries in a follower ledger.

**Scope decisions (locked):**
1. **Self-contained in RacingBoard.** All code — logging, features, training,
   inference, serving, ledger — lives in the RacingBoard repo. No dependency on the
   `sportsdata-agents` stack. (Keeps its one existing dep: the `sportsdata-mcp`
   engine it already imports for live data.)
2. **We collect our own data from the board logs.** The warehouse's `odds_snapshots`
   table is **empty** — it never captured anything (audited 2026‑07‑09: 0 rows;
   only 1,107 racing *results*, no price history). There is no historical shortcut.
   RacingBoard logs its own point-in-time snapshots going forward into a local
   store. **This means a data-accrual wait of weeks before a model can train.**
   That cost is accepted.
3. **AU/NZ only.** Every stage — board filter, logging, features, model, serving —
   is restricted to Australian + New Zealand meetings. US/international is filtered
   out at discovery.

**Honest framing.** Racing markets are semi-efficient; the available edge is modest.
The win isn't a crystal ball — it's a small, measurable lean that only counts if it
beats the obvious baselines (back the favourite / follow the tips) **out of sample**.
The whole pipeline is built to *prove or disprove* that lean, and the immutable
ledger makes the verdict impossible to fudge.

---

## 0. TL;DR

1. **Ship the plumbing now** (no model needed): AU/NZ board filter → SQLite
   `DataLogger` → data starts accruing today.
2. **Ship a transparent heuristic firm-score** immediately so there's a prediction
   on screen and a baseline the model must beat.
3. **Ship the follower ledger** — append-only, immutable, Kelly-staked. Strict entry
   bar because entries can never be removed. Works with today's signals now; becomes
   the ML model's committal surface later.
4. **Wait weeks** while data accrues. Meanwhile build baselines + EDA.
5. **Train v1 (LightGBM)** on the accrued data → calibrated `P(firm)`; validate
   walk-forward; backtest ROI/CLV with Kelly staking through the existing scorer.
6. **Decision gate:** beats baseline out-of-sample? → serve behind an experimental
   label and let it feed the ledger. No? → iterate or shelve honestly.

---

## 1. Objective & success criteria

**Prediction unit:** one AU/NZ runner in one race, scored at a fixed horizon before
the jump (primary **T‑60min**; stretch **T‑120min**).

**Label — the runner firmed:**
- **Binary (primary):** `firmed = (jump_price / open_price) − 1 ≤ −θ`, θ ≈ 0.08–0.10.
- **Regression (secondary):** continuous `price_move_pct` (open→jump).
- **Rank (v2):** ordering of runners within a race by firm-likelihood.

**Success = beat these out-of-sample:**
- **B0 — favourite:** back the shortest opening price.
- **B1 — opening price / favouritism rank** (logistic).
- **B2 — tips/ratings:** back the tipped / top-rated runner.
- **B‑heuristic:** the rule-based firm-score (§5).

Ship only if it beats these on **AUC / log-loss / calibration** *and* the business
metric — **backtested ROI & strike-rate of a Kelly-staked "back the predicted
firmers" book** through `scorer.py`, plus **closing-line value** (did predicted
firmers actually end up shorter).

---

## 2. Architecture — end to end

```
                         ┌─────────────────────────────────────────┐
   live TAB/Betfair/     │            RacingBoard poller           │
   corp books  ─────────▶│  discover(AU/NZ) → board → store (mem)   │
                         └───────────────┬─────────────────────────┘
                                         │ every offset bucket + on resolve
                                         ▼
                             ┌───────────────────────┐
                             │  DataLogger (datalog) │   forward-collect
                             └───────────┬───────────┘
                                         ▼
                       ┌─────────────────────────────────────┐
                       │  SQLite  ~/.racingboard/racingboard.db│
                       │  races · runners · snapshots · outcomes│
                       │  predictions · follows                 │
                       └───────┬───────────────────────┬───────┘
                   offline     │                       │  online
             ┌────────────────▼─────────┐     ┌────────▼───────────────┐
             │ export_training → Parquet │     │ predict: load model,   │
             │ train (LightGBM) → model  │     │ score at discovery →   │
             │ walk-forward eval         │     │ predictions table      │
             │ backtest (Kelly ROI/CLV)  │     └────────┬───────────────┘
             └────────────┬──────────────┘              ▼
                          │ model artifact        ┌──────────────────────┐
                          └──────────────────────▶│ Serving + Follower    │
                                                  │ ledger (append-only,  │
                                                  │ Kelly-staked, strict) │
                                                  │ + prediction scorecard│
                                                  └──────────────────────┘
```

Two paths off one store: **offline** (train) and **online** (serve). ML never runs
in the hot Betfair loop.

**Repo layout (all inside RacingBoard):**
```
moneyflow/
  datalog.py          # DataLogger: snapshots + outcomes → SQLite (forward-collect)
  db.py               # schema + helpers (~/.racingboard/racingboard.db)
  follow.py           # append-only, immutable follower ledger (Kelly-staked)
  firm/
    features.py       # build point-in-time feature vectors (leakage-guarded)
    heuristic.py      # rule-based firm-score (no ML) — ships first, = a baseline
    baselines.py      # B0–B2
    train.py          # export Parquet, train LightGBM, calibrate, walk-forward
    predict.py        # load model, score per runner at discovery
    backtest.py       # Kelly ROI / CLV vs baselines via the scorer harness
    schema.py         # feature list + label defs (single source of truth)
  models/firm_vN.pkl  # versioned model artifacts
scripts/
  export_training.py  # snapshots+outcomes → data/training/*.parquet
docs/PREDICTION_PLAN.md
```

---

## 3. Data collection — the board logs (forward-collect)

The poller already builds the full board every few seconds. The **DataLogger** taps
that stream and persists point-in-time rows — this *is* our training set, grown from
now.

**When it writes.** At fixed **offset buckets** before the scheduled jump:
`T‑120, 90, 60, 45, 30, 20, 15, 10, 5, 2 min`. One `snapshots` row per active runner
per bucket (deduped per bucket to bound volume). On race resolution, one `outcomes`
row per runner. Reuse the scorer's existing "capture state on resolve" pattern and
RacingBoard's venue/runner normalisation.

**AU/NZ filter** (Phase 0, useful on its own): add `settings.countries`
(default `{"AU","NZ"}`); drop any TAB meeting whose `location` isn't an AU state
(NSW/VIC/QLD/SA/WA/TAS/NT/ACT) or NZ. The board, tape, scorecard, logger and model
all inherit one clean universe — and it kills the noisy overnight-US experience.

**Volume.** AU/NZ only ⇒ ~200–300 races/day × ~10 runners × ~10 buckets ≈
20–30k snapshot rows/day → well under 1 GB/year in SQLite. Keep everything; index on
`race_key`, `ts`.

### SQLite schema (`db.py`)
```
races(race_key PK, date, code, country, venue, race_no, distance, class,
      field_size, jump_time, results_json, created_at)
runners(race_key, number, name, jockey, trainer, barrier, weight,
        PK(race_key, number))
snapshots(race_key, number, offset_min, ts, best_price, price_rank, implied,
          tote_share, overround, wom, form_rating, speed_band, last5,
          career_sr, days_since_run, is_tipped, is_best_bet, n_confirm, …,
          PK(race_key, number, offset_min))
outcomes(race_key, number, open_price, horizon_price, jump_price,
         price_move_pct, firmed, share_delta, finish_pos, won, placed)
predictions(race_key, number, horizon_min, p_firm, model_version, generated_at)
follows(… see §7 …)
```

---

## 4. Features & labels

### 4a. Features — must be knowable at the horizon (≥60 min out); anything later = leakage
- **Market (strongest):** opening price, price@horizon, favouritism rank, implied
  prob, log-odds, z-score within field, book disagreement (spread across corp
  books), opening overround, early drift observed open→horizon, tote-vs-fixed
  divergence, Betfair WoM, short-window momentum.
- **Form:** `dfsFormRating`, `earlySpeedRating` + band, `last5` parsed to a recent
  -form score, career strike/place rate, `daysSinceLastRun`, weight, barrier.
- **Connections:** jockey, trainer → later joined to rolling strike-rate stats.
- **Consensus:** tipster picks, rating categories, best-bets → tipped? rank? how many
  sources agree.
- **Context:** code (R/G/H), field size, distance band, class, track, jurisdiction,
  time-of-day, day-of-week.
- **Cross-features:** *well-tipped but longer opening price*; *good form but drifting*.

`schema.py` is the single source of truth for the feature list; `features.py`
builds vectors **only from `snapshots` rows with `offset_min ≥ horizon`** — a hard
leakage guard.

### 4b. Labels (from the snapshot series + results)
Per runner: `open_price` (first bucket ≥ ~60 min out), `horizon_price` (price at the
horizon bucket), `jump_price` (last bucket before the off), `price_move_pct`,
`firmed`, `share_delta`, `finish_pos`, `won`, `placed`. Results from TAB.

---

## 5. Heuristic firm-score (ships first — a real baseline, not a toy)

Before any ML, a transparent rule combining the signals RacingBoard already trusts —
each a known firming precursor:
```
score = w1·(short opening price / favouritism)      # money already respects it
      + w2·(strong recent form + speed rating)
      + w3·(tipped / best-bet / consensus agreement)
      + w4·(in-form jockey/trainer)
      − w5·(early drift already against it)
```
Shown as an *experimental* panel and **logged to `predictions`** so it's scored like
any model. It's simultaneously: something useful on day one, the bar the ML must
clear (B‑heuristic), and the first thing allowed to feed the follower ledger.

---

## 6. Model

1. **Baselines B0–B2 + B‑heuristic** — implemented first; everything measured against
   them.
2. **v1 — LightGBM** binary classifier → per-runner `P(firm)`. Interpretable
   (importance / SHAP), handles missing values & non-linearities, strong on tabular;
   class weights for imbalance (most runners don't firm).
3. **Calibration** — isotonic/Platt so `p_firm` is a true probability (it must be, to
   drive Kelly staking).
4. **v2 — within-race ranking** (LambdaMART) — predicts the *order* of firmers per
   race, matching the competition structure.
5. **Per-code handling** — R/G/H differ; start with a `code` feature, split into
   per-code models later if it helps.

---

## 7. Follower ledger — committed, immutable, Kelly-staked

**The accountability spine.** A prediction that clears a strict bar becomes a
**committed** entry: a real, staked, permanent record. **Once committed it can never
be removed or edited** (only settled). Because you can't prune losers, the track
record is trustworthy — which is the entire point.

**Why immutability forces discipline.** Every commit permanently moves the P&L, so
the *entry criterion must be strict*. The ledger is deliberately hard to add to.

### Entry criteria (strict — must clear ALL)
- **Time:** none — commit any time the race is still open. Multi-market steam is a
  near-jump phenomenon (the data shows 0 runners reach ≥3 confirmations ≥60 min
  out), so a time gate never fires. The only invariant is **immutability**.
- **Conviction = intersection of signals, not any single one.** Pre-ML:
  multi-source **confirmed (≥3 markets shortening)** **AND** positive **value edge**
  (best price ≥ fair × (1+m)) **AND** it is the race's **top pick**. Post-ML:
  `p_firm ≥ τ` (e.g. 0.65, tuned on validation) **AND** positive edge.
- **One selection per race**, max — the single strongest. No scatter-gun.
- **Liquidity/sanity:** price within a sane band; market open; not a short-priced
  odds-on where firming is meaningless.
- **De-dup:** a race can be committed only once, ever.

### `follows` table (append-only)
```
follows(id PK, committed_at, race_key, number, selection, code, venue, race_no,
        jump_time, trigger,            -- 'heuristic' | 'model:firm_vN' | 'confirmed+value'
        entry_price, fair_price, p_firm,
        kelly_fraction, stake,          -- the ½-Kelly stake at commit (§ Kelly)
        status,                         -- 'pending' → 'won' | 'lost' | 'void'
        settled_price, settled_at, pnl) -- written once, at settlement
```
- **Immutable by construction:** the only mutation allowed is the one-time
  `pending → settled` transition (status/settled_*/pnl). **No delete, no re-price,
  no back-dating.** Enforced in `follow.py` (append + settle-once API) and guarded in
  the DB layer (no UPDATE path except the settle transition; no DELETE).
- **Kelly stake** reuses the scorecard engine we just built: `stake = ½·Kelly` off
  the ledger's running bankroll, `p = p_firm` (or `1/fair` pre-ML), odds = entry
  price; positive-edge only.

### UI — a locked ledger panel
`COMMITTED · permanent record` — each row: runner, venue/race, trigger, entry price,
**stake**, live status, settled result, and a cumulative **bankroll / ROI / CLV**
line. Visually distinct from the observational scorecard: the scorecard grades every
signal; the **ledger only holds what we were willing to permanently stand behind.**

---

## 8. Training, validation, backtesting

- **Time-based split only** (never random — leaks the future); **walk-forward**
  retrain to mirror live use.
- **Leakage audit:** every feature provably knowable at the horizon — no jump-time
  price, no late tote pool, no result-derived field. The `offset_min ≥ horizon`
  guard is the mechanical backstop.
- **Metrics:** AUC, log-loss, Brier/calibration **plus** the business metric —
  Kelly-stake the predicted firmers and grade ROI / strike-rate through `scorer.py`
  against B0–B2/B‑heuristic, plus **closing-line value**.
- **Overfitting guards:** small edge ⇒ regularise hard, few robust features, and a
  serving ceiling that refuses to commit implausible predictions.

---

## 9. Serving & retraining

- At discovery (≥1 hr out, AU/NZ): build the live feature vector per runner → score
  → write `predictions`. Surface a **PREDICTED FIRMERS** panel + `FIRM?` score,
  **clearly distinct** from the observed `✓ confirmed` (predicted vs happened).
- **Prediction scorecard** (mirrors the signal scorecard): of runners we predicted
  would firm, how many actually did / won — tracked continuously → drift is visible.
- **Strict predictions auto-commit to the follower ledger** with a Kelly stake.
- **Retrain** is an offline script, scheduled (e.g. weekly) once data suffices;
  versioned artifacts (`firm_vN.pkl`), champion/challenger before promotion.
- Inference is cheap: load artifact at startup, score at discovery. No ML in the hot
  loop.

---

## 10. Phased roadmap

- **Phase 0 — Plumbing (days).** AU/NZ board filter; `db.py` schema; `DataLogger`
  writing snapshots + outcomes. **Data starts accruing here.**
- **Phase 1 — Something on screen now (days).** Heuristic firm-score panel (logged to
  `predictions`); the **follower ledger** wired to the heuristic + confirmed/value
  intersection with Kelly stakes. Immediately useful, and the accountability spine is
  live before the model exists.
- **Phase 2 — Wait + build (weeks of accrual).** Baselines B0–B2; EDA on accruing
  data (which features correlate with firming; label/leakage sanity); `export_training`.
- **Phase 3 — v1 model.** Train LightGBM, calibrate, walk-forward validate, backtest
  Kelly ROI/CLV vs baselines.
- **Phase 4 — Decision gate.** Beats baseline out-of-sample? → serve behind the
  experimental label; let it feed the ledger. No? → iterate features or shelve
  honestly.
- **Phase 5 — Iterate.** Ranking model, per-code models, T‑120 horizon,
  jockey/trainer strike-rate joins, alerting on strong predicted firmers.

---

## 11. Risks & open questions

- **Accrual wait (weeks)** is the accepted cost of self-collection — there is no
  historical shortcut (warehouse is empty).
- **Edge may be too small to matter** — an honest possible outcome; the ledger's CLV
  & ROI decide, not us.
- **≥1hr-out data may be thin** — opening prices may not exist that early for some
  AU/NZ meetings; the model may only work at T‑45/30. Log from T‑120, start scoring
  T‑60, test T‑120.
- **Leakage** is the #1 self-deception risk — time-split + provenance audit + the
  `offset_min` guard.
- **Scratchings** reshape the field after a prediction — re-score / void the ledger
  entry on late scratchings (void, never delete).
- **Non-stationarity** — market behaviour drifts; walk-forward retrain + the live
  prediction scorecard as a drift monitor.

---

## 12. Tech stack

- **Store:** SQLite (`sqlite3` stdlib) — no new runtime dep; Parquet training
  exports (`pyarrow`); optional DuckDB for fast local analytics.
- **Train:** pandas/Polars, scikit-learn, **LightGBM**, SHAP. Offline scripts in
  `moneyflow/firm/`.
- **Serve:** load artifact at startup; numpy/sklearn inference at discovery.
- **Ops:** manual or scheduled retrain; prediction scorecard for monitoring; the
  follower ledger as the permanent, immutable paper-trade record before anything
  drives real money.

---

## First concrete step

**Phase 0 + Phase 1** need no external data and no model:
1. Filter the board to AU/NZ.
2. Stand up `db.py` + `DataLogger` so real data starts accruing **today**.
3. Ship the heuristic firm-score and the **immutable, Kelly-staked follower ledger**
   (strict entry bar).
The ML model (Phases 2–4) follows once enough data has accrued.
