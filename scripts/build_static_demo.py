"""Build deterministic, outcome-separated static replay data from committed evidence."""

import argparse
import gzip
import hashlib
import json
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CITY_CODES = ("chi", "mia", "aus", "lax", "phil")
CITY_NAMES = {
    "Austin Bergstrom": "Austin",
    "Chicago Midway": "Chicago",
    "Los Angeles Airport": "Los Angeles",
    "Miami International Airport": "Miami",
    "Philadelphia International Airport": "Philadelphia",
    "Central Park": "New York City",
}
GOLDEN_EVENT_ID = "KXHIGHNY-26AUG13"
GOLDEN_FIXTURE = "tests/fixtures/golden_v2_KXHIGHNY-26AUG13.json"
NYC_PREDICTIONS = "evidence/nyc-0800-v2/eval-audit/predictions.jsonl"
POOLED_FAMILY = "market_recal_pooled"
DISPLAY_FAMILIES = (
    "market_raw",
    "market_recal",
    POOLED_FAMILY,
    "weather",
    "fixed_blend",
    "adaptive_blend",
)
CITY_FAMILIES = set(DISPLAY_FAMILIES) - {POOLED_FAMILY}


def read_jsonl(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def family_of(row):
    if row.get("family"):
        return row["family"]
    return row["model_id"].split(".", 1)[1].split("@", 1)[0]


def bucket_label(bucket_id):
    if bucket_id.startswith("le"):
        return f"≤ {bucket_id[2:]}"
    if bucket_id.startswith("ge"):
        return f"≥ {bucket_id[2:]}"
    return bucket_id.replace("-", "–")


def read_config(path):
    with path.open("rb") as stream:
        return tomllib.load(stream)


def shared_probability_floor():
    floors = {
        read_config(ROOT / f"configs/v3-{city}-0800.toml")["models"]["prob_floor"]
        for city in (*CITY_CODES, "nyc")
    }
    if len(floors) != 1:
        raise ValueError("static demo city configs must share a probability floor")
    return floors.pop()


def source_manifest(paths):
    manifest = []
    for relative_path in sorted(set(paths)):
        payload = (ROOT / relative_path).read_bytes()
        manifest.append({
            "path": relative_path,
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    return manifest


def rounded_probabilities(probabilities):
    return [round(float(probability), 6) for probability in probabilities]


def city_events(city, config, input_paths, source_paths, pooled_rows):
    prediction_path = ROOT / f"evidence/v3/{city}-0800/eval-test/predictions.jsonl.gz"
    evaluation_path = ROOT / f"evidence/v3/{city}-0800/eval-test/evaluation_rows.jsonl.gz"
    source_paths.extend((input_paths["config"], input_paths["predictions"], input_paths["evaluation_rows"]))

    predictions = defaultdict(dict)
    metadata = {}
    for row in read_jsonl(prediction_path):
        if row.get("mode") != "model":
            continue
        family = family_of(row)
        if family not in CITY_FAMILIES:
            continue
        event_id = row["event_id"]
        bucket_ids = row["bucket_ids"]
        event_metadata = {
            "bucket_ids": bucket_ids,
            "cutoff_utc": row["cutoff_utc"],
        }
        if event_id in metadata and metadata[event_id] != event_metadata:
            raise ValueError(f"inconsistent prediction metadata for {event_id}")
        metadata[event_id] = event_metadata
        if family in predictions[event_id]:
            raise ValueError(f"duplicate {family} prediction for {event_id}")
        predictions[event_id][family] = row["probabilities"]

    outcomes = {}
    event_dates = {}
    for row in read_jsonl(evaluation_path):
        if row.get("mode") != "model":
            continue
        event_id = row["event_id"]
        winner = row.get("winner_bucket")
        if winner is None:
            continue
        if event_id in outcomes and outcomes[event_id] != winner:
            raise ValueError(f"inconsistent winner bucket for {event_id}")
        outcomes[event_id] = winner
        event_dates[event_id] = row["event_date"]

    events = []
    city_name = CITY_NAMES[config["station_phrase"]]
    for event_id, family_predictions in predictions.items():
        if set(family_predictions) != CITY_FAMILIES:
            continue
        if event_id not in outcomes or event_id not in pooled_rows:
            continue
        event_metadata = metadata[event_id]
        pooled_prediction = pooled_rows[event_id]
        if pooled_prediction["bucket_ids"] != event_metadata["bucket_ids"]:
            raise ValueError(f"pooled bucket ids do not match {event_id}")
        probabilities = {
            family: rounded_probabilities(values)
            for family, values in family_predictions.items()
        }
        probabilities[POOLED_FAMILY] = rounded_probabilities(pooled_prediction["probabilities"])
        events.append({
            "event_id": event_id,
            "city": city,
            "city_name": city_name,
            "event_date": event_dates[event_id],
            "cutoff_local": config["cutoff_local"],
            "timezone": config["timezone"],
            "cutoff_utc": event_metadata["cutoff_utc"],
            "split": "test",
            "buckets": [
                {"bucket_id": bucket_id, "label": bucket_label(bucket_id)}
                for bucket_id in event_metadata["bucket_ids"]
            ],
            "probs": probabilities,
        })
    return events, {
        event["event_id"]: {"winner_bucket": outcomes[event["event_id"]]}
        for event in events
    }


def golden_event(source_paths):
    fixture_path = ROOT / GOLDEN_FIXTURE
    predictions_path = ROOT / NYC_PREDICTIONS
    config_path = ROOT / "configs/v3-nyc-0800.toml"
    source_paths.extend((GOLDEN_FIXTURE, NYC_PREDICTIONS, "configs/v3-nyc-0800.toml"))
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    config = read_config(config_path)
    bucket_ids = [bucket["bucket_id"] for bucket in fixture["buckets"]]
    probabilities = {}
    for row in read_jsonl(predictions_path):
        if row.get("event_id") != GOLDEN_EVENT_ID or row.get("mode") != "model":
            continue
        family = family_of(row)
        if family not in CITY_FAMILIES:
            continue
        if row["bucket_ids"] != bucket_ids:
            raise ValueError(f"NYC golden bucket ids do not match {family}")
        probabilities[family] = rounded_probabilities(row["probabilities"])
    if set(probabilities) != CITY_FAMILIES:
        raise ValueError("NYC golden predictions are incomplete")
    if probabilities.get("adaptive_blend") != rounded_probabilities(fixture["probabilities"]):
        raise ValueError("NYC golden adaptive blend does not match its fixture")
    probabilities["adaptive_blend"] = rounded_probabilities(fixture["probabilities"])
    event = {
        "event_id": GOLDEN_EVENT_ID,
        "city": "nyc",
        "city_name": CITY_NAMES[config["station_phrase"]],
        "event_date": "2026-08-13",
        "cutoff_local": config["cutoff_local"],
        "timezone": config["timezone"],
        "cutoff_utc": fixture["cutoff_utc"],
        "split": "golden (development)",
        "buckets": [
            {"bucket_id": bucket_id, "label": bucket_label(bucket_id)}
            for bucket_id in bucket_ids
        ],
        "probs": probabilities,
    }
    return event, fixture["label"]["winner_bucket"]


def build(output_dir):
    source_paths = []
    prob_floor = shared_probability_floor()
    pooled_path = ROOT / "evidence/v3/pooled/predictions.jsonl.gz"
    source_paths.append("evidence/v3/pooled/predictions.jsonl.gz")
    pooled_rows = {}
    for row in read_jsonl(pooled_path):
        if row.get("mode") == "model" and row.get("family") == POOLED_FAMILY:
            pooled_rows[row["event_id"]] = row

    all_events = []
    outcomes = {}
    for city in CITY_CODES:
        config_relative = f"configs/v3-{city}-0800.toml"
        config = read_config(ROOT / config_relative)
        input_paths = {
            "config": config_relative,
            "predictions": f"evidence/v3/{city}-0800/eval-test/predictions.jsonl.gz",
            "evaluation_rows": f"evidence/v3/{city}-0800/eval-test/evaluation_rows.jsonl.gz",
        }
        events, city_outcomes = city_events(city, config, input_paths, source_paths, pooled_rows)
        all_events.extend(events)
        outcomes.update(city_outcomes)

    golden, winner = golden_event(source_paths)
    all_events = [golden, *sorted(all_events, key=lambda event: (event["city"], event["event_date"]))]
    outcomes[golden["event_id"]] = {"winner_bucket": winner}
    payload = {
        "prob_floor": prob_floor,
        "source": source_manifest(source_paths),
        "events": all_events,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "events.json").write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (output_dir / "outcomes.json").write_text(
        json.dumps(outcomes, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return payload, outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, help="output directory (defaults to docs/demo/data)")
    args = parser.parse_args()
    output_dir = args.out or ROOT / "docs/demo/data"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    payload, outcomes = build(output_dir)
    print(f"wrote {len(payload['events'])} events and {len(outcomes)} outcomes to {output_dir}")


if __name__ == "__main__":
    main()
