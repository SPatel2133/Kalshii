"""Local replay screen (standard-library HTTP server + plain HTML/JS).

Routes:
  GET /                         page
  GET /api/events               event list with split labels (no outcomes)
  GET /api/replay?event=&as_of=&drop_last_quote=&weather_delay_h=   outcome-free (allowlisted) replay
  GET /api/threshold?event=&lo=&hi=   threshold query summing the same model distribution
  GET /api/reveal?event=        explicit outcome reveal (separate request)
  GET /api/showcase             one held-out favourable case and one failure from the frozen test evaluation
"""
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import contracts
from .service import ReplayService, load_state, split_of
from .util import read_jsonl

PAGE = Path(__file__).with_name("demo.html")


def make_handler(svc, cfg):
    def showcase():
        p = Path(cfg["paths"]["evidence_dir"]) / "eval-test" / "evaluation_rows.jsonl"
        if not p.exists():
            return {"available": False}
        rows = read_jsonl(p)
        by = {}
        for r in rows:
            by.setdefault(r["event_id"], {})[r["family"]] = r
        diffs = []
        for e, fr in by.items():
            a, b = fr.get(svc.family), fr.get("market_raw")
            if a and b and "log_loss" in a and "log_loss" in b:
                diffs.append((a["log_loss"] - b["log_loss"], e))
        if not diffs:
            return {"available": False}
        diffs.sort()
        return {"available": True, "comparator": "market_raw", "favourable": diffs[0][1], "failure": diffs[-1][1],
                "note": "held-out test events; selected after scoring the frozen predictions"}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj, default=float).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/events":
                    evs = []
                    for e in sorted(svc.src.events):
                        d = contracts.event_date_from_ticker(e)
                        if d and cfg["split"]["dev_start"] <= d.isoformat() <= cfg["split"]["audit_end"]:
                            evs.append({"event_id": e, "event_date": d.isoformat(), "split": split_of(cfg, d)})
                    return self._send(200, {"events": evs, "model_id": f"{svc.state['experiment_id']}.{svc.family}@{svc.state['fit_boundary']}",
                                            "golden": cfg["split"]["golden_event"]})
                if u.path == "/api/replay":
                    r = svc.replay(q["event"], q.get("as_of", "08:00"), drop_last_quote=q.get("drop_last_quote") == "1",
                                   weather_delay_h=float(q.get("weather_delay_h", 0) or 0))
                    return self._send(200, r)
                if u.path == "/api/threshold":
                    lo = int(q["lo"]) if q.get("lo") not in (None, "") else None
                    hi = int(q["hi"]) if q.get("hi") not in (None, "") else None
                    return self._send(200, svc.threshold(q["event"], lo, hi, q.get("as_of", "08:00")))
                if u.path == "/api/reveal":
                    return self._send(200, svc.reveal(q["event"]))
                if u.path == "/api/showcase":
                    return self._send(200, showcase())
                return self._send(404, {"error": "not found"})
            except Exception as ex:  # surface errors to the page rather than hiding them
                return self._send(400, {"error": f"{type(ex).__name__}: {ex}"})

    return H


def serve(cfg, model_id, port=8765):
    st, fam = load_state(cfg, model_id)
    svc = ReplayService(cfg, st, fam)
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc, cfg))
    print(f"demo on http://localhost:{port}/ model={model_id}")
    srv.serve_forever()
