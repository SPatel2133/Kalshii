# Experiment v3 report: would this have made money after fees?

**Pre-registration SHA:** `074fcdea36fc0597b3384b7774d1e8203dad00ae` (`docs/v3-preregistration.md`). Development evidence
(fits, frozen margins, pooled α) was committed in `e0cb831f8e68fd9ca4f6ca56f4fedfc5cbc2bf82` before any test-window scoring.
Test evidence: `e279e97905e2a35e9d170a3e257d86d8dfe32fc2`. Research only: historical paper simulation, no orders.

## Answer
**No.** In the pre-registered test (5 cities, 1,140 events, 2025-06-24 to 2026-08-13, 08:00 local), the market-recalibration strategy
lost **$0.0010 per event per 1-contract unit** at 1× fees (7-day block 95% CI [−0.0049, +0.0028]; 28 trades). That is "no supported
profit". The log-loss hypothesis H1 is also **not supported**: Δ = −0.0063 nats, CI [−0.0146, +0.0016]. No strategy is profitable under
the decision rule at 08:00. The exploratory 14:00 cutoff shows a real calibration gain (H1-style Δ = −0.0187, CI excludes 0), but its
P&L CI still includes 0.

## 0. Verified facts
- **CLI day = midnight-to-midnight LST** (NWS Directive 10-1004 §4.1.2). The v2 erratum changed `obs_max_so_far_f` for 76 of 287 test
  events and 223 of 811 scored development events. `sampled_max_local_f` changed for 0. See `docs/v2-erratum.md` and `evidence/v3/v2-erratum.json`.
- **Fees:** taker fee = ceil-to-cent(0.07 × C × P × (1 − P)), multiplier 1 for every KXHIGH series (Kalshi Fee Schedule, effective
  2026-07-07; API `fee_multiplier = 1`; no KXHIGH fee changes). See `docs/fees.md`.

## 1. Cities, switch dates and coverage (08:00)
Every city's last NWS-regime event was 2026-08-13; The Weather Company rules start 2026-08-14. Denver was dropped because its rules
are ambiguous ("Denver, CO").

| City | Station phrase | Dev | Test | Test cohort | Funnel (dates → label valid) | Final α |
|---|---|---|---|---|---|---|
| chi | Chicago Midway | 2025-03-05 → 2026-02-09 | 2026-02-10 → 2026-08-13 | 185 | 185 → 185 | 1.363 |
| mia | Miami International Airport | 2025-04-26 → 2026-02-27 | 2026-02-28 → 2026-08-13 | 166 | 167 → 166 (1 settlement_exceptional) | 1.260 |
| aus | Austin Bergstrom | 2023-05-11 → 2025-06-23 | 2025-06-24 → 2026-08-13 | 416 | 416 → 416 | 1.105 |
| lax | Los Angeles Airport | 2025-01-05 → 2026-01-19 | 2026-01-20 → 2026-08-13 | 206 | 206 → 206 | 1.174 |
| phil | Philadelphia International Airport | 2025-04-26 → 2026-02-27 | 2026-02-28 → 2026-08-13 | 167 | 167 → 167 | 1.203 |
| **pooled α** | | | | **1,140** | | **1.195** |

Pooled OOF fold αs range from 1.01 to 1.19 (`evidence/v3/pooled/pooled_state.json`). Candle downloads finished with 0 failures.

## 2. Confirmatory hypotheses (08:00, NYC excluded)
| Hypothesis | Estimate | 7-day 95% CI | 3-day / 14-day CI | Decision |
|---|---|---|---|---|
| H1: logloss(market_recal_pooled) − logloss(market_raw) | −0.0063 nats | [−0.0146, +0.0016] | [−0.0143, +0.0020] / [−0.0142, +0.0014] | **not supported** (needs CI < 0 and ≤ −0.01; also fails 0.02) |
| H2: market_recal_pooled P&L per event, 1× fees (margin 0.05) | −$0.0010 | [−0.0049, +0.0028] | — | **no supported profit** (bootstrap p = 0.68) |

Per city (secondary; one-sided bootstrap p, Holm across 5 cities):

| City | H1 Δ | H1 CI | p / Holm | H2 P&L/event | H2 CI | p / Holm |
|---|---|---|---|---|---|---|
| chi | −0.0005 | [−0.0200, +0.0190] | 0.47 / 0.94 | −$0.0028 | [−0.0083, 0] | 1.00 / 1.00 |
| mia | −0.0098 | [−0.0281, +0.0101] | 0.17 / 0.58 | −$0.0008 | [−0.0152, +0.0120] | 0.54 / 1.00 |
| aus | −0.0091 | [−0.0221, +0.0035] | 0.09 / 0.45 | −$0.0002 | [−0.0068, +0.0062] | 0.55 / 1.00 |
| lax | −0.0092 | [−0.0252, +0.0093] | 0.14 / 0.58 | −$0.0002 | [−0.0143, +0.0135] | 0.50 / 1.00 |
| phil | +0.0013 | [−0.0184, +0.0233] | 0.52 / 0.94 | −$0.0022 | [−0.0154, +0.0091] | 0.62 / 1.00 |

## 3. Paper P&L per strategy, pooled 5 cities, 08:00 (per 1-contract unit; mean P&L per event, 7-day 95% CI)
| Strategy | Trades (1×) | 0× fees | **1× fees** | 2× fees | 1× + 1¢ slippage | ROI (1×) | Max DD (1×) |
|---|---|---|---|---|---|---|---|
| market_raw (control) | 55 | −0.0019 | **+0.0012 [−0.0042, +0.0075]** | −0.0006 | +0.0008 | +6.0% | $3.65 |
| market_recal (city α) | 955 | +0.0135 | **−0.0084 [−0.0338, +0.0161]** | −0.0079 | −0.0168 | −1.3% | $14.84 |
| market_recal_pooled | 28 | +0.0030 | **−0.0010 [−0.0049, +0.0028]** | +0.0001 | −0.0013 | −6.8% | $2.14 |
| fixed_blend | 30 | +0.0037 | **−0.0012 [−0.0046, +0.0023]** | −0.0001 | −0.0015 | −11.1% | $2.14 |
| adaptive_blend | 421 | +0.0016 | **−0.0068 [−0.0183, +0.0045]** | −0.0086 | −0.0105 | −5.3% | $7.95 |
| weather | 2,633 | −0.0106 | **−0.0532 [−0.0889, −0.0184]** | −0.0739 | −0.0764 | −7.0% | $67.14 |
| favourite_yes | 1,128 | −0.0001 | **−0.0196 [−0.0473, +0.0079]** | −0.0383 | −0.0295 | −3.6% | $30.88 |

Every strategy's decision is "no supported profit". The liquidity cut drops 0 events for every strategy except weather (1); results
are unchanged. Before fees, favourite_yes (buying at the ask) breaks even (−$0.0001); fees are what make it lose.

## 4. Secondary log-loss comparisons (08:00)
| City | market_raw | market_recal | weather | fixed_blend | adaptive_blend | fixed w | fixed − raw [CI] | adaptive − fixed [CI] |
|---|---|---|---|---|---|---|---|---|
| chi | 1.1392 | 1.1502 | 1.7346 | 1.1398 | 1.1398 | 1.000 | +0.0007 [−0.0082, +0.0146] | 0.0000 |
| mia | 0.9835 | 0.9746 | 1.3169 | 0.9774 | 0.9774 | 1.000 | −0.0061 [−0.0095, −0.0023] | 0.0000 |
| aus | 1.0976 | 1.0903 | 1.5683 | 1.0969 | 1.0970 | 0.989 | −0.0007 [−0.0066, +0.0072] | +0.0001 [−0.0014, +0.0014] |
| lax | 1.0103 | 1.0011 | 1.3175 | 1.0077 | 1.0371 | 1.000 | −0.0025 [−0.0050, +0.0004] | +0.0294 [+0.0049, +0.0517] |
| phil | 1.1371 | 1.1387 | 1.6983 | 1.1335 | 1.1443 | 0.974 | −0.0036 [−0.0209, +0.0084] | +0.0108 [−0.0004, +0.0242] |
| pooled | 1.0777 | 1.0742 | 1.5324 | 1.0757 | 1.0826 | | −0.0020 [−0.0064, +0.0022] | +0.0069 [+0.0015, +0.0122] |

Weather alone trails the market by 0.31–0.60 nats in every city, and the fixed blend puts ≥ 97% of its weight on the market. The
adaptive gate hurts out of sample (pooled +0.0069, CI excludes 0; LAX +0.029). Strict 11 h MOS lag: it changed the selected run for
26 (CHI), 126 (AUS) and 206 (LAX) test events and 0 elsewhere. No blend decision changes, except that LAX adaptive's losses lose
significance. H1/H2 use market data only and do not depend on the lag.

## 5. Exploratory: NYC
- **v2 re-analysis** (sealed v2 test predictions, 287 events, margins from reproduced v2 OOF; the refit matched v2 state
  `cee0c69b…`): market_raw, market_recal and adaptive_blend made 0 trades; fixed_blend −$0.0009 (1 trade); weather −$0.045
  [−0.101, +0.011]; favourite_yes +$0.005 [−0.046, +0.056] at 1×, −$0.004 with slippage. No supported profit.
  `evidence/v3/nyc-reanalysis/`.
- **v3-nyc-0800** (LST window): weather log loss 1.4411 (v2: 1.4440); adaptive − fixed +0.0012 [+0.0003, +0.0021]; fixed − raw
  −0.0036 [−0.0097, +0.0031]. Audit window (30 events): market_recal 1.0316 vs market_raw 1.0399. The backtest matches the
  re-analysis: no supported profit.

## 6. Exploratory: intraday cutoff profile
Selected MOS run: at 08:00 the 00Z run for NYC, MIA and PHIL. LAX uses the 06Z run every day (available 13:30Z, before 08:00
PST/PDT), and CHI/AUS use it in standard time (08:00 CST = 14:00Z), otherwise 00Z. At 11:00 and 14:00 every city uses the 06Z run.
The 12Z run (available 19:30Z) is never eligible by 14:00.

| Cutoff | Pooled n | market_raw | market_recal | weather | fixed_blend | Weather − market | H1-style Δ (pooled α) [7-day CI] | market_recal_pooled P&L/event, 1× [CI] | Best 1× P&L/event |
|---|---|---|---|---|---|---|---|---|---|
| 08:00 | 1,140 | 1.0777 | 1.0742 | 1.5324 | 1.0757 | +0.455 | −0.0063 [−0.0146, +0.0016] | −$0.0010 [−0.0049, +0.0028] | market_raw +$0.0012 [−0.0042, +0.0075] |
| 11:00 | 1,139 | 0.9447 | 0.9386 | 1.4384 | 0.9452 | +0.494 | −0.0093 [−0.0146, −0.0043] | +$0.0014 [−0.0012, +0.0039] (11 trades) | market_raw +$0.0022 [−0.0033, +0.0079] |
| 14:00 | 1,139 | 0.5777 | 0.5607 | 1.1989 | 0.5733 | +0.621 | −0.0187 [−0.0274, −0.0104] | +$0.0042 [−0.0019, +0.0105] (86 trades) | market_recal_pooled +$0.0042 |

- The market's lead over weather does **not** shrink later in the day; it grows (0.46 → 0.62 nats), probably because the market
  absorbs the day's observations faster than the 06Z MOS run plus our observation features (inference, not tested).
- Recalibration helps more later in the day: the 14:00 pooled Δ −0.0187 would pass the 0.01 bar. Treat that as a new hypothesis
  for a pre-registered follow-up (it was not the pre-registered cutoff), not as a result. Pooled α rises to 1.28.
- No pooled strategy is profitable at any cutoff. Two small cells pass the decision rule: MIA 14:00 market_recal_pooled
  (11 trades, +$0.0134/event, CI [+0.0054, +0.0232], +$0.0127 with slippage, Holm p < 0.001) and NYC 14:00 market_recal (4 trades, +$0.0039). Both come from a handful
  of trades in an exploratory grid of 7 strategies × 6 cities × 3 cutoffs, so they are probably noise.

## 7. Post-hoc sensitivity: leak-free pooled fit (exploratory)
The confirmatory pooled α and pooled margin were fitted on all cities' development events up to each city's own `test_start`,
so some of those events are dated inside the AUS/LAX test windows. This rerun, done after the confirmatory scoring, fits
both only on development events dated before 2025-06-24 (the earliest city `test_start`) and settled before that day's
first test cutoff (12:00 UTC). It then rescores the same
1,140-event cohort, which is unchanged in its hash and per-city counts. Config: `configs/v3-pooled-leakfree.toml`. Evidence:
`evidence/v3/pooled-leakfree/`. The confirmatory dev stage reruns byte-identically.

| | Confirmatory | Leak-free |
|---|---|---|
| Dev rows in α fit | 2,080 | 1,138 |
| Pooled α / margin | 1.1952 / 0.05 | 1.1546 / 0.05 |
| H1 Δ log loss, 7-day CI | −0.0063 [−0.0146, +0.0016] | −0.0065 [−0.0131, −0.0002] |
| H1 3-day / 14-day CI | ≈[−0.0143, +0.0020] / ≈[−0.0142, +0.0014] | [−0.0128, +0.0001] / [−0.0128, −0.0004] |
| H2 P&L per event (trades) | −$0.0010 [−0.0049, +0.0028] (28) | +$0.0005 [−0.0021, +0.0031] (12) |
| H2 +1¢ slippage, bootstrap p | ≈−$0.0013, 0.68 | +$0.0004, 0.37 |

Neither hypothesis is supported. H1's 7-day CI just excludes zero, but the estimate misses the pre-registered −0.01 effect size, and the 3-day CI includes zero. No city is significant after Holm correction (smallest adjusted p: AUS H1 0.248, LAX H2 0.135). The conclusion does not change.

## Limitations
- **Fill assumption:** orders fill in full at the as-of bid/ask with no depth or queue (candles carry no depth). This is the main
  optimism; +1¢ slippage only partly covers it.
- The July 2026 fee schedule is applied to the whole history.
- Pooled α and the pooled margin use other cities' development events whose calendar dates overlap the AUS/LAX test windows
  (pre-registered design; no event's own label is used). A leak-free rerun (§7) gives the same conclusion.
- MOS publication time is assumed (runtime + 7.5 h); AFOS has no historical MAV bulletins.
- Dev OOF margins for the blends apply final development downstream parameters to OOF upstream predictions.
- Excluded on rules-text grounds: Denver; "Miami, FL" and "Philadelphia, PA" wordings; the legacy CHI "NWS's Daily Climate Report"
  wording (1,291 events).

## Files
Per city: `evidence/v3/<city>-<hhmm>/{model_manifest.json, eval-test/, backtest-test/}` (sealed `predictions.jsonl.gz`,
`evaluation_rows.jsonl.gz`, `trades.jsonl.gz`). Pooled: `evidence/v3/pooled[-1100|-1400]/{pooled_state.json, summary.json,
predictions.jsonl.gz, trades.jsonl.gz}`. Reproduce: `docs/CHECKPOINT.md`.

Per-event files are deterministic gzip; CHECKSUMS.sha256 lists uncompressed and compressed hashes; sealed hashes are of the uncompressed bytes.
