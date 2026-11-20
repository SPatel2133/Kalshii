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
CITY_FAMILIES = {
    "market_raw",
    "market_recal",
    "market_recal_pooled",
    "weather",
    "fixed_blend",
    "adaptive_blend",
}
NYC_FAMILIES = CITY_FAMILIES - {"market_recal_pooled"}


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

    for event in events:
        if event["city"] == "nyc":
            expected = NYC_FAMILIES
        else:
            assert event["city"] in CITY_CODES
            expected = CITY_FAMILIES
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


def test_demo_uses_six_families_in_order_and_marks_golden_pool_unavailable():
    html = (ROOT / "docs/demo/index.html").read_text(encoding="utf-8")
    match = re.search(r"const FAMILY_ORDER = \[(.*?)\];", html, re.DOTALL)
    assert match
    assert re.findall(r'"([^"]+)"', match.group(1)) == [
        "market_raw",
        "market_recal",
        "market_recal_pooled",
        "weather",
        "fixed_blend",
        "adaptive_blend",
    ]
    assert "NYC is not in the 5-city pool" in html
    assert "adaptive_blend_raw" not in html
    assert "adaptive_corrected" not in html
    assert "climatology" not in html
    assert "coherent" not in html
    assert "market_context" not in html


def test_outcomes_match_the_event_set_and_golden_fixture():
    payload = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    outcomes = json.loads((DATA_DIR / "outcomes.json").read_text(encoding="utf-8"))
    fixture = json.loads((ROOT / "tests/fixtures/golden_v2_KXHIGHNY-26AUG13.json").read_text(encoding="utf-8"))
    assert set(outcomes) == {event["event_id"] for event in payload["events"]}
    assert all(set(outcome) == {"winner_bucket"} for outcome in outcomes.values())
    assert outcomes[GOLDEN_EVENT_ID]["winner_bucket"] == fixture["label"]["winner_bucket"] == "85-86"
