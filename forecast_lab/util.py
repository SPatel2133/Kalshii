import gzip
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def fingerprint(obj):
    return sha256_bytes(canonical_json(obj).encode())


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n")


def write_jsonl(path, rows):
    atomic_write(path, "".join(canonical_json(r) + "\n" for r in rows))


def read_bytes_maybe_gz(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return f.read()
    if path.exists():
        return path.read_bytes()
    gz_path = Path(str(path) + ".gz")
    if gz_path.exists():
        with gzip.open(gz_path, "rb") as f:
            return f.read()
    return path.read_bytes()


def read_jsonl(path):
    text = read_bytes_maybe_gz(path).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if s is None:
        return None
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def check_finite(x, name="value"):
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ValueError(f"invalid numeric {name}: {x!r}")
    return float(x)
