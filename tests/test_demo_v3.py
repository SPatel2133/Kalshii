import gzip
import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from forecast_lab import __main__ as cli
from forecast_lab.config import load as load_config
from forecast_lab.demo import default_event, make_handler
from forecast_lab.fetch import FetchError
from forecast_lab.service import ReplayService, load_state


class DemoService:
    family = "adaptive_blend"


def test_default_event_prefers_configured_golden():
    cfg = {"split": {"golden_event": "KXHIGHNY-26AUG13"}}
    assert default_event(cfg, {}) == "KXHIGHNY-26AUG13"


def test_default_event_uses_latest_nws_event_in_audit_window():
    cfg = load_config("configs/v3-nyc-0800.toml")
    events = {
        "KXHIGHNY-26AUG12": [{"rules_primary": "The Weather Company"}],
        "KXHIGHNY-26AUG13": [{"rules_primary": "National Weather Service"}],
        "KXHIGHNY-26AUG14": [{"rules_primary": "The Weather Company"}],
    }
    assert default_event(cfg, events) == "KXHIGHNY-26AUG13"


def test_default_event_falls_back_to_latest_event():
    cfg = {"split": {"audit_start": "2026-08-01", "audit_end": "2026-08-13"}}
    events = {
        "KXHIGHNY-26AUG12": [{"rules_primary": "The Weather Company"}],
        "KXHIGHNY-26AUG14": [{"rules_primary": "The Weather Company"}],
    }
    assert default_event(cfg, events) == "KXHIGHNY-26AUG14"


def test_golden_command_exits_clearly_without_golden_event(monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda args: {"split": {}})
    with pytest.raises(SystemExit, match="golden command requires split.golden_event"):
        cli.cmd_golden(SimpleNamespace())


def test_showcase_reads_gzipped_evaluation_rows(tmp_path):
    evaluation_dir = tmp_path / "eval-test"
    evaluation_dir.mkdir()
    rows = [
        {"event_id": "KXHIGHNY-26JUL15", "family": "adaptive_blend", "log_loss": 0.1},
        {"event_id": "KXHIGHNY-26JUL15", "family": "market_raw", "log_loss": 0.2},
        {"event_id": "KXHIGHNY-26JUL16", "family": "adaptive_blend", "log_loss": 0.4},
        {"event_id": "KXHIGHNY-26JUL16", "family": "market_raw", "log_loss": 0.2},
    ]
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    (evaluation_dir / "evaluation_rows.jsonl.gz").write_bytes(gzip.compress(raw))

    cfg = {"paths": {"evidence_dir": str(tmp_path)}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DemoService(), cfg))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/showcase", timeout=5) as response:
            body = json.loads(response.read())
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert body["available"] is True
    assert body["favourable"] == "KXHIGHNY-26JUL15"
    assert body["failure"] == "KXHIGHNY-26JUL16"


def test_chi_demo_events_and_development_replay_without_audit_window():
    cfg = load_config("configs/v3-chi-0800.toml")
    state_path = Path(cfg["paths"]["work_dir"]) / "models" / (
        f"{cfg['experiment_id']}@{cfg['split']['test_start']}.json"
    )
    features_path = Path(cfg["paths"]["work_dir"]) / "dataset" / "features.jsonl"
    if not Path(cfg["paths"]["cache_dir"]).is_dir() or not state_path.is_file() or not features_path.is_file():
        pytest.skip("v3 CHI cache and fitted state are required for the offline demo test")

    try:
        state, family = load_state(
            cfg, f"{cfg['experiment_id']}.adaptive_blend@{cfg['split']['test_start']}"
        )
        svc = ReplayService(cfg, state, family)
    except FetchError as exc:
        if exc.failure_class == "offline_cache_miss":
            pytest.skip("v3 CHI raw cache is incomplete for the offline demo test")
        raise

    event_id = "KXHIGHCHI-26FEB09"
    assert date.fromisoformat("2026-02-09") < date.fromisoformat(cfg["split"]["test_start"])
    assert event_id in svc.src.events
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc, cfg))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/events", timeout=5) as response:
            events_body = json.loads(response.read())
            assert response.status == 200
        assert events_body["golden"] in {row["event_id"] for row in events_body["events"]}
        assert any(row["event_id"] == event_id and row["split"] == "development"
                   for row in events_body["events"])

        with urlopen(f"http://127.0.0.1:{server.server_port}/api/replay?event={event_id}", timeout=10) as response:
            replay_body = json.loads(response.read())
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert replay_body["event_id"] == event_id
    assert replay_body["split"].startswith("development")
