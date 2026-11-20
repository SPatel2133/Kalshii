import gzip
import hashlib
import json
from pathlib import Path

import pytest

from forecast_lab.util import read_bytes_maybe_gz


EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "v3"
if not EVIDENCE.is_dir():
    pytest.skip("v3 evidence is absent", allow_module_level=True)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def test_evaluation_prediction_seals_match_gzip_files():
    summaries = sorted(EVIDENCE.rglob("eval-*/summary.json"))
    assert summaries
    for summary_path in summaries:
        summary = json.loads(summary_path.read_text())
        predictions_path = summary_path.parent / "predictions.jsonl"
        assert (Path(str(predictions_path) + ".gz")).is_file()
        assert sha256(read_bytes_maybe_gz(predictions_path)) == \
            summary["predictions_sha256_sealed_before_scoring"]


def test_pooled_prediction_seals_match_gzip_files():
    summaries = [
        path for path in sorted(EVIDENCE.glob("pooled*/summary.json"))
        if "pooled_predictions_sha256" in json.loads(path.read_text())
    ]
    assert summaries
    for summary_path in summaries:
        summary = json.loads(summary_path.read_text())
        predictions_path = summary_path.parent / "predictions.jsonl"
        assert Path(str(predictions_path) + ".gz").is_file()
        assert sha256(read_bytes_maybe_gz(predictions_path)) == \
            summary["pooled_predictions_sha256"]


def test_every_v3_evidence_checksum_matches():
    checksum_path = EVIDENCE / "CHECKSUMS.sha256"
    assert checksum_path.is_file()
    lines = checksum_path.read_text().splitlines()
    assert lines
    for line in lines:
        uncompressed_sha, compressed_sha, relative_path = line.split(" ", 2)
        compressed_path = EVIDENCE / relative_path
        payload = compressed_path.read_bytes()
        assert sha256(payload) == compressed_sha
        assert sha256(gzip.decompress(payload)) == uncompressed_sha
