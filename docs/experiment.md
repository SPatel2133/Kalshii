# Experiment specification `nyc-0800-v1`

Frozen before test scoring. Config: `configs/nyc-0800-v1.toml` (its SHA-256 is recorded in every evaluation summary).

| Item | Value |
|---|---|
| Series | `KXHIGHNY` (and its predecessor ticker `HIGHNY`, same series in the archive) |
| Regime | NWS Central Park CLI settlement only; The Weather Company regime (2026-08-14 →) is excluded as `other_regime` |
| Cutoff | 08:00 America/New_York on the event date (12:00Z in EDT, 13:00Z in EST) |
| Eligible contracts | listed, created and open at the cutoff; whole board must be an exclusive, exhaustive integer partition |
| Quotes | last hourly-candle `yes_bid.close`/`yes_ask.close` with `end_period_ts <= cutoff`; age ≤ 24 h; bid ≤ ask; bid=0&ask=1 = no information |
| Weather | IEM archive of GFS MOS (MAV) for KNYC; latest run with `runtime + 7.5 h <= cutoff`; predictor `n_x` valid at (D+1) 00Z |
| Label | the single `result = yes` contract; `expiration_value` = CLI high; all markets finalized; settlement must precede the fit boundary |
| Development | 2022-12-01 → 2025-09-30, five expanding chronological folds for out-of-fold training of blends/gates |
| Test | 2025-10-01 → 2026-07-14, untouched contiguous terminal block, scored once with the frozen model |
| Golden / audit | 2026-07-15 → 2026-08-13 (30 days, includes golden `KXHIGHNY-26AUG13`); development use only, never fitted on |
| Primary metric | mean winning-bucket log loss, probabilities floored at 1e-4 and renormalized |
| Secondary | multiclass Brier score; probability of the winner |
| Primary comparison | `adaptive_blend − fixed_blend` (paired per event) |
| Uncertainty | paired contiguous calendar-block bootstrap, 7-day blocks (3/14-day sensitivity), 2000 reps, seed 20260813 |
| Decision | "gain" only if the 95% interval upper end < 0 **and** the mean improves by ≥ 0.02 nats; a null result is a valid outcome |

## Model families

1. `market_raw`: normalized bid-ask midpoints.
2. `market_recal`: midpoints raised to one exponent `alpha`, renormalized.
3. `market_context`: `alpha` as a bounded function of spread, quote age, pre-cutoff movement and trade age.
4. `weather`: Gaussian with interval likelihood on half-degree edges; `mu` is linear in MOS `n_x` plus a seasonal term, and `log sigma` is seasonal.
5. `coherent`: `argmin_m KL(m || c) + λ Σ r_j dist(m_j, [bid_j, ask_j])²` on the simplex. `c` is the normalized midpoint, `r_j` are spread and age weights, and λ is chosen by OOF log loss.
6. `fixed_blend`: `w·coherent + (1−w)·weather`, with constant `w` fitted on OOF predictions.
7. `adaptive_blend`: `g(x)·coherent + (1−g(x))·weather`, where `g` is an L2-regularized logistic gate on JS disagreement, mean spread, max quote age and weather sigma. The L2 penalty is chosen on the last development fold.
8. `adaptive_corrected`: a bounded location/spread correction of `adaptive_blend`. It is reported but promoted only if it improves the validation objective.

If only one source is usable, the blends use it alone with an explicit reason (`source_only:market` or `source_only:weather`). If neither is usable, the result is `unavailable`.

## Weather window and availability evidence (gate before the joint model)

* **Location.** MOS station KNYC is Central Park, the CLINYC station named in the NWS-regime rules.
* **Window.** The CLI reports the max for the local calendar day, midnight to midnight LST. MOS `n_x` valid at (D+1) 00Z is the **daytime max 7am–7pm LST (12Z–00Z)**. The two windows are **not identical**, so `n_x` is a *proxy* for the settlement target. The weather model learns the proxy-to-CLI relationship only from development labels. Nights warmer than the following afternoon (frontal passages) are the known failure mode. An example is 2023-11-26: `n_x` 48, CLI 56.
* **Availability.** The IEM archive records `runtime` but not publication time, so a successful `sts`/`ets` query proves archive access only. Availability is therefore an **assumption**: `runtime + lag`. Observed evidence: the live MAV 12Z bulletin's tgftp `Last-Modified` header was 4 h 13 min after runtime. Primary lag is 7.5 h and the strict rerun uses 11 h. At the 08:00 cutoff (12Z/13Z) both lags select the **00Z run of day D**. The 06Z run (available ≥ 13:30Z) is never selected. The evaluation summary reports a strict-lag rerun of the test comparison.
* **Golden case.** `KXHIGHNY-26AUG13` uses the 00Z 2026-08-13 run, assumed available 07:30Z, before the 12:00Z cutoff. `n_x` = 87°F. CLI high 85°F (IEM AFOS `CLINYC` product 2026-08-14 06:18Z, used only as the label). Winner `B85.5`.

## Corrections to the brief, recorded from source payloads

* Archive candles appear only for hours with a book or trade change, so a quote's age is the time since its last change. The 3 h freshness rule in the draft rejected about 90% of 2023–24 boards that were otherwise valid. The rule is now a 24 h maximum age, with the age kept as a feature and as a quote weight.
* The coherent model's reference `c` is the normalized midpoint. With climatology as `c`, wide early-market bands let the prior dominate (OOF log loss 1.71 vs 1.48 for raw).
* 2021–22 single-threshold markets and events whose board is not a full partition are excluded with `invalid_bucket_partition` / `missing_rule_evidence`.
* Feb 2025 events have null strike fields. Their intervals come from the rule text alone, and the evidence is flagged.

## Versioned experiment `nyc-0800-v2`

V2 was opened after v1 test scoring. Its feature choices were based on v1's documented limitations, not on v1 test-set losses. It uses the same event splits, 08:00 local cutoff, NWS regime, 7.5-hour primary MOS availability lag, 11-hour strict sensitivity lag, scoring policy, thresholds, bootstrap settings, and seed. V1's config, work products, evidence, model IDs and golden fixture remain frozen.

The changes are opt-in through `configs/nyc-0800-v2.toml` and are stored under `data/work-v2/` and `evidence/nyc-0800-v2/`:

* **Empty latest book:** v2 excludes a bucket when its latest quoted candle at or before cutoff has finite bid ≤ 0 and ask ≥ 1. It does not carry forward an older quote behind that empty book. V1's default remains `skip`, preserving its carried-forward quote behavior.
* **Full local-day sampled MOS maximum:** the selected, strictly as-of run supplies 3-hourly `tmp` values from local midnight on event date through (but excluding) local midnight the following day. Bounds are converted through `America/New_York`, so the UTC window follows EST/EDT. At least seven samples are required. The existing `n_x` and its daytime-window meaning are unchanged; the sampled-minus-`n_x` difference supplements it.
* **Morning observations:** cached IEM ASOS `NYC` hourly temperatures from local midnight through `cutoff - 10 minutes` supply the maximum-so-far and latest observed temperature. The 10-minute latency guard is an assumption to avoid using an observation that may not yet have been available at the cutoff; it is not a verified archive publication timestamp. If no observations qualify, the event remains eligible, missing observation features are zero, and the row is flagged.
* **Weather model:** the mean model gains sampled-minus-`n_x`, observed-maximum-minus-`n_x`, and latest-observation-minus-`n_x` coefficients; seasonal sigma is unchanged. With no configured extra features, the weather-model fit retains the v1 parameterization.

The MOS availability assumption is unchanged: historical publication times cannot be proven because the IEM AFOS archive has no MAV bulletins. The primary `runtime + 7.5 h` lag and strict `runtime + 11 h` sensitivity are retained; v2 does not claim that either reproduces actual historical publication times. In what-if replays, `weather_delay_h` delays MOS selection only. It does not delay or exclude observations beyond the fixed observation-latency window.

Known limitations and out-of-scope items: Weather Company regime days from 2026-08-14 onward are too few and outside the test window; historical MOS publication timestamps are not archived; observations use hourly ASOS data and a chosen latency guard; no UI redesign is included. The demo can load a v2 model with the v2 config and model ID. V2 test scoring remains closed until the frozen experiment is explicitly opened.

## Versioned experiment v3

V3 is an opt-in multi-city paper-trading study. The confirmatory pool contains Chicago, Miami, Austin, Los Angeles, Denver, and Philadelphia; NYC remains exploratory because it motivated the hypothesis. Its configured `day_window = "lst"` uses each timezone's January 1 standard offset throughout the year, matching the NWS midnight-to-midnight LST climate day. The configured station phrase is checked against eligible NWS rules, and a mismatch is excluded as `other_regime`.

The v3 OOF file is separate from the model state. Upstream market and weather predictions are produced by fold-fitted models; fixed/adaptive blend parameters are fitted on development OOF rows and then applied to those predictions. Those downstream OOF blends are therefore dev-in-sample with respect to their blend parameters, not fully held-out predictions.

Paper-backtest decisions are label-blind and use the sealed split predictions with the matching cutoff feature quotes. YES and NO trades must clear the configured edge margin after integer-cent Kalshi fees; settlement then applies any slippage, fees, payout, and P&L. Reports include 0x/1x/2x fee scenarios, +1-cent slippage at 1x, a zero-pre-cutoff-volume exclusion sensitivity, and calendar-block confidence intervals. Margins are selected on development OOF P&L at 1x fees and frozen before a test backtest. The pooled calibration uses development outcomes only; fold-specific alpha may use only rows dated and settled before the fold cutoff. All split placeholders remain provisional until the separate preregistration is finalized. No v3 test-window fit, evaluation, or backtest is part of the acquisition/implementation phase.
