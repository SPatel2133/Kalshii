"""Chronological split, fitting policy, scores and evidence reports.

Fitting policy (frozen in configs/nyc-0800-v1.toml):
- Development = cohort events with dev_start <= date < test_start. Test = contiguous block
  [test_start, test_end]. The golden/audit window follows the test block and is never used to fit or tune.
- Only labels settled before a fit boundary (08:00 New York on the boundary date) enter that fit.
- Upstream components (climatology, weather, market recalibration) are refit on expanding chronological
  folds; their out-of-fold (OOF) predictions train downstream learners (fixed blend, gate, correction).
- Final upstream state = fit on all development labels settled by test_start; no refit inside the test block.
"""
import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from . import FEATURE_VERSION, models as M
from . import reasons as R
from .replay import cutoff_utc, market_vector
from .util import canonical_json, fingerprint, parse_iso, read_jsonl, utc_now_iso, write_json, write_jsonl

PRIMARY_FAMILIES = ["market_raw", "market_recal", "market_context", "weather", "coherent", "fixed_blend",
                    "adaptive_blend", "adaptive_blend_raw", "adaptive_corrected", "climatology"]


# ---------------------------------------------------------------- scores

def score(p_buckets, winner_idx, floor):
    p = M.floor_normalize(p_buckets, floor)
    y = np.zeros_like(p)
    y[winner_idx] = 1.0
    return {"p_winner": float(p[winner_idx]), "log_loss": float(-math.log(p[winner_idx])),
            "brier": float(((p - y) ** 2).sum())}


# ---------------------------------------------------------------- data

def load_dataset(work):
    work = Path(work)
    feats = {r["event_id"]: r for r in read_jsonl(work / "features.jsonl")}
    labels = {r["event_id"]: r for r in read_jsonl(work / "labels.jsonl") if r}
    cov = read_jsonl(work / "coverage.jsonl")
    return feats, labels, cov


def d_of(row):
    return date.fromisoformat(row["event_date"])


def label_ok(lab, boundary_dt=None):
    if lab is None or lab.get("reasons") or lab.get("winner_bucket") is None:
        return False
    if boundary_dt is not None:
        st = parse_iso(lab["settlement_ts"])
        if st is None or st >= boundary_dt:
            return False
    return True


def trainable(feats, labels, start, end_excl, boundary_dt):
    """Events usable for fitting: rules-valid NWS rows in [start, end_excl) with labels settled before boundary."""
    out = []
    for e, r in feats.items():
        d = d_of(r)
        if not (start <= d < end_excl) or r.get("reasons") or r.get("regime") != "NWS":
            continue
        if label_ok(labels.get(e), boundary_dt):
            out.append(e)
    return sorted(out, key=lambda e: feats[e]["event_date"])


def winner_index(row, lab):
    ids = [b["bucket_id"] for b in row["buckets"]]
    return ids.index(lab["winner_bucket"])


def truth_interval(row, lab):
    v = lab.get("settled_value_f")
    if v is not None:
        return v, v
    b = row["buckets"][winner_index(row, lab)]
    return b["lo"], b["hi"]


# ---------------------------------------------------------------- upstream fit / predict

def fit_upstream(cfg, feats, labels, train_ids, lam=None):
    sup = M.Support(cfg["support"]["lo_f"], cfg["support"]["hi_f"])
    floor = cfg["models"]["prob_floor"]
    vals, doys = [], []
    wx_nx, wx_doy, wx_lo, wx_hi = [], [], [], []
    mids, wins, ctx = [], [], []
    for e in train_ids:
        r, lab = feats[e], labels[e]
        lo, hi = truth_interval(r, lab)
        dy = M.doy(d_of(r))
        if lab.get("settled_value_f") is not None:
            vals.append(lab["settled_value_f"])
            doys.append(dy)
        if r["weather"]["status"] == "available":
            wx_nx.append(r["weather"]["selected"]["mos_daytime_max_f"])
            wx_doy.append(dy)
            wx_lo.append(lo)
            wx_hi.append(hi)
        mv = market_vector(r)
        if mv is not None:
            mids.append(np.array(mv))
            wins.append(winner_index(r, lab))
            ctx.append(M.market_context_x(r))
    st = {"support": [sup.lo, sup.hi], "floor": floor, "train_event_ids_sha256": hashlib.sha256(
        "\n".join(train_ids).encode()).hexdigest(), "n_train_events": len(train_ids),
        "train_first": feats[train_ids[0]]["event_date"] if train_ids else None,
        "train_last": feats[train_ids[-1]]["event_date"] if train_ids else None,
        "latest_settlement": max((labels[e]["settlement_ts"] for e in train_ids), default=None),
        "clim_values": vals, "clim_doys": doys}
    st["weather"] = M.WeatherModel().fit(wx_nx, wx_doy, wx_lo, wx_hi).state() if len(wx_nx) >= 30 else None
    st["recal"] = M.fit_recal_scalar(mids, wins, floor) if len(mids) >= 30 else None
    st["context"] = M.fit_recal_context(mids, wins, ctx, floor, cfg["models"]["recal_l2"]) if len(mids) >= 30 else None
    st["lambda"] = lam
    return st


def predict_upstream(cfg, st, row, lams=None):
    """All upstream source predictions for one feature row. Returns dict family -> record."""
    sup = M.Support(*st["support"])
    floor = st["floor"]
    out = {}
    dy = M.doy(d_of(row))
    try:
        A = sup.bucket_matrix(row["buckets"])
    except ValueError:
        return {"_error": R.INVALID_PARTITION}
    clim = M.climatology_pmf(sup, st["clim_values"], st["clim_doys"], dy)
    out["climatology"] = {"support": clim, "buckets": A @ clim, "mode": "model"}
    mv = market_vector(row)
    if mv is not None:
        mv = np.array(mv)
        out["market_raw"] = {"buckets": mv, "support": M.spread_buckets_to_support(mv, A, clim), "mode": "model"}
        if st["recal"]:
            p = M.recal_probs(mv, st["recal"]["alpha"], floor)
            out["market_recal"] = {"buckets": p, "support": M.spread_buckets_to_support(p, A, clim), "mode": "model",
                                   "alpha": st["recal"]["alpha"]}
        if st["context"]:
            a = M.context_alpha(st["context"], M.market_context_x(row))
            p = M.recal_probs(mv, a, floor)
            out["market_context"] = {"buckets": p, "support": M.spread_buckets_to_support(p, A, clim),
                                     "mode": "model", "alpha": a}
        bid = [b["bid"] for b in row["buckets"]]
        ask = [b["ask"] for b in row["buckets"]]
        r = M.quote_weights(row["buckets"])
        # reference c = market's own normalized midpoint (floored); the solve reconciles it with the quote bands
        c_ref = M.floor_normalize(np.asarray(mv, float), floor)
        for lam in (lams or [st["lambda"]]):
            m, diag = M.coherent_solve(c_ref, bid, ask, r, lam)
            key = "coherent" if lams is None else f"coherent@{lam}"
            out[key] = {"buckets": m, "support": M.spread_buckets_to_support(m, A, clim), "mode": "model",
                        "diagnostics": diag, "quote_weights": r.tolist()}
    if row["weather"]["status"] == "available" and st["weather"]:
        wm = M.WeatherModel.from_state(st["weather"])
        nx = row["weather"]["selected"]["mos_daytime_max_f"]
        pw = wm.pmf(sup, nx, dy)
        mu, sig = wm.params(nx, dy)
        out["weather"] = {"support": pw, "buckets": A @ pw, "mode": "model", "mu": mu, "sigma": sig}
    out["_A"] = A
    return out


def gate_x(up, mkey="coherent"):
    m, w = up[mkey]["buckets"], up["weather"]["buckets"]
    return None


def gate_features(row, up, mkey):
    bs = row["buckets"]
    return [M.js_divergence(up[mkey]["buckets"], up["weather"]["buckets"]), float(np.mean([b["spread"] for b in bs])),
            float(max(b["quote_age_h"] for b in bs)), float(up["weather"]["sigma"])]


def predict_all(cfg, state, row):
    """Every family's prediction for one row with explicit fallback mode. Pure function of (state, row)."""
    res = {}
    if row.get("reasons"):
        for f in PRIMARY_FAMILIES:
            res[f] = {"mode": "unavailable", "reason": row["reasons"][0]}
        return res
    up = predict_upstream(cfg, state["upstream"], row)
    if "_error" in up:
        for f in PRIMARY_FAMILIES:
            res[f] = {"mode": "unavailable", "reason": up["_error"]}
        return res
    A = up["_A"]
    sup = M.Support(*state["upstream"]["support"])
    for f in ["climatology", "market_raw", "market_recal", "market_context", "weather", "coherent"]:
        res[f] = up[f] if f in up else {"mode": "unavailable",
                                         "reason": R.FORECAST_UNAVAILABLE if f == "weather" else R.QUOTES_UNAVAILABLE}
    for f, mkey in [("fixed_blend", "coherent"), ("adaptive_blend", "coherent"), ("adaptive_blend_raw", "market_raw"),
                    ("adaptive_corrected", "coherent")]:
        has_m, has_w = mkey in up, "weather" in up
        if has_m and has_w:
            if f == "fixed_blend":
                g = state["fixed_blend"]["w"]
                gx = None
            else:
                gs = state["gate_raw"] if f == "adaptive_blend_raw" else state["gate"]
                gx = gate_features(row, up, mkey)
                g = M.gate_value(gs, gx)
            sp = g * up[mkey]["support"] + (1 - g) * up["weather"]["support"]
            rec = {"mode": "model", "gate": g, "gate_inputs": gx}
            if f == "adaptive_corrected":
                c = state["correction"]
                sp = M.corrected_pmf(sp, sup, c["a1"], c["a2"], c["bound"])
            rec["support"], rec["buckets"] = sp, A @ sp
        elif has_m:
            rec = {"mode": "source_only:market", "reason": R.FORECAST_UNAVAILABLE, **{k: up[mkey][k] for k in ("support", "buckets")}}
        elif has_w:
            rec = {"mode": "source_only:weather", "reason": R.QUOTES_UNAVAILABLE, **{k: up["weather"][k] for k in ("support", "buckets")}}
        else:
            rec = {"mode": "unavailable", "reason": R.NO_USABLE_PREDICTORS}
        res[f] = rec
    for f, r in res.items():
        if "buckets" in r:
            r["buckets"] = M.validate_probs(r["buckets"], f)
    return res


# ---------------------------------------------------------------- fit with OOF stacking

def chronological_blocks(ids, feats, k):
    n = len(ids)
    edges = [round(i * n / k) for i in range(k + 1)]
    return [ids[edges[i]:edges[i + 1]] for i in range(k)]


def fit(cfg, feats, labels, boundary=None, log=print):
    sp = cfg["split"]
    boundary = boundary or date.fromisoformat(sp["test_start"])
    bdt = cutoff_utc(boundary, cfg["cutoff_local"])
    dev_ids = trainable(feats, labels, date.fromisoformat(sp["dev_start"]), boundary, bdt)
    excluded_late = [e for e, r in feats.items() if date.fromisoformat(sp["dev_start"]) <= d_of(r) < boundary
                     and not r.get("reasons") and r.get("regime") == "NWS" and labels.get(e)
                     and not labels[e].get("reasons") and labels[e].get("winner_bucket")
                     and e not in dev_ids]
    K = cfg["models"]["blend_folds"]
    blocks = chronological_blocks(dev_ids, feats, K)
    lam_grid = cfg["models"]["lambda_grid"]
    oof = []
    for k in range(1, K):
        first = feats[blocks[k][0]]["event_date"]
        fb = cutoff_utc(date.fromisoformat(first), cfg["cutoff_local"])
        tr = [e for e in sum(blocks[:k], []) if label_ok(labels[e], fb)]
        st = fit_upstream(cfg, feats, labels, tr)
        for e in blocks[k]:
            r = feats[e]
            up = predict_upstream(cfg, st, r, lams=lam_grid)
            if "_error" in up:
                continue
            wi = winner_index(r, labels[e])
            oof.append({"event_id": e, "fold": k, "wi": wi, "up": up, "row": r,
                        "win_mask": up["_A"][wi] > 0})
        log(f"fold {k}: train={len(tr)} predict={len(blocks[k])}")
    floor = cfg["models"]["prob_floor"]

    def nll_of(key, rows):
        rs = [o for o in rows if key in o["up"]]
        return sum(-math.log(M.floor_normalize(o["up"][key]["buckets"], floor)[o["wi"]]) for o in rs) / max(len(rs), 1), len(rs)

    lam_scores = {lam: nll_of(f"coherent@{lam}", oof)[0] for lam in lam_grid}
    lam = min(lam_grid, key=lambda l: (lam_scores[l], l))
    for o in oof:
        if f"coherent@{lam}" in o["up"]:
            o["up"]["coherent"] = o["up"][f"coherent@{lam}"]
    both = [o for o in oof if "coherent" in o["up"] and "weather" in o["up"]]
    pm = [o["up"]["coherent"]["buckets"][o["wi"]] for o in both]
    pw = [o["up"]["weather"]["buckets"][o["wi"]] for o in both]
    fixed = M.fit_fixed_blend(pm, pw)

    def gate_fit(mkey, rows_fit, l2):
        return M.fit_gate([o["up"][mkey]["buckets"][o["wi"]] for o in rows_fit],
                          [o["up"]["weather"]["buckets"][o["wi"]] for o in rows_fit],
                          [gate_features(o["row"], o["up"], mkey) for o in rows_fit], l2)

    def gate_nll(gs, mkey, rows_eval):
        s = 0.0
        for o in rows_eval:
            g = M.gate_value(gs, gate_features(o["row"], o["up"], mkey))
            p = g * o["up"][mkey]["buckets"] + (1 - g) * o["up"]["weather"]["buckets"]
            s -= math.log(M.floor_normalize(p, floor)[o["wi"]])
        return s / max(len(rows_eval), 1)

    gates = {}
    l2_search = {}
    for mkey, name in [("coherent", "gate"), ("market_raw", "gate_raw")]:
        rows = [o for o in both if mkey in o["up"]]
        fit_rows = [o for o in rows if o["fold"] < K - 1]
        val_rows = [o for o in rows if o["fold"] == K - 1]
        scores = {l2: gate_nll(gate_fit(mkey, fit_rows, l2), mkey, val_rows) for l2 in cfg["models"]["gate_l2_grid"]}
        best = min(scores, key=lambda l: (scores[l], -l))
        gates[name] = gate_fit(mkey, rows, best)
        l2_search[name] = {"validation_nll": {str(k): v for k, v in scores.items()}, "selected_l2": best,
                           "n_fit": len(fit_rows), "n_val": len(val_rows)}
    # bounded correction on adaptive blend support pmfs: fit on folds < K-1, validate on K-1
    sup = M.Support(cfg["support"]["lo_f"], cfg["support"]["hi_f"])

    def ab_support(o, gs):
        g = M.gate_value(gs, gate_features(o["row"], o["up"], "coherent"))
        return g * o["up"]["coherent"]["support"] + (1 - g) * o["up"]["weather"]["support"]

    fit_rows = [o for o in both if o["fold"] < K - 1]
    val_rows = [o for o in both if o["fold"] == K - 1]
    g_fit = gate_fit("coherent", fit_rows, l2_search["gate"]["selected_l2"])
    corr_fit = M.fit_correction([ab_support(o, g_fit) for o in fit_rows], [o["win_mask"] for o in fit_rows], sup)

    def corr_val(c):
        s = 0.0
        for o in val_rows:
            p = M.corrected_pmf(ab_support(o, g_fit), sup, c["a1"], c["a2"], c["bound"])
            pb = o["up"]["_A"] @ p
            s -= math.log(M.floor_normalize(pb, floor)[o["wi"]])
        return s / max(len(val_rows), 1)

    base_val = corr_val({"a1": 0.0, "a2": 0.0, "bound": 1.0})
    corr_val_nll = corr_val(corr_fit)
    correction = M.fit_correction([ab_support(o, gates["gate"]) for o in both], [o["win_mask"] for o in both], sup)
    correction["validation"] = {"uncorrected_nll": base_val, "corrected_nll": corr_val_nll,
                                "promoted": bool(corr_val_nll < base_val)}
    final_up = fit_upstream(cfg, feats, labels, dev_ids, lam=lam)
    oof_scores = {"coherent_lambda_nll": {str(k): v for k, v in lam_scores.items()}}
    for key in ["market_raw", "market_recal", "market_context", "weather", "coherent", "climatology"]:
        oof_scores[key] = nll_of(key, both)
    state = {
        "experiment_id": cfg["experiment_id"], "config_sha256": cfg["_sha256"], "feature_version": FEATURE_VERSION,
        "fit_boundary": boundary.isoformat(), "fit_boundary_utc": bdt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": cfg["models"]["seed"], "upstream": final_up, "fixed_blend": fixed, "gate": gates["gate"],
        "gate_raw": gates["gate_raw"], "gate_l2_search": l2_search, "correction": correction,
        "n_dev_events": len(dev_ids), "dev_first": feats[dev_ids[0]]["event_date"], "dev_last": feats[dev_ids[-1]]["event_date"],
        "excluded_settled_after_boundary": sorted(excluded_late), "oof_n": len(oof), "oof_both_n": len(both),
        "oof_folds": {str(k): len(b) for k, b in enumerate(blocks)}, "oof_nll": oof_scores,
        "convergence": {"weather": (final_up["weather"] or {}).get("converged"),
                        "recal": (final_up["recal"] or {}).get("converged"),
                        "context": (final_up["context"] or {}).get("converged"),
                        "fixed_blend": fixed["converged"], "gate": gates["gate"]["converged"],
                        "gate_raw": gates["gate_raw"]["converged"], "correction": correction["converged"]},
        "fitted_utc": utc_now_iso(),
    }
    # model identity is a content hash of everything except the fit timestamp
    state["state_sha256"] = fingerprint({k: v for k, v in state.items() if k != "fitted_utc"})
    return state, oof


def model_id(state, family):
    return f"{state['experiment_id']}.{family}@{state['fit_boundary']}"


def parse_model_id(mid):
    exp, rest = mid.split(".", 1)
    fam, boundary = rest.split("@")
    return exp, fam, boundary


def prediction_row(state, family, row, pred):
    r = {"model_id": model_id(state, family), "state_sha256": state["state_sha256"], "event_id": row["event_id"],
         "cutoff_utc": row["cutoff_utc"], "replay_fingerprint": row["fingerprint"], "feature_version": row["feature_version"],
         "mode": pred["mode"], "reason": pred.get("reason")}
    if "buckets" in pred:
        r["bucket_ids"] = [b["bucket_id"] for b in row["buckets"]]
        r["probabilities"] = [round(float(x), 12) for x in pred["buckets"]]
        r["support_lo"] = state["upstream"]["support"][0]
        r["support_masses"] = [round(float(x), 12) for x in pred["support"]]
    for k in ("gate", "gate_inputs", "alpha", "mu", "sigma"):
        if pred.get(k) is not None:
            r[k] = pred[k]
    if "diagnostics" in pred:
        r["diagnostics"] = pred["diagnostics"]
    r["prediction_fingerprint"] = fingerprint({k: v for k, v in r.items()})
    return r


# ---------------------------------------------------------------- evaluation and uncertainty

def block_bootstrap(dates, deltas, block_days, reps, seed, start, end):
    """Paired moving-block bootstrap over contiguous calendar blocks. Same resampled event indices for all
    comparators because the input is the paired per-event delta. Returns (lo, hi) of the 95% interval."""
    rng = np.random.default_rng(seed)
    days = np.array([(d - start).days for d in dates])
    n_days = (end - start).days + 1
    deltas = np.asarray(deltas, float)
    n_blocks = math.ceil(n_days / block_days)
    means = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, n_days, size=n_blocks)
        idx = []
        for s in starts:
            sel = (days - s) % n_days < block_days  # circular calendar block
            idx.append(np.nonzero(sel)[0])
        idx = np.concatenate(idx)
        means[i] = deltas[idx].mean() if idx.size else np.nan
    return float(np.nanpercentile(means, 2.5)), float(np.nanpercentile(means, 97.5))


def evaluate_rows(cfg, state, feats, labels, ids):
    floor = cfg["models"]["prob_floor"]
    preds, evals = [], []
    for e in ids:
        row = feats[e]
        pa = predict_all(cfg, state, row)
        lab = labels.get(e)
        wi = winner_index(row, lab) if label_ok(lab) else None
        for fam in PRIMARY_FAMILIES:
            pr = prediction_row(state, fam, row, pa[fam])
            preds.append(pr)
            ev = {"event_id": e, "event_date": row["event_date"], "model_id": pr["model_id"], "family": fam,
                  "cutoff_utc": row["cutoff_utc"], "mode": pr["mode"], "reason": pr["reason"],
                  "winner_bucket": lab.get("winner_bucket") if lab else None,
                  "label_reasons": lab.get("reasons") if lab else [R.SETTLEMENT_UNRESOLVED]}
            if wi is not None and "probabilities" in pr:
                ev.update(score(pr["probabilities"], wi, floor))
            evals.append(ev)
    return preds, evals


def summarize(cfg, evals, start, end, families=PRIMARY_FAMILIES):
    ev = cfg["evaluation"]
    by = {}
    for r in evals:
        by.setdefault(r["event_id"], {})[r["family"]] = r
    base = ["market_raw", "weather", "coherent", "fixed_blend", "adaptive_blend"]
    common = sorted(e for e, fr in by.items() if all(fr.get(f, {}).get("mode") == "model" and "log_loss" in fr[f] for f in base))
    out = {"window": [start.isoformat(), end.isoformat()], "n_events_scored_any": len(by),
           "common_cohort_n": len(common), "common_cohort_ids_sha256": hashlib.sha256("\n".join(common).encode()).hexdigest(),
           "families": {}, "comparisons": {}}
    for f in families:
        rs = [by[e][f] for e in common if f in by[e] and "log_loss" in by[e][f]]
        if len(rs) == len(common) and rs:
            out["families"][f] = {"n": len(rs), "mean_log_loss": float(np.mean([r["log_loss"] for r in rs])),
                                  "mean_brier": float(np.mean([r["brier"] for r in rs])),
                                  "mean_p_winner": float(np.mean([r["p_winner"] for r in rs]))}
    dates = [date.fromisoformat(by[e]["market_raw"]["event_date"]) for e in common]
    pairs = [tuple(ev["primary"])] + [tuple(p) for p in ev["secondary"]]
    for a, b in pairs:
        if a not in out["families"] or b not in out["families"]:
            continue
        d = [by[e][a]["log_loss"] - by[e][b]["log_loss"] for e in common]
        db = [by[e][a]["brier"] - by[e][b]["brier"] for e in common]
        lo, hi = block_bootstrap(dates, d, ev["block_days"], ev["bootstrap_reps"], cfg["models"]["seed"], start, end)
        sens = {}
        for bl in ev["block_sensitivity"]:
            sens[str(bl)] = block_bootstrap(dates, d, bl, ev["bootstrap_reps"], cfg["models"]["seed"], start, end)
        mean = float(np.mean(d))
        gain = hi < 0 and mean <= -ev["effect_threshold"]
        out["comparisons"][f"{a}-{b}"] = {
            "metric": "mean log loss (a - b); negative favours a", "n": len(d), "mean_delta": mean,
            "ci95_block": [lo, hi], "block_days": ev["block_days"], "sensitivity": sens,
            "mean_delta_brier": float(np.mean(db)), "effect_threshold": ev["effect_threshold"],
            "decision": "gain" if gain else ("loss" if lo > 0 else "no supported difference"),
            "a_wins": int(sum(x < 0 for x in d)), "b_wins": int(sum(x > 0 for x in d))}
    modes = {}
    for f in families:
        cnt = {}
        for e, fr in by.items():
            m = fr.get(f, {}).get("mode", "missing")
            cnt[m] = cnt.get(m, 0) + 1
        modes[f] = cnt
    out["coverage_modes_all_scored_events"] = modes
    return out, common
