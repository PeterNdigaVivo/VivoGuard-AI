"""Download pinned detector weights into MODELS_DIR and verify checksums.

    python scripts/fetch_models.py                    # every model in the manifest
    python scripts/fetch_models.py yolo11s.pt yolo11m.pt

Run inside a worker container (MODELS_DIR=/data/models). Exits non-zero
if any model is missing from the manifest or fails its checksum.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Support both the worker image (/app/scripts beside /app/app) and direct
# execution from a repository checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.model_fetch import ChecksumMismatch, fetch, load_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manifest = load_manifest()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("models", nargs="*", help=f"default: all of {sorted(manifest)}")
    args = p.parse_args(argv)
    from app.config import settings
    models_dir = Path(settings.models_dir)
    failed = 0
    for name in args.models or sorted(manifest):
        try:
            path, action = fetch(name, models_dir, manifest=manifest)
            print(f"{action:10} {path}")
        except KeyError:
            print(f"UNKNOWN    {name} is not in app/ai/model_manifest.json", file=sys.stderr)
            failed += 1
        except (ChecksumMismatch, OSError) as exc:
            print(f"FAILED     {name}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
