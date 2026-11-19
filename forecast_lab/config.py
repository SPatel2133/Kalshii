import hashlib
import tomllib
from pathlib import Path


def load(path):
    path = Path(path)
    raw = path.read_bytes()
    cfg = tomllib.loads(raw.decode())
    cfg["_path"] = str(path)
    cfg["_sha256"] = hashlib.sha256(raw).hexdigest()
    return cfg


def default_config():
    return load(Path(__file__).resolve().parent.parent / "configs" / "nyc-0800-v1.toml")
