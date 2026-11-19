# Continuation checkpoint (working state)

Branch: devin/1795113555-forecast-lab. Raw cache: data/cache (gitignored). Work: data/work.
Data facts verified (2026-11-19 probes):
- Catalogue KXHIGHNY (archive, /historical/markets): 9586 markets, 1866 event dates 2021-08-06..2026-09-18.
  Tickers HIGHNY-* until 2024-10-23, KXHIGHNY-* after. Archive cutoff market_settled_ts=2026-09-19.
- 6-bucket NWS partition regime from 2022-12-22 (Daily Climate Report wording until 2025-03-04,
  "Climatological Report (Daily)" 2025-03-05..2026-08-13). TWC regime 2026-08-14..2026-09-18 (other_regime).
- 2021-2022 single-threshold "strictly greater than" markets: excluded (rules wording/partition).
- Feb 2025: 25 events with null strike fields; interval taken from rule text only (flagged in evidence).
- Golden KXHIGHNY-26AUG13: winner 85-86, CLI max 85 (IEM CLINYC 202608140618), 00Z MOS n_x 87.
- MOS: IEM mos.py needs sts/ets. n_x at (D+1)00Z = daytime max 12Z-00Z. Observed MAV 12Z publish lag
  4h13m (tgftp Last-Modified 2026-11-19 16:13Z for 12Z cycle). Lag policy 7.5h primary, 11h strict:
  both select the 00Z run of day D at the 08:00 cutoff.
Next: finish candle download (/tmp/dl.py), build dataset, fit, evaluate, demo, tests, docs.
