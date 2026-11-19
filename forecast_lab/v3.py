"""V3 catalogue-only split and station summaries."""
import math
import re
from collections import defaultdict

from . import contracts
from .download import load_catalogue

PREREGISTRATION_SHA = "074fcdea36fc0597b3384b7774d1e8203dad00ae"

STATION_WORDING = re.compile(
    r"\brecorded (?:in|at) (.+?) for [A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b", re.IGNORECASE
)


def station_wording(text):
    match = STATION_WORDING.search(text or "")
    return " ".join(match.group(1).split()) if match else "unparsed"


def catalogue_profile(cfg, offline=True):
    events, pages = load_catalogue(cfg, offline=offline)
    all_dates = []
    eligible_dates = set()
    wording_dates = defaultdict(set)
    nws_dates, twc_dates = [], []
    phrase = cfg["station_phrase"].casefold()

    for event_id, markets in events.items():
        event_date = contracts.event_date_from_ticker(event_id)
        if event_date is None:
            continue
        all_dates.append(event_date)
        texts = [m.get("rules_primary") or "" for m in markets]
        regimes = {contracts.rule_regime(text) or "MISSING" for text in texts}
        regime = next(iter(regimes)) if len(regimes) == 1 else "MIXED"
        if regime == "NWS":
            nws_dates.append(event_date)
            for wording in {station_wording(text) for text in texts}:
                wording_dates[wording].add(event_date)
            if texts and all(phrase in text.casefold() for text in texts):
                eligible_dates.add(event_date)
        elif regime == "TWC":
            twc_dates.append(event_date)

    dates = sorted(eligible_dates)
    n_dev = math.floor(0.65 * len(dates))
    split = {
        "n": len(dates),
        "n_dev": n_dev,
        "dev_start": dates[0].isoformat() if dates else None,
        "test_start": dates[n_dev].isoformat() if n_dev < len(dates) else None,
        "test_end": dates[-1].isoformat() if dates else None,
        "status": "eligible" if n_dev >= 60 else "drop",
    }
    wordings = []
    for wording, word_dates in sorted(wording_dates.items(), key=lambda item: min(item[1])):
        wordings.append({
            "rules_station_wording": wording,
            "event_count": len(word_dates),
            "first_event_date": min(word_dates).isoformat(),
            "last_event_date": max(word_dates).isoformat(),
        })
    return {
        "city": cfg["experiment_id"].removeprefix("v3-").removesuffix("-0800"),
        "series": cfg["series"],
        "station_phrase": cfg["station_phrase"],
        "catalogue": {
            "markets": sum(len(markets) for markets in events.values()),
            "events": len(events),
            "pages": len(pages),
            "earliest_event_date": min(all_dates).isoformat() if all_dates else None,
            "latest_event_date": max(all_dates).isoformat() if all_dates else None,
        },
        "switch_dates": {
            "last_nws_event_date": max(nws_dates).isoformat() if nws_dates else None,
            "first_twc_event_date": min(twc_dates).isoformat() if twc_dates else None,
        },
        "station_wordings": wordings,
        "split": split,
    }
