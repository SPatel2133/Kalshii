"""Pooled calibration and multiple-testing helpers."""
from datetime import date, datetime, timezone

import numpy as np

from . import models as M
from .evaluation import block_bootstrap_means


def fit_pooled_alpha(dev_rows, floor):
    rows = [row for city_rows in dev_rows.values() for row in city_rows]
    if not rows:
        raise ValueError("pooled alpha requires at least one development row")
    return M.fit_recal_scalar([row["mids"] for row in rows],
                              [row["winner_index"] for row in rows], floor)


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


def _as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
