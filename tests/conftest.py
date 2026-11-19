import copy
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from forecast_lab import config as C, contracts, evaluation as E, replay
from tests.synth import candle, markets


def synth_event(d, rng):
    t_true = round(60 + 20 * __import__("math").sin((d.timetuple().tm_yday - 110) / 58.1) + rng.gauss(0, 3))
    nx = t_true + round(rng.gauss(0, 2))
    c = nx - 4 + (nx % 2)
    edges = tuple(range(c - 4, c + 4, 2))
    eid = "KXHIGHNY-" + d.strftime("%y%b%d").upper()
    ms = markets(eid, (d.year, d.month, d.day), edges=edges, winner=0, value=t_true)
    los = [None] + list(edges) + [edges[-1] + 2]
    win = 0
    for i, lo in enumerate(los):
        if lo is not None and t_true >= lo:
            win = i
    for i, m in enumerate(ms):
        m["result"] = "yes" if i == win else "no"
    cut = replay.cutoff_utc(d)
    rules, label = contracts.normalize_event(eid, ms, cut)
    cts = int(cut.timestamp())
    cmap = {}
    for i, b in enumerate(rules["buckets"]):
        mid = {0: .05, 1: .15, 2: .3, 3: .3, 4: .15, 5: .05}[i]
        cmap[b["ticker"]] = [candle(cts - 3600, max(mid - .02, 0), mid + .02, trade=mid, vol=10)]
    wrec = {"status": "available", "reason": None, "source_id": "synthetic", "lag_h": 7.5,
            "availability_basis": "synthetic", "selected": {"mos_daytime_max_f": float(nx)}}
    row = replay.build_snapshot(rules, cmap, wrec, cut, {"max_quote_age_h": 24.0, "history_window_h": 6.0})
    return row, label


@pytest.fixture(scope="session")
def cfg():
    c = copy.deepcopy(C.default_config())
    c["split"].update({"dev_start": "2025-01-01", "test_start": "2025-07-01", "test_end": "2025-08-31"})
    c["models"]["lambda_grid"] = [10.0, 1000.0]
    c["models"]["gate_l2_grid"] = [1.0]
    c["evaluation"]["bootstrap_reps"] = 200
    return c


@pytest.fixture(scope="session")
def synth_data():
    rng = random.Random(7)
    feats, labels = {}, {}
    d = date(2025, 1, 1)
    while d <= date(2025, 8, 31):
        r, l = synth_event(d, rng)
        feats[r["event_id"]], labels[r["event_id"]] = r, l
        d += timedelta(days=1)
    return feats, labels


@pytest.fixture(scope="session")
def synth_state(cfg, synth_data):
    feats, labels = synth_data
    st, _ = E.fit(cfg, feats, labels, log=lambda *a: None)
    return st
