"""Label-blind decisions and after-cost paper settlement."""
import math
from datetime import date
from decimal import Decimal, ROUND_CEILING

from . import models as M
from .evaluation import block_bootstrap

STRATEGIES = ("market_raw", "market_recal", "fixed_blend", "adaptive_blend", "weather")
ALL_STRATEGIES = STRATEGIES + ("favourite_yes",)
COMMON_FAMILIES = ("market_raw", "weather", "coherent", "fixed_blend", "adaptive_blend")
DEFAULT_BACKTEST = {
    "fee_rate": 0.07,
    "fee_multiplier": 1.0,
    "margins": [0.0, 0.02, 0.05, 0.10],
    "fee_sensitivity": [0.0, 1.0, 2.0],
    "slippage_cents": 1,
    "contracts": 1,
}


class Decision(list):
    def __init__(self, trades=(), flags=()):
        super().__init__(trades)
        self.flags = list(flags)


def fee_cents(price_cents, contracts=1, rate=0.07, mult=1.0):
    if isinstance(price_cents, bool) or not isinstance(price_cents, int) or not 0 <= price_cents <= 100:
        raise ValueError("price_cents must be an integer from 0 through 100")
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 0:
        raise ValueError("contracts must be a nonnegative integer")
    amount = (Decimal(str(rate)) * Decimal(str(mult)) * contracts * price_cents *
              (100 - price_cents) / Decimal(100))
    return int((amount - Decimal("0.000000001")).to_integral_value(rounding=ROUND_CEILING))


def price_to_cents(price):
    if price is None:
        raise ValueError("missing quote price")
    cents = float(price) * 100
    rounded = round(cents)
    if not math.isfinite(cents) or abs(cents - rounded) > 1e-6:
        raise ValueError(f"quote {price!r} is not on a one-cent tick")
    return int(rounded)


def quote_cents(buckets):
    bids, asks = [], []
    for bucket in buckets:
        bids.append(price_to_cents(bucket.get("bid")))
        asks.append(price_to_cents(bucket.get("ask")))
    return bids, asks


def decide(probs, bids_c, asks_c, margin, fee_mult, rate=0.07, contracts=1):
    if not (len(probs) == len(bids_c) == len(asks_c)):
        raise ValueError("probabilities and quote arrays must have the same length")
    trades, flags = [], []
    for i, (prob, bid, ask) in enumerate(zip(probs, bids_c, asks_c)):
        if (not 0 <= bid <= 100 or not 0 <= ask <= 100 or
                not math.isfinite(float(prob)) or not 0 <= float(prob) <= 1):
            raise ValueError("invalid probability or quote")
        yes_edge = float(prob) - ask / 100 - fee_cents(ask, contracts, rate, fee_mult) / 100
        no_price = 100 - bid
        no_edge = bid / 100 - float(prob) - fee_cents(no_price, contracts, rate, fee_mult) / 100
        yes = ask < 100 and yes_edge >= margin - 1e-12
        no = bid > 0 and no_edge >= margin - 1e-12
        if yes and no:
            flags.append({"bucket_index": i, "flag": "both_sides_qualify"})
        elif yes:
            trades.append({"bucket_index": i, "side": "YES", "quoted_price_c": ask,
                           "price_c": ask, "edge": yes_edge})
        elif no:
            trades.append({"bucket_index": i, "side": "NO", "quoted_price_c": no_price,
                           "price_c": no_price, "edge": no_edge})
    return Decision(trades, flags)


def settle(trades, winner_bucket, slippage_cents=0, fee_mult=1.0, rate=0.07, contracts=1):
    if slippage_cents < 0:
        raise ValueError("slippage_cents must be nonnegative")
    settled = []
    for trade in trades:
        if trade.get("side") not in ("YES", "NO"):
            continue
        price = min(99, trade["quoted_price_c"] + slippage_cents)
        won = (trade.get("bucket_id", trade.get("bucket_index")) == winner_bucket)
        if trade["side"] == "NO":
            won = not won
        fee = fee_cents(price, contracts, rate, fee_mult)
        payout = float(contracts if won else 0)
        cost = contracts * price / 100 + fee / 100
        settled.append({
            **trade,
            "price_c": price,
            "fee_c": fee,
            "payout": payout,
            "pnl": payout - cost,
            "contracts": contracts,
            "won": bool(won),
        })
    return settled


def favourite_yes(buckets):
    if not buckets or any(b.get("bid") is None or b.get("ask") is None for b in buckets):
        return Decision(flags=["incomplete_board"])
    bids, asks = quote_cents(buckets)
    mids = [float(b.get("mid") if b.get("mid") is not None else (b["bid"] + b["ask"]) / 2)
            for b in buckets]
    index = max(range(len(mids)), key=lambda i: (mids[i], -i))
    if asks[index] >= 100:
        return Decision()
    return Decision([{"bucket_index": index, "side": "YES", "quoted_price_c": asks[index],
                      "price_c": asks[index], "edge": None}])


def _common_events(predictions, features, start, end):
    by_event = {}
    for prediction in predictions:
        family = prediction.get("family")
        if family is None and prediction.get("model_id"):
            family = prediction["model_id"].split(".", 1)[1].split("@", 1)[0]
        if family is not None:
            by_event.setdefault(prediction["event_id"], {})[family] = prediction
    common = []
    required = COMMON_FAMILIES
    for event_id, families in by_event.items():
        if event_id not in features:
            continue
        event_date = date.fromisoformat(features[event_id]["event_date"])
        if start <= event_date <= end and all(
                families.get(f, {}).get("mode") == "model" and families[f].get("probabilities") is not None
                for f in required):
            common.append(event_id)
    return sorted(common, key=lambda e: (features[e]["event_date"], e)), by_event


def _metrics(event_rows, traded_rows, dropped, block_days, sensitivity, reps, seed, start, end):
    event_rows = sorted(event_rows, key=lambda r: (r["date"], r["event_id"]))
    pnl_values = [r["pnl"] for r in event_rows]
    costs = sum(t["price_c"] * t["contracts"] / 100 + t["fee_c"] / 100 for t in traded_rows)
    pnl = sum(t["pnl"] for t in traded_rows)
    cum = peak = max_drawdown = 0.0
    for row in event_rows:
        cum += row["pnl"]
        peak = max(peak, cum)
        max_drawdown = max(max_drawdown, peak - cum)
    dates = [date.fromisoformat(r["date"]) for r in event_rows]

    def interval(days):
        if not event_rows:
            return [None, None]
        return list(block_bootstrap(dates, pnl_values, days, reps, seed, start, end))

    win_count = sum(t["won"] for t in traded_rows)
    return {
        "n_events": len(event_rows),
        "n_events_dropped_liquidity": dropped,
        "trades": len(traded_rows),
        "contracts": sum(t["contracts"] for t in traded_rows),
        "cost": costs,
        "pnl": pnl,
        "roi": pnl / costs if costs else None,
        "mean_pnl_per_event": pnl / len(event_rows) if event_rows else None,
        "hit_rate": win_count / len(traded_rows) if traded_rows else None,
        "max_drawdown": max_drawdown,
        "ci95_block": interval(block_days),
        "block_days": block_days,
        "block_sensitivity": {str(days): interval(days) for days in sensitivity},
    }


def run_backtest(cfg, predictions, features, labels, start, end, margins, split, include_event_pnl=False):
    bt = {**DEFAULT_BACKTEST, **cfg.get("backtest", {})}
    cohort, by_event = _common_events(predictions, features, start, end)
    cohort = [event_id for event_id in cohort
              if labels.get(event_id) and labels[event_id].get("winner_bucket") is not None
              and not labels[event_id].get("reasons")]
    if not cohort:
        raise ValueError("backtest requires a nonempty five-family common cohort")
    strategies = list(STRATEGIES)
    if any("market_recal_pooled" in families for families in by_event.values()):
        strategies.append("market_recal_pooled")
    strategies.append("favourite_yes")
    scenarios = [(f"fee_{mult:g}x", float(mult), 0, False) for mult in bt["fee_sensitivity"]]
    slippage = int(bt["slippage_cents"])
    scenarios.append((f"fee_1x_slippage_{slippage}c", 1.0, slippage, False))
    scenarios.append(("fee_1x_liquidity_cut", 1.0, 0, True))
    all_trades, metrics = [], {}

    for strategy in strategies:
        metrics[strategy] = {}
        for scenario, fee_mult, slip, liquidity_cut in scenarios:
            event_rows, scenario_trades, dropped, decision_flags = [], [], 0, 0
            for event_id in cohort:
                feature = features[event_id]
                buckets = feature["buckets"]
                bids, asks = quote_cents(buckets)
                if strategy == "favourite_yes":
                    decision = favourite_yes(buckets)
                    margin = None
                else:
                    family = by_event[event_id].get(strategy)
                    if family is None or family.get("mode") != "model":
                        continue
                    decision = decide(family["probabilities"], bids, asks,
                                      margins[strategy], fee_mult, bt["fee_rate"], bt["contracts"])
                    margin = margins[strategy]
                decision_flags += len(decision.flags)
                decision_trades = []
                for trade in decision:
                    trade = {**trade, "bucket_id": buckets[trade["bucket_index"]]["bucket_id"]}
                    decision_trades.append(trade)
                label = labels.get(event_id)
                winner = label.get("winner_bucket") if label else None
                if winner is None or label.get("reasons"):
                    continue
                settled = settle(decision_trades, winner, slip, fee_mult, bt["fee_rate"], bt["contracts"])
                if liquidity_cut and any(
                        (buckets[t["bucket_index"]].get("volume_pre_cutoff") or 0) == 0 for t in settled):
                    dropped += 1
                    continue
                event_date = feature["event_date"]
                event_pnl = sum(t["pnl"] for t in settled)
                event_rows.append({"event_id": event_id, "date": event_date, "pnl": event_pnl})
                for trade in settled:
                    record = {
                        "event_id": event_id,
                        "date": event_date,
                        "strategy": strategy,
                        "scenario": scenario,
                        "bucket_id": trade["bucket_id"],
                        "side": trade["side"],
                        "price_c": trade["price_c"],
                        "fee_c": trade["fee_c"],
                        "payout": trade["payout"],
                        "pnl": trade["pnl"],
                    }
                    scenario_trades.append({**record, "contracts": trade["contracts"], "won": trade["won"]})
            all_trades.extend(scenario_trades)
            scenario_metrics = _metrics(
                event_rows, scenario_trades, dropped, bt.get("block_days", 7),
                bt.get("block_sensitivity", [3, 14]), bt.get("bootstrap_reps", 2000),
                bt.get("seed", 20260813), start, end)
            scenario_metrics["decision_flags"] = decision_flags
            if include_event_pnl:
                scenario_metrics["event_pnl"] = event_rows
            metrics[strategy][scenario] = scenario_metrics
    decision_by_strategy = {}
    for strategy, scenarios_for_strategy in metrics.items():
        base_metrics = scenarios_for_strategy.get("fee_1x", {})
        slip_metrics = scenarios_for_strategy.get(f"fee_1x_slippage_{slippage}c", {})
        lower = base_metrics.get("ci95_block", [None])[0]
        decision = "profitable" if lower is not None and lower > 0 and slip_metrics.get("pnl", 0) > 0 \
            else "no supported profit"
        scenarios_for_strategy["decision"] = decision
        decision_by_strategy[strategy] = decision
    return {
        "experiment_id": cfg["experiment_id"],
        "split": split,
        "window": [start.isoformat(), end.isoformat()],
        "common_cohort_n": len(cohort),
        "common_cohort_ids": cohort,
        "margins": margins,
        "scenarios": [s[0] for s in scenarios],
        "strategies": metrics,
        "decision_by_strategy": decision_by_strategy,
    }, all_trades


def select_margin(oof_rows, features, labels, test_start, margins, fee_rate=0.07, contracts=1):
    boundary = date.fromisoformat(test_start) if isinstance(test_start, str) else test_start
    for row in oof_rows:
        if date.fromisoformat(row["event_date"]) >= boundary:
            raise ValueError("OOF margin selection rows must all precede test_start")
    grouped = {}
    for row in oof_rows:
        grouped.setdefault(row["event_id"], {})[row["family"]] = row
    common = [event_id for event_id, families in grouped.items()
              if all(families.get(f, {}).get("mode") == "model" and
                     families[f].get("probabilities") is not None for f in COMMON_FAMILIES)]
    if not common:
        raise ValueError("margin selection requires a nonempty five-family OOF cohort")
    result, scores = {}, {}
    margin_families = list(STRATEGIES)
    if any("market_recal_pooled" in families for families in grouped.values()):
        margin_families.append("market_recal_pooled")
    for family in margin_families:
        family_scores = {}
        for margin in margins:
            total = 0.0
            for event_id in common:
                if family not in grouped[event_id] or grouped[event_id][family].get("mode") != "model":
                    continue
                feature, label = features[event_id], labels.get(event_id)
                if not label or not label.get("winner_bucket") or label.get("reasons"):
                    continue
                bids, asks = quote_cents(feature["buckets"])
                decision = decide(grouped[event_id][family]["probabilities"], bids, asks,
                                  margin, 1.0, fee_rate, contracts)
                trades = [{**trade, "bucket_id": feature["buckets"][trade["bucket_index"]]["bucket_id"]}
                          for trade in decision]
                total += sum(t["pnl"] for t in settle(
                    trades, label["winner_bucket"], fee_mult=1.0, rate=fee_rate, contracts=contracts))
            family_scores[str(margin)] = total
        if not family_scores:
            raise ValueError(f"no OOF margin scores for {family}")
        selected = max(margins, key=lambda margin: (family_scores[str(margin)], margin))
        result[family] = selected
        scores[family] = family_scores
    return {"test_start": boundary.isoformat(), "fee_multiplier": 1.0,
            "selected": result, "dev_oof_pnl": scores, "common_oof_events": len(common)}
