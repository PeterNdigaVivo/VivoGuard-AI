"""Standalone streamer service.

Loop:
  - read active cameras from Postgres (re-using the backend's models)
  - reconcile the StreamManager
  - sleep

Note: the streamer image is built FROM the backend image (or shares the
same base layer) so all `app.*` modules are available on PYTHONPATH.
"""
from __future__ import annotations
import logging
import os
import sys
import time

# Backend package is mounted/copied at /app/app in the container.
sys.path.insert(0, "/app")

from sqlalchemy import select                                       # noqa: E402

try:
    from app.config   import settings                               # type: ignore
    from app.database import SessionLocal                           # type: ignore
    from app.models   import Camera                                 # type: ignore
    from app.utils.crypto  import decrypt                           # type: ignore
    from app.utils.network import build_rtsp_url                    # type: ignore
    from app.stream.manager import StreamManager, CameraSpec        # type: ignore
except ImportError as e:                                            # pragma: no cover
    print(f"streamer: backend modules not on PYTHONPATH ({e})", file=sys.stderr)
    raise

log = logging.getLogger("streamer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

# Reconcile cadence. Lower = quicker pickup of newly added cameras.
# At 40+ cameras this is still cheap — just a single SELECT.
POLL_INTERVAL_SECONDS = int(os.environ.get("STREAMER_POLL_INTERVAL", "5"))


def _snapshot_url_for(c, password: str) -> str:
    """Compose the HTTP-snapshot URL for a camera.

    Override beats everything; otherwise build the Dahua default:
       http://USER:PASS@HOST:HTTP_PORT/cgi-bin/snapshot.cgi?channel=N
    """
    if c.snapshot_url_override:
        return c.snapshot_url_override
    # We embed creds in the URL only when no override is provided, and
    # we use HTTP Digest in the worker anyway — the URL form is just a
    # convenience for logging / Dahua firmwares that accept it.
    from urllib.parse import quote
    user = quote(c.username or "", safe="")
    pw   = quote(password or "", safe="")
    auth = f"{user}:{pw}@" if user else ""
    ch   = c.channel_number or 1
    return f"http://{auth}{c.host}:{c.http_port}/cgi-bin/snapshot.cgi?channel={ch}"


def desired_specs() -> list[CameraSpec]:
    out: list[CameraSpec] = []
    with SessionLocal() as db:
        cams = db.execute(select(Camera).where(Camera.ai_enabled == True)).scalars().all()  # noqa: E712
        for c in cams:
            try:
                pw = decrypt(c.password_encrypted or "")
            except Exception:
                pw = ""
            transport = getattr(c, "transport", "rtsp") or "rtsp"
            rtsp_url = build_rtsp_url(
                brand=c.brand,
                host=c.host,
                port=c.rtsp_port,
                username=c.username,
                password=pw,
                channel=c.channel_number,
                subtype=1,             # substream — cheaper for AI inference
                override=c.rtsp_url_override,
            )
            snap_url = _snapshot_url_for(c, pw) if transport == "http_snapshot" else ""
            out.append(CameraSpec(
                camera_id=c.id,
                rtsp_url=rtsp_url,
                fps=c.inference_fps or settings.inference_fps_default,
                width=640,
                transport=transport,
                snapshot_url=snap_url,
                username=c.username or "",
                password=pw,
            ))
    return out


def main() -> None:
    mgr = StreamManager()
    log.info("streamer: polling every %ss", POLL_INTERVAL_SECONDS)
    try:
        while True:
            try:
                specs = desired_specs()
                mgr.reconcile(specs)
            except Exception as e:
                log.exception("reconcile failed: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop_all()


if __name__ == "__main__":
    main()
