"""Dated eligibility and interval normalization from actual contract rule text."""
import re
from datetime import date, datetime

from . import RULE_PARSE_VERSION
from . import reasons as R
from .util import parse_iso

NEG_INF = None  # open lower tail
POS_INF = None  # open upper tail

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

RE_BETWEEN = re.compile(r"is between (-?\d+)-(-?\d+)°")
RE_GREATER = re.compile(r"is greater than (-?\d+)°")
RE_LESS = re.compile(r"is less than (-?\d+)°")
RE_DATE = re.compile(r"for ([A-Z][a-z]{2,8}) (\d{1,2}), (\d{4})")


def event_date_from_ticker(event_ticker):
    m = re.match(r"^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if not m:
        return None
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def rule_regime(text):
    if not text:
        return None
    if "National Weather Service" in text:
        return "NWS"
    if "The Weather Company" in text:
        return "TWC"
    return "UNKNOWN"


def parse_interval(market):
    """Return (lo, hi, evidence) as inclusive integer °F bounds (None = open tail), or raise ValueError.

    Derived from rules_primary wording ("between a-b" inclusive, "greater than n" => >= n+1,
    "less than n" => <= n-1) and cross-checked against strike_type/floor_strike/cap_strike."""
    text = market.get("rules_primary") or ""
    st = market.get("strike_type")
    fl, cp = market.get("floor_strike"), market.get("cap_strike")
    mb, mg, ml = RE_BETWEEN.search(text), RE_GREATER.search(text), RE_LESS.search(text)
    hits = [x for x in (mb, mg, ml) if x]
    if len(hits) != 1:
        raise ValueError(f"ambiguous or missing interval wording in rules for {market.get('ticker')}")
    if mb:
        lo, hi = int(mb.group(1)), int(mb.group(2))
        ok = st == "between" and fl == lo and cp == hi and lo <= hi
        ev = mb.group(0)
    elif mg:
        n = int(mg.group(1))
        lo, hi = n + 1, POS_INF
        ok = st == "greater" and fl == n
        ev = mg.group(0)
    else:
        n = int(ml.group(1))
        lo, hi = NEG_INF, n - 1
        ok = st == "less" and cp == n
        ev = ml.group(0)
    if not ok and st is None and fl is None and cp is None:
        ev += " [strike fields absent; rule text only]"
        ok = True
    if not ok:
        raise ValueError(f"rule text and strike fields disagree for {market.get('ticker')}: {ev} vs {st},{fl},{cp}")
    return lo, hi, ev


def validate_partition(intervals):
    """intervals: list of (lo, hi). Must be disjoint, gap-free and exhaustive including both tails."""
    if not intervals:
        return False, "empty"
    iv = sorted(intervals, key=lambda x: (-10**9 if x[0] is None else x[0]))
    if iv[0][0] is not None:
        return False, "missing lower tail"
    if iv[-1][1] is not None:
        return False, "missing upper tail"
    for (alo, ahi), (blo, bhi) in zip(iv, iv[1:]):
        if ahi is None or blo is None:
            return False, "open tail in interior"
        if blo <= ahi:
            return False, f"overlap at {blo}"
        if blo != ahi + 1:
            return False, f"gap between {ahi} and {blo}"
    for lo, hi in iv:
        if lo is not None and hi is not None and lo > hi:
            return False, "inverted interval"
    return True, "ok"


def bucket_id(lo, hi):
    if lo is None:
        return f"le{hi}"
    if hi is None:
        return f"ge{lo}"
    return f"{lo}-{hi}"


def contains(lo, hi, t):
    return (lo is None or t >= lo) and (hi is None or t <= hi)


def normalize_event(event_ticker, markets, cutoff_utc, station_phrase=None):
    """Build the event rules record (no outcome fields) plus a separate label record.

    cutoff_utc: datetime. Eligibility universe = contracts created and open at the cutoff."""
    rec = {"event_id": event_ticker, "parse_version": RULE_PARSE_VERSION, "reasons": [], "buckets": []}
    ed = event_date_from_ticker(event_ticker)
    rec["event_date"] = ed.isoformat() if ed else None
    regimes = sorted({rule_regime(m.get("rules_primary")) or "MISSING" for m in markets})
    rec["regime"] = regimes[0] if len(regimes) == 1 else "MIXED"
    rec["regimes_seen"] = regimes
    if not markets:
        rec["reasons"].append(R.MISSING_CATALOGUE)
        return rec, None
    if rec["regime"] in ("MISSING", "MIXED", "UNKNOWN"):
        rec["reasons"].append(R.MISSING_RULE_EVIDENCE)
    elif rec["regime"] != "NWS":
        rec["reasons"].append(R.OTHER_REGIME)
    text0 = markets[0].get("rules_primary") or ""
    if station_phrase is None:
        rec["station"] = "Central Park, New York (CLINYC)" if "Central Park" in text0 else (
            "New York City (CLINYC)" if "CLINYC" in text0 else None)
    else:
        rec["station"] = station_phrase
    md = RE_DATE.search(text0)
    rec["rule_date_text"] = md.group(0) if md else None
    if md and ed:
        try:
            rd = datetime.strptime(f"{md.group(1)[:3]} {md.group(2)} {md.group(3)}", "%b %d %Y").date()
            if rd != ed:
                rec["reasons"].append(R.MISSING_RULE_EVIDENCE)
        except ValueError:
            pass
    rec["day_convention"] = ("NWS Climatological Report (Daily): calendar day midnight-midnight local standard time"
                             if rec["regime"] == "NWS" else "per rules text")
    eligible, ineligible = [], []
    for m in markets:
        op, cl, cr = parse_iso(m.get("open_time")), parse_iso(m.get("close_time")), parse_iso(m.get("created_time"))
        if op and cl and op <= cutoff_utc < cl and (cr is None or cr <= cutoff_utc):
            eligible.append(m)
        else:
            ineligible.append(m["ticker"])
    if station_phrase and rec["regime"] == "NWS" and any(
            station_phrase.casefold() not in (m.get("rules_primary") or "").casefold() for m in eligible):
        rec["reasons"].append(R.OTHER_REGIME)
    rec["not_listed_at_cutoff"] = sorted(ineligible)
    try:
        parsed = []
        for m in eligible:
            lo, hi, ev = parse_interval(m)
            parsed.append((lo, hi, m, ev))
    except ValueError as e:
        rec["reasons"].append(R.MISSING_RULE_EVIDENCE)
        rec["parse_error"] = str(e)
        parsed = []
    ok, why = validate_partition([(lo, hi) for lo, hi, _, _ in parsed])
    rec["partition_ok"] = ok
    rec["partition_check"] = why
    if not ok:
        rec["reasons"].append(R.INVALID_PARTITION)
    parsed.sort(key=lambda x: (-10**9 if x[0] is None else x[0]))
    for lo, hi, m, ev in parsed:
        rec["buckets"].append({"bucket_id": bucket_id(lo, hi), "ticker": m["ticker"], "lo": lo, "hi": hi,
                               "open_time": m.get("open_time"), "close_time": m.get("close_time"),
                               "rule_evidence": ev})
    rec["rules_primary_example"] = text0
    rec["reasons"] = sorted(set(rec["reasons"]))
    return rec, make_label(event_ticker, markets, rec)


def make_label(event_ticker, markets, rec):
    """Label record kept separate from predictors."""
    lab = {"event_id": event_ticker, "reasons": []}
    statuses = sorted({m.get("status") for m in markets})
    results = {m["ticker"]: m.get("result") for m in markets}
    yes = [t for t, r in results.items() if r == "yes"]
    lab["statuses"] = statuses
    lab["settlement_ts"] = max((m.get("settlement_ts") or "" for m in markets), default="") or None
    vals = sorted({m.get("expiration_value") for m in markets if m.get("expiration_value") not in (None, "")})
    lab["expiration_values"] = vals
    lab["winner_ticker"] = yes[0] if len(yes) == 1 else None
    if any(s not in ("finalized", "settled") for s in statuses) or lab["settlement_ts"] is None:
        lab["reasons"].append(R.SETTLEMENT_UNRESOLVED)
    if len(yes) != 1 or any(r not in ("yes", "no") for r in results.values()):
        lab["reasons"].append(R.SETTLEMENT_EXCEPTIONAL)
    if any(m.get("settlement_bounds_type") not in (None, "default") for m in markets):
        lab["reasons"].append(R.SETTLEMENT_EXCEPTIONAL)
    lab["settled_value_f"] = None
    if len(vals) == 1:
        try:
            v = float(vals[0])
            if v.is_integer():
                lab["settled_value_f"] = int(v)
        except ValueError:
            pass
    # Map winner to the eligible bucket; check the settled value falls inside it.
    lab["winner_bucket"] = None
    for b in rec.get("buckets", []):
        if b["ticker"] == lab["winner_ticker"]:
            lab["winner_bucket"] = b["bucket_id"]
            if lab["settled_value_f"] is not None and not contains(b["lo"], b["hi"], lab["settled_value_f"]):
                lab["reasons"].append(R.SETTLEMENT_EXCEPTIONAL)
                lab["value_bucket_mismatch"] = True
    if lab["winner_ticker"] and lab["winner_bucket"] is None and rec.get("buckets"):
        lab["reasons"].append(R.SETTLEMENT_EXCEPTIONAL)
    lab["reasons"] = sorted(set(lab["reasons"]))
    return lab
