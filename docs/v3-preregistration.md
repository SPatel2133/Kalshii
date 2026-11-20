# Experiment v3 pre-registration: can anyone make money here, measured properly?

Status: committed **before any v3 test-window fit, evaluation or backtest**. Every v3 report records the SHA of the commit that adds
this file. Development choices may use only development folds. Nothing below may change after a test window is scored; deviations
are reported as deviations.

Opened after v1 and v2 test scoring. The motivating NYC signal (v2 test: market_recal − market_raw = −0.0108 nats, 7-day CI
[−0.0227, +0.0015], n = 287) was computed post hoc and is a **hypothesis, not a result**. NYC therefore takes no part in the
confirmatory tests.

## 0. Verified facts
- NWS CLI climate day = midnight-to-midnight Local Standard Time all year (NWS Directive 10-1004 §4.1.2). See `docs/v2-erratum.md`.
  v3 uses the LST window (UTC bounds from the zone's 1 January offset) for the MOS sampled maximum and for the observation window.
- Fees: taker fee = ceil-to-cent(0.07 × M × C × P × (1 − P)), M = 1 for every KXHIGH series. See `docs/fees.md`.

## 1. Cities, eligibility and splits
Eligible event = catalogue rules text is NWS regime (contains "National Weather Service", the v1/v2 parser, unchanged) **and** every
market's rules_primary contains the city's station phrase. Splits come from catalogue dates only (`python -m forecast_lab v3-splits`,
output sha256 `5d1245d1106b98bd5021402b7eaf36689eb42ef7f4e46f05830907cb48d7512e`): n = eligible dates, n_dev = floor(0.65 n),
test_start = the (n_dev+1)-th eligible date, test_end = the last eligible date. Labels and prices were not inspected.

| City | Series | Station phrase | MOS / ASOS | Timezone | dev_start | test_start | test_end | n / n_dev |
|---|---|---|---|---|---|---|---|---|
| chi | KXHIGHCHI | Chicago Midway | KMDW / MDW | America/Chicago | 2025-03-05 | 2026-02-10 | 2026-08-13 | 527 / 342 |
| mia | KXHIGHMIA | Miami International Airport | KMIA / MIA | America/New_York | 2025-04-26 | 2026-02-28 | 2026-08-13 | 475 / 308 |
| aus | KXHIGHAUS | Austin Bergstrom | KAUS / AUS | America/Chicago | 2023-05-11 | 2025-06-24 | 2026-08-13 | 1187 / 771 |
| lax | KXHIGHLAX | Los Angeles Airport | KLAX / LAX | America/Los_Angeles | 2025-01-05 | 2026-01-20 | 2026-08-13 | 586 / 380 |
| phil | KXHIGHPHIL | Philadelphia International Airport | KPHL / PHL | America/New_York | 2025-04-26 | 2026-02-28 | 2026-08-13 | 475 / 308 |

Exclusions decided before any label or price was inspected:
- **Denver dropped.** Every NWS-regime KXHIGHDEN rule names only "Denver, CO" (no DEN / CLIDEN / Denver International), which is ambiguous.
- **Ambiguous older wordings excluded** (`other_regime`): "Miami, FL" (MIA, 2023-05-11 to 2025-04-25) and "Philadelphia, PA" (PHIL,
  2024-11-20 to 2025-04-25).
- **Legacy "NWS's Daily Climate Report" wording** (CHI 2021-08 to 2025-03, 1,291 events) is not NWS regime under the unchanged v1/v2
  parser, consistent with the 207 NYC events v1/v2 excluded for the same wording.
- Every city switched to The Weather Company on 2026-08-14 (last NWS event 2026-08-13); only NWS-regime events are used.
- No audit or golden window for these cities.

## 2. Features and models (unchanged from v2 except the LST window)
Cutoff 08:00 local clock time, DST-aware. GFS MOS selected by the strict availability rule, runtime + 7.5 h (strict-lag sensitivity at
11 h). Observations up to cutoff − 10 min. Empty-book exclusion as in v2. Features `nx, sampled_minus_nx, obs_max_minus_nx,
obs_latest_minus_nx`. The v2 families and fitting policy (5 chronological folds, OOF stacking, no refit in test) are fitted **per city**.
`n_x` is read at ftime D+1 00Z for every station (verified in the IEM rows).

**Pooled α (`market_recal_pooled`):** one scalar α fitted by `fit_recal_scalar` on the concatenation of every city's final development
(market midpoint, winner) pairs, then applied unchanged to every city's test events: p_i ∝ mid_i^α.

## 3. Confirmatory hypotheses (5 cities pooled; NYC excluded)
Pooled test cohort = union of each city's five-family common cohort. Bootstrap = paired moving-block bootstrap over calendar dates
spanning 2025-06-24 to 2026-08-13, with all cities' events on a date kept together. 7-day blocks are primary, with 3- and 14-day
sensitivity; 2,000 reps; seed 20260813.
- **H1 (primary).** Δ = mean log loss(market_recal_pooled) − mean log loss(market_raw) < 0. **Supported** iff the 7-day CI upper
  bound < 0 **and** Δ ≤ −0.01 nats. Also reported against 0.02.
- **H2.** The market_recal_pooled paper strategy (§4) has mean P&L per event > 0. **Profitable** iff the 7-day CI lower bound of mean
  P&L per event > 0 at 1× fees **and** the point estimate is > 0 with +1¢ slippage. Otherwise "no supported profit".

## 4. Paper strategy rules (historical simulation only; no orders)
Inputs per event: as-of bid/ask per bucket from the sealed feature rows, the family's sealed bucket probabilities p_i, and the label
(used only after the decision). Each bucket independently gets at most one side and 1 contract:
- Buy YES if p_i − ask_i − fee(ask_i) ≥ m. Cost ask_i + fee. Pays $1 if the bucket wins.
- Buy NO if bid_i − p_i − fee(1 − bid_i) ≥ m, at NO price 1 − bid_i. Pays $1 if the bucket loses.
- If both qualify, take neither and flag it.

Fill assumption: the full order fills at the quoted price with no slippage. Candles carry no depth, so this is the main optimism.
Margin grid m ∈ {0.00, 0.02, 0.05, 0.10}. m is selected per family (per city; pooled for market_recal_pooled) by maximizing total
development OOF P&L at 1× fees, ties to the larger m, and frozen before test. OOF probabilities are the fold-fitted upstream
predictions; the blends apply the final development weight/gate to the OOF upstream predictions. OOF pooled α for an event in a city
fold = α fitted on all cities' development events dated before that fold's first date and settled before its cutoff.

Strategies: market_raw (control), market_recal, market_recal_pooled, fixed_blend, adaptive_blend, weather, and favourite_yes (YES on
the highest-midpoint bucket every event, no margin). Metrics: trades, contracts, cost, P&L, ROI, mean P&L per event, hit rate and max
drawdown. Scenarios: 0×/1×/2× fees (decision and cost), +1¢ slippage at 1× (decision at quoted price), and a liquidity cut that drops
events where any traded bucket had volume_pre_cutoff = 0.

## 5. Secondary (reported without multiplicity claims)
- Per city, H1 and H2 for market_recal_pooled: one-sided bootstrap p (fraction of 7-day bootstrap means ≥ 0 for H1, ≤ 0 for H2),
  Holm-adjusted across the 5 cities, separately for H1 and H2.
- fixed_blend − market_raw and adaptive_blend − fixed_blend, per city and pooled.
- Per-city-α market_recal; favourite_yes P&L; fitted α pooled and per city.

## 6. Exploratory (labelled exploratory everywhere)
- **NYC re-analysis:** §4 backtest on the existing sealed v2 NYC test predictions (2025-10-01 to 2026-07-14). Margins come from v2
  development OOF reproduced by refitting v2 in a scratch directory, which must reproduce the committed v2 state_sha256.
- **NYC v3** (`v3-nyc-0800`): v2 features with the LST window, on the v2 split (dev from 2022-12-01, test 2025-10-01 to 2026-07-14,
  audit 2026-07-15 to 2026-08-13).
- **Intraday:** cutoffs 11:00 and 14:00 local for NYC and the 5 pooled cities; same splits, refit per cutoff on development. Reported:
  the selected MOS run per cutoff; log loss of market_raw, market_recal, weather and fixed_blend; and the §4 P&L.
