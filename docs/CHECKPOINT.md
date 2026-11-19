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
