"""As-of selection, immutable pre-cutoff snapshot and provenance.

Nothing in this module reads label fields. Inputs are the event rules record (outcome-free),
raw candles per ticker and a weather input record. The snapshot fingerprint covers canonical selected
inputs plus FEATURE_VERSION/REPLAY_VERSION, never retrieval time.
"""
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import FEATURE_VERSION, REPLAY_VERSION
from . import reasons as R
from .kalshi import parse_candle
from .util import fingerprint, iso_utc

NY = ZoneInfo("America/New_York")


def cutoff_utc(event_date, local_hhmm="08:00", tz=NY):
    hh, mm = map(int, local_hhmm.split(":"))
    return datetime.combine(event_date, time(hh, mm), tz).astimezone(timezone.utc)


def canonical_candles(raw_candles):
    """Sort and deduplicate parsed candles by end_ts. Identical duplicates collapse; conflicting
    duplicates resolve deterministically (lexicographically greatest canonical record) and are counted."""
    by = {}
    conflicts = 0
    for c in raw_candles:
        p = parse_candle(c) if "end_period_ts" in c else dict(c)
        k = p["end_ts"]
        if k in by and by[k] != p:
            conflicts += 1
            p = max(by[k], p, key=lambda x: fingerprint(x))
        by[k] = p
    return [by[k] for k in sorted(by)], conflicts


def _valid_quote(c):
    b, a = c["bid"], c["ask"]
    if b is None or a is None:
        return False
    if not (math.isfinite(b) and math.isfinite(a)):
        return False
    if b <= 0.0 and a >= 1.0:  # no resting interest on either side: no information
        return False
    return True


def _empty_book(c):
    b, a = c["bid"], c["ask"]
    return b is not None and a is not None and math.isfinite(b) and math.isfinite(a) and b <= 0.0 and a >= 1.0


def select_quote(candles, cut_ts, max_age_h, hist_h, empty_book="skip"):
    """Return the bucket's as-of quote record from candles (parsed, canonical) with end_ts <= cut_ts.

    empty_book="skip" (v1): an empty latest book is ignored and the previous valid quote is carried forward.
    empty_book="exclude" (v2): if the latest quoted candle is an empty book, the bucket has no as-of price."""
    elig = [c for c in candles if c["end_ts"] <= cut_ts]
    out = {"n_candles_eligible": len(elig), "disposition": "ok", "flags": []}
    if empty_book == "exclude":
        quoted = [c for c in elig if c["bid"] is not None and c["ask"] is not None]
        if quoted and _empty_book(quoted[-1]):
            out["disposition"] = R.QUOTES_EMPTY_BOOK
            out["flags"].append(R.QUOTES_EMPTY_BOOK)
            out["empty_book_obs_utc"] = iso_utc(quoted[-1]["end_ts"])
            return out
    quotes = [c for c in elig if _valid_quote(c)]
    if not quotes:
        out["disposition"] = R.QUOTES_UNAVAILABLE
        return out
    q = quotes[-1]
    age_h = (cut_ts - q["end_ts"]) / 3600.0
    out.update({"bid": q["bid"], "ask": q["ask"], "quote_obs_utc": iso_utc(q["end_ts"]), "quote_age_h": age_h,
                "mid": (q["bid"] + q["ask"]) / 2.0, "spread": q["ask"] - q["bid"]})
    if q["bid"] > q["ask"] or q["bid"] < 0 or q["ask"] > 1:
        out["disposition"] = R.CROSSED_BOOK
        out["flags"].append(R.CROSSED_BOOK)
    elif age_h > max_age_h:
        out["disposition"] = R.QUOTES_STALE
        out["flags"].append(R.QUOTES_STALE)
    traded = [c for c in elig if c["trade_close"] is not None and (c["volume"] or 0) > 0]
    if traded:
        t = traded[-1]
        out["last_traded_candle_utc"] = iso_utc(t["end_ts"])
        out["trade_age_h"] = (cut_ts - t["end_ts"]) / 3600.0
        out["last_trade_close"] = t["trade_close"]
    else:
        out["last_traded_candle_utc"] = None
        out["trade_age_h"] = None
        out["last_trade_close"] = None
    past = [c for c in quotes if c["end_ts"] <= cut_ts - hist_h * 3600]
    out["mid_hist"] = (past[-1]["bid"] + past[-1]["ask"]) / 2.0 if past else None
    out["volume_pre_cutoff"] = sum((c["volume"] or 0) for c in elig)
    return out


def build_snapshot(rules, candles_by_ticker, weather_rec, cut, cfg_quotes, exploratory=False, scenario=None,
                   feature_version=FEATURE_VERSION):
    """rules: outcome-free event rules record. Returns the historical feature row."""
    cut_ts = int(cut.timestamp())
    row = {"event_id": rules["event_id"], "event_date": rules["event_date"], "cutoff_utc": iso_utc(cut_ts),
           "feature_version": feature_version, "replay_version": REPLAY_VERSION, "regime": rules.get("regime"),
           "exploratory_horizon": bool(exploratory), "scenario": scenario or "historical", "buckets": [],
           "reasons": list(rules.get("reasons", [])), "flags": []}
    conflicts = 0
    for b in rules.get("buckets", []):
        cs, nconf = canonical_candles(candles_by_ticker.get(b["ticker"], []))
        conflicts += nconf
        q = select_quote(cs, cut_ts, cfg_quotes["max_quote_age_h"], cfg_quotes["history_window_h"],
                         cfg_quotes.get("empty_book", "skip"))
        row["buckets"].append({"bucket_id": b["bucket_id"], "ticker": b["ticker"], "lo": b["lo"], "hi": b["hi"], **q})
    row["candle_conflicts"] = conflicts
    disp = [b["disposition"] for b in row["buckets"]]
    if row["buckets"] and all(d == "ok" for d in disp):
        row["market_status"] = "complete"
    else:
        row["market_status"] = "unavailable"
        for d in disp:
            if d != "ok":
                row["flags"].append(d)
        if any(d == "ok" for d in disp):
            row["flags"].append(R.PARTIAL_BOARD)
        if not row["buckets"]:
            row["flags"].append(R.QUOTES_UNAVAILABLE)
    sel = (weather_rec or {}).get("selected")
    row["weather"] = {"status": (weather_rec or {}).get("status", "unavailable"),
                      "reason": (weather_rec or {}).get("reason", R.FORECAST_UNAVAILABLE),
                      "source_id": (weather_rec or {}).get("source_id"),
                      "lag_h": (weather_rec or {}).get("lag_h"),
                      "availability_basis": (weather_rec or {}).get("availability_basis"),
                      "selected": sel}
    if row["weather"]["status"] != "available":
        row["flags"].append(row["weather"]["reason"])
    row["flags"] = sorted(set(row["flags"]))
    row["fingerprint"] = snapshot_fingerprint(row)
    return row


FP_EXCLUDE = {"fingerprint"}


def snapshot_fingerprint(row):
    return fingerprint({k: v for k, v in row.items() if k not in FP_EXCLUDE})


def market_vector(row):
    """Normalized eligible bid-ask midpoints over the whole board, or None if not complete-case."""
    if row.get("market_status") != "complete":
        return None
    mids = [b["mid"] for b in row["buckets"]]
    s = sum(mids)
    if not s > 0:
        return None
    return [m / s for m in mids]
