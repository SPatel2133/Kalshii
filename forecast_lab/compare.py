"""Cross-experiment paired comparison (v2 vs v1) on the intersection of events both score."""
import json
from datetime import date
from pathlib import Path

import numpy as np

from . import config as C, evaluation as E
from .util import read_jsonl, write_json


def _scored(rows, family):
    return {r["event_id"]: r for r in rows
            if r["family"] == family and r.get("mode") == "model" and r.get("log_loss") is not None}


def paired(cfg, rows_a, rows_b, fam_a, fam_b):
    """Mean log loss (a - b) over events both score, with the experiment's block bootstrap."""
    ev, sp = cfg["evaluation"], cfg["split"]
    a, b = _scored(rows_a, fam_a), _scored(rows_b, fam_b)
    ids = sorted(set(a) & set(b))
    if not ids:
        return {"n": 0}
    start, end = date.fromisoformat(sp["test_start"]), date.fromisoformat(sp["test_end"])
    dates = [date.fromisoformat(a[e]["event_date"]) for e in ids]
    d = [a[e]["log_loss"] - b[e]["log_loss"] for e in ids]
    lo, hi = E.block_bootstrap(dates, d, ev["block_days"], ev["bootstrap_reps"], cfg["models"]["seed"], start, end)
    sens = {str(bl): E.block_bootstrap(dates, d, bl, ev["bootstrap_reps"], cfg["models"]["seed"], start, end)
            for bl in ev["block_sensitivity"]}
    mean = float(np.mean(d))
    gain = hi < 0 and mean <= -ev["effect_threshold"]
    return {"metric": "mean log loss (a - b); negative favours a", "a": fam_a, "b": fam_b, "n": len(ids),
            "only_a": len(set(a) - set(b)), "only_b": len(set(b) - set(a)),
            "mean_log_loss_a": float(np.mean([a[e]["log_loss"] for e in ids])),
            "mean_log_loss_b": float(np.mean([b[e]["log_loss"] for e in ids])),
            "mean_delta": mean, "ci95_block": [lo, hi], "block_days": ev["block_days"], "sensitivity": sens,
            "decision": "gain" if gain else ("loss" if lo > 0 else "no supported difference"),
            "a_wins": int(sum(x < 0 for x in d)), "b_wins": int(sum(x > 0 for x in d))}


def empty_book_exposure(feats_v2, ids):
    """Events (by id) whose board has a bucket whose latest pre-cutoff quoted candle is an empty book."""
    return sorted(e for e in ids if e in feats_v2 and any(
        b.get("disposition") == "quotes_empty_book" for b in feats_v2[e].get("buckets", [])))


def run(cfg_v2_path, cfg_v1_path="configs/nyc-0800-v1.toml"):
    c2, c1 = C.load(cfg_v2_path), C.load(cfg_v1_path)
    ev2, ev1 = Path(c2["paths"]["evidence_dir"]), Path(c1["paths"]["evidence_dir"])
    r2 = read_jsonl(ev2 / "eval-test" / "evaluation_rows.jsonl")
    r1 = read_jsonl(ev1 / "eval-test" / "evaluation_rows.jsonl")
    f2, _, _ = E.load_dataset(Path(c2["paths"]["work_dir"]) / "dataset")
    s1 = json.loads((ev1 / "eval-test" / "summary.json").read_text())
    v1_test_ids = sorted(_scored(r1, "market_raw"))
    sp = c2["split"]
    dev_ids = [e for e, r in f2.items() if sp["dev_start"] <= r["event_date"] < sp["test_start"]]
    test_ids = [e for e, r in f2.items() if sp["test_start"] <= r["event_date"] <= sp["test_end"]]
    out = {"v2_config_sha256": c2["_sha256"], "v1_config_sha256": c1["_sha256"],
           "weather_v2_vs_v1": paired(c2, r2, r1, "weather", "weather"),
           "adaptive_v2_vs_v1": paired(c2, r2, r1, "adaptive_blend", "adaptive_blend"),
           "fixed_v2_vs_v1": paired(c2, r2, r1, "fixed_blend", "fixed_blend"),
           "v1_common_cohort_n": s1["common_cohort_n"],
           "v1_test_events_behind_empty_latest_book": empty_book_exposure(f2, v1_test_ids),
           "v2_empty_book_excluded": {"dev": len(empty_book_exposure(f2, dev_ids)),
                                      "test": len(empty_book_exposure(f2, test_ids))}}
    write_json(ev2 / "compare-v1.json", out)
    return out
