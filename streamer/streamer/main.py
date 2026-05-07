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

POLL_INTERVAL_SECONDS = int(os.environ.get("STREAMER_POLL_INTERVAL", "10"))


def desired_specs() -> list[CameraSpec]:
    out: list[CameraSpec] = []
    with SessionLocal() as db:
        cams = db.execute(select(Camera).where(Camera.ai_enabled == True)).scalars().all()  # noqa: E712
        for c in cams:
            try:
                pw = decrypt(c.password_encrypted or "")
            except Exception:
                pw = ""
            url = build_rtsp_url(
                brand=c.brand,
                host=c.host,
                port=c.rtsp_port,
                username=c.username,
                password=pw,
                channel=c.channel_number,
                subtype=1,             # substream — cheaper for AI inference
                override=c.rtsp_url_override,
            )
            out.append(CameraSpec(
                camera_id=c.id,
                rtsp_url=url,
                fps=c.inference_fps or settings.inference_fps_default,
                width=640,
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
