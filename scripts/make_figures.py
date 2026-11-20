"""Create deterministic SVG summaries from committed v3 evidence."""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "v3"
OUTPUT = ROOT / "docs" / "figures"
CITY_CODES = ("chi", "mia", "aus", "lax", "phil")
CUTOFFS = ("0800", "1100", "1400")
LOGLOSS_FAMILIES = ("market_raw", "market_recal", "fixed_blend", "weather")
DISPLAY_LABELS = {
    "market_raw": "Market (raw)",
    "market_recal": "Market (recalibrated)",
    "fixed_blend": "Fixed blend",
    "weather": "Weather model",
    "adaptive_blend": "Adaptive blend",
    "favourite_yes": "Buy favourite",
    "market_recal_pooled": "Recalibrated market (pooled α)",
}

matplotlib.rcParams["svg.hashsalt"] = "kalshi"
matplotlib.rcParams["svg.fonttype"] = "none"
matplotlib.rcParams["font.family"] = "DejaVu Sans"


def read_summary(path):
    return json.loads(path.read_text(encoding="utf-8"))


def pooled_logloss(cutoff):
    totals = {family: 0.0 for family in LOGLOSS_FAMILIES}
    n_total = 0
    for city in CITY_CODES:
        summary = read_summary(EVIDENCE / f"{city}-{cutoff}" / "eval-test" / "summary.json")
        n = summary["common_cohort_n"]
        n_total += n
        for family in LOGLOSS_FAMILIES:
            totals[family] += n * summary["families"][family]["mean_log_loss"]
    return {family: value / n_total for family, value in totals.items()}


def save(fig, name):
    path = OUTPUT / name
    fig.tight_layout()
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def make_logloss_figure():
    values = {cutoff: pooled_logloss(cutoff) for cutoff in CUTOFFS}
    expected_0800 = {
        "market_raw": 1.0777,
        "market_recal": 1.0742,
        "fixed_blend": 1.0757,
        "weather": 1.5324,
    }
    for family, expected in expected_0800.items():
        actual = round(values["0800"][family], 4)
        if actual != expected:
            raise AssertionError(f"0800 {family} log loss {actual} != {expected}")

    x = range(len(CUTOFFS))
    labels = [f"{cutoff[:2]}:{cutoff[2:]}" for cutoff in CUTOFFS]
    fig, ax = plt.subplots(figsize=(7.2, 4.5))
    for family in LOGLOSS_FAMILIES:
        ax.plot(x, [values[cutoff][family] for cutoff in CUTOFFS], marker="o", label=DISPLAY_LABELS[family])
    ax.set_xticks(list(x), labels)
    ax.set_xlabel("Local cutoff")
    ax.set_ylabel("Mean log loss")
    ax.set_title("Pooled 5-city test log loss by cutoff (lower is better)")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.25)
    save(fig, "logloss_by_cutoff.svg")


def make_h1_figure():
    pooled_names = ("pooled", "pooled-1100", "pooled-1400")
    summaries = [read_summary(EVIDENCE / name / "summary.json") for name in pooled_names]
    means = [summary["h1"]["mean_delta"] for summary in summaries]
    intervals = [summary["h1"]["ci95_block"] for summary in summaries]
    expected = (-0.0063, -0.0146, 0.0016)
    actual = (round(means[0], 4), round(intervals[0][0], 4), round(intervals[0][1], 4))
    if actual != expected:
        raise AssertionError(f"0800 H1 estimate and interval {actual} != {expected}")

    lower = [mean - interval[0] for mean, interval in zip(means, intervals)]
    upper = [interval[1] - mean for mean, interval in zip(means, intervals)]
    fig, ax = plt.subplots(figsize=(7.2, 4.5))
    ax.errorbar(range(3), means, yerr=[lower, upper], fmt="o", capsize=5, linewidth=1.6)
    ax.axhline(0, color="black", linewidth=1)
    ax.axhline(-0.01, color="gray", linestyle="--", linewidth=1)
    ax.set_xticks(
        range(3),
        ["08:00\npre-registered", "11:00\nexploratory", "14:00\nexploratory"],
    )
    ax.set_ylabel("Mean log-loss delta")
    ax.set_title("Pooled H1 by cutoff (point estimate and 95% CI)")
    ax.grid(axis="y", alpha=0.25)
    save(fig, "h1_by_cutoff.svg")


def make_pnl_figure():
    summary = read_summary(EVIDENCE / "pooled" / "summary.json")
    pooled_pnl = summary["pooled_pnl"]
    strategies = sorted(pooled_pnl)
    means = [pooled_pnl[strategy]["fee_1x"]["mean_pnl_per_event"] for strategy in strategies]
    intervals = [pooled_pnl[strategy]["fee_1x"]["ci95_block"] for strategy in strategies]
    expected = (-0.0010, -0.0049, 0.0028)
    market_recal = strategies.index("market_recal_pooled")
    actual = (
        round(means[market_recal], 4),
        round(intervals[market_recal][0], 4),
        round(intervals[market_recal][1], 4),
    )
    if actual != expected:
        raise AssertionError(f"market_recal_pooled estimate and interval {actual} != {expected}")

    lower = [mean - interval[0] for mean, interval in zip(means, intervals)]
    upper = [interval[1] - mean for mean, interval in zip(means, intervals)]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.errorbar(means, range(len(strategies)), xerr=[lower, upper], fmt="o", capsize=4, linewidth=1.5)
    ax.axvline(0, color="black", linewidth=1)
    ax.set_yticks(range(len(strategies)), [DISPLAY_LABELS[strategy] for strategy in strategies])
    ax.set_xlabel("Mean P&L per event, $ per 1-contract unit, after fees")
    ax.set_title("Pooled 08:00 test P&L by strategy (fee 1×)")
    ax.grid(axis="x", alpha=0.25)
    save(fig, "pnl_0800.svg")


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    make_logloss_figure()
    make_h1_figure()
    make_pnl_figure()
    for name in ("logloss_by_cutoff.svg", "h1_by_cutoff.svg", "pnl_0800.svg"):
        print(OUTPUT / name)


if __name__ == "__main__":
    main()
