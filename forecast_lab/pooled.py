"""Pooled calibration and multiple-testing helpers."""
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from . import backtest as B
from . import config as C
from . import evaluation as E
from . import models as M
from .evaluation import block_bootstrap_means
from .replay import cutoff_utc, market_vector
from .service import models_dir
from .util import read_bytes_maybe_gz, read_jsonl, sha256_bytes, write_json, write_jsonl


def _pooled_is_exploratory(pool_cfg, city_configs):
    return (any(cfg["cutoff_local"] != "08:00" for cfg in city_configs.values())
            or pool_cfg.get("exploratory", False)
            or pool_cfg.get("leak_free", False))


def fit_pooled_alpha(dev_rows, floor):
    rows = [row for city_rows in dev_rows.values() for row in city_rows]
    if not rows:
        raise ValueError("pooled alpha requires at least one development row")
    return M.fit_recal_scalar([row["mids"] for row in rows],
                              [row["winner_index"] for row in rows], floor)


def _pool_label_cutoff(pool_cfg):
    if pool_cfg.get("leak_free", False):
        return date.fromisoformat(pool_cfg["split"]["test_start"])
    return None


def _rows_before_label_cutoff(rows, label_cutoff):
    if label_cutoff is None:
        return rows
    return [row for row in rows if date.fromisoformat(row["event_date"]) < label_cutoff]


def _fit_pooled_alpha_for_pool(pool_cfg, dev_rows, floor):
    label_cutoff = _pool_label_cutoff(pool_cfg)
    fit_rows = dev_rows if label_cutoff is None else {
        city: _rows_before_label_cutoff(rows, label_cutoff)
        for city, rows in dev_rows.items()
    }
    return fit_pooled_alpha(fit_rows, floor)


def _select_pooled_margin_for_pool(pool_cfg, city_configs, oof_rows, features, labels,
                                   margins, fee_rate, contracts):
    label_cutoff = _pool_label_cutoff(pool_cfg)
    boundary = (label_cutoff if label_cutoff is not None else
                max(date.fromisoformat(cfg["split"]["test_start"])
                    for cfg in city_configs.values()))
    margin_rows = _rows_before_label_cutoff(oof_rows, label_cutoff)
    return B.select_margin(margin_rows, features, labels, boundary, margins, fee_rate, contracts)


def pooled_recalibrate_predictions(predictions, alpha, floor):
    rows = []
    for prediction in predictions:
        family = prediction.get("family")
        if family is None and prediction.get("model_id"):
            family = prediction["model_id"].split(".", 1)[1].split("@", 1)[0]
        if family != "market_raw" or prediction.get("mode") != "model":
            continue
        record = dict(prediction)
        record["family"] = "market_recal_pooled"
        record["alpha"] = alpha
        record["probabilities"] = [round(float(p), 12) for p in
                                   M.recal_probs(prediction["probabilities"], alpha, floor)]
        rows.append(record)
    return rows


def pooled_alpha_for_fold(dev_rows, target_city, first_date, fold_cutoff_utc, floor):
    first = date.fromisoformat(first_date) if isinstance(first_date, str) else first_date
    cutoff = _as_utc(fold_cutoff_utc)
    eligible = []
    for city, rows in dev_rows.items():
        for row in rows:
            event_date = date.fromisoformat(row["event_date"])
            if not row.get("settled_at_utc"):
                continue
            settled = _as_utc(row["settled_at_utc"])
            if event_date < first and settled < cutoff:
                eligible.append(row)
    if not eligible:
        raise ValueError(f"no pooled development labels precede {first} for {target_city} fold")
    return M.fit_recal_scalar([row["mids"] for row in eligible],
                              [row["winner_index"] for row in eligible], floor)


def holm_adjust(p_values):
    values = [float(p) for p in p_values]
    if any(p < 0 or p > 1 for p in values):
        raise ValueError("p-values must be in [0, 1]")
    order = sorted(range(len(values)), key=lambda i: values[i])
    adjusted = [0.0] * len(values)
    running = 0.0
    count = len(values)
    for rank, index in enumerate(order):
        running = max(running, (count - rank) * values[index])
        adjusted[index] = min(1.0, running)
    return adjusted


def bootstrap_p_value(samples, alternative):
    values = list(samples)
    if not values:
        raise ValueError("bootstrap samples are required")
    if alternative == "less":
        return sum(value >= 0 for value in values) / len(values)
    if alternative == "greater":
        return sum(value <= 0 for value in values) / len(values)
    raise ValueError("alternative must be 'less' or 'greater'")


def city_bootstrap_p_values(samples_by_city, alternative):
    cities = list(samples_by_city)
    p_values = [bootstrap_p_value(samples_by_city[city], alternative) for city in cities]
    adjusted = holm_adjust(p_values)
    return {city: {"p": p_value, "holm_p": holm_p}
            for city, p_value, holm_p in zip(cities, p_values, adjusted)}


def pooled_h1_result(dates, deltas, block_days=7, block_sensitivity=(3, 14), reps=2000,
                     seed=20260813, start=None, end=None, h1_threshold=0.01,
                     report_threshold=0.02):
    parsed = [date.fromisoformat(value) if isinstance(value, str) else value for value in dates]
    values = np.asarray(deltas, dtype=float)
    if len(parsed) != len(values) or not parsed:
        raise ValueError("dates and deltas must have the same nonzero length")
    start = date.fromisoformat(start) if isinstance(start, str) else (start or min(parsed))
    end = date.fromisoformat(end) if isinstance(end, str) else (end or max(parsed))
    if min(parsed) < start or max(parsed) > end:
        raise ValueError("bootstrap span must cover every event date")

    def interval(days):
        samples = block_bootstrap_means(parsed, values, days, reps, seed, start, end)
        return [float(x) for x in np.nanpercentile(samples, [2.5, 97.5])]

    mean_delta = float(values.mean())
    ci = interval(block_days)
    supported = ci[1] < 0 and mean_delta <= -h1_threshold
    return {
        "n": len(values),
        "mean_delta": mean_delta,
        "ci95_block": ci,
        "block_days": block_days,
        "sensitivity": {str(days): interval(days) for days in block_sensitivity},
        "h1_supported": supported,
        "report_threshold_supported": supported and mean_delta <= -report_threshold,
        "h1_threshold": h1_threshold,
        "report_threshold": report_threshold,
        "calendar_span": [start.isoformat(), end.isoformat()],
    }


def validate_city_oof_boundaries(oof_by_city, city_configs):
    for city, rows in oof_by_city.items():
        boundary = date.fromisoformat(city_configs[city]["split"]["test_start"])
        for row in rows:
            if date.fromisoformat(row["event_date"]) >= boundary:
                raise ValueError(f"{city} OOF row {row['event_id']} is on or after test_start {boundary}")


def require_pooled_state(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing; run pooled --stage dev before pooled --stage test")
    return json.loads(path.read_text())


def validate_frozen_city_state(frozen, city, cfg, state):
    if city not in frozen.get("input_state_sha256", {}):
        raise ValueError(f"{city} is not present in the frozen pooled development state")
    if frozen["input_state_sha256"][city] != state.get("state_sha256"):
        raise ValueError(f"{city} model state changed after pooled development")
    if frozen.get("input_config_sha256", {}).get(city) != cfg.get("_sha256"):
        raise ValueError(f"{city} config changed after pooled development")


def development_market_rows(cfg, features, labels, state):
    boundary = date.fromisoformat(cfg["split"]["test_start"])
    boundary_utc = cutoff_utc(boundary, cfg["cutoff_local"], cfg.get("timezone", "America/New_York"))
    train_ids = E.trainable(features, labels, date.fromisoformat(cfg["split"]["dev_start"]), boundary, boundary_utc)
    train_hash = hashlib.sha256("\n".join(train_ids).encode()).hexdigest()
    upstream = state["upstream"]
    if train_hash != upstream["train_event_ids_sha256"] or len(train_ids) != upstream["n_train_events"]:
        raise ValueError(f"{cfg['experiment_id']} development train ids do not match its final upstream fit")
    rows = []
    for event_id in train_ids:
        row = features[event_id]
        mids = market_vector(row)
        if mids is None:
            continue
        rows.append({
            "event_id": event_id,
            "event_date": row["event_date"],
            "settled_at_utc": labels[event_id]["settlement_ts"],
            "mids": mids,
            "winner_index": E.winner_index(row, labels[event_id]),
        })
    if not rows:
        raise ValueError(f"{cfg['experiment_id']} has no final-development market vectors")
    return rows, train_ids


def fold_alphas_for_city(city, cfg, oof_rows, all_dev_rows, floor):
    first_dates = {}
    for row in oof_rows:
        family = row.get("family")
        if family is None and row.get("model_id"):
            family = row["model_id"].split(".", 1)[1].split("@", 1)[0]
        if family == "market_raw" and row.get("mode") == "model":
            fold = str(row["fold"])
            first_dates[fold] = min(first_dates.get(fold, row["event_date"]), row["event_date"])
    if not first_dates:
        raise ValueError(f"{city} has no market_raw OOF rows")
    result = {}
    for fold, first in sorted(first_dates.items(), key=lambda item: int(item[0])):
        boundary = cutoff_utc(date.fromisoformat(first), cfg["cutoff_local"],
                              cfg.get("timezone", "America/New_York"))
        fit = pooled_alpha_for_fold(all_dev_rows, city, first, boundary, floor)
        result[fold] = {
            "first_date": first,
            "cutoff_utc": boundary.isoformat(),
            "alpha": fit["alpha"],
            "fit": fit,
        }
    return result


def pooled_oof_rows(city, cfg, oof_rows, features, fold_alphas):
    floor = cfg["models"]["prob_floor"]
    out = []
    for raw in oof_rows:
        family = raw.get("family")
        if family is None and raw.get("model_id"):
            family = raw["model_id"].split(".", 1)[1].split("@", 1)[0]
        if family != "market_raw" or raw.get("mode") != "model":
            continue
        feature = features.get(raw["event_id"])
        if feature is None:
            raise ValueError(f"missing feature row for {raw['event_id']}")
        mids = market_vector(feature)
        if mids is None:
            continue
        alpha = fold_alphas[str(raw["fold"])]["alpha"]
        probabilities = M.recal_probs(mids, alpha, floor)
        out.append({
            "event_id": raw["event_id"],
            "event_date": raw["event_date"],
            "family": "market_recal_pooled",
            "city": city,
            "fold": raw["fold"],
            "mode": "model",
            "bucket_ids": [bucket["bucket_id"] for bucket in feature["buckets"]],
            "probabilities": [round(float(value), 12) for value in probabilities],
            "alpha": alpha,
            "source": "market_vector",
        })
    return out


def market_recalibration_check(cfg, state, predictions, features, tolerance=1e-9):
    alpha = (state.get("upstream", {}).get("recal") or {}).get("alpha")
    if alpha is None:
        raise ValueError(f"{cfg['experiment_id']} has no final market recalibration alpha")
    floor = cfg["models"]["prob_floor"]
    checked = 0
    for prediction in predictions:
        family = prediction.get("family")
        if family is None and prediction.get("model_id"):
            family = prediction["model_id"].split(".", 1)[1].split("@", 1)[0]
        if family != "market_recal" or prediction.get("mode") != "model":
            continue
        feature = features.get(prediction["event_id"])
        mids = market_vector(feature) if feature is not None else None
        if mids is None:
            continue
        expected = M.recal_probs(mids, alpha, floor)
        actual = np.asarray(prediction.get("probabilities"), dtype=float)
        if actual.shape != expected.shape or np.max(np.abs(actual - expected)) > tolerance:
            raise ValueError(f"{cfg['experiment_id']} market_recal mismatch for {prediction['event_id']}")
        checked += 1
    if not checked:
        raise ValueError(f"{cfg['experiment_id']} has no market_recal predictions to verify")
    return checked


def pooled_test_predictions(city, cfg, predictions, features, alpha):
    floor = cfg["models"]["prob_floor"]
    out = []
    for raw in predictions:
        family = raw.get("family")
        if family is None and raw.get("model_id"):
            family = raw["model_id"].split(".", 1)[1].split("@", 1)[0]
        if family != "market_raw" or raw.get("mode") != "model":
            continue
        feature = features.get(raw["event_id"])
        if feature is None:
            raise ValueError(f"missing feature row for {raw['event_id']}")
        mids = market_vector(feature)
        if mids is None:
            continue
        probabilities = M.recal_probs(mids, alpha, floor)
        out.append({
            "event_id": raw["event_id"],
            "event_date": raw.get("event_date") or feature["event_date"],
            "family": "market_recal_pooled",
            "city": city,
            "mode": "model",
            "bucket_ids": [bucket["bucket_id"] for bucket in feature["buckets"]],
            "probabilities": [round(float(value), 12) for value in probabilities],
            "alpha": alpha,
            "source": "market_vector",
        })
    return out


def comparison_result(dates, deltas, block_days, block_sensitivity, reps, seed, start, end):
    values = np.asarray(deltas, dtype=float)
    parsed = [date.fromisoformat(value) if isinstance(value, str) else value for value in dates]
    if len(parsed) != len(values) or not len(values):
        raise ValueError("comparison requires paired nonempty dates and deltas")

    def interval(days):
        samples = block_bootstrap_means(parsed, values, days, reps, seed, start, end)
        return [float(value) for value in np.nanpercentile(samples, [2.5, 97.5])]

    return {
        "n": len(values),
        "mean_delta": float(values.mean()),
        "ci95_block": interval(block_days),
        "block_days": block_days,
        "sensitivity": {str(days): interval(days) for days in block_sensitivity},
        "calendar_span": [start.isoformat(), end.isoformat()],
    }


def pooled_dev_stage(pool_cfg, preregistration_sha):
    city_configs = {}
    city_data = {}
    dev_rows = {}
    oof_by_city = {}
    oof_files = {}
    city_state_shas = {}
    city_margins = {}
    for config_path in pool_cfg["city_configs"]:
        cfg = C.load(config_path)
        city = cfg["experiment_id"].removeprefix("v3-").removesuffix("-0800")
        city_configs[city] = cfg
        dataset_dir = Path(cfg["paths"]["work_dir"]) / "dataset"
        features, labels, _ = E.load_dataset(dataset_dir)
        model_path = models_dir(cfg) / f"{cfg['experiment_id']}@{cfg['split']['test_start']}.json"
        oof_path = models_dir(cfg) / f"{cfg['experiment_id']}@{cfg['split']['test_start']}.oof.jsonl"
        margin_path = models_dir(cfg) / f"{cfg['experiment_id']}@{cfg['split']['test_start']}.backtest.json"
        for path in (model_path, oof_path, margin_path):
            if not path.is_file():
                raise FileNotFoundError(f"{path} is required; fit {cfg['experiment_id']} first")
        state = json.loads(model_path.read_text())
        if state.get("fit_boundary") != cfg["split"]["test_start"]:
            raise ValueError(f"{cfg['experiment_id']} model state has the wrong fit boundary")
        if state.get("config_sha256") and state["config_sha256"] != cfg["_sha256"]:
            raise ValueError(f"{cfg['experiment_id']} model state does not match its config")
        oof_rows = read_jsonl(oof_path)
        margin_state = json.loads(margin_path.read_text())
        city_margins[city] = margin_state["selected"]
        dev_rows[city], _ = development_market_rows(cfg, features, labels, state)
        oof_by_city[city] = oof_rows
        oof_files[city] = str(oof_path)
        city_state_shas[city] = state["state_sha256"]
        city_data[city] = {"features": features, "labels": labels}

    validate_city_oof_boundaries(oof_by_city, city_configs)
    all_dev_rows = {
        city: [row for rows in dev_rows.values() for row in rows]
        for city in city_configs
    }
    combined_oof = []
    combined_features, combined_labels = {}, {}
    fold_alpha_table = {}
    city_alpha = {}
    floors = {cfg["models"]["prob_floor"] for cfg in city_configs.values()}
    if len(floors) != 1:
        raise ValueError("pooled cities must use a common probability floor")
    pooled_alpha_fit = _fit_pooled_alpha_for_pool(pool_cfg, dev_rows, floors.pop())
    pooled_alpha = pooled_alpha_fit["alpha"]
    for city, cfg in city_configs.items():
        state = json.loads((models_dir(cfg) /
                            f"{cfg['experiment_id']}@{cfg['split']['test_start']}.json").read_text())
        alpha = state["upstream"]["recal"]["alpha"]
        city_alpha[city] = alpha
        fold_alphas = fold_alphas_for_city(
            city, cfg, oof_by_city[city], all_dev_rows, cfg["models"]["prob_floor"])
        fold_alpha_table[city] = fold_alphas
        pooled_rows = pooled_oof_rows(city, cfg, oof_by_city[city],
                                      city_data[city]["features"], fold_alphas)
        combined_oof.extend(oof_by_city[city])
        combined_oof.extend(pooled_rows)
        for collection in ("features", "labels"):
            target = combined_features if collection == "features" else combined_labels
            for event_id, row in city_data[city][collection].items():
                if event_id in target:
                    raise ValueError(f"duplicate event id across pooled cities: {event_id}")
                target[event_id] = row

    first_cfg = next(iter(city_configs.values()))
    margin_fit = _select_pooled_margin_for_pool(
        pool_cfg, city_configs, combined_oof, combined_features, combined_labels,
        first_cfg["backtest"]["margins"], first_cfg["backtest"]["fee_rate"],
        first_cfg["backtest"]["contracts"])
    pooled_margin = margin_fit["selected"]["market_recal_pooled"]
    output_dir = Path(pool_cfg.get("output_dir", "evidence/v3/pooled"))
    state = {
        "experiment_id": "v3-pooled",
        "pre_registration_sha": preregistration_sha,
        "pooled_test_span": [pool_cfg["split"]["test_start"], pool_cfg["split"]["test_end"]],
        "pooled_alpha": pooled_alpha,
        "pooled_alpha_fit": pooled_alpha_fit,
        "city_alpha": city_alpha,
        "fold_alpha": fold_alpha_table,
        "pooled_margin": pooled_margin,
        "city_margins": city_margins,
        "dev_oof_pnl": margin_fit["dev_oof_pnl"]["market_recal_pooled"],
        "dev_oof_common_events": margin_fit["common_oof_events"],
        "input_state_sha256": city_state_shas,
        "input_config_sha256": {
            city: cfg["_sha256"] for city, cfg in city_configs.items()
        },
        "input_oof_sha256": {
            city: sha256_bytes(Path(oof_files[city]).read_bytes())
            for city in city_configs
        },
        "final_train_ids_sha256": {
            city: json.loads((models_dir(cfg) /
                              f"{cfg['experiment_id']}@{cfg['split']['test_start']}.json").read_text())
            ["upstream"]["train_event_ids_sha256"]
            for city, cfg in city_configs.items()
        },
    }
    label_cutoff = _pool_label_cutoff(pool_cfg)
    if label_cutoff is not None:
        state.update({
            "leak_free": True,
            "label_cutoff": label_cutoff.isoformat(),
            "exploratory": _pooled_is_exploratory(pool_cfg, city_configs),
        })
    write_json(output_dir / "pooled_state.json", state)
    state["pooled_state_sha256"] = sha256_bytes((output_dir / "pooled_state.json").read_bytes())
    print(json.dumps({
        "output": str(output_dir / "pooled_state.json"),
        "pooled_state_sha256": state["pooled_state_sha256"],
        "pooled_alpha": pooled_alpha,
        "city_alpha": city_alpha,
        "fold_alpha": fold_alpha_table,
        "pooled_margin": pooled_margin,
        "dev_oof_pnl": state["dev_oof_pnl"],
        "input_state_sha256": city_state_shas,
    }, indent=1))
    return state


def _prediction_family(row):
    family = row.get("family")
    if family is None and row.get("model_id"):
        family = row["model_id"].split(".", 1)[1].split("@", 1)[0]
    return family


def _prediction_map(rows):
    grouped = defaultdict(dict)
    for row in rows:
        family = _prediction_family(row)
        if family:
            grouped[row["event_id"]][family] = row
    return grouped


def _log_loss(prediction, winner_index, floor):
    probabilities = M.floor_normalize(np.asarray(prediction["probabilities"], dtype=float), floor)
    return -math.log(max(float(probabilities[winner_index]), 1e-300))


def _comparison_rows(city_result, pair, floor):
    left, right = pair
    grouped = _prediction_map(city_result["predictions"])
    dates, deltas = [], []
    for event_id in city_result["common_ids"]:
        families = grouped[event_id]
        if left not in families or right not in families:
            raise ValueError(f"missing {left}/{right} predictions for common event {event_id}")
        winner_index = E.winner_index(city_result["features"][event_id],
                                     city_result["labels"][event_id])
        dates.append(city_result["features"][event_id]["event_date"])
        deltas.append(_log_loss(families[left], winner_index, floor) -
                      _log_loss(families[right], winner_index, floor))
    return dates, deltas


def _load_pooled_test_inputs(pool_cfg, preregistration_sha):
    output_dir = Path(pool_cfg.get("output_dir", "evidence/v3/pooled"))
    pooled_state_path = output_dir / "pooled_state.json"
    frozen = require_pooled_state(pooled_state_path)
    if frozen.get("pre_registration_sha") != preregistration_sha:
        raise ValueError("pooled_state.json belongs to a different pre-registration commit")

    city_configs, city_inputs = {}, {}
    input_prediction_shas = {}
    for config_path in pool_cfg["city_configs"]:
        cfg = C.load(config_path)
        city = cfg["experiment_id"].removeprefix("v3-").removesuffix("-0800")
        city_configs[city] = cfg
        model_path = models_dir(cfg) / f"{cfg['experiment_id']}@{cfg['split']['test_start']}.json"
        state = json.loads(model_path.read_text())
        validate_frozen_city_state(frozen, city, cfg, state)
        eval_dir = Path(cfg["paths"]["evidence_dir"]) / "eval-test"
        prediction_path = eval_dir / "predictions.jsonl"
        summary_path = eval_dir / "summary.json"
        predictions = read_jsonl(prediction_path)
        eval_summary = json.loads(summary_path.read_text())
        prediction_sha = sha256_bytes(read_bytes_maybe_gz(prediction_path))
        if prediction_sha != eval_summary.get("predictions_sha256_sealed_before_scoring"):
            raise ValueError(f"{city} eval-test predictions do not match their sealed summary")
        input_prediction_shas[city] = prediction_sha
        city_inputs[city] = {
            "cfg": cfg,
            "state": state,
            "predictions": predictions,
            "prediction_sha256": prediction_sha,
        }
    return output_dir, frozen, city_configs, city_inputs, input_prediction_shas


def verify_pooled_test_seals(pool_cfg, preregistration_sha):
    _, _, _, _, input_prediction_shas = _load_pooled_test_inputs(pool_cfg, preregistration_sha)
    return input_prediction_shas


def _verify_existing_pooled_outputs(output_dir, pooled_predictions, city_cohorts):
    manifest_path = output_dir / "predictions_manifest.json"
    if manifest_path.is_file():
        predictions_path = output_dir / "predictions.jsonl"
        manifest = json.loads(manifest_path.read_text())
        existing_bytes = read_bytes_maybe_gz(predictions_path)
        if sha256_bytes(existing_bytes) != manifest.get("predictions_sha256"):
            raise ValueError("existing pooled predictions do not match their seal")
        existing_predictions = read_jsonl(predictions_path)
        existing_event_ids = [row["event_id"] for row in existing_predictions]
        if (len(existing_event_ids) != len(set(existing_event_ids))
                or manifest.get("n_predictions") != len(existing_event_ids)
                or set(existing_event_ids) != {row["event_id"] for row in pooled_predictions}):
            raise ValueError("rebuilt pooled prediction event set differs from the existing seal")

    summary_path = output_dir / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text())
        rebuilt_ids = {event_id for cohort in city_cohorts.values() for event_id in cohort}
        rebuilt_hash = sha256_bytes("\n".join(sorted(rebuilt_ids)).encode())
        if ("common_cohort_ids_sha256" in summary
                and (summary.get("common_cohort_n") != len(rebuilt_ids)
                     or summary["common_cohort_ids_sha256"] != rebuilt_hash)):
            raise ValueError("rebuilt pooled cohort differs from the event set in the existing seal")

        city_summaries = summary.get("cities", {})
        if isinstance(city_summaries, dict) and city_summaries:
            if set(city_summaries) != set(city_cohorts):
                raise ValueError("rebuilt pooled cohort cities differ from the existing seal")
            for city, cohort in city_cohorts.items():
                city_summary = city_summaries[city]
                cohort_hash = sha256_bytes("\n".join(cohort).encode())
                if ("common_cohort_ids_sha256" in city_summary
                        and (city_summary.get("common_cohort_n") != len(cohort)
                             or city_summary["common_cohort_ids_sha256"] != cohort_hash)):
                    raise ValueError(f"rebuilt {city} cohort differs from the event set in the existing seal")


def pooled_test_stage(pool_cfg, preregistration_sha):
    output_dir, frozen, city_configs, city_inputs, input_prediction_shas = _load_pooled_test_inputs(
        pool_cfg, preregistration_sha)
    pooled_state_path = output_dir / "pooled_state.json"

    pooled_predictions = []
    recalibration_checks = {}
    for city, city_input in city_inputs.items():
        cfg = city_input["cfg"]
        state = city_input["state"]
        predictions = city_input["predictions"]
        feature_path = Path(cfg["paths"]["work_dir"]) / "dataset" / "features.jsonl"
        features = {row["event_id"]: row for row in read_jsonl(feature_path)}
        n_checked = market_recalibration_check(cfg, state, predictions, features)
        recalibration_checks[city] = n_checked
        alpha = frozen["pooled_alpha"]
        pooled_predictions.extend(pooled_test_predictions(city, cfg, predictions, features, alpha))
        city_input["features"] = features

    seen = set()
    for row in pooled_predictions:
        event_id = row["event_id"]
        if event_id in seen:
            raise ValueError(f"duplicate pooled prediction event id: {event_id}")
        seen.add(event_id)
    pooled_predictions.sort(key=lambda row: (row["event_date"], row["event_id"]))

    city_preflight, city_cohorts = {}, {}
    for city, city_input in city_inputs.items():
        cfg = city_input["cfg"]
        features = city_input["features"]
        label_path = Path(cfg["paths"]["work_dir"]) / "dataset" / "labels.jsonl"
        labels = {row["event_id"]: row for row in read_jsonl(label_path)}
        predictions = city_input["predictions"] + [
            row for row in pooled_predictions if row["city"] == city
        ]
        start = date.fromisoformat(cfg["split"]["test_start"])
        end = date.fromisoformat(cfg["split"]["test_end"])
        cohort, by_event = B._common_events(predictions, features, start, end)
        cohort = [event_id for event_id in cohort
                  if labels.get(event_id) and labels[event_id].get("winner_bucket") is not None
                  and not labels[event_id].get("reasons")]
        if not cohort:
            raise ValueError(f"{city} has no test common cohort")
        B.check_prediction_snapshots(features, by_event, cohort)
        city_cohorts[city] = cohort
        city_preflight[city] = {
            "cfg": cfg,
            "features": features,
            "labels": labels,
            "predictions": predictions,
            "start": start,
            "end": end,
        }

    _verify_existing_pooled_outputs(output_dir, pooled_predictions, city_cohorts)

    predictions_path = output_dir / "predictions.jsonl"
    write_jsonl(predictions_path, pooled_predictions)
    pooled_predictions_sha = sha256_bytes(read_bytes_maybe_gz(predictions_path))
    prediction_manifest = {
        "pre_registration_sha": preregistration_sha,
        "predictions_sha256": pooled_predictions_sha,
        "city_input_predictions_sha256": input_prediction_shas,
        "pooled_state_sha256": sha256_bytes(pooled_state_path.read_bytes()),
        "n_predictions": len(pooled_predictions),
    }
    write_json(output_dir / "predictions_manifest.json", prediction_manifest)

    city_results, pooled_trades = {}, []
    for city, prepared in city_preflight.items():
        cfg = prepared["cfg"]
        labels = prepared["labels"]
        features = prepared["features"]
        predictions = prepared["predictions"]
        margins = dict(frozen["city_margins"][city])
        margins["market_recal_pooled"] = frozen["pooled_margin"]
        start, end = prepared["start"], prepared["end"]
        city_summary, trades = B.run_backtest(
            cfg, predictions, features, labels, start, end, margins, "test", include_event_pnl=True)
        common_ids = city_summary["common_cohort_ids"]
        if not common_ids:
            raise ValueError(f"{city} has no test common cohort")
        pooled_trades.extend({**trade, "city": city} for trade in trades)
        public_summary = dict(city_summary)
        public_summary.pop("common_cohort_ids", None)
        public_summary["common_cohort_ids_sha256"] = sha256_bytes(
            "\n".join(common_ids).encode())
        public_summary["pre_registration_sha"] = preregistration_sha
        public_summary["strategies"] = {
            strategy: {
                scenario: ({key: value for key, value in metrics.items() if key != "event_pnl"}
                           if isinstance(metrics, dict) else metrics)
                for scenario, metrics in scenarios_for_strategy.items()
            }
            for strategy, scenarios_for_strategy in city_summary["strategies"].items()
        }
        city_results[city] = {
            "summary": city_summary,
            "public_summary": public_summary,
            "features": features,
            "labels": labels,
            "predictions": predictions,
            "common_ids": common_ids,
        }

    pool_start = date.fromisoformat(pool_cfg["split"]["test_start"])
    pool_end = date.fromisoformat(pool_cfg["split"]["test_end"])
    pool_settings = pool_cfg.get("pool", {})
    block_days = int(pool_settings.get("block_days", 7))
    block_sensitivity = pool_settings.get("block_sensitivity", [3, 14])
    reps = int(pool_settings.get("bootstrap_reps", 2000))
    seed = int(pool_settings.get("seed", 20260813))
    strategies = list(next(iter(city_results.values()))["summary"]["strategies"])
    scenarios = list(next(iter(city_results.values()))["summary"]["scenarios"])
    pooled_pnl = {}
    for strategy in strategies:
        pooled_pnl[strategy] = {}
        for scenario in scenarios:
            event_rows, traded_rows, dropped, decision_flags = [], [], 0, 0
            for city, result in city_results.items():
                metrics = result["summary"]["strategies"][strategy][scenario]
                event_rows.extend({**row, "city": city} for row in metrics["event_pnl"])
                dropped += metrics["n_events_dropped_liquidity"]
                decision_flags += metrics["decision_flags"]
            traded_rows = [row for row in pooled_trades
                           if row["strategy"] == strategy and row["scenario"] == scenario]
            combined = B._metrics(event_rows, traded_rows, dropped, block_days,
                                  block_sensitivity, reps, seed, pool_start, pool_end)
            combined["decision_flags"] = decision_flags
            pooled_pnl[strategy][scenario] = combined
        base = pooled_pnl[strategy].get("fee_1x", {})
        slip_key = next((key for key in pooled_pnl[strategy]
                         if key.startswith("fee_1x_slippage_")), "")
        lower = (base.get("ci95_block") or [None])[0]
        slippage_metrics = pooled_pnl[strategy].get(slip_key, {})
        decision = "profitable" if lower is not None and lower > 0 and \
            slippage_metrics.get("mean_pnl_per_event", 0) > 0 else "no supported profit"
        pooled_pnl[strategy]["decision"] = decision

    all_common, h1_dates, h1_deltas = set(), [], []
    city_h1_samples, city_h2_samples = {}, {}
    city_h1_rows, city_h2_rows = {}, {}
    comparisons = {
        "fixed_blend_minus_market_raw": ("fixed_blend", "market_raw"),
        "adaptive_blend_minus_fixed_blend": ("adaptive_blend", "fixed_blend"),
    }
    city_comparisons, pooled_comparisons = {}, {}
    pooled_comparison_values = {name: ([], []) for name in comparisons}
    for city, result in city_results.items():
        common_ids = result["common_ids"]
        overlap = all_common.intersection(common_ids)
        if overlap:
            raise ValueError(f"event ids are not unique across pooled cities: {sorted(overlap)[:3]}")
        all_common.update(common_ids)
        grouped = _prediction_map(result["predictions"])
        h1_dates_city, h1_deltas_city = [], []
        for event_id in common_ids:
            families = grouped[event_id]
            if "market_raw" not in families or "market_recal_pooled" not in families:
                raise ValueError(f"missing H1 predictions for {event_id}")
            winner_index = E.winner_index(result["features"][event_id], result["labels"][event_id])
            floor = city_inputs[city]["cfg"]["models"]["prob_floor"]
            date_value = result["features"][event_id]["event_date"]
            delta = (_log_loss(families["market_recal_pooled"], winner_index, floor) -
                     _log_loss(families["market_raw"], winner_index, floor))
            h1_dates_city.append(date_value)
            h1_deltas_city.append(delta)
            h1_dates.append(date_value)
            h1_deltas.append(delta)
        cfg = city_inputs[city]["cfg"]
        start, end = date.fromisoformat(cfg["split"]["test_start"]), date.fromisoformat(cfg["split"]["test_end"])
        h1_bootstrap_dates = [date.fromisoformat(value) for value in h1_dates_city]
        city_h1_samples[city] = block_bootstrap_means(
            h1_bootstrap_dates, h1_deltas_city, block_days, reps, seed, start, end)
        h1_rows = {
            "n": len(h1_deltas_city),
            "mean_delta": float(np.mean(h1_deltas_city)),
            "ci95_block": [float(v) for v in np.nanpercentile(city_h1_samples[city], [2.5, 97.5])],
        }
        city_h1_rows[city] = h1_rows
        pnl_rows = result["summary"]["strategies"]["market_recal_pooled"]["fee_1x"]["event_pnl"]
        pnl_dates = [date.fromisoformat(row["date"]) for row in pnl_rows]
        pnl_values = [row["pnl"] for row in pnl_rows]
        city_h2_samples[city] = block_bootstrap_means(
            pnl_dates, pnl_values, block_days, reps, seed, start, end)
        city_h2_rows[city] = {
            "n": len(pnl_values),
            "mean_pnl_per_event": float(np.mean(pnl_values)),
            "ci95_block": [float(v) for v in np.nanpercentile(city_h2_samples[city], [2.5, 97.5])],
        }
        for name, pair in comparisons.items():
            dates, deltas = _comparison_rows(result, pair, cfg["models"]["prob_floor"])
            city_comparisons.setdefault(city, {})[name] = comparison_result(
                dates, deltas, block_days, block_sensitivity, reps, seed, start, end)
            pooled_comparison_values[name][0].extend(dates)
            pooled_comparison_values[name][1].extend(deltas)

    city_h1_p = city_bootstrap_p_values(city_h1_samples, "less")
    city_h2_p = city_bootstrap_p_values(city_h2_samples, "greater")
    for name, (dates, deltas) in pooled_comparison_values.items():
        pooled_comparisons[name] = comparison_result(
            dates, deltas, block_days, block_sensitivity, reps, seed, pool_start, pool_end)

    pooled_h1 = pooled_h1_result(
        h1_dates, h1_deltas, block_days, block_sensitivity, reps, seed,
        pool_start, pool_end,
        h1_threshold=float(pool_settings.get("h1_threshold", 0.01)),
        report_threshold=float(pool_settings.get("report_threshold", 0.02)))
    h2_metrics = pooled_pnl["market_recal_pooled"]["fee_1x"]
    h2_samples = block_bootstrap_means(
        [date.fromisoformat(row["date"]) for result in city_results.values()
         for row in result["summary"]["strategies"]["market_recal_pooled"]["fee_1x"]["event_pnl"]],
        [row["pnl"] for result in city_results.values()
         for row in result["summary"]["strategies"]["market_recal_pooled"]["fee_1x"]["event_pnl"]],
        block_days, reps, seed, pool_start, pool_end)
    pooled_h2 = {
        "n": h2_metrics["n_events"],
        "mean_pnl_per_event": h2_metrics["mean_pnl_per_event"],
        "ci95_block": h2_metrics["ci95_block"],
        "bootstrap_p": bootstrap_p_value(h2_samples, "greater"),
        "supported": (h2_metrics["ci95_block"][0] is not None and
                      h2_metrics["ci95_block"][0] > 0 and
                      pooled_pnl["market_recal_pooled"].get("fee_1x_slippage_1c", {})
                      .get("mean_pnl_per_event", 0) > 0),
        "calendar_span": [pool_start.isoformat(), pool_end.isoformat()],
    }
    output_summary = {
        "experiment_id": "v3-pooled",
        "stage": "test",
        "exploratory": _pooled_is_exploratory(pool_cfg, city_configs),
        "pre_registration_sha": preregistration_sha,
        "window": [pool_start.isoformat(), pool_end.isoformat()],
        "pooled_state_sha256": sha256_bytes(pooled_state_path.read_bytes()),
        "pooled_alpha": frozen["pooled_alpha"],
        "pooled_margin": frozen["pooled_margin"],
        "city_margins": frozen["city_margins"],
        "pooled_predictions_sha256": pooled_predictions_sha,
        "city_input_predictions_sha256": input_prediction_shas,
        "city_recalibration_checks": recalibration_checks,
        "common_cohort_n": len(all_common),
        "common_cohort_ids_sha256": sha256_bytes("\n".join(sorted(all_common)).encode()),
        "h1": pooled_h1,
        "h2": pooled_h2,
        "city_hypothesis_tests": {
            "h1": {city: {**city_h1_rows[city], **city_h1_p[city]} for city in city_configs},
            "h2": {city: {**city_h2_rows[city], **city_h2_p[city]} for city in city_configs},
        },
        "secondary_comparisons": {"pooled": pooled_comparisons, "cities": city_comparisons},
        "pooled_pnl": pooled_pnl,
        "cities": {city: result["public_summary"] for city, result in city_results.items()},
    }
    if frozen.get("leak_free"):
        output_summary.update({
            "leak_free": True,
            "label_cutoff": frozen["label_cutoff"],
        })
    write_json(output_dir / "summary.json", output_summary)
    write_jsonl(output_dir / "trades.jsonl", pooled_trades)
    print(json.dumps({
        "summary": str(output_dir / "summary.json"),
        "trades": str(output_dir / "trades.jsonl"),
        "common_cohort_n": len(all_common),
        "h1": pooled_h1,
        "h2": pooled_h2,
    }, indent=1))
    return output_summary


def _as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
