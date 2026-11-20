# Evidence report `nyc-0800-v2`

Config sha256 `f07a5bd6dc945859c62273b909b8df1baf93a5d45ac0a648bb4ef61ac95c7f12` · model state `cee0c69b15ec491c0b239dd39fb039b91d1dd3cb43ae34090385911b90166678` · fit boundary 2025-10-01 · 1033 development events.

Predictions were sealed before scoring: `predictions.jsonl` sha256 `3bbdcb27d1acdb38cbc15e0a7b50314a780781334e9763a17566c5f81815631e`.

## Answer

On the untouched test block 2025-10-01 → 2026-07-14 (n = 287 events, common cohort), the adaptive blend minus the fixed blend in mean log loss is **+0.00129** nats, 95% block-bootstrap CI [+0.00036, +0.00229]: **loss** (threshold 0.02).

| Family | n | Log loss | Brier | P(winner) |
|---|---|---|---|---|
| market_recal | 287 | 0.9780 | 0.5295 | 0.468 |
| coherent | 287 | 0.9853 | 0.5322 | 0.451 |
| fixed_blend | 287 | 0.9853 | 0.5322 | 0.451 |
| adaptive_blend | 287 | 0.9866 | 0.5324 | 0.450 |
| market_context | 287 | 0.9872 | 0.5313 | 0.489 |
| market_raw | 287 | 0.9889 | 0.5330 | 0.445 |
| adaptive_blend_raw | 287 | 0.9904 | 0.5334 | 0.443 |
| adaptive_corrected | 287 | 0.9919 | 0.5320 | 0.480 |
| weather | 287 | 1.4440 | 0.7222 | 0.281 |
| climatology | 287 | 2.7108 | 1.2350 | 0.106 |

| Comparison (a − b) | Δ log loss | 95% CI (7-day) | 3-day | 14-day | Δ Brier | Decision |
|---|---|---|---|---|---|---|
| adaptive_blend-fixed_blend | +0.00129 | [+0.00036, +0.00229] | [+0.0003, +0.0022] | [+0.0004, +0.0022] | +0.00025 | loss |
| adaptive_blend-market_raw | -0.00227 | [-0.00829, +0.00433] | [-0.0085, +0.0049] | [-0.0081, +0.0044] | -0.00058 | no supported difference |
| adaptive_blend-market_recal | +0.00854 | [-0.00135, +0.01793] | [-0.0031, +0.0188] | [-0.0007, +0.0180] | +0.00294 | no supported difference |
| fixed_blend-market_raw | -0.00356 | [-0.00970, +0.00309] | [-0.0100, +0.0037] | [-0.0094, +0.0029] | -0.00083 | no supported difference |

Strict weather lag (11.0 h): weather selection changed on 0 events; adaptive − fixed +0.00129 [+0.00036, +0.00229], loss.

Fitted parameters: fixed-blend market weight w = 1.000; coherent λ = 100.0; correction promoted on validation = True.


## Coverage

| Stage | Test | Audit (30 days to 2026-08-13) |
|---|---|---|
| candidate_dates | 287 | 30 |
| catalogue | 287 | 30 |
| rules_valid | 287 | 30 |
| quote_complete | 287 | 30 |
| weather_available | 287 | 30 |
| label_valid | 287 | 30 |

Exclusion reasons (test): none; audit: none. Whole development window: see `coverage` in the build output. The Weather Company regime (2026-08-14 →) is excluded as `other_regime`.

## Held-out examples (adaptive_blend vs market_raw)

* Favourable: `KXHIGHNY-25DEC17` Δ = -0.1505
* Failure: `KXHIGHNY-26JAN25` Δ = +0.6100

## Golden replay

`KXHIGHNY-26AUG13`, cutoff 2026-08-13T12:00:00Z (08:00 EDT). The weather run is 2026-08-13T00:00:00Z (assumed available 2026-08-13T07:30:00Z) with n_x 87.0°F. The model probabilities are le84 0.630, 85-86 0.321, 87-88 0.035, 89-90 0.005, 91-92 0.005, ge93 0.005. Revealed: winner 85-86, CLI 85°F. Replay fingerprint `f3fbe1ba69e0003369794d656b7ba93ac19f51fadd869675d2ec6f898a351f05`.

Audit-window mean log loss (development use only): adaptive 1.0409, fixed 1.0378, market_raw 1.0399.

## Files

* `eval-test/predictions.jsonl`: per-event predictions.
* `eval-test/evaluation_rows.jsonl`: per-event losses.
* `eval-test/summary.json`: aggregates.
* `model_manifest.json`: split manifest and fit diagnostics.
* `audit-*/coverage.csv`, `audit-*/sources.json`: fetch parameters and payload checksums.

Reproduce: see the README.

## v2 vs v1 (paired, test events both experiments score)

| Comparison (v2 − v1) | n | v2 log loss | v1 log loss | Δ | 95% CI (7-day) | Sensitivity | Decision |
|---|---|---|---|---|---|---|---|
| weather only | 287 | 1.4440 | 1.6037 | -0.15974 | [-0.21790, -0.10413] | 14-day [-0.2263, -0.1009], 3-day [-0.2208, -0.1027] | gain |
| adaptive blend | 287 | 0.9866 | 0.9854 | +0.00117 | [+0.00027, +0.00214] | 14-day [+0.0004, +0.0021], 3-day [+0.0003, +0.0021] | loss |
| fixed blend | 287 | 0.9853 | 0.9853 | +0.00000 | [+0.00000, +0.00000] | 14-day [+0.0000, +0.0000], 3-day [+0.0000, +0.0000] | no supported difference |

Weather model mean coefficients: intercept +0.7601, nx +1.0303, cos -0.2539, sin +0.5368, sampled_minus_nx +0.6290, obs_max_minus_nx +0.5198, obs_latest_minus_nx +0.1224; log-sigma (intercept, cos, sin) +0.8857, -0.0628, +0.2534; converged True.
Fixed-blend market weight w = 1.000 (v1: 1.000).

## v2 coverage

* Test common cohort: v2 287 vs v1 287. v1 test events that relied on a quote carried forward behind an empty latest book: 0.
* Events with an empty-book bucket (excluded as `quotes_empty_book` in v2): development 221 (220 as primary reason), test 0.
* Full built window flags: `obs_unavailable` 1, `forecast_sampled_missing` 0.

## v2 golden (development only)

`KXHIGHNY-26AUG13`: run 2026-08-13T00:00:00Z (assumed available 2026-08-13T07:30:00Z), n_x 87.0°F, local-day sampled max 86°F, observed max so far 77°F, latest observation 72°F at 10:51Z (10-minute latency). Probabilities le84 0.630, 85-86 0.321, 87-88 0.035, 89-90 0.005, 91-92 0.005, ge93 0.005. Revealed winner 85-86, CLI 85°F.

## Conclusion

* Measured: the full-day proxy and morning observations make the weather-only model much better (Δ −0.160 nats, CI excludes 0, passes the 0.02 threshold).
* Measured: neither blend beats the market by the threshold. The fixed blend again puts all its weight on the coherent market model. The adaptive blend is slightly worse than the fixed blend (+0.0013, CI above 0) but far below the 0.02 threshold.
* Inferred, not tested: even the improved weather model (1.44) stays well behind the 08:00 market (0.99), so it adds no information the market has not already priced.
