# Continuation checkpoint

If a session ends before the release gate, resume with:

```sh
.venv/bin/python -m forecast_lab download          # resumable; cached requests are skipped
.venv/bin/python -m forecast_lab build && .venv/bin/python -m forecast_lab fit
.venv/bin/python -m forecast_lab evaluate --split test && .venv/bin/python -m forecast_lab evaluate --split audit
.venv/bin/python -m forecast_lab golden --model nyc-0800-v1.adaptive_blend@2025-10-01
.venv/bin/python -m forecast_lab audit --series KXHIGHNY --end 2026-08-13 --days 30 --offline
.venv/bin/python -m pytest -q
```

Release gates: golden real replay (tests/test_golden_real.py), explicit reveal, held-out test comparison vs fixed blend (evidence/nyc-0800-v1/eval-test/summary.json).

## V3 acquisition checkpoint

The v3 worktree is `/home/ubuntu/repos/Kalshii-v3`; its `data/cache` symlink points to the shared cache used by v2. Raw downloads stay under ignored `data/`. Catalogue requests use the resumable page cursor in each city's work directory; re-running the same command resumes from cached pages. Bulk downloads are also resumable because each raw request is content-addressed in the shared cache.

Catalogue commands (run one series at a time):

```sh
cd /home/ubuntu/repos/Kalshii-v3
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-chi-0800.toml catalogue
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-mia-0800.toml catalogue
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-aus-0800.toml catalogue
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-lax-0800.toml catalogue
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-den-0800.toml catalogue
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-phil-0800.toml catalogue
```

Once the six new catalogues are cached, derive provisional splits from catalogue rules only (no labels or prices):

```sh
cd /home/ubuntu/repos/Kalshii-v3
PYTHONPATH=. .venv/bin/python -m forecast_lab v3-splits --pooled configs/v3-pooled.toml
```

Resume any city's candles, MOS, and ASOS downloads with the matching command, appending to its progress log:

```sh
cd /home/ubuntu/repos/Kalshii-v3
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-chi-0800.toml download --workers 2 >> data/work-v3-chi/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-mia-0800.toml download --workers 2 >> data/work-v3-mia/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-aus-0800.toml download --workers 2 >> data/work-v3-aus/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-lax-0800.toml download --workers 2 >> data/work-v3-lax/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-den-0800.toml download --workers 2 >> data/work-v3-den/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-phil-0800.toml download --workers 2 >> data/work-v3-phil/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-nyc-0800.toml download --workers 2 >> data/work-v3-nyc/download.log 2>&1
```

Do not launch a second copy for a city while its current download is running. If IEM reports a throttle response, stop the affected bulk run, wait, and resume it with the same command; do not substitute stations. The six-city candle range is 2021-01-01 through 2026-08-31; NYC starts 2022-12-01.
