"""Shared replay service used by the CLI replay command and the demo server.

The pre-reveal response is built from an allowlist; outcome fields only come from reveal()."""
import json
from datetime import date
from pathlib import Path

from . import contracts, dataset, evaluation as E, models as M
from . import reasons as R
from .util import read_jsonl

PRE_REVEAL_ALLOWED = {"event_id", "event_date", "series", "cutoff_utc", "cutoff_local", "as_of_local", "exploratory_horizon",
                      "scenario", "what_if", "rules", "buckets", "weather", "model_id", "prediction", "sources",
                      "replay_fingerprint", "provenance", "split", "flags", "market_status"}
FORBIDDEN_KEYS = {"result", "winner", "winner_bucket", "winner_ticker", "settled_value_f", "settlement_value_dollars",
                  "expiration_value", "expiration_values", "log_loss", "brier", "p_winner", "settlement_ts",
                  "last_price_dollars", "previous_price_dollars", "yes_bid_dollars", "yes_ask_dollars"}


def models_dir(cfg):
    return Path(cfg["paths"]["work_dir"]) / "models"


def load_state(cfg, mid):
    exp, fam, boundary = E.parse_model_id(mid)
    p = models_dir(cfg) / f"{exp}@{boundary}.json"
    if not p.exists():
        raise FileNotFoundError(f"model state {p} not found; run `python -m forecast_lab fit`")
    st = json.loads(p.read_text())
    return st, fam


def split_of(cfg, d):
    sp = cfg["split"]
    if date.fromisoformat(sp["test_start"]) <= d <= date.fromisoformat(sp["test_end"]):
        return "test"
    if date.fromisoformat(sp["audit_start"]) <= d <= date.fromisoformat(sp["audit_end"]):
        return "audit/golden (development, never fit)"
    if d < date.fromisoformat(sp["test_start"]):
        return "development"
    return "out of experiment window"


class ReplayService:
    def __init__(self, cfg, state, family, src=None):
        self.cfg, self.state, self.family = cfg, state, family
        self.src = src or dataset.Sources(cfg, offline=True)
        if date.fromisoformat(state["fit_boundary"]) > date.fromisoformat(cfg["split"]["test_start"]):
            raise ValueError("model fit boundary after test start")

    def replay(self, event_id, as_of="08:00", drop_last_quote=False, weather_delay_h=0.0):
        d = contracts.event_date_from_ticker(event_id)
        if d is None:
            raise ValueError(f"bad event id {event_id}")
        bd = date.fromisoformat(self.state["fit_boundary"])
        what_if = bool(drop_last_quote or weather_delay_h)
        scen = "what_if:" + ",".join(x for x in [("drop_last_quote" if drop_last_quote else ""),
                                                    (f"weather_delay_{weather_delay_h}h" if weather_delay_h else "")] if x) if what_if else None
        rules, _label, wrec, row, prov = dataset.build_event(self.src, event_id, local_hhmm=as_of, scenario=scen,
                                                             drop_last_quote=drop_last_quote, weather_delay_h=weather_delay_h)
        del _label  # never used before reveal
        pa = E.predict_all(self.cfg, self.state, row)
        pred = E.prediction_row(self.state, self.family, row, pa[self.family])
        sources = {}
        for f in ["market_raw", "coherent", "weather", "fixed_blend", "adaptive_blend"]:
            p = pa[f]
            sources[f] = {"mode": p["mode"], "reason": p.get("reason"),
                          "probabilities": [float(x) for x in p["buckets"]] if "buckets" in p else None}
        coh = pa.get("coherent", {})
        out = {
            "event_id": event_id, "event_date": row["event_date"], "series": self.cfg["series"],
            "cutoff_utc": row["cutoff_utc"], "cutoff_local": self.cfg["cutoff_local"], "as_of_local": as_of,
            "exploratory_horizon": row["exploratory_horizon"], "scenario": row["scenario"], "what_if": what_if,
            "split": split_of(self.cfg, d) + ("" if d >= bd else "; model fit includes this date's period only if settled before boundary"),
            "rules": {"regime": rules.get("regime"), "station": rules.get("station"), "day_convention": rules.get("day_convention"),
                      "rules_primary_example": rules.get("rules_primary_example"), "reasons": rules.get("reasons"),
                      "partition_check": rules.get("partition_check")},
            "buckets": [{k: b.get(k) for k in ("bucket_id", "ticker", "lo", "hi", "bid", "ask", "mid", "spread", "quote_obs_utc",
                                                "quote_age_h", "last_traded_candle_utc", "trade_age_h", "last_trade_close",
                                                "disposition")} for b in row["buckets"]],
            "weather": row["weather"], "market_status": row["market_status"], "flags": row["flags"],
            "model_id": pred["model_id"], "prediction": pred, "sources": sources,
            "replay_fingerprint": row["fingerprint"],
            "provenance": {"state_sha256": self.state["state_sha256"], "fit_boundary": self.state["fit_boundary"],
                           "candles": prov["candles"], "weather_cache": prov["weather"],
                           "fetch_failures": prov["fetch_failures"],
                           "coherent_diagnostics": coh.get("diagnostics"), "quote_weights": coh.get("quote_weights"),
                           "gate": pa["adaptive_blend"].get("gate"), "gate_inputs": pa["adaptive_blend"].get("gate_inputs")},
        }
        extra = set(out) - PRE_REVEAL_ALLOWED
        if extra:
            raise AssertionError(f"non-allowlisted keys in pre-reveal response: {sorted(extra)}")
        assert_outcome_free(out)
        return out

    def reveal(self, event_id):
        d = contracts.event_date_from_ticker(event_id)
        rules, label, wrec, row, prov = dataset.build_event(self.src, event_id)
        res = {"event_id": event_id, "label": label}
        if label and E.label_ok(label):
            pa = E.predict_all(self.cfg, self.state, row)
            wi = E.winner_index(row, label)
            res["evaluation"] = {}
            for f in E.PRIMARY_FAMILIES:
                p = pa[f]
                res["evaluation"][f] = {"mode": p["mode"], **(E.score(p["buckets"], wi, self.cfg["models"]["prob_floor"]) if "buckets" in p else {})}
        # post-cutoff market path (future data) belongs only here
        return res

    def threshold(self, event_id, lo=None, hi=None, as_of="08:00"):
        r = self.replay(event_id, as_of)
        pred = r["prediction"]
        if "support_masses" not in pred:
            return {"available": False, "reason": pred.get("reason")}
        sup = M.Support(*self.state["upstream"]["support"])
        import numpy as np
        return {"available": True, "lo": lo, "hi": hi, "probability": sup.query(np.array(pred["support_masses"]), lo, hi),
                "model_id": r["model_id"], "replay_fingerprint": r["replay_fingerprint"]}


def assert_outcome_free(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                raise AssertionError(f"outcome-bearing key in pre-reveal response: {k}")
            assert_outcome_free(v)
    elif isinstance(obj, list):
        for v in obj:
            assert_outcome_free(v)


def top_level_allowlisted(resp):
    return set(resp) <= PRE_REVEAL_ALLOWED
