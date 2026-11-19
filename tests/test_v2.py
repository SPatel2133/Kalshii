"""V2 feature and compatibility checks; real-cache coverage skips when local data is absent."""
import copy
import json
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from scipy.optimize import minimize

from forecast_lab import config as C, contracts, dataset, evaluation as E, models as M, replay, weather
from forecast_lab import reasons as R
from forecast_lab.service import ReplayService, assert_outcome_free, top_level_allowlisted
from tests.synth import candle, markets

NY = ZoneInfo("America/New_York")


def test_empty_book_exclude_does_not_fall_back_but_skip_does():
    candles, _ = replay.canonical_candles([candle(100, 0.40, 0.50), candle(200, 0.0, 1.0)])
    excluded = replay.select_quote(candles, 200, 24, 6, empty_book="exclude")
    skipped = replay.select_quote(candles, 200, 24, 6, empty_book="skip")
    assert excluded["disposition"] == R.QUOTES_EMPTY_BOOK
    assert excluded["empty_book_obs_utc"] == "1970-01-01T00:03:20Z"
    assert "mid" not in excluded
    assert skipped["disposition"] == "ok" and skipped["mid"] == 0.45


@pytest.mark.parametrize(
    ("event_date", "start_utc", "end_utc"),
    [
        (date(2026, 1, 15), "2026-01-15T05:00:00Z", "2026-01-16T05:00:00Z"),
        (date(2026, 7, 15), "2026-07-15T04:00:00Z", "2026-07-16T04:00:00Z"),
    ],
)
def test_sampled_forecast_window_uses_dst_aware_local_midnight(event_date, start_utc, end_utc):
    lo = datetime.combine(event_date, time.min, NY).astimezone(timezone.utc)
    hi = datetime.combine(event_date + timedelta(days=1), time.min, NY).astimezone(timezone.utc)
    runtime = datetime.combine(event_date, time.min, timezone.utc)
    rows = [{"runtime": runtime, "ftime": runtime + timedelta(days=1), "station": "KNYC", "model": "GFS",
             "n_x": 80.0, "tmp": None}]
    for i in range(8):
        rows.append({"runtime": runtime, "ftime": lo + timedelta(hours=3 * i), "station": "KNYC", "model": "GFS",
                     "n_x": None, "tmp": float(50 + i)})
    rows.extend([
        {"runtime": runtime, "ftime": lo - timedelta(hours=3), "station": "KNYC", "model": "GFS",
         "n_x": None, "tmp": 98.0},
        {"runtime": runtime, "ftime": hi, "station": "KNYC", "model": "GFS", "n_x": None, "tmp": 99.0},
    ])
    selected = weather.run_forecast(rows, event_date, local_day=True)
    assert selected["sample_window_local_utc"] == [start_utc, end_utc]
    assert selected["n_samples_local"] == 8 and selected["sampled_max_local_f"] == 57.0


def test_observation_latency_excludes_recent_and_future_values():
    d = date(2026, 7, 15)
    cutoff = replay.cutoff_utc(d)
    rows = [
        (cutoff - timedelta(minutes=9), 90.0),
        (cutoff - timedelta(minutes=11), 88.0),
        (cutoff + timedelta(minutes=1), 95.0),
        (datetime.combine(d, time.min, NY).astimezone(timezone.utc), 70.0),
    ]
    summary = weather.observation_summary(rows, d, cutoff, 10)
    assert summary["n_obs"] == 2
    assert summary["obs_max_so_far_f"] == 88.0
    assert summary["obs_latest_f"] == 88.0
    assert summary["obs_latest_valid_utc"] == "2026-07-15T11:49:00Z"
    assert summary["window_utc"] == ["2026-07-15T04:00:00Z", "2026-07-15T11:50:00Z"]


def _old_weather_fit(nx, doys, lo, hi, l2=1e-3):
    X = M.weather_design(nx, doys)
    S = M.WeatherModel._sdesign(doys)
    a = np.array([-np.inf if x is None else x - 0.5 for x in lo])
    b = np.array([np.inf if x is None else x + 0.5 for x in hi])
    xm, xs = X[:, 1].mean(), X[:, 1].std() + 1e-9

    def nll(theta):
        beta, gamma = theta[:4], theta[4:]
        mu = X @ beta
        sig = np.exp(S @ gamma)
        ll = M.log_interval_mass((a - mu) / sig, (b - mu) / sig)
        return -ll.sum() + l2 * (theta[2:4] @ theta[2:4] + theta[5:] @ theta[5:])

    theta0 = np.array([0.0, 1.0, 0.0, 0.0, math.log(3.0), 0.0, 0.0])
    return minimize(nll, theta0, method="L-BFGS-B", options={"maxiter": 2000, "gtol": 1e-7})


def test_weather_model_without_extras_matches_frozen_v1_fit():
    rng = np.random.default_rng(18)
    nx = rng.normal(75, 12, 120)
    doys = rng.integers(1, 366, len(nx))
    values = np.rint(0.7 * nx + 20 + 5 * np.sin(doys / 365 * 2 * np.pi) + rng.normal(0, 3, len(nx))).astype(int)
    lo, hi = values.tolist(), values.tolist()
    old = _old_weather_fit(nx, doys, lo, hi)
    fitted = M.WeatherModel().fit(nx, doys, lo, hi)
    assert np.array_equal(fitted.beta, old.x[:4])
    assert np.array_equal(fitted.gamma, old.x[4:])
    assert "features" not in fitted.state()


def test_weather_model_extras_are_recorded_and_change_v2_mean():
    rng = np.random.default_rng(22)
    nx = rng.normal(75, 10, 150)
    extra = rng.normal(0, 1, len(nx))
    doys = rng.integers(1, 366, len(nx))
    values = np.rint(nx + 3 * extra + rng.normal(0, 2, len(nx))).astype(int)
    lo, hi = values.tolist(), values.tolist()
    base = M.WeatherModel().fit(nx, doys, lo, hi)
    expanded = M.WeatherModel().fit(nx, doys, lo, hi, extras=extra[:, None], names=("obs_delta",))
    assert expanded.state()["features"] == ["obs_delta"]
    assert len(expanded.beta) == 5 and len(expanded.gamma) == 3
    assert expanded.params(75, 180, [1.0])[0] != base.params(75, 180)[0]


def _v2_config():
    cfg = copy.deepcopy(C.default_config())
    cfg["experiment_id"] = "nyc-0800-v2"
    cfg["feature_version"] = "feat-v2"
    cfg["quotes"]["empty_book"] = "exclude"
    cfg["weather"]["features"] = ["nx", "sampled_minus_nx", "obs_max_minus_nx", "obs_latest_minus_nx"]
    cfg["weather"]["obs_latency_min"] = 10
    return cfg


class _SyntheticSource:
    def __init__(self, cfg, event_id="KXHIGHNY-26JUL15"):
        self.cfg = cfg
        self.events = {event_id: markets(event_id, (2026, 7, 15))}

    def candles(self, market):
        cut = replay.cutoff_utc(date(2026, 7, 15))
        return [candle(int(cut.timestamp()) - 1800, 0.09, 0.11)], {
            "sha256": "synthetic", "path": "synthetic", "params": {}, "retrieved_utc": "synthetic"
        }, None

    def mos_runs(self, d):
        runtime = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        lo = datetime.combine(d, time.min, NY).astimezone(timezone.utc)
        rows = [{"runtime": runtime, "ftime": runtime + timedelta(days=1), "station": "KNYC", "model": "GFS",
                 "n_x": 84.0, "tmp": None}]
        for i in range(8):
            rows.append({"runtime": runtime, "ftime": lo + timedelta(hours=3 * i), "station": "KNYC", "model": "GFS",
                         "n_x": None, "tmp": float(80 + i)})
        return weather.index_runs(rows), [{"sha256": "synthetic", "params": {}, "path": "synthetic"}]

    def asos(self, year):
        return []


def test_missing_observations_flag_and_zero_weather_inputs():
    cfg = _v2_config()
    src = _SyntheticSource(cfg)
    rules, label, _, row, _ = dataset.build_event(src, "KXHIGHNY-26JUL15")
    nx, extras, names = E.weather_inputs(cfg, row)
    assert row["weather"]["obs"]["n_obs"] == 0
    assert R.OBS_UNAVAILABLE in row["flags"]
    assert names == ["sampled_minus_nx", "obs_max_minus_nx", "obs_latest_minus_nx"]
    assert nx == 84.0 and extras[1:] == [0.0, 0.0]
    assert row["market_status"] == "complete"
    assert dataset.primary_reason(rules, label, row)[0] is None


def test_missing_local_samples_flag_and_zero_sampled_feature():
    cfg = _v2_config()
    src = _SyntheticSource(cfg)
    src.mos_runs = lambda d: (weather.index_runs([{
        "runtime": datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        "ftime": datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1),
        "station": "KNYC", "model": "GFS", "n_x": 84.0, "tmp": None
    }]), [{"sha256": "synthetic", "params": {}, "path": "synthetic"}])
    _, _, _, row, _ = dataset.build_event(src, "KXHIGHNY-26JUL15")
    nx, extras, _ = E.weather_inputs(cfg, row)
    assert nx == 84.0 and extras == [0.0, 0.0, 0.0]
    assert R.FORECAST_SAMPLED_MISSING in row["flags"]


def test_v2_pre_reveal_replay_is_outcome_free(synth_state):
    cfg = _v2_config()
    state = copy.deepcopy(synth_state)
    state["upstream"]["weather"] = None
    src = _SyntheticSource(cfg)
    svc = ReplayService(cfg, state, "adaptive_blend", src=src)
    response = svc.replay("KXHIGHNY-26JUL15")
    assert top_level_allowlisted(response)
    assert_outcome_free(response)


V1_CFG = C.default_config()
V1_DATASET = Path(V1_CFG["paths"]["work_dir"]) / "dataset"
V1_MODEL = V1_DATASET.parent / "models" / "nyc-0800-v1@2025-10-01.json"
V1_GOLDEN = Path("tests/fixtures/golden_KXHIGHNY-26AUG13.json")
HAVE_V1_CACHE = Path(V1_CFG["paths"]["cache_dir"], "manifest.jsonl").exists()
HAVE_V1_REAL_DATA = HAVE_V1_CACHE and (V1_DATASET / "features.jsonl").exists() and V1_GOLDEN.exists() and V1_MODEL.exists()


@pytest.mark.skipif(not HAVE_V1_CACHE, reason="local Kalshi/IEM cache is absent")
def test_v1_cached_rows_and_golden_fingerprints_remain_identical():
    if not HAVE_V1_REAL_DATA:
        pytest.skip("v1 dataset, golden fixture, or fitted state is absent")
    rows = {r["event_id"]: r for r in dataset.load_dataset(V1_DATASET)[0].values()}
    golden = json.loads(V1_GOLDEN.read_text())
    ids = sorted(rows)
    chosen = set(random.Random(20260813).sample(ids, min(20, len(ids))))
    chosen.add(golden["event_id"])
    src = dataset.Sources(V1_CFG, offline=True)
    for event_id in sorted(chosen):
        _, _, _, built, _ = dataset.build_event(src, event_id)
        assert built == rows[event_id]
    assert golden["replay_fingerprint"] == "9c32cfdcd738459b748446f44fc37a87a94d1e51f81e4fb59236aa816f71a382"
    assert golden["prediction_fingerprint"] == "c76afa0da28884179e5d629bdca69951c2a0134efae980d83c5010a504cd5904"
    state = json.loads(V1_MODEL.read_text())
    service = ReplayService(V1_CFG, state, "adaptive_blend", src=src)
    replayed = service.replay(golden["event_id"])
    assert replayed["replay_fingerprint"] == golden["replay_fingerprint"]
    assert replayed["prediction"]["prediction_fingerprint"] == golden["prediction_fingerprint"]

