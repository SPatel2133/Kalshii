"""Synthetic fixtures (no network, no Kalshi payloads)."""
from datetime import datetime, timedelta, timezone

RULE = ("If the highest temperature recorded in Central Park, New York for {mon} {d}, {y} as reported by the "
        "National Weather Service's Climatological Report (Daily), is {cond}, then the market resolves to Yes.")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def markets(event_id="KXHIGHNY-26JUL15", day=(2026, 7, 15), edges=(81, 83, 85, 87), winner=1, value=82,
            open_shift_h=-18):
    y, mo, d = day
    mon = datetime(y, mo, d).strftime("%B")
    op = datetime(y, mo, d, 14, tzinfo=timezone.utc) - timedelta(days=1)
    cl = datetime(y, mo, d, 4, 59, tzinfo=timezone.utc) + timedelta(days=1)
    specs = [("T%d" % edges[0], f"less than {edges[0]}°", "less", None, edges[0])]
    for a in edges:
        specs.append(("B%.1f" % (a + 0.5), f"between {a}-{a + 1}°", "between", a, a + 1))
    specs.append(("T%d" % (edges[-1] + 1), f"greater than {edges[-1] + 1}°", "greater", edges[-1] + 1, None))
    out = []
    for i, (suf, cond, st, fl, cp) in enumerate(specs):
        out.append({"ticker": f"{event_id}-{suf}", "event_ticker": event_id, "strike_type": st, "floor_strike": fl,
                    "cap_strike": cp, "rules_primary": RULE.format(mon=mon, d=d, y=y, cond=cond),
                    "open_time": iso(op), "close_time": iso(cl), "created_time": iso(op - timedelta(hours=1)),
                    "status": "finalized", "result": "yes" if i == winner else "no",
                    "settlement_ts": iso(cl + timedelta(hours=8)), "expiration_value": str(value)})
    return out


def candle(ts, bid, ask, trade=None, vol=0):
    return {"end_period_ts": int(ts), "yes_bid": {"close": None if bid is None else f"{bid:.4f}"},
            "yes_ask": {"close": None if ask is None else f"{ask:.4f}"},
            "price": {"close": None if trade is None else f"{trade:.4f}"}, "volume": f"{vol:.2f}", "open_interest": "0"}
