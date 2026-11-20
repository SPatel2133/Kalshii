import inspect
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from forecast_lab import backtest, contracts, evaluation, pooled, v3, weather
from forecast_lab import reasons as R


def test_fee_table_known_values_and_integer_cents():
    prices = [1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99]
    assert [backtest.fee_cents(p) for p in prices] == [1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1]
    assert [backtest.fee_cents(p, contracts=100) for p in (1, 5, 10, 50, 99)] == [7, 34, 63, 175, 7]
    assert backtest.fee_cents(50, rate=0.08) == 2
    assert backtest.fee_cents(50, rate=0.0800000001) == 3
    assert backtest.fee_cents(0) == backtest.fee_cents(100) == 0


def test_decide_uses_exact_fee_adjusted_yes_and_no_boundaries():
    assert "label" not in inspect.signature(backtest.decide).parameters
    yes = backtest.decide([0.7], [0], [60], 0.08, 1.0)
    no = backtest.decide([0.3], [40], [100], 0.08, 1.0)
    assert [(t["side"], t["price_c"]) for t in yes] == [("YES", 60)]
    assert [(t["side"], t["price_c"]) for t in no] == [("NO", 60)]
    assert backtest.decide([0.7], [0], [60], 0.080001, 1.0) == []
    assert backtest.decide([0.3], [40], [100], 0.080001, 1.0) == []
    labels = ["a", "b"]
    original = backtest.decide([0.7, 0.3], [0, 40], [60, 100], 0.08, 1.0)
    labels.reverse()
    assert backtest.decide([0.7, 0.3], [0, 40], [60, 100], 0.08, 1.0) == original


def test_decide_flags_crossed_two_sided_opportunity_and_is_label_blind():
    a = backtest.decide([0.5], [70], [30], 0.0, 0.0)
    b = backtest.decide([0.5], [70], [30], 0.0, 0.0)
    assert a == b
    assert a == []
    assert a.flags == [{"bucket_index": 0, "flag": "both_sides_qualify"}]


def test_no_settlement_uses_complement_price_and_fees_after_slippage():
    decision = backtest.decide([0.3], [40], [100], 0.08, 1.0)
    trade = {**decision[0], "bucket_id": "b1"}
    settled = backtest.settle([trade], "b2", slippage_cents=1, fee_mult=1.0)[0]
    assert settled["price_c"] == 61
    assert settled["fee_c"] == 2
    assert settled["payout"] == 1
    assert math.isclose(settled["pnl"], 0.37)


def test_quote_conversion_requires_cent_tick():
    assert backtest.price_to_cents(0.27) == 27
    with pytest.raises(ValueError, match="one-cent tick"):
        backtest.price_to_cents(0.271)


@pytest.mark.parametrize(
    "zone,january,july",
    [
        ("America/New_York", "05:00", "05:00"),
        ("America/Chicago", "06:00", "06:00"),
        ("America/Denver", "07:00", "07:00"),
        ("America/Los_Angeles", "08:00", "08:00"),
    ],
)
def test_lst_day_bounds_use_january_standard_offset(zone, january, july):
    lo_jan, hi_jan = weather.lst_day_bounds(date(2026, 1, 15), zone)
    lo_jul, hi_jul = weather.lst_day_bounds(date(2026, 7, 15), zone)
    assert lo_jan.strftime("%H:%M") == january
    assert lo_jul.strftime("%H:%M") == july
    assert (hi_jan - lo_jan).total_seconds() == 86400
    assert (hi_jul - lo_jul).total_seconds() == 86400
    assert lo_jan.tzinfo == lo_jul.tzinfo == timezone.utc


def test_lst_forecast_window_is_half_open_and_requires_seven_samples():
    event_date = date(2026, 7, 15)
    lo, hi = weather.lst_day_bounds(event_date, "America/Los_Angeles")
    rows = [{"ftime": lo + timedelta(hours=3 * i), "tmp": i, "n_x": None} for i in range(9)]
    rows.extend([
        {"ftime": hi - timedelta(hours=1), "tmp": 99, "n_x": None},
        {"ftime": hi, "tmp": 100, "n_x": None},
    ])
    result = weather.run_forecast(rows, event_date, day_window="lst", tz="America/Los_Angeles")
    assert result["n_samples"] == 8
    assert result["n_samples_local"] == 9
    assert result["sampled_max_f"] == 7
    assert result["sampled_max_local_f"] == 99
    assert result["sample_window_utc"] != result["sample_window_local_utc"]
    fewer = weather.run_forecast(rows[:6], event_date, day_window="lst", tz="America/Los_Angeles")
    assert fewer["sampled_max_f"] == fewer["sampled_max_local_f"] is None


def test_lst_observation_window_uses_standard_offset_and_latency():
    event_date = date(2026, 7, 15)
    lo, _ = weather.lst_day_bounds(event_date, "America/New_York")
    cutoff = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    hi = cutoff - timedelta(minutes=10)
    observations = [
        (lo - timedelta(minutes=1), 1),
        (lo, 2),
        (hi - timedelta(minutes=1), 3),
        (hi + timedelta(minutes=1), 4),
    ]
    summary = weather.observation_summary(
        observations, event_date, cutoff, 10, tz="America/New_York", day_window="lst"
    )
    assert summary["n_obs"] == 2
    assert summary["obs_max_so_far_f"] == 3
    assert summary["day_window"] == "lst"
    assert summary["window_utc"] == [lo.isoformat().replace("+00:00", "Z"),
                                     hi.isoformat().replace("+00:00", "Z")]


def test_station_phrase_mismatch_marks_other_regime():
    event_id = "KXHIGHCHI-26JAN01"
    market = {
        "ticker": event_id + "-70",
        "rules_primary": ("If the highest temperature recorded in Chicago, IL for January 01, 2026 "
                          "as reported by the National Weather Service's Daily Climate Report, "
                          "is between 70-71°, then the market resolves to Yes."),
        "strike_type": "between",
        "floor_strike": 70,
        "cap_strike": 71,
        "open_time": "2025-12-31T14:00:00Z",
        "close_time": "2026-01-01T14:00:00Z",
        "created_time": "2025-12-31T13:00:00Z",
    }
    record, _ = contracts.normalize_event(
        event_id, [market], datetime(2026, 1, 1, 8, tzinfo=timezone.utc), station_phrase="Chicago Midway"
    )
    assert record["station"] == "Chicago Midway"
    assert R.OTHER_REGIME in record["reasons"]


def test_calendar_bootstrap_keeps_same_date_events_together():
    dates = [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2)]
    samples = evaluation.calendar_block_indices(
        dates, block_days=1, reps=30, seed=4, start=date(2026, 1, 1), end=date(2026, 1, 2)
    )
    assert all(sample.tolist().count(0) == sample.tolist().count(1) for sample in samples)


def test_margin_selection_rejects_test_date_rows():
    with pytest.raises(ValueError, match="precede test_start"):
        backtest.select_margin(
            [{"event_date": "2026-01-01", "event_id": "e", "family": "market_raw"}],
            {}, {}, "2026-01-01", [0.0],
        )


def test_margin_selection_breaks_dev_pnl_ties_toward_larger_margin():
    event_id = "KXHIGHCHI-26JAN01"
    required = dict.fromkeys((*backtest.STRATEGIES, *backtest.COMMON_FAMILIES))
    families = {
        family: {"event_id": event_id, "event_date": "2026-01-01", "family": family,
                 "mode": "model", "probabilities": [0.5, 0.5]}
        for family in required
    }
    features = {event_id: {"event_date": "2026-01-01", "buckets": [
        {"bucket_id": "a", "bid": 0.4, "ask": 0.6},
        {"bucket_id": "b", "bid": 0.4, "ask": 0.6},
    ]}}
    labels = {event_id: {"winner_bucket": "a", "reasons": []}}
    frozen = backtest.select_margin(
        list(families.values()), features, labels, "2026-01-02", [0.0, 0.1]
    )
    assert frozen["selected"] == {family: 0.1 for family in backtest.STRATEGIES}


def test_backtest_common_cohort_matches_evaluation_common_families():
    event_id = "KXHIGHCHI-26JAN01"
    predictions = [
        {"event_id": event_id, "family": family, "mode": "model", "probabilities": [0.5, 0.5]}
        for family in backtest.COMMON_FAMILIES
    ]
    cohort, _ = backtest._common_events(
        predictions, {event_id: {"event_date": "2026-01-01"}}, date(2026, 1, 1), date(2026, 1, 1)
    )
    assert cohort == [event_id]


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("replay_fingerprint", "different-fingerprint"),
        ("cutoff_utc", "2026-01-01T15:00:00Z"),
        ("bucket_ids", ["b2", "b1"]),
    ],
)
def test_backtest_rejects_predictions_sealed_against_different_snapshot(field, bad_value):
    event_id = "KXHIGHCHI-26JAN01"
    feature = {
        "event_id": event_id,
        "event_date": "2026-01-01",
        "fingerprint": "feature-fingerprint",
        "cutoff_utc": "2026-01-01T14:00:00Z",
        "buckets": [
            {"bucket_id": "b1", "bid": 0.4, "ask": 0.6},
            {"bucket_id": "b2", "bid": 0.4, "ask": 0.6},
        ],
    }
    predictions = [
        {"event_id": event_id, "family": family, "mode": "model", "probabilities": [0.5, 0.5],
         "replay_fingerprint": "feature-fingerprint", "cutoff_utc": feature["cutoff_utc"],
         "bucket_ids": ["b1", "b2"]}
        for family in backtest.COMMON_FAMILIES
    ]
    predictions[0][field] = bad_value
    labels = {event_id: {"winner_bucket": "b1", "reasons": []}}

    with pytest.raises(
        ValueError, match=f"predictions were sealed against a different snapshot: {event_id}"
    ):
        backtest.run_backtest(
            {"experiment_id": "test"}, predictions, {event_id: feature}, labels,
            date(2026, 1, 1), date(2026, 1, 1),
            dict.fromkeys(backtest.STRATEGIES, 0.0), "test",
        )


def test_pooled_alpha_uses_all_prior_city_rows_and_excludes_late_settlements():
    rows = {
        "target": [
            {"event_date": "2025-12-29", "settled_at_utc": "2025-12-30T08:00:00Z",
             "mids": [0.5, 0.5], "winner_index": 0},
            {"event_date": "2026-01-01", "settled_at_utc": "2026-01-01T08:00:00Z",
             "mids": [0.5, 0.5], "winner_index": 0},
        ],
        "other": [{"event_date": "2025-12-30", "settled_at_utc": "2025-12-31T08:00:00Z",
                   "mids": [0.5, 0.5], "winner_index": 1}],
        "late": [{"event_date": "2025-12-29", "settled_at_utc": "2026-01-02T08:00:00Z",
                  "mids": [0.5, 0.5], "winner_index": 1}],
    }
    state = pooled.pooled_alpha_for_fold(
        rows, "target", "2026-01-01", "2026-01-01T12:00:00Z", floor=1e-6
    )
    assert state["n"] == 2


def test_pooled_alpha_uses_concatenated_city_development_rows():
    state = pooled.fit_pooled_alpha({
        "chi": [{"mids": [0.8, 0.2], "winner_index": 0}],
        "mia": [{"mids": [0.2, 0.8], "winner_index": 1}],
    }, floor=1e-6)
    assert state["n"] == 2


def test_holm_adjust_preserves_input_order_and_monotonicity():
    adjusted = pooled.holm_adjust([0.01, 0.04, 0.03])
    assert adjusted == pytest.approx([0.03, 0.06, 0.06])
    assert all(0 <= p <= 1 for p in adjusted)
    assert pooled.bootstrap_p_value([-0.2, 0.0, 0.1], "less") == pytest.approx(2 / 3)
    city_p = pooled.city_bootstrap_p_values({"chi": [-1, 0, 1], "mia": [1, 2, 3]}, "greater")
    assert city_p["chi"]["p"] == pytest.approx(2 / 3)
    assert city_p["mia"]["holm_p"] == 0


def test_pooled_h1_uses_calendar_bootstrap_and_both_effect_thresholds():
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(14)]
    result = pooled.pooled_h1_result(
        dates, [-0.03] * len(dates), reps=20, start=dates[0], end=dates[-1]
    )
    assert result["ci95_block"] == pytest.approx([-0.03, -0.03])
    assert result["h1_supported"]
    assert result["report_threshold_supported"]


def test_v3_split_profile_uses_catalogue_rules_only(monkeypatch):
    events = {}
    start = date(2025, 1, 1)
    for day in range(100):
        event_date = date.fromordinal(start.toordinal() + day)
        event_id = f"KXHIGHCHI-{event_date.year % 100:02d}{event_date.strftime('%b').upper()}{event_date.day:02d}"
        rules = (f"If the highest temperature recorded in Chicago Midway for {event_date.strftime('%b %d, %Y')} "
                 "as reported by the National Weather Service's Daily Climate Report, is between 70-71°.")
        events[event_id] = [{"rules_primary": rules}]
    monkeypatch.setattr(v3, "load_catalogue", lambda cfg, offline: (events, ["page"]))
    profile = v3.catalogue_profile({
        "experiment_id": "v3-chi-0800",
        "series": "KXHIGHCHI",
        "station_phrase": "Chicago Midway",
    })
    assert profile["split"] == {
        "n": 100,
        "n_dev": 65,
        "dev_start": "2025-01-01",
        "test_start": date.fromordinal(start.toordinal() + 65).isoformat(),
        "test_end": date.fromordinal(start.toordinal() + 99).isoformat(),
        "status": "eligible",
    }
    assert profile["station_wordings"] == [{
        "rules_station_wording": "Chicago Midway",
        "event_count": 100,
        "first_event_date": "2025-01-01",
        "last_event_date": date.fromordinal(start.toordinal() + 99).isoformat(),
    }]
    assert v3.station_wording("recorded at Chicago Midway, IL for April 01, 2025") == "Chicago Midway, IL"


def test_oof_blends_use_fold_sources_and_final_dev_gate_parameters():
    row = {
        "event_id": "KXHIGHCHI-25JAN01",
        "event_date": "2025-01-01",
        "buckets": [
            {"bucket_id": "le50", "spread": 0.1, "quote_age_h": 1.0},
            {"bucket_id": "ge51", "spread": 0.1, "quote_age_h": 1.0},
        ],
    }
    up = {
        "_A": np.eye(2),
        "market_raw": {"buckets": np.array([0.6, 0.4]), "support": np.array([0.6, 0.4])},
        "market_recal": {"buckets": np.array([0.55, 0.45]), "support": np.array([0.55, 0.45])},
        "weather": {"buckets": np.array([0.2, 0.8]), "support": np.array([0.2, 0.8]), "sigma": 1.0},
        "coherent": {"buckets": np.array([0.5, 0.5]), "support": np.array([0.5, 0.5])},
    }
    gate = {"theta": [0.0, 0.0, 0.0, 0.0, 0.0], "x_mean": [0.0] * 4, "x_sd": [1.0] * 4}
    rows = evaluation.oof_prediction_rows(
        {"fixed_blend": {"w": 0.25}, "gate": gate},
        [{"row": row, "up": up, "fold": 2}],
    )
    by_family = {record["family"]: record for record in rows}
    assert by_family["market_raw"]["probabilities"] == [0.6, 0.4]
    assert by_family["fixed_blend"]["probabilities"] == [0.275, 0.725]
    assert by_family["adaptive_blend"]["probabilities"] == [0.35, 0.65]
    assert all("winner_bucket" not in record for record in rows)
