"""Command dispatch: audit, build, fit, evaluate, replay, demo."""
import argparse
import csv
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import config as C, dataset, download, evaluation as E
from .service import ReplayService, load_state, models_dir
from .util import atomic_write, read_jsonl, sha256_bytes, write_json, write_jsonl


def _cfg(args):
    return C.load(args.config) if getattr(args, "config", None) else C.default_config()


def cmd_audit(args):
    cfg = _cfg(args)
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
    start, end = date.fromisoformat(sp["dev_start"]), date.fromisoformat(sp["audit_end"])
    ev, _ = download.load_catalogue(cfg, offline=False)
    st = download.download_candles(cfg, ev, start, end, workers=args.workers)
    print(f"candles ok={st['ok']} failed={len(st['failed'])}")
    src = dataset.Sources(cfg, offline=False)
    d = start
    while d <= end:
        src.mos_runs(d)
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    print("mos cached")


def cmd_build(args):
    cfg = _cfg(args)
    sp = cfg["split"]
    dataset.build_all(cfg, date.fromisoformat(sp["dev_start"]), date.fromisoformat(sp["audit_end"]),
                      Path(cfg["paths"]["work_dir"]) / "dataset")
    print("built", Path(cfg["paths"]["work_dir"]) / "dataset")


def _ensure_dataset(cfg):
    d = Path(cfg["paths"]["work_dir"]) / "dataset"
    if not (d / "features.jsonl").exists():
        sp = cfg["split"]
        dataset.build_all(cfg, date.fromisoformat(sp["dev_start"]), date.fromisoformat(sp["audit_end"]), d)
    return E.load_dataset(d)


def cmd_fit(args):
    cfg = _cfg(args)
    feats, labels, cov = _ensure_dataset(cfg)
    state, oof = E.fit(cfg, feats, labels)
    p = models_dir(cfg) / f"{cfg['experiment_id']}@{state['fit_boundary']}.json"
    write_json(p, state)
    ev = Path(cfg["paths"]["evidence_dir"])
    man = {k: v for k, v in state.items() if k not in ("upstream",)}
    man["upstream"] = {k: v for k, v in state["upstream"].items() if k not in ("clim_values", "clim_doys")}
    man["state_path"] = str(p)
    write_json(ev / "model_manifest.json", man)
    print(json.dumps({"state": str(p), "state_sha256": state["state_sha256"], "n_dev": state["n_dev_events"],
                      "oof_nll": state["oof_nll"], "fixed_w": state["fixed_blend"]["w"],
                      "lambda": state["upstream"]["lambda"], "correction": state["correction"]["validation"],
                      "model_ids": [E.model_id(state, f) for f in E.PRIMARY_FAMILIES]}, indent=1))


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
    sealed = sha256_bytes((out / "predictions.jsonl").read_bytes())
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
                                          "weather_selection_changed": sum(1 for e in ids_s if fs[e]["weather"] != feats[e]["weather"])}
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
    fx = {"event_id": ev, "model_id": r["model_id"], "state_sha256": st["state_sha256"], "cutoff_utc": r["cutoff_utc"],
          "replay_fingerprint": r["replay_fingerprint"], "prediction_fingerprint": r["prediction"]["prediction_fingerprint"],
          "buckets": [{k: b[k] for k in ("bucket_id", "ticker", "lo", "hi", "bid", "ask", "quote_obs_utc")} for b in r["buckets"]],
          "weather": {k: r["weather"]["selected"][k] for k in ("runtime_utc", "available_utc", "mos_daytime_max_f")},
          "probabilities": r["prediction"]["probabilities"],
          "candle_sha256": {t: c["sha256"] for t, c in r["provenance"]["candles"].items()},
          "label": {k: rev["label"][k] for k in ("winner_bucket", "winner_ticker", "settled_value_f", "settlement_ts")}}
    write_json(Path("tests/fixtures/golden_KXHIGHNY-26AUG13.json"), fx)
    print(json.dumps(fx, indent=1))


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


def cmd_demo(args):
    from . import demo
    demo.serve(_cfg(args), args.model, args.port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forecast_lab")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit"); a.add_argument("--series", default="KXHIGHNY"); a.add_argument("--end", required=True)
    a.add_argument("--days", type=int, default=30); a.add_argument("--offline", action="store_true"); a.set_defaults(f=cmd_audit)
    dl = sub.add_parser("download"); dl.add_argument("--workers", type=int, default=2); dl.set_defaults(f=cmd_download)
    b = sub.add_parser("build"); b.set_defaults(f=cmd_build)
    f = sub.add_parser("fit"); f.add_argument("--config", dest="config"); f.set_defaults(f=cmd_fit)
    e = sub.add_parser("evaluate"); e.add_argument("--config", dest="config"); e.add_argument("--split", choices=["test", "audit"], default="test")
    e.set_defaults(f=cmd_evaluate)
    r = sub.add_parser("replay"); r.add_argument("--event", required=True); r.add_argument("--as-of", default="08:00")
    r.add_argument("--model", required=True); r.add_argument("--reveal", action="store_true"); r.add_argument("--full", action="store_true")
    r.add_argument("--drop-last-quote", action="store_true"); r.add_argument("--weather-delay-h", type=float, default=0.0)
    r.add_argument("--config", dest="config"); r.set_defaults(f=cmd_replay)
    g = sub.add_parser("golden"); g.add_argument("--model", required=True); g.add_argument("--config", dest="config")
    g.set_defaults(f=cmd_golden)
    d = sub.add_parser("demo"); d.add_argument("--model", required=True); d.add_argument("--port", type=int, default=8765)
    d.add_argument("--config", dest="config"); d.set_defaults(f=cmd_demo)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
