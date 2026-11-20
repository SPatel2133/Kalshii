import hashlib
import json

import numpy as np
import pytest

from forecast_lab import backtest as B
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


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows).encode()
    path.write_bytes(payload)
    return payload


def _pooled_stage_preservation_fixture(tmp_path, monkeypatch, *, stale_snapshot=False, drop_cohort_event=False):
    event_ids = ["KXHIGHCHI-26AUG13"]
    if drop_cohort_event:
        event_ids.append("KXHIGHCHI-26AUG14")
    event_dates = {event_id: f"2026-08-{13 + index:02d}" for index, event_id in enumerate(event_ids)}
    features = {
        event_id: {
            **_feature(event_id, mids=(0.4, 0.6)),
            "event_date": event_dates[event_id],
            "fingerprint": f"fingerprint-{event_id}",
            "cutoff_utc": f"{event_dates[event_id]}T13:00:00Z",
        }
        for event_id in event_ids
    }
    predictions = []
    for index, event_id in enumerate(event_ids):
        for family in B.COMMON_FAMILIES:
            if drop_cohort_event and index == 0 and family == "weather":
                continue
            prediction = {
                "event_id": event_id,
                "event_date": features[event_id]["event_date"],
                "family": family,
                "mode": "model",
                "probabilities": [0.4, 0.6],
                "replay_fingerprint": features[event_id]["fingerprint"],
                "cutoff_utc": features[event_id]["cutoff_utc"],
                "bucket_ids": ["b0", "b1"],
            }
            if stale_snapshot and index == 0 and family == "weather":
                prediction["replay_fingerprint"] = "stale-fingerprint"
            predictions.append(prediction)

    work_dir = tmp_path / "city-work"
    dataset_dir = work_dir / "dataset"
    _write_jsonl(dataset_dir / "features.jsonl", list(features.values()))
    _write_jsonl(dataset_dir / "labels.jsonl", [
        {"event_id": event_id, "winner_bucket": "b0", "reasons": []}
        for event_id in event_ids
    ])
    cfg = {
        "experiment_id": "v3-chi-0800",
        "cutoff_local": "08:00",
        "paths": {"work_dir": str(work_dir), "evidence_dir": str(tmp_path / "city-evidence")},
        "split": {"test_start": "2026-08-01", "test_end": "2026-08-31"},
        "models": {"prob_floor": 1e-4},
    }
    frozen = {
        "pooled_alpha": 1.2,
        "pooled_margin": 0.0,
        "city_margins": {"chi": dict.fromkeys(B.STRATEGIES, 0.0)},
    }
    output_dir = tmp_path / "pooled-evidence"
    output_dir.mkdir()
    (output_dir / "pooled_state.json").write_text(json.dumps(frozen))
    existing_rows = [{
        "event_id": event_id,
        "event_date": features[event_id]["event_date"],
        "family": "market_recal_pooled",
        "city": "chi",
        "mode": "model",
        "probabilities": [0.4, 0.6],
        "bucket_ids": ["b0", "b1"],
    } for event_id in event_ids]
    prediction_bytes = _write_jsonl(output_dir / "predictions.jsonl", existing_rows)
    (output_dir / "predictions_manifest.json").write_text(json.dumps({
        "predictions_sha256": hashlib.sha256(prediction_bytes).hexdigest(),
        "n_predictions": len(existing_rows),
    }))
    city_cohort_summary = {
        "common_cohort_n": len(event_ids),
        "common_cohort_ids_sha256": hashlib.sha256("\n".join(event_ids).encode()).hexdigest(),
    }
    (output_dir / "summary.json").write_text(json.dumps({
        "common_cohort_n": len(event_ids),
        "common_cohort_ids_sha256": hashlib.sha256(
            "\n".join(sorted(event_ids)).encode()).hexdigest(),
        "cities": {"chi": city_cohort_summary},
    }))
    (output_dir / "trades.jsonl").write_bytes(b"previous trades\n")
    before = {
        path.relative_to(output_dir): path.read_bytes()
        for path in output_dir.rglob("*") if path.is_file()
    }
    city_input = {
        "cfg": cfg,
        "state": {},
        "predictions": predictions,
        "prediction_sha256": "sealed-city-predictions",
    }
    monkeypatch.setattr(P, "_load_pooled_test_inputs", lambda *_: (
        output_dir, frozen, {"chi": cfg}, {"chi": city_input}, {"chi": "sealed-city-predictions"}
    ))
    monkeypatch.setattr(P, "market_recalibration_check", lambda *_: 1)
    return output_dir, before, {
        "split": {"test_start": "2026-08-01", "test_end": "2026-08-31"},
    }


def _assert_pooled_stage_preserves_outputs(output_dir, before):
    after = {
        path.relative_to(output_dir): path.read_bytes()
        for path in output_dir.rglob("*") if path.is_file()
    }
    assert after == before


def test_pooled_test_rejects_stale_snapshot_without_overwriting_outputs(tmp_path, monkeypatch):
    output_dir, before, pool_cfg = _pooled_stage_preservation_fixture(
        tmp_path, monkeypatch, stale_snapshot=True)

    with pytest.raises(ValueError, match="predictions were sealed against a different snapshot"):
        P.pooled_test_stage(pool_cfg, "preregistration")

    _assert_pooled_stage_preserves_outputs(output_dir, before)


def test_pooled_test_rejects_changed_cohort_without_overwriting_outputs(tmp_path, monkeypatch):
    output_dir, before, pool_cfg = _pooled_stage_preservation_fixture(
        tmp_path, monkeypatch, drop_cohort_event=True)

    with pytest.raises(ValueError, match="rebuilt pooled cohort differs"):
        P.pooled_test_stage(pool_cfg, "preregistration")

    _assert_pooled_stage_preserves_outputs(output_dir, before)
