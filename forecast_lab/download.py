"""Resumable bulk candle download. Every request is cached by stable key, so rerunning resumes."""
import collections
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from . import contracts, kalshi
from .fetch import Fetcher, FetchError
from .util import atomic_write, parse_iso, utc_now_iso


def load_catalogue(cfg, offline=False):
    f = Fetcher.from_config(cfg, offline=offline)
    ms, pages = kalshi.fetch_catalogue(f, cfg["sources"]["kalshi_base"], cfg["series"],
                                       progress_path=f"{cfg['paths']['work_dir']}/catalogue_progress.json")
    ev = collections.defaultdict(list)
    for m in ms:
        ev[m["event_ticker"]].append(m)
    return dict(ev), pages


def candle_range(m):
    return int(parse_iso(m["open_time"]).timestamp()), int(parse_iso(m["close_time"]).timestamp())


def download_candles(cfg, events, start, end, workers=4, offline=False, log=print):
    """events: dict event_id -> markets. Download hourly candles for markets with event date in [start, end]."""
    jobs = []
    for e, mm in sorted(events.items()):
        d = contracts.event_date_from_ticker(e)
        if d and start <= d <= end:
            for m in mm:
                jobs.append(m)
    status = {"ok": 0, "failed": [], "started": utc_now_iso(), "n_jobs": len(jobs)}
    lock = threading.Lock()
    local = threading.local()

    def work(m):
        if not hasattr(local, "f"):
            local.f = Fetcher.from_config(cfg, offline=offline)
            local.f.min_interval_s = max(cfg["http"]["min_interval_s"], 0.3)
        s, t = candle_range(m)
        try:
            kalshi.fetch_candles(local.f, cfg["sources"]["kalshi_base"], cfg["series"], m["ticker"], s, t,
                                 cfg["quotes"]["candle_period_min"], archive=True)
            with lock:
                status["ok"] += 1
                if status["ok"] % 500 == 0:
                    log(f"candles ok={status['ok']}/{len(jobs)}")
        except FetchError as ex:
            with lock:
                status["failed"].append({"ticker": m["ticker"], "failure_class": ex.failure_class})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, jobs))
    status["finished"] = utc_now_iso()
    atomic_write(f"{cfg['paths']['work_dir']}/candle_download_{start}_{end}.json", json.dumps(status, indent=1))
    return status
