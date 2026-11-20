"""Kalshi catalogue, candles, parsers and live/archive routing (public read-only endpoints)."""
import json
from pathlib import Path

from .fetch import FetchError
from .util import atomic_write, parse_iso

PROVIDER = "kalshi"


def historical_cutoff(fetcher, base):
    data, meta = fetcher.get_json(PROVIDER, base, "/historical/cutoff", {"_day": meta_day()})
    return data, meta


def meta_day():
    # The cutoff moves over time; key it by retrieval day so a cached value is not reused forever.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_catalogue(fetcher, base, series, archive=True, progress_path=None, page_limit=1000):
    """Paginate the market catalogue, deduplicating by ticker. Returns (markets, pages_meta).

    Progress (cursor list) is persisted so an interrupted run resumes; each page is also
    cached under its own request key, so a resumed run re-reads cached pages.
    """
    endpoint = "/historical/markets" if archive else "/markets"
    progress = {"cursors": [None], "done": False}
    if progress_path and Path(progress_path).exists():
        progress = json.loads(Path(progress_path).read_text())
    by_ticker = {}
    pages = []
    seen_cursors = set()
    i = 0
    while True:
        cursor = progress["cursors"][i] if i < len(progress["cursors"]) else None
        if i >= len(progress["cursors"]):
            break
        if cursor in seen_cursors:
            raise FetchError("pagination_cycle", endpoint, str(cursor))
        seen_cursors.add(cursor)
        params = {"series_ticker": series, "limit": page_limit, "cursor": cursor}
        data, meta = fetcher.get_json(PROVIDER, base, endpoint, params)
        if "markets" not in data:
            raise FetchError("malformed_payload", endpoint, "no markets key")
        pages.append({"cursor": cursor, "n": len(data["markets"]), "sha256": meta["sha256"], "path": meta["path"]})
        for m in data["markets"]:
            t = m["ticker"]
            if t in by_ticker and by_ticker[t] != m:
                # Same ticker, different content: keep the most recently updated record.
                if (m.get("updated_time") or "") < (by_ticker[t].get("updated_time") or ""):
                    continue
            by_ticker[t] = m
        nxt = data.get("cursor") or None
        if nxt and len(data["markets"]) > 0:
            if i + 1 >= len(progress["cursors"]):
                progress["cursors"].append(nxt)
        else:
            progress["done"] = True
        if progress_path:
            atomic_write(progress_path, json.dumps(progress))
        i += 1
        if progress["done"] and i >= len(progress["cursors"]):
            break
    markets = [by_ticker[t] for t in sorted(by_ticker)]
    return markets, pages


def fetch_candles(fetcher, base, series, ticker, start_ts, end_ts, period_min=60, archive=True):
    if archive:
        endpoint = f"/historical/markets/{ticker}/candlesticks"
    else:
        endpoint = f"/series/{series}/markets/{ticker}/candlesticks"
    params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": int(period_min)}
    data, meta = fetcher.get_json(PROVIDER, base, endpoint, params)
    if "candlesticks" not in data:
        raise FetchError("malformed_payload", endpoint, "no candlesticks key")
    return data["candlesticks"], meta


def _dec(x):
    if x is None or x == "":
        return None
    return float(x)


def parse_candle(c):
    """Normalize one historical candle. Field names validated against archive payloads:
    end_period_ts, yes_bid{open,high,low,close}, yes_ask{...}, price{open,high,low,close,mean,previous},
    volume, open_interest — all decimal strings in dollars/contracts; price.close may be null."""
    yb = c.get("yes_bid") or {}
    ya = c.get("yes_ask") or {}
    pr = c.get("price") or {}
    return {
        "end_ts": int(c["end_period_ts"]),
        "bid": _dec(yb.get("close")),
        "ask": _dec(ya.get("close")),
        "trade_close": _dec(pr.get("close")),
        "volume": _dec(c.get("volume")),
        "open_interest": _dec(c.get("open_interest")),
    }


def is_archived(market, cutoff_data):
    st = parse_iso(cutoff_data.get("market_settled_ts"))
    ts = parse_iso(market.get("settlement_ts")) or parse_iso(market.get("close_time"))
    return ts is not None and st is not None and ts < st
