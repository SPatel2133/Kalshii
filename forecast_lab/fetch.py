"""Raw fetch layer: stable request keys, content-hashed cache, manifest, retry/backoff.

A cache entry is written atomically and only after a successful (2xx) response, so an
interrupted download never becomes a valid-looking cache hit. Failures are recorded in
the manifest as typed acquisition failures and raised; they are never treated as empty.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import atomic_write, canonical_json, sha256_bytes, utc_now_iso

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, failure_class, url, detail):
        super().__init__(f"{failure_class}: {url}: {detail}")
        self.failure_class = failure_class
        self.url = url
        self.detail = detail


class Fetcher:
    def __init__(self, cache_dir, timeout_s=30, max_retries=5, backoff_s=1.5, min_interval_s=0.25,
                 offline=False, opener=None):
        self.cache_dir = Path(cache_dir)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.min_interval_s = min_interval_s
        self.offline = offline
        self.opener = opener or urllib.request.urlopen
        self._last = 0.0
        self.manifest_path = self.cache_dir / "manifest.jsonl"

    @classmethod
    def from_config(cls, cfg, offline=False):
        h = cfg["http"]
        return cls(cfg["paths"]["cache_dir"], h["timeout_s"], h["max_retries"], h["backoff_s"],
                   h["min_interval_s"], offline=offline)

    @staticmethod
    def request_key(provider, endpoint, params):
        norm = {k: str(v) for k, v in sorted((params or {}).items()) if v is not None}
        return canonical_json({"provider": provider, "endpoint": endpoint, "params": norm}), norm

    def _paths(self, provider, key):
        h = sha256_bytes(key.encode())
        d = self.cache_dir / provider / h[:2]
        return d / f"{h}.body", d / f"{h}.meta.json"

    def _manifest(self, rec):
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "a") as f:
            f.write(canonical_json(rec) + "\n")

    def get(self, provider, base, endpoint, params=None):
        key, norm = self.request_key(provider, endpoint, params)
        body_p, meta_p = self._paths(provider, key)
        if body_p.exists() and meta_p.exists():
            meta = json.loads(meta_p.read_text())
            body = body_p.read_bytes()
            if sha256_bytes(body) == meta["sha256"]:
                return body, meta
        if self.offline:
            raise FetchError("offline_cache_miss", endpoint, key)
        url = base.rstrip("/") + endpoint
        if norm:
            url += "?" + urllib.parse.urlencode(norm)
        last_err = None
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "forecast-lab/0.1 (research)"})
                with self.opener(req, timeout=self.timeout_s) as resp:
                    status = getattr(resp, "status", 200)
                    body = resp.read()
                if status < 200 or status >= 300:
                    raise urllib.error.HTTPError(url, status, "bad status", None, None)
                meta = {"provider": provider, "endpoint": endpoint, "params": norm, "url": url,
                        "sha256": sha256_bytes(body), "retrieved_utc": utc_now_iso(), "status": status,
                        "bytes": len(body), "path": str(body_p), "attempts": attempt + 1}
                atomic_write(body_p, body)
                atomic_write(meta_p, json.dumps(meta, sort_keys=True))
                self._manifest({"ok": True, **meta})
                return body, meta
            except urllib.error.HTTPError as e:
                code = e.code
                detail = ""
                try:
                    detail = e.read()[:300].decode(errors="replace") if e.fp else ""
                except Exception:
                    pass
                if code in TRANSIENT_STATUS:
                    last_err = FetchError(f"http_{code}", url, detail)
                else:
                    err = FetchError(f"http_{code}", url, detail)
                    self._manifest({"ok": False, "provider": provider, "endpoint": endpoint, "params": norm,
                                    "url": url, "failure_class": err.failure_class, "detail": detail,
                                    "retrieved_utc": utc_now_iso()})
                    raise err
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = FetchError("network", url, repr(e))
            time.sleep(self.backoff_s * (2 ** attempt))
        self._manifest({"ok": False, "provider": provider, "endpoint": endpoint, "params": norm, "url": url,
                        "failure_class": last_err.failure_class, "detail": last_err.detail,
                        "retrieved_utc": utc_now_iso()})
        raise last_err

    def get_json(self, provider, base, endpoint, params=None):
        body, meta = self.get(provider, base, endpoint, params)
        return json.loads(body), meta
