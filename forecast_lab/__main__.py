"""Command dispatch: audit, build, fit, evaluate, replay, demo."""
import argparse
import csv
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import backtest as B, config as C, contracts, dataset, download, evaluation as E, pooled as P, v3, weather
from .service import ReplayService, load_state, models_dir
from .util import atomic_write, read_bytes_maybe_gz, read_jsonl, sha256_bytes, write_json, write_jsonl


def _cfg(args):
    return C.load(args.config) if getattr(args, "config", None) else C.default_config()


def cmd_audit(args):
    cfg = _cfg(args)
    if args.series is None:
        args.series = cfg["series"]
    if args.series != cfg["series"]:
        sys.exit(f"--series {args.series} does not match config series {cfg['series']}; use a config for that series")
    end = date.fromisoformat(args.end)
    start = end - timedelta(days=args.days - 1)
    ev, pages = download.load_catalogue(cfg, offline=args.offline)
    if not args.offline:
        st = download.download_candles(cfg, ev, start, end, workers=2)
        print(f"candles ok={st['ok']} failed={len(st['failed'])}")
        src = dataset.Sources(cfg, offline=False)
        for d in [start + timedelta(days=i) for i in range(args.days)]:
            src.mos_runs(d)
    out = Path(cfg["paths"]["evidence_dir"]) / f"audit-{args.series}-{start}-{end}"
    rules, labels, feats, cov = dataset.build_all(cfg, start, end, out / "records")
    fun = dataset.funnel(cov)
    lab_by = {l["event_id"]: l for l in labels if l}
    rows = []
    for c in cov:
        f = next((x for x in feats if x["event_id"] == c["event_id"]), None)
        l = lab_by.get(c["event_id"])
        rows.append({"event_date": c["event_date"], "event_id": c["event_id"] or "", "regime": c.get("regime") or "",
                     "n_buckets": c.get("n_buckets", 0), "market_status": (f or {}).get("market_status", ""),
                     "weather_runtime": c.get("weather_runtime") or "", "weather_status": ((f or {}).get("weather") or {}).get("status", ""),
                     "label_ok": bool(l and not l["reasons"]), "settlement_ts": c.get("settlement_ts") or "",
                     "stage_failed": c["stage_failed"] or "", "primary_reason": c["primary_reason"] or "",
                     "flags": ";".join(c["flags"]), "replay_fingerprint": (f or {}).get("fingerprint", "")})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    atomic_write(out / "coverage.csv", buf.getvalue())
    write_json(out / "funnel.json", fun)
    pages_meta = [{k: p[k] for k in ("cursor", "n", "sha256")} for p in pages]
    write_json(out / "sources.json", {"catalogue_endpoint": cfg["sources"]["kalshi_base"] + "/historical/markets",
                                      "catalogue_query": {"series_ticker": args.series, "limit": 1000},
                                      "catalogue_pages": pages_meta,
                                      "candles_endpoint": cfg["sources"]["kalshi_base"] + "/historical/markets/{ticker}/candlesticks",
                                      "candles_query": {"period_interval": cfg["quotes"]["candle_period_min"], "start_ts": "open_time", "end_ts": "close_time"},
                                      "mos_endpoint": cfg["sources"]["iem_mos"], "mos_query": {"station": "KNYC", "model": "GFS", "sts/ets": "monthly"},
                                      "failure_classes": sorted({f["failure_class"] for f in read_jsonl(cfg["paths"]["cache_dir"] + "/manifest.jsonl") if not f.get("ok")})})
    print(json.dumps(fun, indent=1))
    print(f"wrote {out}")


def cmd_download(args):
    cfg = _cfg(args)
    sp = cfg["split"]
    if "download" in cfg:
        start = date.fromisoformat(cfg["download"]["start"])
        end = date.fromisoformat(cfg["download"]["end"])
    else:
        start, end = date.fromisoformat(sp["dev_start"]), date.fromisoformat(sp["audit_end"])
    if "download" in cfg:
        print(f"download range {start} through {end}", flush=True)
    ev, _ = download.load_catalogue(cfg, offline=False)
    n_markets = sum(len(markets) for event_id, markets in ev.items()
                    if (event_date := contracts.event_date_from_ticker(event_id)) and start <= event_date <= end)
    if "download" in cfg:
        print(f"candles start markets={n_markets}", flush=True)
    st = download.download_candles(cfg, ev, start, end, workers=args.workers)
    print(f"candles ok={st['ok']} failed={len(st['failed'])}")
    src = dataset.Sources(cfg, offline=False)
    d = start
    previous_month = None
    while d <= end:
        month = (d.year, d.month)
        if month != previous_month:
            print(f"mos month {d:%Y-%m}", flush=True)
            previous_month = month
        src.mos_runs(d)
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    print("mos cached")
    if any(f.startswith("obs_") for f in cfg["weather"].get("features", ["nx"])):
        for y in range(start.year, end.year + 1):
            weather.fetch_asos(src.fetcher, cfg, y)  # raises on failure instead of silently zero observations
            print(f"asos cached {y}", flush=True)


def cmd_catalogue(args):
    cfg = _cfg(args)
    events, pages = download.load_catalogue(cfg, offline=False)
    dates = [contracts.event_date_from_ticker(event_id) for event_id in events]
    dates = [d for d in dates if d is not None]
    print(json.dumps({
        "series": cfg["series"],
        "markets": sum(len(markets) for markets in events.values()),
        "events": len(events),
        "earliest_event_date": min(dates).isoformat() if dates else None,
        "latest_event_date": max(dates).isoformat() if dates else None,
        "pages": len(pages),
    }, indent=1))


def cmd_v3_splits(args):
    pooled = C.load(args.pooled)
    profiles = [v3.catalogue_profile(C.load(path), offline=True) for path in pooled["city_configs"]]
    print(json.dumps({"cities": profiles}, indent=1))


def cmd_build(args):
    cfg = _cfg(args)
    sp = cfg["split"]
    if "download" in cfg:
        start, end = date.fromisoformat(cfg["download"]["start"]), date.fromisoformat(cfg["download"]["end"])
    else:
        start, end = date.fromisoformat(sp["dev_start"]), date.fromisoformat(sp["audit_end"])
    dataset.build_all(cfg, start, end, Path(cfg["paths"]["work_dir"]) / "dataset")
    print("built", Path(cfg["paths"]["work_dir"]) / "dataset")


def _ensure_dataset(cfg):
    d = Path(cfg["paths"]["work_dir"]) / "dataset"
    stamp = d / "config_sha256.txt"
    if not (d / "features.jsonl").exists() or not stamp.exists() or stamp.read_text().strip() != cfg["_sha256"]:
        if "download" in cfg:
            start, end = date.fromisoformat(cfg["download"]["start"]), date.fromisoformat(cfg["download"]["end"])
        else:
            sp = cfg["split"]
            start, end = date.fromisoformat(sp["dev_start"]), date.fromisoformat(sp["audit_end"])
        dataset.build_all(cfg, start, end, d)
    return E.load_dataset(d)


def cmd_fit(args):
    cfg = _cfg(args)
    feats, labels, cov = _ensure_dataset(cfg)
    state, oof = E.fit(cfg, feats, labels)
    p = models_dir(cfg) / f"{cfg['experiment_id']}@{state['fit_boundary']}.json"
    oof_path = None
    backtest_path = None
    if cfg.get("feature_version") == "feat-v3":
        oof_rows = E.oof_prediction_rows(state, oof)
        oof_path = models_dir(cfg) / f"{cfg['experiment_id']}@{state['fit_boundary']}.oof.jsonl"
        backtest_path = models_dir(cfg) / f"{cfg['experiment_id']}@{state['fit_boundary']}.backtest.json"
        write_jsonl(oof_path, oof_rows)
        bt = cfg["backtest"]
        frozen = B.select_margin(oof_rows, feats, labels, cfg["split"]["test_start"], bt["margins"],
                                 bt["fee_rate"], bt["contracts"])
        write_json(backtest_path, frozen)
    write_json(p, state)
    ev = Path(cfg["paths"]["evidence_dir"])
    man = {k: v for k, v in state.items() if k not in ("upstream",)}
    man["upstream"] = {k: v for k, v in state["upstream"].items() if k not in ("clim_values", "clim_doys")}
    man["state_path"] = str(p)
    write_json(ev / "model_manifest.json", man)
    result = {"state": str(p), "state_sha256": state["state_sha256"], "n_dev": state["n_dev_events"],
                      "oof_nll": state["oof_nll"], "fixed_w": state["fixed_blend"]["w"],
                      "lambda": state["upstream"]["lambda"], "correction": state["correction"]["validation"],
                      "model_ids": [E.model_id(state, f) for f in E.PRIMARY_FAMILIES]}
    if oof_path:
        result["oof"] = str(oof_path)
        result["backtest_margins"] = str(backtest_path)
    print(json.dumps(result, indent=1))


def cmd_evaluate(args):
    cfg = _cfg(args)
    feats, labels, cov = _ensure_dataset(cfg)
    sp = cfg["split"]
    p = models_dir(cfg) / f"{cfg['experiment_id']}@{sp['test_start']}.json"
    state = json.loads(p.read_text())
    if args.split == "test":
        start, end = date.fromisoformat(sp["test_start"]), date.fromisoformat(sp["test_end"])
    else:
        start, end = date.fromisoformat(sp["audit_start"]), date.fromisoformat(sp["audit_end"])
    ids = sorted((e for e, r in feats.items() if start <= date.fromisoformat(r["event_date"]) <= end
                  and r.get("regime") == "NWS" and not r.get("reasons")), key=lambda e: feats[e]["event_date"])
    preds, evals = E.evaluate_rows(cfg, state, feats, labels, ids)
    out = Path(cfg["paths"]["evidence_dir"]) / f"eval-{args.split}"
    write_jsonl(out / "predictions.jsonl", preds)
    sealed = sha256_bytes(read_bytes_maybe_gz(out / "predictions.jsonl"))
    write_jsonl(out / "evaluation_rows.jsonl", evals)
    summ, common = E.summarize(cfg, evals, start, end)
    cov_w = [c for c in cov if start.isoformat() <= c["event_date"] <= end.isoformat()]
    summ.update({"split": args.split, "predictions_sha256_sealed_before_scoring": sealed, "state_sha256": state["state_sha256"],
                 "config_sha256": cfg["_sha256"], "funnel": dataset.funnel(cov_w), "model_ids": sorted({p["model_id"] for p in preds})})
    if args.split == "test":
        strict = Path(cfg["paths"]["work_dir"]) / "dataset_strict_test"
        dataset.build_all(cfg, start, end, strict, lag_h=cfg["weather"]["lag_strict_h"], log=lambda *a: None)
        fs, ls, _ = E.load_dataset(strict)
        ids_s = [e for e in ids if e in fs]
        _, ev_s = E.evaluate_rows(cfg, state, fs, ls, ids_s)
        ss, _ = E.summarize(cfg, ev_s, start, end)
        summ["strict_lag_sensitivity"] = {"lag_h": cfg["weather"]["lag_strict_h"], "common_cohort_n": ss["common_cohort_n"],
                                          "comparisons": ss["comparisons"],
                                          "weather_selection_changed": sum(1 for e in ids_s if (fs[e]["weather"]["selected"] or {}).get("runtime_utc") != (feats[e]["weather"]["selected"] or {}).get("runtime_utc"))}
    write_json(out / "summary.json", summ)
    print(json.dumps({k: summ[k] for k in ("window", "common_cohort_n", "families", "comparisons", "funnel")}, indent=1))


def cmd_golden(args):
    """Write the golden replay fixture (derived pre-cutoff fields + frozen predictions) for regression tests."""
    cfg = _cfg(args)
    st, fam = load_state(cfg, args.model)
    svc = ReplayService(cfg, st, fam)
    ev = cfg["split"]["golden_event"]
    r = svc.replay(ev)
    rev = svc.reveal(ev)
    selected = r["weather"]["selected"]
    weather_keys = ["runtime_utc", "available_utc", "mos_daytime_max_f"]
    weather_keys.extend(k for k in ("sampled_max_local_f", "n_samples_local", "sample_window_local_utc")
                         if selected and k in selected)
    fx = {"event_id": ev, "model_id": r["model_id"], "state_sha256": st["state_sha256"], "cutoff_utc": r["cutoff_utc"],
          "replay_fingerprint": r["replay_fingerprint"], "prediction_fingerprint": r["prediction"]["prediction_fingerprint"],
          "buckets": [{k: b[k] for k in ("bucket_id", "ticker", "lo", "hi", "bid", "ask", "quote_obs_utc")} for b in r["buckets"]],
          "weather": {**{k: selected[k] for k in weather_keys},
                      **({"obs": r["weather"]["obs"]} if "obs" in r["weather"] else {})},
          "probabilities": r["prediction"]["probabilities"],
          "candle_sha256": {t: c["sha256"] for t, c in r["provenance"]["candles"].items()},
          "label": {k: rev["label"][k] for k in ("winner_bucket", "winner_ticker", "settled_value_f", "settlement_ts")}}
    fixture = cfg["paths"].get("golden_fixture", "tests/fixtures/golden_KXHIGHNY-26AUG13.json")
    write_json(Path(fixture), fx)
    print(json.dumps(fx, indent=1))


def cmd_report(args):
    cfg = _cfg(args)
    ev = Path(cfg["paths"]["evidence_dir"])
    t = json.loads((ev / "eval-test" / "summary.json").read_text())
    a = json.loads((ev / "eval-audit" / "summary.json").read_text())
    man = json.loads((ev / "model_manifest.json").read_text())
    fixture = cfg["paths"].get("golden_fixture", "tests/fixtures/golden_KXHIGHNY-26AUG13.json")
    fx = json.loads(Path(fixture).read_text())
    rows = read_jsonl(ev / "eval-test" / "evaluation_rows.jsonl")
    L = [f"# Evidence report `{cfg['experiment_id']}`", "",
         f"Config sha256 `{t['config_sha256']}` · model state `{t['state_sha256']}` · fit boundary {man['fit_boundary']} · "
         f"{man['n_dev_events']} development events.", "",
         f"Predictions were sealed before scoring: `predictions.jsonl` sha256 `{t['predictions_sha256_sealed_before_scoring']}`.", "",
         "## Answer", ""]
    c = t["comparisons"]["adaptive_blend-fixed_blend"]
    L += [f"On the untouched test block {t['window'][0]} → {t['window'][1]} (n = {t['common_cohort_n']} events, common cohort), "
          f"the adaptive blend minus the fixed blend in mean log loss is **{c['mean_delta']:+.5f}** nats, "
          f"95% block-bootstrap CI [{c['ci95_block'][0]:+.5f}, {c['ci95_block'][1]:+.5f}]: **{c['decision']}** "
          f"(threshold {c['effect_threshold']}).", "",
          "| Family | n | Log loss | Brier | P(winner) |", "|---|---|---|---|---|"]
    for f, v in sorted(t["families"].items(), key=lambda x: x[1]["mean_log_loss"]):
        L.append(f"| {f} | {v['n']} | {v['mean_log_loss']:.4f} | {v['mean_brier']:.4f} | {v['mean_p_winner']:.3f} |")
    L += ["", "| Comparison (a − b) | Δ log loss | 95% CI (7-day) | 3-day | 14-day | Δ Brier | Decision |", "|---|---|---|---|---|---|---|"]
    for k, v in t["comparisons"].items():
        s3, s14 = v["sensitivity"]["3"], v["sensitivity"]["14"]
        L.append(f"| {k} | {v['mean_delta']:+.5f} | [{v['ci95_block'][0]:+.5f}, {v['ci95_block'][1]:+.5f}] | "
                 f"[{s3[0]:+.4f}, {s3[1]:+.4f}] | [{s14[0]:+.4f}, {s14[1]:+.4f}] | {v['mean_delta_brier']:+.5f} | {v['decision']} |")
    sl = t.get("strict_lag_sensitivity")
    if sl:
        cs = sl["comparisons"]["adaptive_blend-fixed_blend"]
        L += ["", f"Strict weather lag ({sl['lag_h']} h): weather selection changed on {sl['weather_selection_changed']} events; "
              f"adaptive − fixed {cs['mean_delta']:+.5f} [{cs['ci95_block'][0]:+.5f}, {cs['ci95_block'][1]:+.5f}], {cs['decision']}."]
    L += ["", f"Fitted parameters: fixed-blend market weight w = {man['fixed_blend']['w']:.3f}; coherent λ = {man['upstream']['lambda']}; "
          f"correction promoted on validation = {man['correction']['validation']['promoted']}.", ""]
    if cfg["experiment_id"] == "nyc-0800-v1":
        L.append("Likely interpretation (an inference, not tested here): by 08:00 the market already prices in the public MOS forecast. The OOF fit put all of the fixed blend's weight "
                 "on the coherent market model, and the regularized gate stays close to that constant, so neither blend can beat the market by the "
                 "declared effect size. This is a valid null result. The weather model alone is clearly worse than the market (see table).")
    L += ["", "## Coverage", "", "| Stage | Test | Audit (30 days to 2026-08-13) |", "|---|---|---|"]
    for k in ("candidate_dates", "catalogue", "rules_valid", "quote_complete", "weather_available", "label_valid"):
        L.append(f"| {k} | {t['funnel'][k]} | {a['funnel'][k]} |")
    L += ["", f"Exclusion reasons (test): {t['funnel']['reasons'] or 'none'}; audit: {a['funnel']['reasons'] or 'none'}. "
          "Whole development window: see `coverage` in the build output. The Weather Company regime (2026-08-14 →) is excluded as `other_regime`.", ""]
    by = {}
    for r in rows:
        by.setdefault(r["event_id"], {})[r["family"]] = r
    d = sorted((fr["adaptive_blend"]["log_loss"] - fr["market_raw"]["log_loss"], e) for e, fr in by.items()
               if "log_loss" in fr.get("adaptive_blend", {}) and "log_loss" in fr.get("market_raw", {}))
    L += ["## Held-out examples (adaptive_blend vs market_raw)", "",
          f"* Favourable: `{d[0][1]}` Δ = {d[0][0]:+.4f}", f"* Failure: `{d[-1][1]}` Δ = {d[-1][0]:+.4f}", "",
          "## Golden replay", "",
          f"`{fx['event_id']}`, cutoff {fx['cutoff_utc']} (08:00 EDT). The weather run is {fx['weather']['runtime_utc']} (assumed available "
          f"{fx['weather']['available_utc']}) with n_x {fx['weather']['mos_daytime_max_f']}°F. The model probabilities are "
          + ", ".join(f"{b['bucket_id']} {p:.3f}" for b, p in zip(fx["buckets"], fx["probabilities"]))
          + f". Revealed: winner {fx['label']['winner_bucket']}, CLI {fx['label']['settled_value_f']}°F. Replay fingerprint `{fx['replay_fingerprint']}`.",
          "", f"Audit-window mean log loss (development use only): adaptive {a['families']['adaptive_blend']['mean_log_loss']:.4f}, "
          f"fixed {a['families']['fixed_blend']['mean_log_loss']:.4f}, market_raw {a['families']['market_raw']['mean_log_loss']:.4f}.", "",
          "## Files", "", "* `eval-test/predictions.jsonl`: per-event predictions.", "* `eval-test/evaluation_rows.jsonl`: per-event losses.",
          "* `eval-test/summary.json`: aggregates.", "* `model_manifest.json`: split manifest and fit diagnostics.",
          "* `audit-*/coverage.csv`, `audit-*/sources.json`: fetch parameters and payload checksums.", "",
          "Reproduce: see the README."]
    atomic_write(ev / "REPORT.md", "\n".join(L) + "\n")
    print("\n".join(L))


def cmd_replay(args):
    cfg = _cfg(args)
    st, fam = load_state(cfg, args.model)
    svc = ReplayService(cfg, st, fam)
    if args.reveal:
        print(json.dumps(svc.reveal(args.event), indent=1, default=float))
        return
    r = svc.replay(args.event, args.as_of, drop_last_quote=args.drop_last_quote, weather_delay_h=args.weather_delay_h)
    print(json.dumps(r if args.full else {k: r[k] for k in ("event_id", "cutoff_utc", "as_of_local", "scenario", "model_id",
                                                            "replay_fingerprint", "market_status", "flags")} |
                     {"bucket_ids": r["prediction"].get("bucket_ids"), "probabilities": r["prediction"].get("probabilities"),
                      "mode": r["prediction"]["mode"], "prediction_fingerprint": r["prediction"]["prediction_fingerprint"]},
                     indent=1, default=float))


def cmd_backtest(args):
    cfg = _cfg(args)
    if args.reps is not None:
        cfg["backtest"] = {**cfg.get("backtest", {}), "bootstrap_reps": args.reps}
    if cfg["experiment_id"] == "v3-nyc-0800" and args.split == "test" and not args.exploratory:
        raise ValueError("NYC is exploratory; pass --exploratory to run its test-window backtest")
    sp = cfg["split"]
    if args.split == "test":
        start, end = date.fromisoformat(sp["test_start"]), date.fromisoformat(sp["test_end"])
    elif "audit_start" in sp and "audit_end" in sp:
        start, end = date.fromisoformat(sp["audit_start"]), date.fromisoformat(sp["audit_end"])
    else:
        raise ValueError(f"{cfg['experiment_id']} has no audit window")
    pred_path = Path(cfg["paths"]["evidence_dir"]) / f"eval-{args.split}" / "predictions.jsonl"
    if not pred_path.exists() and not Path(str(pred_path) + ".gz").exists():
        raise FileNotFoundError(f"sealed split predictions not found: {pred_path}")
    eval_summary_path = pred_path.parent / "summary.json"
    if eval_summary_path.is_file():
        eval_summary = json.loads(eval_summary_path.read_text())
        expected_sha = eval_summary.get("predictions_sha256_sealed_before_scoring")
        if expected_sha is not None and sha256_bytes(read_bytes_maybe_gz(pred_path)) != expected_sha:
            raise ValueError("sealed split predictions do not match their summary")
    feats, labels, _ = _ensure_dataset(cfg)
    if args.margin is not None:
        margins = {family: args.margin for family in B.STRATEGIES}
    else:
        frozen_path = models_dir(cfg) / f"{cfg['experiment_id']}@{sp['test_start']}.backtest.json"
        if not frozen_path.exists():
            raise FileNotFoundError(f"dev-frozen backtest margins not found: {frozen_path}")
        frozen = json.loads(frozen_path.read_text())
        if frozen.get("test_start") != sp["test_start"]:
            raise ValueError("frozen margin file does not match the configured test_start")
        margins = frozen["selected"]
    summary, trades = B.run_backtest(cfg, read_jsonl(pred_path), feats, labels, start, end, margins, args.split)
    output = Path(args.output) if args.output else Path(cfg["paths"]["evidence_dir"]) / f"backtest-{args.split}"
    write_jsonl(output / "trades.jsonl", trades)
    write_json(output / "summary.json", summary)
    print(json.dumps({"output": str(output), "summary": summary}, indent=1))


def cmd_pooled(args):
    cfg = C.load(args.pooled)
    if args.stage == "dev":
        P.pooled_dev_stage(cfg, v3.PREREGISTRATION_SHA)
    else:
        P.pooled_test_stage(cfg, v3.PREREGISTRATION_SHA)


def cmd_nyc_reanalysis(args):
    cfg = C.load("configs/nyc-0800-v2.toml")
    dataset_dir = Path(cfg["paths"]["work_dir"]) / "dataset"
    feats, labels, _ = E.load_dataset(dataset_dir)
    state, oof = E.fit(cfg, feats, labels)
    state_name = f"{cfg['experiment_id']}@{cfg['split']['test_start']}.json"
    manifest_path = Path(cfg["paths"]["evidence_dir"]) / "model_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected_sha = manifest["state_sha256"]
    references = [
        models_dir(cfg) / state_name,
        Path(__file__).resolve().parents[2] / "Kalshii-v2" / cfg["paths"]["work_dir"] / "models" / state_name,
    ]
    for reference_path in references:
        if reference_path.is_file():
            saved_sha = json.loads(reference_path.read_text())["state_sha256"]
            if saved_sha != expected_sha:
                raise ValueError(f"saved v2 model SHA does not match its committed manifest: {reference_path}")
    if state["state_sha256"] != expected_sha:
        raise ValueError("scratch v2 refit does not match the committed v2 model SHA")

    settings = {**B.DEFAULT_BACKTEST, **cfg.get("backtest", {})}
    oof_rows = E.oof_prediction_rows(state, oof)
    margins = B.select_margin(
        oof_rows, feats, labels, cfg["split"]["test_start"],
        settings["margins"], settings["fee_rate"], settings["contracts"])
    prediction_path = Path(cfg["paths"]["evidence_dir"]) / "eval-test" / "predictions.jsonl"
    test_summary = json.loads((prediction_path.parent / "summary.json").read_text())
    prediction_sha = sha256_bytes(read_bytes_maybe_gz(prediction_path))
    if prediction_sha != test_summary["predictions_sha256_sealed_before_scoring"]:
        raise ValueError("saved v2 test predictions do not match their seal")
    start, end = date.fromisoformat(cfg["split"]["test_start"]), date.fromisoformat(cfg["split"]["test_end"])
    backtest_summary, trades = B.run_backtest(
        cfg, read_jsonl(prediction_path), feats, labels, start, end, margins["selected"], "test")
    out = Path("evidence/v3/nyc-reanalysis")
    summary = {
        "experiment_id": "nyc-0800-v2-reanalysis",
        "exploratory": True,
        "pre_registration_sha": v3.PREREGISTRATION_SHA,
        "window": [start.isoformat(), end.isoformat()],
        "refit_state_sha256": state["state_sha256"],
        "saved_v2_state_sha256": expected_sha,
        "source_test_predictions_sha256": prediction_sha,
        "selected_margins": margins["selected"],
        "dev_oof_pnl": margins["dev_oof_pnl"],
        "backtest": backtest_summary,
    }
    write_json(out / "summary.json", summary)
    write_jsonl(out / "trades.jsonl", trades)
    print(json.dumps({
        "output": str(out),
        "state_sha256": state["state_sha256"],
        "selected_margins": margins["selected"],
        "strategies": backtest_summary["strategies"],
    }, indent=1))


def cmd_demo(args):
    from . import demo
    demo.serve(_cfg(args), args.model, args.port, args.host)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forecast_lab")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit"); a.add_argument("--series", default=None); a.add_argument("--end", required=True)
    a.add_argument("--days", type=int, default=30); a.add_argument("--offline", action="store_true"); a.set_defaults(f=cmd_audit)
    dl = sub.add_parser("download"); dl.add_argument("--workers", type=int, default=2); dl.set_defaults(f=cmd_download)
    cat = sub.add_parser("catalogue"); cat.set_defaults(f=cmd_catalogue)
    vs = sub.add_parser("v3-splits")
    vs.add_argument("--pooled", required=True)
    vs.set_defaults(f=cmd_v3_splits)
    pool = sub.add_parser("pooled")
    pool.add_argument("--pooled", required=True)
    pool.add_argument("--stage", choices=["dev", "test"], required=True)
    pool.set_defaults(f=cmd_pooled)
    nr = sub.add_parser("nyc-reanalysis")
    nr.set_defaults(f=cmd_nyc_reanalysis)
    b = sub.add_parser("build"); b.set_defaults(f=cmd_build)
    f = sub.add_parser("fit"); f.add_argument("--config", dest="config", default=argparse.SUPPRESS); f.set_defaults(f=cmd_fit)
    e = sub.add_parser("evaluate"); e.add_argument("--config", dest="config", default=argparse.SUPPRESS); e.add_argument("--split", choices=["test", "audit"], default="test")
    e.set_defaults(f=cmd_evaluate)
    bt = sub.add_parser("backtest")
    bt.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    bt.add_argument("--split", choices=["test", "audit"], required=True)
    bt.add_argument("--exploratory", action="store_true")
    bt.add_argument("--margin", type=float)
    bt.add_argument("--reps", type=int)
    bt.add_argument("--output")
    bt.set_defaults(f=cmd_backtest)
    r = sub.add_parser("replay"); r.add_argument("--event", required=True); r.add_argument("--as-of", default="08:00")
    r.add_argument("--model", required=True); r.add_argument("--reveal", action="store_true"); r.add_argument("--full", action="store_true")
    r.add_argument("--drop-last-quote", action="store_true"); r.add_argument("--weather-delay-h", type=float, default=0.0)
    r.add_argument("--config", dest="config", default=argparse.SUPPRESS); r.set_defaults(f=cmd_replay)
    g = sub.add_parser("golden"); g.add_argument("--model", required=True); g.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    g.set_defaults(f=cmd_golden)
    rp = sub.add_parser("report"); rp.set_defaults(f=cmd_report)
    d = sub.add_parser("demo"); d.add_argument("--model", required=True); d.add_argument("--port", type=int, default=8765)
    d.add_argument("--host", default="127.0.0.1", help="bind address; loopback by default")
    d.add_argument("--config", dest="config", default=argparse.SUPPRESS); d.set_defaults(f=cmd_demo)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
