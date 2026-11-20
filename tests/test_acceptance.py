import copy
import random
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from forecast_lab import contracts, replay, weather
from forecast_lab import evaluation as E
from forecast_lab import models as M
from forecast_lab import reasons as R
from forecast_lab.service import assert_outcome_free, top_level_allowlisted
from tests.synth import candle, markets

Q = {"max_quote_age_h": 3.0, "history_window_h": 6.0}


def test_cutoff_dst():
    assert replay.cutoff_utc(date(2026, 7, 15)) == datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    assert replay.cutoff_utc(date(2026, 1, 15)) == datetime(2026, 1, 15, 13, tzinfo=timezone.utc)


def test_candle_cutoff_boundary():
    cut = int(replay.cutoff_utc(date(2026, 7, 15)).timestamp())
    cs, _ = replay.canonical_candles([candle(cut - 1, .40, .44), candle(cut, .41, .45), candle(cut + 1, .90, .95)])
    q = replay.select_quote(cs, cut, 3, 6)
    assert q["n_candles_eligible"] == 2 and q["bid"] == .41 and q["ask"] == .45
    cs2, _ = replay.canonical_candles([candle(cut - 1, .40, .44)])
    assert replay.select_quote(cs2, cut, 3, 6)["bid"] == .40


def _runs(runtime, nx):
    d = runtime.date()
    rows = [{"runtime": runtime, "ftime": datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1),
             "station": "KNYC", "model": "GFS", "n_x": nx, "tmp": None}]
    return rows


def test_weather_initialized_before_but_available_after_cutoff_rejected():
    d = date(2026, 7, 15)
    cut = replay.cutoff_utc(d)
    runs = weather.index_runs(_runs(datetime(2026, 7, 15, 6, tzinfo=timezone.utc), 88.0))
    rec = weather.select_forecast(runs, d, cut, 7.5)  # 06Z + 7.5h = 13:30Z > 12Z
    assert rec["status"] == "unavailable" and rec["reason"] == R.FORECAST_UNAVAILABLE
    assert rec["candidates"][0]["eligible"] is False
    runs2 = weather.index_runs(_runs(datetime(2026, 7, 15, 0, tzinfo=timezone.utc), 88.0) +
                               _runs(datetime(2026, 7, 15, 6, tzinfo=timezone.utc), 91.0))
    rec2 = weather.select_forecast(runs2, d, cut, 7.5)
    assert rec2["status"] == "available" and rec2["selected"]["mos_daytime_max_f"] == 88.0


def _rules(d=date(2026, 7, 15)):
    return contracts.normalize_event("KXHIGHNY-26JUL15", markets(), replay.cutoff_utc(d))


def _board(cut, extra=None):
    rules, _ = _rules()
    mids = [.05, .2, .4, .25, .07, .03]
    cm = {b["ticker"]: [candle(cut - 1800, m - .01, m + .01)] + (extra or []) for b, m in zip(rules["buckets"], mids)}
    return rules, cm


WX = {"status": "available", "reason": None, "source_id": "s", "lag_h": 7.5, "availability_basis": "x",
      "selected": {"mos_daytime_max_f": 84.0}}


def test_future_values_do_not_change_fingerprints(cfg, synth_state):
    cut = replay.cutoff_utc(date(2026, 7, 15))
    ct = int(cut.timestamp())
    rules, cm = _board(ct)
    r1 = replay.build_snapshot(rules, cm, WX, cut, Q)
    rules, cm2 = _board(ct, extra=[candle(ct + 60, .99, 1.0, trade=.99, vol=1000)])
    r2 = replay.build_snapshot(rules, cm2, WX, cut, Q)
    assert r1["fingerprint"] == r2["fingerprint"]
    p1 = E.prediction_row(synth_state, "adaptive_blend", r1, E.predict_all(cfg, synth_state, r1)["adaptive_blend"])
    p2 = E.prediction_row(synth_state, "adaptive_blend", r2, E.predict_all(cfg, synth_state, r2)["adaptive_blend"])
    assert p1["prediction_fingerprint"] == p2["prediction_fingerprint"]


def test_valid_midpoint_with_null_trade_close():
    cut = 1_000_000
    cs, _ = replay.canonical_candles([candle(cut - 10, .30, .34, trade=None, vol=0)])
    q = replay.select_quote(cs, cut, 3, 6)
    assert q["disposition"] == "ok" and abs(q["mid"] - .32) < 1e-12 and q["last_trade_close"] is None


def test_crossed_stale_empty_dispositions():
    cut = 1_000_000
    crossed, _ = replay.canonical_candles([candle(cut - 10, .50, .40)])
    assert replay.select_quote(crossed, cut, 3, 6)["disposition"] == R.CROSSED_BOOK
    stale, _ = replay.canonical_candles([candle(cut - 4 * 3600, .30, .34)])
    assert replay.select_quote(stale, cut, 3, 6)["disposition"] == R.QUOTES_STALE
    assert replay.select_quote([], cut, 3, 6)["disposition"] == R.QUOTES_UNAVAILABLE


def test_shuffled_duplicated_records_normalize_identically():
    cut = replay.cutoff_utc(date(2026, 7, 15))
    ms = markets()
    a, la = contracts.normalize_event("KXHIGHNY-26JUL15", ms, cut)
    sh = list(ms)
    random.Random(3).shuffle(sh)
    b, lb = contracts.normalize_event("KXHIGHNY-26JUL15", sh, cut)
    assert a == b and la == lb
    cs = [candle(100, .1, .2), candle(200, .2, .3), candle(300, .3, .4)]
    dup = [cs[2], cs[0], cs[1], cs[0], cs[2]]
    assert replay.canonical_candles(cs) == replay.canonical_candles(dup)


def test_incoherent_three_bucket_board():
    bid, ask = np.array([.5, .5, .5]), np.array([.6, .6, .6])
    m1, d1 = M.coherent_solve(np.ones(3) / 3, bid, ask, np.ones(3), 100.0)
    m2, d2 = M.coherent_solve(np.ones(3) / 3, bid, ask, np.ones(3), 100.0)
    assert np.all(np.isfinite(m1)) and abs(m1.sum() - 1) < 1e-9 and np.array_equal(m1, m2)
    assert d1["infeasible_book"] and d1["max_abs_residual"] > 0.1


def test_partial_partition_rejected():
    ok, why = contracts.validate_partition([(None, 80), (81, 82), (83, 84)])
    assert not ok and "tail" in why
    cut = replay.cutoff_utc(date(2026, 7, 15))
    rules, cm = _board(int(cut.timestamp()))
    cm.pop(rules["buckets"][2]["ticker"])
    row = replay.build_snapshot(rules, cm, WX, cut, Q)
    assert row["market_status"] != "complete" and R.PARTIAL_BOARD in row["flags"]
    assert replay.market_vector(row) is None


def test_rebinning_and_threshold_conservation():
    sup = M.Support(0, 120)
    rules, _ = _rules()
    A = sup.bucket_matrix(rules["buckets"])
    bp = np.array([.05, .2, .4, .25, .07, .03])
    pmf = M.spread_buckets_to_support(bp, A, M.floor_normalize(np.ones(sup.n), 1e-9))
    assert np.allclose(A @ pmf, bp) and abs(pmf.sum() - 1) < 1e-12
    assert abs(sup.query(pmf, 85, None) + sup.query(pmf, None, 84) - 1) < 1e-12
    assert abs(sup.query(pmf, 83, 86) - (.4 + .25)) < 1e-12
    with pytest.raises(ValueError):
        sup.query(pmf, 130, None)


def test_scoring_vector():
    s = E.score([0.2, 0.5, 0.3], 1, 1e-4)
    assert abs(s["log_loss"] - 0.69314718) < 1e-6 and abs(s["brier"] - 0.38) < 1e-12


def test_fallbacks(cfg, synth_state, synth_data):
    feats, _ = synth_data
    row = copy.deepcopy(next(iter(feats.values())))
    row["weather"] = {"status": "unavailable", "reason": R.FORECAST_UNAVAILABLE, "selected": None}
    p = E.predict_all(cfg, synth_state, row)["adaptive_blend"]
    assert p["mode"] == "source_only:market" and abs(sum(p["buckets"]) - 1) < 1e-9
    row2 = copy.deepcopy(next(iter(feats.values())))
    row2["market_status"] = "unavailable"
    p2 = E.predict_all(cfg, synth_state, row2)["adaptive_blend"]
    assert p2["mode"] == "source_only:weather"
    row3 = copy.deepcopy(row2)
    row3["weather"] = row["weather"]
    p3 = E.predict_all(cfg, synth_state, row3)["adaptive_blend"]
    assert p3["mode"] == "unavailable" and p3["reason"] == R.NO_USABLE_PREDICTORS


def test_synthetic_fit_and_evaluate(cfg, synth_state, synth_data):
    feats, labels = synth_data
    ids = sorted(e for e, r in feats.items() if r["event_date"] >= "2025-07-01")
    preds, evals = E.evaluate_rows(cfg, synth_state, feats, labels, ids)
    summ, _ = E.summarize(cfg, evals, date(2025, 7, 1), date(2025, 8, 31))
    c = summ["comparisons"]["adaptive_blend-fixed_blend"]
    assert c["n"] == len(ids) and c["ci95_block"][0] <= c["mean_delta"] <= c["ci95_block"][1]
    assert c["decision"] in ("gain", "loss", "no supported difference")
    st2, _ = E.fit(cfg, feats, labels, log=lambda *a: None)
    assert st2["state_sha256"] == synth_state["state_sha256"]


def test_outcome_free_guard():
    with pytest.raises(AssertionError):
        assert_outcome_free({"event_id": "x", "buckets": [{"result": "yes"}]})
    assert_outcome_free({"event_id": "x", "buckets": [{"bid": .1}]})
    assert not top_level_allowlisted({"event_id": "x", "label": {}})
