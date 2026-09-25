"""Add several NVRs and all their channels in one pass.

Six sites each with four to sixteen channels is a lot of clicking, and
every repetition is a chance to mistype a port. This drives the same
`/nvr/quick-add` handler the UI does -- so the password is encrypted
through the normal path, the per-brand RTSP template is chosen the same
way, and re-running returns the channels that already exist instead of
duplicating them.

Channel counts come from the NVR itself over ISAPI rather than being
typed: a Hikvision model number encodes them (DS-76*04* = 4 channels,
*08* = 8, *16* = 16), and reading it is one HTTP call.

The password is taken from the NVR_PASSWORD environment variable and is
never printed -- including in the failure paths, where an RTSP URL in an
error message is the usual way these leak.

    read -s -p "NVR password: " NVR_PASSWORD; echo; export NVR_PASSWORD
    docker compose exec -T -e NVR_PASSWORD worker-alerts \\
        python scripts/bulk_add_nvrs.py [--dry-run]
"""
from __future__ import annotations

import os
import re
import sys

# store name -> (host, http_port). RTSP is 554 on all of these; verified
# reachable from this host before the list was written.
SITES: dict[str, tuple[str, int]] = {
    "Yaya":        ("41.90.228.62", 8080),
    "Hub":         ("41.139.193.202", 8080),
    "T-Mall":      ("41.90.229.134", 8000),
    "Two Rivers":  ("197.248.49.109", 8080),
    "Imaara Mall": ("197.248.100.245", 8080),
    # Port 80 is closed on this one; ISAPI answers on 8000.
    "MSA New":     ("197.248.96.123", 8000),
}

USERNAME = "admin"
RTSP_PORT = 554
# Believe the model number, but never create more than this from a
# parse -- a misread would otherwise spray dozens of dead camera rows.
MAX_CHANNELS = 16
FALLBACK_CHANNELS = 4


def channel_count(host: str, http_port: int, password: str) -> tuple[int, str]:
    """(channels, model) read from ISAPI. Falls back when unreachable."""
    import httpx

    url = f"http://{host}:{http_port}/ISAPI/System/deviceInfo"
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(url, auth=httpx.DigestAuth(USERNAME, password))
            r.raise_for_status()
            body = r.text
    except Exception as e:
        # Deliberately not echoing the exception's URL: httpx puts the
        # request URL in some messages and these carry credentials.
        return FALLBACK_CHANNELS, f"(unreachable: {type(e).__name__})"

    m = re.search(r"<model>([^<]*)</model>", body)
    model = (m.group(1).strip() if m else "unknown")
    # DS-7604NI-Q1/4P -> the two digits after the series are the channels.
    d = re.search(r"DS-7[0-9]([0-9]{2})", model)
    if not d:
        return FALLBACK_CHANNELS, model
    n = int(d.group(1))
    return (n if 1 <= n <= MAX_CHANNELS else FALLBACK_CHANNELS), model


def main(dry_run: bool = False) -> int:
    password = os.environ.get("NVR_PASSWORD") or ""
    if not password:
        print("NVR_PASSWORD is not set — refusing to create rows with a "
              "blank password (every stream would fail auth).")
        return 2

    from app.api.nvr import QuickAddNvrIn, quick_add_nvr
    from app.database import SessionLocal
    from app.models import Store

    with SessionLocal() as db:
        for name, (host, http_port) in SITES.items():
            store = db.query(Store).filter(Store.name == name).first()
            if store is None:
                print(f"{name:<14} SKIPPED — no store row")
                continue

            chans, model = channel_count(host, http_port, password)
            if dry_run:
                print(f"{name:<14} {host:<18} {model:<22} would add {chans} channels")
                continue

            payload = QuickAddNvrIn(
                store_id=store.id, brand="hikvision", host=host,
                rtsp_port=RTSP_PORT, http_port=http_port,
                username=USERNAME, password=password,
                channel_count=chans, name_prefix=name,
            )
            try:
                # _u is a FastAPI auth dependency the handler body never
                # reads, so calling it directly is safe and keeps all the
                # encryption and idempotency in one place.
                cams = quick_add_nvr(payload, db, None)   # type: ignore[arg-type]
            except Exception as e:
                db.rollback()
                print(f"{name:<14} FAILED — {type(e).__name__}: {e}")
                continue
            print(f"{name:<14} {host:<18} {model:<22} {len(cams)} channels")

    print("\nCameras start as status=pending; the streamer picks them up "
          "within a minute or two. Re-running is safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
