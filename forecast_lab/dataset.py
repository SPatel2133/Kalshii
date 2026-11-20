"""Assemble rules, labels, weather inputs and pre-cutoff feature rows from the local cache.

Labels are written to a separate file and never passed to replay.build_snapshot."""
import collections
from datetime import date, timedelta
from pathlib import Path

from . import FEATURE_VERSION, contracts, kalshi, replay, weather
from . import reasons as R
from .download import candle_range, load_catalogue
from .fetch import FetchError, Fetcher
from .util import atomic_write, parse_iso, read_jsonl, write_jsonl


class Sources:
    """Offline-by-default access to cached raw inputs, reused by CLI, evaluation and demo."""

    def __init__(self, cfg, offline=True):
        self.cfg = cfg
        self.fetcher = Fetcher.from_config(cfg, offline=offline)
        self.events, self.pages = load_catalogue(cfg, offline=offline)
        self._runs = {}
        self._months = {}
        self._asos = {}

    def candles(self, market):
        s, t = candle_range(market)
        try:
            cs, meta = kalshi.fetch_candles(self.fetcher, self.cfg["sources"]["kalshi_base"], self.cfg["series"],
                                            market["ticker"], s, t, self.cfg["quotes"]["candle_period_min"])
            return cs, meta, None
        except FetchError as e:
            return None, None, e.failure_class

    def mos_runs(self, d):
        months = tuple(sorted({date(x.year, x.month, 1) for x in (d - timedelta(days=1), d)}))
        key = months
        if key not in self._runs:
            rows, metas = [], []
            for dd in months:
                if dd in self._months:
                    r, m = self._months[dd]
                else:
                    try:
                        r, m = self._months[dd] = weather.fetch_mos_month(self.fetcher, self.cfg, dd)
                    except FetchError as e:  # not cached: the next window retries this month
                        r, m = [], {"failure_class": e.failure_class}
                rows += r
                metas.append(m)
            self._runs = {key: (weather.index_runs(rows), metas)}  # keep only the latest month window
        return self._runs[key]

    def asos(self, year):
        if year not in self._asos:
            try:
                self._asos[year], _ = weather.fetch_asos(self.fetcher, self.cfg, year)
            except FetchError:
                self._asos[year] = []
        return self._asos[year]


def build_event(src, event_id, local_hhmm=None, lag_h=None, scenario=None, drop_last_quote=False,
                weather_delay_h=0.0):
    """Return (rules, label, weather_rec, feature_row, provenance) for one event at one as-of time."""
    cfg = src.cfg
    lag_h = cfg["weather"]["lag_h"] if lag_h is None else lag_h
    local_hhmm = local_hhmm or cfg["cutoff_local"]
    markets = src.events.get(event_id, [])
    d = contracts.event_date_from_ticker(event_id)
    cut = replay.cutoff_utc(d, local_hhmm)
    rules, label = contracts.normalize_event(event_id, markets, cut)
    prov = {"candles": {}, "fetch_failures": []}
    candles = {}
    for b in rules.get("buckets", []):
        m = next(m for m in markets if m["ticker"] == b["ticker"])
        cs, meta, fail = src.candles(m)
        if fail:
            prov["fetch_failures"].append({"ticker": b["ticker"], "failure_class": fail})
            candles[b["ticker"]] = []
            continue
        if drop_last_quote:
            cut_ts = int(cut.timestamp())
            pre = [c for c in cs if c["end_period_ts"] <= cut_ts]
            if pre:
                last = max(c["end_period_ts"] for c in pre)
                cs = [c for c in cs if c["end_period_ts"] != last]
        candles[b["ticker"]] = cs
        prov["candles"][b["ticker"]] = {"sha256": meta["sha256"], "path": meta["path"], "params": meta["params"],
                                        "retrieved_utc": meta["retrieved_utc"]}
    runs, metas = src.mos_runs(d)
    prov["weather"] = [{"sha256": m.get("sha256"), "params": m.get("params"), "path": m.get("path"),
                        "failure_class": m.get("failure_class")} for m in metas]
    features = cfg["weather"].get("features", ["nx"])
    local_day = "sampled_minus_nx" in features
    wrec = weather.select_forecast(runs, d, cut, lag_h + weather_delay_h, local_day=local_day)
    row = replay.build_snapshot(rules, candles, wrec, cut, cfg["quotes"], exploratory=(local_hhmm != cfg["cutoff_local"]),
                                scenario=scenario, feature_version=cfg.get("feature_version", FEATURE_VERSION))
    if "sampled_minus_nx" in features:
        selected = row["weather"]["selected"]
        if selected is None or selected.get("sampled_max_local_f") is None:
            row["flags"] = sorted(set(row["flags"] + [R.FORECAST_SAMPLED_MISSING]))
    if any(name in features for name in ("obs_max_minus_nx", "obs_latest_minus_nx")):
        obs = weather.observation_summary(src.asos(d.year), d, cut, cfg["weather"].get("obs_latency_min", 10))
        row["weather"]["obs"] = obs
        if obs["n_obs"] == 0:
            row["flags"] = sorted(set(row["flags"] + [R.OBS_UNAVAILABLE]))
    if "sampled_minus_nx" in features or any(name in features for name in ("obs_max_minus_nx", "obs_latest_minus_nx")):
        row["fingerprint"] = replay.snapshot_fingerprint(row)
    if prov["fetch_failures"]:
        row["flags"] = sorted(set(row["flags"] + [R.FETCH_FAILED]))
        row["market_status"] = "unavailable"
        row["fingerprint"] = replay.snapshot_fingerprint(row)
    return rules, label, wrec, row, prov


def primary_reason(rules, label, row, regime="NWS"):
    """Single primary exclusion reason following the funnel order, plus all detailed flags."""
    flags = sorted(set(rules.get("reasons", []) + row.get("flags", []) + (label or {}).get("reasons", [])))
    if not rules.get("buckets") and R.MISSING_CATALOGUE in rules.get("reasons", []):
        return "catalogue", R.MISSING_CATALOGUE, flags
    if rules.get("reasons"):
        order = [R.MISSING_RULE_EVIDENCE, R.OTHER_REGIME, R.INVALID_PARTITION]
        return "rules_valid", next((o for o in order if o in rules["reasons"]), rules["reasons"][0]), flags
    if row.get("market_status") != "complete":
        order = [R.FETCH_FAILED, R.QUOTES_UNAVAILABLE, R.QUOTES_EMPTY_BOOK, R.CROSSED_BOOK, R.QUOTES_STALE, R.PARTIAL_BOARD]
        return "quote_complete", next((o for o in order if o in row["flags"]), R.QUOTES_UNAVAILABLE), flags
    if row["weather"]["status"] != "available":
        return "weather_available", row["weather"]["reason"], flags
    if label is None or label.get("reasons"):
        return "label_valid", (label or {}).get("reasons", [R.SETTLEMENT_UNRESOLVED])[0], flags
    return None, None, flags


def build_all(cfg, start, end, out_dir, lag_h=None, log=print):
    src = Sources(cfg, offline=True)
    out = Path(out_dir)
    rules_rows, label_rows, feat_rows, cov_rows = [], [], [], []
    evs_by_date = collections.defaultdict(list)
    for e in src.events:
        d = contracts.event_date_from_ticker(e)
        if d:
            evs_by_date[d].append(e)
    d = start
    while d <= end:
        evs = sorted(evs_by_date.get(d, []))
        if not evs:
            cov_rows.append({"event_date": d.isoformat(), "event_id": None, "stage_failed": "catalogue",
                             "primary_reason": R.MISSING_CATALOGUE, "flags": [R.MISSING_CATALOGUE]})
        for e in evs:
            rules, label, wrec, row, prov = build_event(src, e, lag_h=lag_h)
            stage, reason, flags = primary_reason(rules, label, row)
            rules_rows.append(rules)
            label_rows.append(label)
            feat_rows.append(row)
            cov_rows.append({"event_date": d.isoformat(), "event_id": e, "stage_failed": stage, "primary_reason": reason,
                             "flags": flags, "regime": rules.get("regime"), "n_buckets": len(rules.get("buckets", [])),
                             "weather_runtime": (row["weather"]["selected"] or {}).get("runtime_utc"),
                             "settlement_ts": (label or {}).get("settlement_ts")})
        d += timedelta(days=1)
    atomic_write(out / "config_sha256.txt", cfg["_sha256"] + "\n")
    write_jsonl(out / "rules.jsonl", rules_rows)
    write_jsonl(out / "labels.jsonl", label_rows)
    write_jsonl(out / "features.jsonl", feat_rows)
    write_jsonl(out / "coverage.jsonl", cov_rows)
    return rules_rows, label_rows, feat_rows, cov_rows


def funnel(cov_rows):
    stages = ["catalogue", "rules_valid", "quote_complete", "weather_available", "label_valid"]
    dates = sorted({r["event_date"] for r in cov_rows})
    out = {"candidate_dates": len(dates)}
    failed_at = {}
    for r in cov_rows:
        failed_at.setdefault(r["event_date"], r["stage_failed"])
    remaining = set(dates)
    for s in stages:
        remaining -= {d for d, st in failed_at.items() if st == s}
        out[s] = len(remaining)
    out["reasons"] = dict(collections.Counter(r["primary_reason"] for r in cov_rows if r["primary_reason"]))
    return out
