"""Weather forecast adapter (IEM archive of NWS GFS MOS, station KNYC = Central Park) and availability evidence.

Variable mapping (documented in docs/experiment.md):
- `n_x` at ftime (D+1) 00:00Z is the MOS daytime maximum for 7 a.m.-7 p.m. LST (12Z-00Z) of day D.
  The Kalshi NWS settlement uses the CLI daily maximum for midnight-midnight LST (05Z-05Z). The MOS
  daytime max is therefore a named proxy (`mos_daytime_max_proxy`), and its error vs. the settled
  value is fitted on training dates only.
- `tmp` 3-hourly samples with ftime in (D 05Z, D+1 05Z] give a secondary sampled-max proxy.
Availability: the archive records model runtime only, not publication. We use
available_utc = runtime + lag_h (conservative vs. an observed 4h13m publication lag), and the strict
selector only accepts runs with available_utc <= cutoff_utc.
"""
import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import reasons as R
from .util import iso_utc

PROVIDER = "iem"
SOURCE_ID = "iem-mos-gfs-knyc"
NY = ZoneInfo("America/New_York")


def month_starts(start, end):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def lst_day_bounds(event_date, tz):
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    offset = datetime(event_date.year, 1, 1, tzinfo=zone).utcoffset()
    lo = datetime.combine(event_date, time.min, timezone.utc) - offset
    return lo, lo + timedelta(days=1)


def fetch_mos(fetcher, cfg, start, end):
    """Fetch GFS MOS rows for runtimes in [start-1 day, end] in month chunks. Returns (rows, metas)."""
    src = cfg["sources"]
    rows, metas = [], []
    for m0 in month_starts(start - timedelta(days=1), end):
        r, meta = fetch_mos_month(fetcher, cfg, m0)
        metas.append(meta)
        rows.extend(r)
    return rows, metas


def fetch_mos_month(fetcher, cfg, m0):
    """Fetch GFS MOS rows for runtimes in the calendar month starting at m0. Returns (rows, meta)."""
    src = cfg["sources"]
    m1 = date(m0.year + (m0.month == 12), m0.month % 12 + 1, 1)
    params = {"station": src["mos_station"], "model": src["mos_model"], "format": "csv",
              "sts": f"{m0.isoformat()}T00:00Z", "ets": f"{m1.isoformat()}T00:00Z"}
    body, meta = fetcher.get(PROVIDER, src["iem_mos"], "", params)
    return parse_mos_csv(body.decode()), meta


def _ts(s):
    s = s.strip()
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _num(x):
    x = (x or "").strip()
    return float(x) if x else None


def parse_mos_csv(text):
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if not r.get("runtime"):
            continue
        out.append({"runtime": _ts(r["runtime"]), "ftime": _ts(r["ftime"]), "station": (r.get("station") or "").strip(),
                    "model": (r.get("model") or "").strip(), "n_x": _num(r.get("n_x")), "tmp": _num(r.get("tmp"))})
    return out


def index_runs(rows):
    runs, seen = {}, set()
    for r in rows:
        k = (r["runtime"], r["ftime"], r["station"], r["model"])
        if k in seen:
            continue
        seen.add(k)
        runs.setdefault(r["runtime"], []).append(r)
    for v in runs.values():
        v.sort(key=lambda r: r["ftime"])
    return runs


def run_forecast(run_rows, event_date, local_day=False, day_window=None, tz=NY):
    """Extract the day-D max proxies from one run. Returns dict or None fields when missing."""
    d0 = datetime(event_date.year, event_date.month, event_date.day, tzinfo=timezone.utc)
    t_nx = d0 + timedelta(days=1)
    nx = [r["n_x"] for r in run_rows if r["ftime"] == t_nx and r["n_x"] is not None]
    lo, hi = d0 + timedelta(hours=5), d0 + timedelta(days=1, hours=5)
    samples = [r["tmp"] for r in run_rows if lo < r["ftime"] <= hi and r["tmp"] is not None]
    out = {"mos_daytime_max_f": nx[0] if nx else None,
           "nx_valid_utc": iso_utc(t_nx.timestamp()),
           "nx_window": "daytime max 7am-7pm LST (12Z-00Z) of event date",
           "sampled_max_f": max(samples) if len(samples) >= 7 else None,
           "n_samples": len(samples),
           "sample_window_utc": [iso_utc(lo.timestamp()), iso_utc(hi.timestamp())]}
    if local_day or day_window == "lst":
        if day_window == "lst":
            local_lo, local_hi = lst_day_bounds(event_date, tz)
        else:
            zone = ZoneInfo(tz) if isinstance(tz, str) else tz
            local_lo = datetime.combine(event_date, time.min, zone).astimezone(timezone.utc)
            local_hi = datetime.combine(event_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        local_samples = [r["tmp"] for r in run_rows
                         if local_lo <= r["ftime"] < local_hi and r["tmp"] is not None]
        out.update({"sampled_max_local_f": max(local_samples) if len(local_samples) >= 7 else None,
                    "n_samples_local": len(local_samples),
                    "sample_window_local_utc": [iso_utc(local_lo.timestamp()), iso_utc(local_hi.timestamp())]})
    if day_window is not None:
        out["day_window"] = day_window
    return out


def select_forecast(runs, event_date, cutoff_utc, lag_h, local_day=False, day_window=None, tz=NY, station=None):
    """Strict as-of selector: latest run whose assumed availability (runtime + lag) <= cutoff and which
    carries the day-D daytime max. Returns a weather input record (never None)."""
    cands = []
    for rt in sorted(runs):
        if rt > cutoff_utc or rt < cutoff_utc - timedelta(hours=36):
            continue
        avail = rt + timedelta(hours=lag_h)
        fc = run_forecast(runs[rt], event_date, local_day=local_day, day_window=day_window, tz=tz)
        cands.append({"runtime_utc": iso_utc(rt.timestamp()), "available_utc": iso_utc(avail.timestamp()),
                      "eligible": avail <= cutoff_utc, **fc})
    elig = [c for c in cands if c["eligible"] and c["mos_daytime_max_f"] is not None]
    rec = {"source_id": SOURCE_ID, "station": station or "KNYC", "lag_h": lag_h,
           "availability_basis": "runtime+lag assumption", "candidates": cands}
    if day_window is not None:
        rec["day_window"] = day_window
    if elig:
        best = elig[-1]
        rec.update({"status": "available", "reason": None, "selected": best})
    else:
        reason = R.FORECAST_UNAVAILABLE if not any(c["eligible"] for c in cands) else R.FORECAST_MISSING_FIELD
        rec.update({"status": "unavailable", "reason": reason, "selected": None})
    return rec


def fetch_asos(fetcher, cfg, year):
    """Hourly Central Park ASOS temperatures (audit only: time-of-max window check; never a forecast)."""
    src = cfg["sources"]
    params = {"station": src["asos_station"], "data": "tmpf", "year1": year, "month1": 1, "day1": 1,
              "year2": year + 1, "month2": 1, "day2": 1, "tz": "Etc/UTC", "format": "onlycomma",
              "latlon": "no", "missing": "M", "trace": "T", "direct": "no"}
    body, meta = fetcher.get(PROVIDER, src["iem_asos"], "", params)
    out = []
    for r in csv.DictReader(io.StringIO(body.decode())):
        t = (r.get("tmpf") or "M").strip()
        if t in ("M", ""):
            continue
        out.append((datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc), float(t)))
    return out, meta


def observation_summary(observations, event_date, cutoff_utc, latency_min, tz=NY, day_window=None):
    if day_window == "lst":
        lo, _ = lst_day_bounds(event_date, tz)
    else:
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        lo = datetime.combine(event_date, time.min, zone).astimezone(timezone.utc)
    hi = cutoff_utc - timedelta(minutes=latency_min)
    selected = sorted(((valid, temp) for valid, temp in observations if lo <= valid <= hi), key=lambda x: x[0])
    summary = {"obs_max_so_far_f": max((temp for _, temp in selected), default=None),
               "obs_latest_f": selected[-1][1] if selected else None,
               "obs_latest_valid_utc": iso_utc(selected[-1][0].timestamp()) if selected else None,
               "n_obs": len(selected), "latency_min": latency_min,
               "window_utc": [iso_utc(lo.timestamp()), iso_utc(hi.timestamp())]}
    if day_window is not None:
        summary["day_window"] = day_window
    return summary
