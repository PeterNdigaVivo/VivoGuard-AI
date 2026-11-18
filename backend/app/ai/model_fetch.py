"""Download pinned detector weights and verify their checksums.

The manifest (model_manifest.json) names each model's exact release URL
and SHA-256. A file is only ever placed under its final name after its
checksum matches, so a truncated or tampered download can never be loaded
by a worker. Weights live in MODELS_DIR (a mounted volume), never in git.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).with_name("model_manifest.json")
CHUNK = 1 << 20


class ChecksumMismatch(RuntimeError):
    pass


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict]:
    return json.loads(path.read_text())["models"]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        for block in iter(lambda: resp.read(CHUNK), b""):
            out.write(block)


def fetch(name: str, models_dir: Path, *, manifest: dict[str, dict] | None = None,
          download: Callable[[str, Path], None] = _download) -> tuple[Path, str]:
    """Ensure models_dir/name exists with the pinned checksum.

    Returns (path, action) where action is 'present' or 'downloaded'.
    Raises KeyError for a model not in the manifest and ChecksumMismatch
    when the existing file or the download does not match."""
    entry = (manifest or load_manifest())[name]
    expected = entry["sha256"]
    target = models_dir / name
    if target.exists():
        actual = sha256_of(target)
        if actual == expected:
            return target, "present"
        raise ChecksumMismatch(
            f"{target} exists but its sha256 is {actual}, expected {expected}; "
            "move it aside and fetch again")
    models_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=models_dir, prefix=f".{name}.", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        download(entry["url"], tmp)
        actual = sha256_of(tmp)
        if actual != expected:
            raise ChecksumMismatch(
                f"download of {name} has sha256 {actual}, expected {expected}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("fetched %s (%s)", name, entry.get("release", entry["url"]))
    return target, "downloaded"
