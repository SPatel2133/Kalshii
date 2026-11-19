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

## V3 registered-data checkpoint

The v3 worktree is `/home/ubuntu/repos/Kalshii-v3`; its `data/cache` symlink points to the shared cache used by v2. Raw downloads stay under ignored `data/`. Catalogue requests and bulk downloads are resumable from the cache. The registered five-city pool excludes Denver; do not download Denver.

The five confirmatory city ranges are frozen in their configs and run sequentially with two workers. Each command appends progress to that city's log:

```sh
cd /home/ubuntu/repos/Kalshii-v3
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-chi-0800.toml download --workers 2 >> data/work-v3-chi/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-mia-0800.toml download --workers 2 >> data/work-v3-mia/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-aus-0800.toml download --workers 2 >> data/work-v3-aus/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-lax-0800.toml download --workers 2 >> data/work-v3-lax/download.log 2>&1
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-phil-0800.toml download --workers 2 >> data/work-v3-phil/download.log 2>&1
```

After all five cities finish, refresh NYC (2022-12-01 through 2026-08-13); most raw data is already cached:

```sh
cd /home/ubuntu/repos/Kalshii-v3
PYTHONPATH=. .venv/bin/python -m forecast_lab --config configs/v3-nyc-0800.toml download --workers 2 >> data/work-v3-nyc/download.log 2>&1
```

Do not launch a second copy for a city while its current download is running. If IEM reports a throttle response, stop the affected bulk run, wait/back off, and resume it with the same command; do not substitute stations. The five confirmatory ranges start at each city's preregistered `dev_start` and end on 2026-08-13.

The v2 erratum count artifact can be reproduced offline:

```sh
cd /home/ubuntu/repos/Kalshii-v3
PYTHONPATH=. .venv/bin/python scripts/v2_erratum.py
```
