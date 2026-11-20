"""Deterministically gzip tracked per-event v3 evidence."""
import gzip
import hashlib
import io
import subprocess
from pathlib import Path


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def compressed_bytes(data):
    output = io.BytesIO()
    with gzip.GzipFile(
            filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as stream:
        stream.write(data)
    result = output.getvalue()
    if gzip.decompress(result) != data:
        raise ValueError("gzip round trip changed evidence bytes")
    return result


def main():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "evidence/v3"],
        cwd=root, check=True, stdout=subprocess.PIPE).stdout
    paths = sorted(
        value.decode("utf-8") for value in tracked.split(b"\0")
        if value.endswith(b".jsonl"))
    if not paths:
        raise ValueError("no tracked evidence/v3 JSONL files found")

    checksums = []
    for relative in paths:
        source = root / relative
        compressed_path = Path(str(source) + ".gz")
        if source.is_file():
            original = source.read_bytes()
        elif compressed_path.is_file():
            original = gzip.decompress(compressed_path.read_bytes())
        else:
            raise FileNotFoundError(f"neither {source} nor {compressed_path} exists")

        payload = compressed_bytes(original)
        compressed_path.write_bytes(payload)
        written = compressed_path.read_bytes()
        if gzip.decompress(written) != original:
            raise ValueError(f"gzip round trip changed evidence bytes: {compressed_path}")
        if source.exists():
            source.unlink()
        checksum_path = compressed_path.relative_to(root / "evidence" / "v3").as_posix()
        checksums.append(f"{sha256(original)} {sha256(written)} {checksum_path}")

    checksum_file = root / "evidence" / "v3" / "CHECKSUMS.sha256"
    checksum_file.write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(f"compressed {len(paths)} tracked v3 JSONL files")


if __name__ == "__main__":
    main()
