# Kalshi Forecast Lab

**Product contract.** Rewind an actual Kalshi market to 8 a.m. New York time. Show the historical market odds, an independent weather forecast, and a trained model's forecast. Then reveal the settled outcome. On later events, test whether the learned model improves on the market and a strong fixed blend.

Series: `KXHIGHNY` (NYC Central Park daily high, NWS CLI settlement regime). The project uses public read-only data only. It does not trade, does not use credentials, and runs the demo locally.

Spec and timing assumptions: [docs/experiment.md](docs/experiment.md). Results: [evidence/nyc-0800-v1/REPORT.md](evidence/nyc-0800-v1/REPORT.md).

## Versioned experiment `nyc-0800-v2`

V2 is a separate experiment, opened after v1 test scoring. It was chosen from v1's documented limitations, not from v1 test-set losses. Its frozen configuration is `configs/nyc-0800-v2.toml`; it adds an America/New_York full-day sampled MOS maximum, cached morning ASOS observations with a 10-minute latency guard, an expanded weather model, and explicit exclusion of empty latest quote books. The v1 configuration, evidence, fixture and work directory remain unchanged. Details and assumptions are in [docs/experiment.md](docs/experiment.md).

Build, fit, and inspect only the development and audit windows while the test block is closed:

```sh
$PY -m forecast_lab --config configs/nyc-0800-v2.toml build
$PY -m forecast_lab fit --config configs/nyc-0800-v2.toml
$PY -m forecast_lab evaluate --config configs/nyc-0800-v2.toml --split audit
$PY -m forecast_lab golden --config configs/nyc-0800-v2.toml --model nyc-0800-v2.adaptive_blend@2025-10-01
```

The v2 `weather_delay_h` what-if delays only the MOS run; ASOS observations retain their configured cutoff-latency window. V2 has not been scored on the test split.

## Reproduce

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export PY=.venv/bin/python
$PY -m forecast_lab audit --series KXHIGHNY --end 2026-08-13 --days 30   # fetch + cache + 30-day coverage audit
$PY -m forecast_lab download                                           # all candles/MOS for the experiment window (resumable)
$PY -m forecast_lab fit --config configs/nyc-0800-v1.toml
$PY -m forecast_lab evaluate --config configs/nyc-0800-v1.toml --split test
$PY -m forecast_lab replay --event KXHIGHNY-26AUG13 --as-of 08:00 --model nyc-0800-v1.adaptive_blend@2025-10-01
$PY -m forecast_lab replay --event KXHIGHNY-26AUG13 --model nyc-0800-v1.adaptive_blend@2025-10-01 --reveal
$PY -m forecast_lab demo --model nyc-0800-v1.adaptive_blend@2025-10-01  # http://localhost:8765/
$PY -m pytest -q
```

Raw Kalshi and IEM responses are cached under `data/cache/`, keyed by request and SHA-256. They are kept out of Git because redistribution terms are unclear. `evidence/` contains the fetch parameters, payload checksums, coverage tables, per-event predictions and summaries. `tests/fixtures/` contains only the minimal derived golden-case fields. The tests run offline. Tests that need the local cache are skipped when it is absent.

## Layout

`forecast_lab/`
- `fetch.py`: cache, retries and manifest.
- `kalshi.py`: catalogue and candles.
- `contracts.py`: rules, partition and labels.
- `weather.py`: IEM MOS.
- `replay.py`: strict as-of snapshot.
- `dataset.py`: build and coverage funnel.
- `models.py`: all model families.
- `evaluation.py`: chronological fit, scoring and block bootstrap.
- `service.py`: outcome-free replay and explicit reveal.
- `demo.py` + `demo.html`: local screen.
