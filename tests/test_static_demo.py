import gzip
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs/demo/data"
CITY_CODES = {"chi", "mia", "aus", "lax", "phil"}
GOLDEN_EVENT_ID = "KXHIGHNY-26AUG13"
FORBIDDEN = {
    "winner",
    "winner_bucket",
    "outcome",
    "settlement",
    "settlement_value",
    "won",
    "pnl",
    "log_loss",
    "brier",
    "p_winner",
    "label_reasons",
}


def read_jsonl(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def family_of(row):
    return row.get("family") or row["model_id"].split(".", 1)[1].split("@", 1)[0]


def families_in(path):
    return {
        family_of(row)
        for row in read_jsonl(path)
        if row.get("mode") == "model" and row.get("probabilities") is not None
    }


def assert_outcome_free(value):
    if isinstance(value, dict):
        for key, child in value.items():
            assert key.lower() not in FORBIDDEN
            assert_outcome_free(child)
    elif isinstance(value, list):
        for child in value:
            assert_outcome_free(child)
    elif isinstance(value, str):
        assert value.lower() not in FORBIDDEN


def test_generator_reproduces_committed_data(tmp_path):
    output = tmp_path / "data"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_static_demo.py"), "--out", str(output)],
        cwd=ROOT,
        check=True,
    )
    for name in ("events.json", "outcomes.json"):
        assert (output / name).read_bytes() == (DATA_DIR / name).read_bytes()


def test_events_are_outcome_free_and_cover_all_city_families():
    payload = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    events = payload["events"]
    assert_outcome_free(payload)
    assert {event["city"] for event in events} >= CITY_CODES
    assert GOLDEN_EVENT_ID in {event["event_id"] for event in events}
    assert events[0]["event_id"] == GOLDEN_EVENT_ID
    assert [(event["city"], event["event_date"]) for event in events[1:]] == sorted(
        (event["city"], event["event_date"]) for event in events[1:]
    )

    golden = next(event for event in events if event["event_id"] == GOLDEN_EVENT_ID)
    assert golden["split"] == "golden (development)"
    golden_families = families_in(ROOT / "evidence/nyc-0800-v2/eval-audit/predictions.jsonl")
    city_families = {
        city: families_in(ROOT / f"evidence/v3/{city}-0800/eval-test/predictions.jsonl.gz")
        | {"market_recal_pooled"}
        for city in CITY_CODES
    }

    for event in events:
        if event["city"] == "nyc":
            expected = golden_families
        else:
            expected = city_families[event["city"]]
        assert set(event["probs"]) == expected
        assert all(len(vector) == len(event["buckets"]) for vector in event["probs"].values())


def test_outcomes_url_is_only_referenced_inside_reveal():
    html = (ROOT / "docs/demo/index.html").read_text(encoding="utf-8")
    assert html.count("outcomes.json") == 1
    reveal_start = html.index("async function reveal()")
    reveal_end = html.index("function currentCityEvents()", reveal_start)
    outcome_reference = html.index("outcomes.json")
    assert reveal_start < outcome_reference < reveal_end
    assert re.search(r"fetch\(['\"]data/outcomes\.json['\"]\)", html)


def test_outcomes_match_the_event_set_and_golden_fixture():
    payload = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    outcomes = json.loads((DATA_DIR / "outcomes.json").read_text(encoding="utf-8"))
    assert set(outcomes) == {event["event_id"] for event in payload["events"]}
    assert outcomes[GOLDEN_EVENT_ID] == "85-86"
