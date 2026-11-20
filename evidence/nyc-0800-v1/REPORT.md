# Evidence report `nyc-0800-v1`

Config sha256 `c3a7b751c32834e26f13b16594e28f86fc9712e103f764865aa10d8fb9469d84` · model state `f5eaaa3f188d37908c379f1b41718b9b91fdcabd4bfd328538644c93683658c2` · fit boundary 2025-10-01 · 1033 development events.

Predictions were sealed before scoring: `predictions.jsonl` sha256 `fb3040a7655bc38f11c89429fa1cc35584d9e61ad740c943f6e45bfae5c26dab`.

## Answer

On the untouched test block 2025-10-01 → 2026-07-14 (n = 287 events, common cohort), the adaptive blend minus the fixed blend in mean log loss is **+0.00012** nats, 95% block-bootstrap CI [-0.00005, +0.00030]: **no supported difference** (threshold 0.02).

| Family | n | Log loss | Brier | P(winner) |
|---|---|---|---|---|
| market_recal | 287 | 0.9782 | 0.5296 | 0.466 |
| coherent | 287 | 0.9853 | 0.5322 | 0.451 |
| fixed_blend | 287 | 0.9853 | 0.5322 | 0.451 |
| adaptive_blend | 287 | 0.9854 | 0.5322 | 0.451 |
| market_context | 287 | 0.9870 | 0.5316 | 0.488 |
| market_raw | 287 | 0.9889 | 0.5330 | 0.445 |
| adaptive_blend_raw | 287 | 0.9892 | 0.5331 | 0.444 |
| adaptive_corrected | 287 | 1.0012 | 0.5334 | 0.482 |
| weather | 287 | 1.6037 | 0.7850 | 0.254 |
| climatology | 287 | 2.7108 | 1.2350 | 0.106 |

| Comparison (a − b) | Δ log loss | 95% CI (7-day) | 3-day | 14-day | Δ Brier | Decision |
|---|---|---|---|---|---|---|
| adaptive_blend-fixed_blend | +0.00012 | [-0.00005, +0.00030] | [-0.0001, +0.0003] | [-0.0000, +0.0003] | +0.00003 | no supported difference |
| adaptive_blend-market_raw | -0.00344 | [-0.00953, +0.00318] | [-0.0098, +0.0038] | [-0.0092, +0.0029] | -0.00081 | no supported difference |
| adaptive_blend-market_recal | +0.00719 | [-0.00130, +0.01559] | [-0.0030, +0.0162] | [-0.0013, +0.0156] | +0.00263 | no supported difference |
| fixed_blend-market_raw | -0.00356 | [-0.00970, +0.00309] | [-0.0100, +0.0037] | [-0.0094, +0.0029] | -0.00083 | no supported difference |

Strict weather lag (11.0 h): weather selection changed on 0 events; adaptive − fixed +0.00012 [-0.00005, +0.00030], no supported difference.

Fitted parameters: fixed-blend market weight w = 1.000; coherent λ = 100.0; correction promoted on validation = True.

Likely interpretation (an inference, not tested here): by 08:00 the market already prices in the public MOS forecast. The OOF fit put all of the fixed blend's weight on the coherent market model, and the regularized gate stays close to that constant, so neither blend can beat the market by the declared effect size. This is a valid null result. The weather model alone is clearly worse than the market (see table).

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

* Favourable: `KXHIGHNY-25DEC17` Δ = -0.1118
* Failure: `KXHIGHNY-26JAN25` Δ = +0.6100

## Golden replay

`KXHIGHNY-26AUG13`, cutoff 2026-08-13T12:00:00Z (08:00 EDT). The weather run is 2026-08-13T00:00:00Z (assumed available 2026-08-13T07:30:00Z) with n_x 87.0°F. The model probabilities are le84 0.630, 85-86 0.321, 87-88 0.035, 89-90 0.005, 91-92 0.005, ge93 0.005. Revealed: winner 85-86, CLI 85°F. Replay fingerprint `cb6c152619f21bdfe51c5f1f9ba96be07fc006049005b7831a31d19b58d2afbf`.

Audit-window mean log loss (development use only): adaptive 1.0381, fixed 1.0378, market_raw 1.0399.

## Files

* `eval-test/predictions.jsonl`: per-event predictions.
* `eval-test/evaluation_rows.jsonl`: per-event losses.
* `eval-test/summary.json`: aggregates.
* `model_manifest.json`: split manifest and fit diagnostics.
* `audit-*/coverage.csv`, `audit-*/sources.json`: fetch parameters and payload checksums.

Reproduce: see the README.
