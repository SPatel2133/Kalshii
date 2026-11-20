import numpy as np
import pytest

from forecast_lab import models as M
from forecast_lab import pooled as P
from forecast_lab.replay import market_vector


@pytest.mark.parametrize(
    "pool_cfg,city_configs,expected",
    [
        ({}, {"chi": {"cutoff_local": "08:00"}}, False),
        ({}, {"chi": {"cutoff_local": "11:00"}}, True),
        ({"exploratory": True}, {"chi": {"cutoff_local": "08:00"}}, True),
    ],
)
def test_pooled_exploratory_uses_city_cutoffs_and_pool_override(pool_cfg, city_configs, expected):
    assert P._pooled_is_exploratory(pool_cfg, city_configs) is expected


def _feature(event_id="KXHIGHCHI-26AUG13", mids=(0.2, 0.5, 0.3)):
    return {
        "event_id": event_id,
        "event_date": "2026-08-13",
        "market_status": "complete",
        "buckets": [
            {"bucket_id": f"b{i}", "mid": mid}
            for i, mid in enumerate(mids)
        ],
    }


def test_city_recalibration_matches_raw_market_vector_to_one_e_minus_nine():
    feature = _feature()
    alpha, floor = 1.35, 1e-4
    expected = M.recal_probs(market_vector(feature), alpha, floor)
    predictions = [{
        "event_id": feature["event_id"],
        "family": "market_recal",
        "mode": "model",
        "probabilities": [round(float(value), 12) for value in expected],
    }]
    cfg = {"experiment_id": "synthetic", "models": {"prob_floor": floor}}
    state = {"upstream": {"recal": {"alpha": alpha}}}

    assert P.market_recalibration_check(
        cfg, state, predictions, {feature["event_id"]: feature}) == 1

    predictions[0]["probabilities"] = [0.8, 0.1, 0.1]
    with pytest.raises(ValueError, match="market_recal mismatch"):
        P.market_recalibration_check(cfg, state, predictions, {feature["event_id"]: feature})


def test_pooled_oof_recalibration_uses_market_vector_not_market_raw_probabilities():
    feature = _feature()
    alpha, floor = 1.6, 1e-4
    raw = [{
        "event_id": feature["event_id"],
        "event_date": feature["event_date"],
        "family": "market_raw",
        "fold": 1,
        "mode": "model",
        "probabilities": [0.8, 0.1, 0.1],
    }]
    rows = P.pooled_oof_rows(
        "chi", {"models": {"prob_floor": floor}}, raw,
        {feature["event_id"]: feature}, {"1": {"alpha": alpha}})

    expected = M.recal_probs(market_vector(feature), alpha, floor)
    recalibrated_raw = M.recal_probs(raw[0]["probabilities"], alpha, floor)
    assert rows[0]["family"] == "market_recal_pooled"
    assert np.allclose(rows[0]["probabilities"], expected, atol=1e-12, rtol=0)
    assert not np.allclose(rows[0]["probabilities"], recalibrated_raw, atol=1e-6, rtol=0)


def test_city_specific_oof_test_start_guard_rejects_boundary_row():
    configs = {
        "chi": {"split": {"test_start": "2026-02-10"}},
        "aus": {"split": {"test_start": "2025-06-24"}},
    }
    oof = {
        "chi": [{"event_id": "CHI-1", "event_date": "2026-02-09"}],
        "aus": [{"event_id": "AUS-1", "event_date": "2025-06-24"}],
    }
    with pytest.raises(ValueError, match="OOF row AUS-1"):
        P.validate_city_oof_boundaries(oof, configs)

    P.validate_city_oof_boundaries({"chi": oof["chi"]}, configs)


def test_pooled_test_requires_frozen_dev_state(tmp_path):
    with pytest.raises(FileNotFoundError, match="pooled --stage dev"):
        P.require_pooled_state(tmp_path / "pooled_state.json")


def test_test_stage_rejects_city_state_or_config_changed_after_dev():
    frozen = {
        "input_state_sha256": {"chi": "state"},
        "input_config_sha256": {"chi": "config"},
    }
    cfg = {"_sha256": "config"}
    P.validate_frozen_city_state(frozen, "chi", cfg, {"state_sha256": "state"})

    with pytest.raises(ValueError, match="model state changed"):
        P.validate_frozen_city_state(frozen, "chi", cfg, {"state_sha256": "changed"})
    with pytest.raises(ValueError, match="config changed"):
        P.validate_frozen_city_state(
            frozen, "chi", {"_sha256": "changed"}, {"state_sha256": "state"})
