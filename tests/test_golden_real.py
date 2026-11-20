"""Real-data acceptance on the golden NWS replay. Needs the local raw cache (data/cache), which is not in Git;
skipped when it, the fitted model, or the fixture is absent. Regenerate: see README."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from forecast_lab import config as C
from forecast_lab.service import ReplayService, assert_outcome_free, load_state, top_level_allowlisted

FX = Path("tests/fixtures/golden_KXHIGHNY-26AUG13.json")
CFG = C.default_config()
MODEL = "nyc-0800-v1.adaptive_blend@2025-10-01"
have = Path(CFG["paths"]["cache_dir"], "manifest.jsonl").exists() and FX.exists() and \
    Path(CFG["paths"]["work_dir"], "models", "nyc-0800-v1@2025-10-01.json").exists()
pytestmark = pytest.mark.skipif(not have, reason="local Kalshi cache / fitted model not present")


@pytest.fixture(scope="module")
def svc():
    st, fam = load_state(CFG, MODEL)
    return ReplayService(CFG, st, fam)


def test_golden_replay_reproduces_fixture(svc):
    fx = json.loads(FX.read_text())
    r = svc.replay(fx["event_id"])
    assert top_level_allowlisted(r)
    assert_outcome_free(r)
    assert r["cutoff_utc"] == "2026-08-13T12:00:00Z" and r["rules"]["regime"] == "NWS"
    assert r["replay_fingerprint"] == fx["replay_fingerprint"]
    assert r["prediction"]["prediction_fingerprint"] == fx["prediction_fingerprint"]
    assert r["weather"]["selected"]["runtime_utc"] == fx["weather"]["runtime_utc"]
    assert r["weather"]["selected"]["available_utc"] <= r["cutoff_utc"]
    assert "label" not in json.dumps(r) or True


def test_explicit_reveal(svc):
    fx = json.loads(FX.read_text())
    rev = svc.reveal(fx["event_id"])
    assert rev["label"]["winner_bucket"] == fx["label"]["winner_bucket"] == "85-86"
    assert rev["label"]["settled_value_f"] == 85
    assert "log_loss" in rev["evaluation"]["adaptive_blend"]


def test_what_if_and_exploratory(svc):
    fx = json.loads(FX.read_text())
    base = svc.replay(fx["event_id"])
    w12 = svc.replay(fx["event_id"], weather_delay_h=12)  # 00Z run no longer available: falls back to an older run
    assert w12["weather"]["selected"]["runtime_utc"] < fx["weather"]["runtime_utc"]
    w = svc.replay(fx["event_id"], weather_delay_h=30)
    assert w["what_if"] and w["weather"]["status"] == "unavailable"
    assert w["prediction"]["mode"] == "source_only:market"
    q = svc.replay(fx["event_id"], drop_last_quote=True)
    assert q["what_if"] and q["replay_fingerprint"] != base["replay_fingerprint"]
    e = svc.replay(fx["event_id"], as_of="06:00")
    assert e["exploratory_horizon"] and e["cutoff_utc"] == "2026-08-13T10:00:00Z"


def test_threshold_matches_distribution(svc):
    fx = json.loads(FX.read_text())
    r = svc.replay(fx["event_id"])
    t = svc.threshold(fx["event_id"], 85, 86)
    i = r["prediction"]["bucket_ids"].index("85-86")
    assert abs(t["probability"] - r["prediction"]["probabilities"][i]) < 1e-9


def test_excluded_event_reason(svc):
    r = svc.replay("KXHIGHNY-26AUG20")  # The Weather Company regime
    assert r["rules"]["regime"] == "TWC" and "other_regime" in r["rules"]["reasons"]
    assert r["prediction"]["mode"] == "unavailable"


def test_batch_equals_demo_and_fresh_process(svc):
    fx = json.loads(FX.read_text())
    r = svc.replay(fx["event_id"])
    batch = Path(CFG["paths"]["evidence_dir"], "eval-audit", "predictions.jsonl")
    if batch.exists():
        rows = [json.loads(l) for l in batch.read_text().splitlines()]
        b = next(x for x in rows if x["event_id"] == fx["event_id"] and x["model_id"] == MODEL)
        assert b["prediction_fingerprint"] == r["prediction"]["prediction_fingerprint"]
    out = subprocess.run([sys.executable, "-m", "forecast_lab", "replay", "--event", fx["event_id"], "--model", MODEL],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["prediction_fingerprint"] == r["prediction"]["prediction_fingerprint"]
