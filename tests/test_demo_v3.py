import gzip
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from forecast_lab.demo import make_handler


class DemoService:
    family = "adaptive_blend"


def test_showcase_reads_gzipped_evaluation_rows(tmp_path):
    evaluation_dir = tmp_path / "eval-test"
    evaluation_dir.mkdir()
    rows = [
        {"event_id": "KXHIGHNY-26JUL15", "family": "adaptive_blend", "log_loss": 0.1},
        {"event_id": "KXHIGHNY-26JUL15", "family": "market_raw", "log_loss": 0.2},
        {"event_id": "KXHIGHNY-26JUL16", "family": "adaptive_blend", "log_loss": 0.4},
        {"event_id": "KXHIGHNY-26JUL16", "family": "market_raw", "log_loss": 0.2},
    ]
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    (evaluation_dir / "evaluation_rows.jsonl.gz").write_bytes(gzip.compress(raw))

    cfg = {"paths": {"evidence_dir": str(tmp_path)}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DemoService(), cfg))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/showcase", timeout=5) as response:
            body = json.loads(response.read())
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert body["available"] is True
    assert body["favourable"] == "KXHIGHNY-26JUL15"
    assert body["failure"] == "KXHIGHNY-26JUL16"
