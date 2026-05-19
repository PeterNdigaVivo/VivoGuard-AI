"""/cameras endpoints — add, list, update, delete, test, discover, snapshot."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.connectors.base import ConnectionTestResult
from app.connectors.dahua_http import DahuaHTTP
from app.connectors.discovery import discover_onvif
from app.connectors.hikvision_isapi import HikvisionISAPI
from app.connectors.rtsp import grab_thumbnail, grab_thumbnail_verbose, probe_rtsp
from app.database import get_db
from app.deps import get_current_user, require_role
from app.models import Camera, User
from app.schemas.camera import (
    CameraCreate, CameraOut, CameraUpdate, ONVIFDiscoverEntry,
    TestConnectionIn, TestConnectionOut,
)
from app.utils.crypto import encrypt
from app.utils.network import build_rtsp_url

router = APIRouter(prefix="/cameras", tags=["cameras"])


# ---------- helpers ---------------------------------------------------

def _camera_rtsp_url(cam: Camera, *, subtype: int = 0, password_plain: str | None = None) -> str:
    """Resolve the RTSP URL for a camera using its current settings.
    `password_plain` lets the caller pass the unencrypted password to avoid
    decrypting twice in the same request path."""
    return build_rtsp_url(
        brand=cam.brand,
        host=cam.host,
        port=cam.rtsp_port,
        username=cam.username,
        password=password_plain,
        channel=cam.channel_number,
        subtype=subtype,
        override=cam.rtsp_url_override,
    )


# ---------- list / get ------------------------------------------------

@router.get("", response_model=list[CameraOut])
def list_cameras(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    """List cameras. Returns [] (not 500) on schema drift so the page
    renders something rather than hard-erroring."""
    import logging as _l
    _l_log = _l.getLogger(__name__)
    try:
        return db.query(Camera).order_by(Camera.id).all()
    except Exception as e:
        _l_log.exception("GET /cameras failed (likely schema drift): %s", e)
        db.rollback()
        return []


@router.get("/{camera_id}", response_model=CameraOut)
def get_camera(camera_id: int, db: Session = Depends(get_db),
               _u: User = Depends(get_current_user)):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    return cam


# ---------- create / update / delete ----------------------------------

@router.post("/add", response_model=CameraOut)
async def add_camera(payload: CameraCreate, db: Session = Depends(get_db),
                     _u: User = Depends(require_role("admin", "operator"))):
    # Store-first onboarding rule (May-2026 overhaul): every new camera
    # must belong to a store at creation time. Existing rows without
    # store_id remain valid (the UI surfaces an "Assign to store" prompt
    # for them) — but no NEW orphans are accepted via this endpoint.
    store_id = getattr(payload, "store_id", None)
    if not store_id:
        raise HTTPException(
            status_code=400,
            detail="store_id is required. Create a store first, then attach this camera to it."
        )
    from app.models import Store
    if not db.get(Store, store_id):
        raise HTTPException(status_code=400, detail=f"store_id={store_id} not found")

    cam = Camera(
        name=payload.name,
        site=payload.site,
        brand=payload.brand,
        connection_type=payload.connection_type,
        host=payload.host,
        public_ip=payload.public_ip,
        sdk_port=payload.sdk_port,
        rtsp_port=payload.rtsp_port,
        http_port=payload.http_port,
        username=payload.username,
        password_encrypted=encrypt(payload.password or ""),
        nvr_id=payload.nvr_id,
        channel_number=payload.channel_number,
        rtsp_url_override=payload.rtsp_url_override,
        ddns_hostname=payload.ddns_hostname,
        network_type=payload.network_type,
        ai_enabled=payload.ai_enabled,
        inference_fps=payload.inference_fps,
        rtsp_transport=payload.rtsp_transport or "tcp",
        store_id=store_id,
        status="pending",
    )

    # Save-time transport negotiation. Three-tier preference:
    #   1. Direct RTSP on the configured port (TCP transport)
    #   2. RTSP over HTTP tunnel on an alternate port (full video,
    #      just routed through 80/8000/etc)
    #   3. HTTP-snapshot polling (last resort — lower quality)
    #
    # We honor `rtsp_transport` if the operator explicitly set it via
    # the wizard — they may know better than the probe.
    try:
        from app.connectors.rtsp import probe_rtsp
        from app.stream.auto_transport import (
            discover_ports, probe_rtsp_tunnel,
        )
        primary_url = build_rtsp_url(
            brand=cam.brand, host=cam.host, port=cam.rtsp_port,
            username=cam.username, password=payload.password or "",
            channel=cam.channel_number or 1, subtype=1,
        )
        ok, _err = await probe_rtsp(
            primary_url, timeout=6.0,
            rtsp_transport=cam.rtsp_transport or "tcp",
        )
        if not ok:
            # Tier 2: RTSP tunnel.
            tunnel = probe_rtsp_tunnel(
                brand=cam.brand, host=cam.host,
                username=cam.username or "",
                password=payload.password or "",
                channel=cam.channel_number or 1,
            )
            if tunnel.get("working_port"):
                cam.rtsp_port = tunnel["working_port"]
                cam.rtsp_transport = "http"
            else:
                # Tier 3: HTTP snapshot.
                result = discover_ports(
                    cam.host, cam.username, payload.password or "",
                    cam.channel_number or 1,
                )
                if result.get("http_port"):
                    cam.transport = "http_snapshot"
                    if result["http_port"] != cam.http_port:
                        cam.http_port = result["http_port"]
                    snap_url = result.get("snapshot_url") or ""
                    vendor = result.get("snapshot_vendor") or ""
                    if snap_url and (vendor != "dahua-cgi" or
                                     snap_url.startswith("https://")):
                        cam.snapshot_url_override = snap_url
    except Exception:
        # Don't let probing failure block camera creation.
        pass

    db.add(cam)
    db.commit()
    db.refresh(cam)
    return cam


@router.get("/unassigned", response_model=list[CameraOut])
def list_unassigned(db: Session = Depends(get_db),
                    _u: User = Depends(get_current_user)):
    """Legacy/orphan cameras with no store_id — surfaced in the UI as
    an "Assign to store" prompt so operators can clean them up."""
    return db.query(Camera).filter(Camera.store_id.is_(None)).order_by(Camera.id).all()


class TcpProbeIn(BaseModel):
    host: str
    port: int = 554
    timeout: float = 5.0


@router.post("/probe-tcp")
async def probe_tcp(payload: TcpProbeIn,
                    _u: User = Depends(require_role("admin", "operator"))):
    """Open a raw TCP connection to `host:port` from inside the api
    container and report success/failure. Use this to disambiguate
    "browser works but VivoGuard doesn't" — the browser may be hitting
    a different port than RTSP, or running on a different network than
    Docker. Run a few probes against 80 and 554 to find out which
    ports are actually forwarded at the store router."""
    import asyncio
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(payload.host, payload.port),
            timeout=payload.timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"host": payload.host, "port": payload.port, "ok": True,
                "detail": "TCP connection succeeded — port is reachable from VivoGuard."}
    except asyncio.TimeoutError:
        return {"host": payload.host, "port": payload.port, "ok": False,
                "detail": "Timed out — host unreachable or firewall dropping packets. "
                          "Most often the store router doesn't forward this port to the NVR's LAN IP."}
    except ConnectionRefusedError:
        return {"host": payload.host, "port": payload.port, "ok": False,
                "detail": "Connection refused — host responded but nothing listens on this port. "
                          "Either the NVR's RTSP service is disabled, listening on a different port, "
                          "or the router forwards a different port number."}
    except Exception as e:
        return {"host": payload.host, "port": payload.port, "ok": False,
                "detail": f"{type(e).__name__}: {e}"}


class TestRtspPortsIn(BaseModel):
    """Body for /cameras/test-rtsp-ports — wizard's 'Test RTSP
    Connection' button uses this BEFORE saving a row."""
    brand: str
    host: str
    username: str | None = None
    password: str | None = None
    channel_number: int | None = 1
    rtsp_port: int = 554
    # Ports to try as HTTP-tunneled RTSP if the primary fails.
    # Defaults to the Vivo-fleet HTTP-port catalogue.
    fallback_ports: list[int] = [80, 800, 8000, 8080, 7000]


@router.post("/test-rtsp-ports")
async def test_rtsp_ports(payload: TestRtspPortsIn,
                          _u: User = Depends(require_role("admin", "operator"))):
    """Probe RTSP on the configured port (TCP transport), then iterate
    through fallback HTTP-tunnel ports if the primary fails.

    Returns a step-by-step report so the wizard UI can show
    "✅ Connected on port 554" or "❌ Port 554 failed → ✅ Connected
    on port 8000 (via HTTP tunnel)".
    """
    from app.connectors.rtsp import probe_rtsp

    attempts: list[dict] = []
    working: dict | None = None

    # Primary attempt: TCP transport on the configured RTSP port.
    primary_url = build_rtsp_url(
        brand=payload.brand, host=payload.host, port=payload.rtsp_port,
        username=payload.username, password=payload.password,
        channel=payload.channel_number or 1, subtype=1,
    )
    ok, err = await probe_rtsp(primary_url, timeout=8.0, rtsp_transport="tcp")
    attempts.append({
        "port": payload.rtsp_port, "transport": "tcp",
        "url": primary_url, "ok": ok,
        "reason": "Connected" if ok else (err or "failed"),
    })
    if ok:
        working = {"port": payload.rtsp_port, "transport": "tcp",
                   "url": primary_url}

    # Fallbacks: tunnel through each HTTP port until one negotiates.
    if not working:
        for p in payload.fallback_ports:
            if p == payload.rtsp_port:
                continue          # already tried as TCP above
            url = build_rtsp_url(
                brand=payload.brand, host=payload.host, port=p,
                username=payload.username, password=payload.password,
                channel=payload.channel_number or 1, subtype=1,
            )
            ok, err = await probe_rtsp(url, timeout=8.0, rtsp_transport="http")
            attempts.append({
                "port": p, "transport": "http",
                "url": url, "ok": ok,
                "reason": "Connected via HTTP tunnel" if ok else (err or "failed"),
            })
            if ok:
                working = {"port": p, "transport": "http", "url": url}
                break

    return {
        "host": payload.host,
        "brand": payload.brand,
        "ok": working is not None,
        "working": working,
        "attempts": attempts,
        "summary": (
            f"✅ Connected on port {working['port']}"
            + (" (HTTP tunnel)" if working and working["transport"] == "http" else "")
            if working
            else "❌ No working RTSP port found on " + payload.host
        ),
    }


@router.post("/{camera_id}/try-alternate-ports")
async def try_alternate_ports(camera_id: int,
                              db: Session = Depends(get_db),
                              _u: User = Depends(require_role("admin", "operator"))):
    """Per-camera version of test-rtsp-ports. If a working port is
    found, the camera row is updated to use it (rtsp_port +
    rtsp_transport) and the streamer's next reconcile spawns a worker
    on the new settings."""
    from app.utils.crypto import decrypt
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")

    try:
        pw = decrypt(cam.password_encrypted or "")
    except Exception:
        pw = ""

    body = TestRtspPortsIn(
        brand=cam.brand, host=cam.host,
        username=cam.username, password=pw,
        channel_number=cam.channel_number or 1,
        rtsp_port=cam.rtsp_port or 554,
    )
    result = await test_rtsp_ports(body, _u=_u)        # reuse the logic

    if result["ok"]:
        w = result["working"]
        cam.rtsp_port = w["port"]
        cam.rtsp_transport = w["transport"]
        # If we were on http_snapshot but tunnel works, prefer tunnel
        # (full video over polling).
        if cam.transport == "http_snapshot":
            cam.transport = "rtsp"
            cam.snapshot_url_override = None
        db.commit()
        # Bust the auto_transport probe cache so the streamer picks
        # the new settings up immediately on the next reconcile.
        from app.stream.auto_transport import forget
        forget(cam.host, cam.rtsp_port)

    return {**result, "camera_id": cam.id, "updated": result["ok"]}


class IntelligentProbeIn(BaseModel):
    host: str
    username: str | None = None
    password: str | None = None
    channel_number: int | None = 1


@router.post("/intelligent-probe")
async def intelligent_probe(payload: IntelligentProbeIn,
                            _u: User = Depends(require_role("admin", "operator"))):
    """Smart port discovery for the Add Camera wizard.

    Tries every common Dahua/Hik port we've seen in the Vivo fleet —
    RTSP on 554/10554/5544/8554 and HTTP CGI on 80/8080/8000/800/7000 —
    in parallel and reports which ones answer. Operators no longer
    have to remember whether THIS NVR firmware put its admin port on
    7000 or 8080.

    The wizard pre-fills `rtsp_port` and `http_port` from the
    response so the operator just clicks Save.
    """
    import asyncio
    from app.stream.auto_transport import discover_ports
    # discover_ports does sync TCP probes; offload so we don't block
    # the asyncio loop while 8 sockets time out.
    result = await asyncio.to_thread(
        discover_ports,
        payload.host, payload.username, payload.password,
        payload.channel_number or 1,
    )
    return result


@router.get("/{camera_id}/transport-diagnose")
def transport_diagnose(camera_id: int, fresh: bool = False,
                       db: Session = Depends(get_db),
                       _u: User = Depends(require_role("admin", "operator"))):
    """Per-camera transport diagnostic.

    Returns the streamer's most recent auto-failover probe result —
    which RTSP port it tried, which HTTP ports + URL templates it
    tried, and exactly what each returned. Operators use this from
    the Live View tile to see WHY a camera is paused with "Connection
    refused" instead of guessing.

    Pass `?fresh=true` to force a fresh probe right now (otherwise
    the cached result from the last reconcile is returned, which can
    be up to 5 min old).
    """
    from app.stream.auto_transport import (
        last_diagnostic, negotiate, forget,
    )
    from app.utils.crypto import decrypt

    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")

    host = cam.host
    rtsp_port = cam.rtsp_port or 554

    if fresh:
        # Bust the cache so negotiate() actually re-runs the probe.
        forget(host, rtsp_port)
        try:
            pw = decrypt(cam.password_encrypted or "")
        except Exception:
            pw = ""
        # negotiate() returns None either when RTSP works (no change
        # needed) OR when nothing worked. The diagnostic record is
        # written either way — we just want to trigger it now.
        camera_dict = {
            "host": host, "rtsp_port": rtsp_port,
            "http_port": cam.http_port,
            "transport": "rtsp",   # force probe even if already switched
            "username": cam.username,
            "channel_number": cam.channel_number,
        }
        try:
            updates = negotiate(camera_dict, pw)
            if updates:
                for k, v in updates.items():
                    setattr(cam, k, v)
                db.add(cam)
                db.commit()
        except Exception:
            db.rollback()

    record = last_diagnostic(host, rtsp_port)
    return {
        "camera_id": cam.id,
        "name": cam.name,
        "host": host,
        "rtsp_port": rtsp_port,
        "http_port": cam.http_port,
        "transport": cam.transport,
        "snapshot_url_override": cam.snapshot_url_override,
        "diagnostic": record,
        "explanation": _explain_diagnostic(record, host, rtsp_port),
    }


def _explain_diagnostic(d: dict | None, host: str, rtsp_port: int) -> str:
    """One-paragraph plain-English summary suitable for the UI."""
    if not d:
        return (f"No probe data yet for {host}:{rtsp_port}. The streamer "
                f"hasn't tried this camera since startup. Click 'Re-probe' "
                f"to run one now.")
    if d.get("outcome") == "rtsp_ok":
        return f"RTSP/{rtsp_port} is reachable — using direct RTSP."
    if d.get("outcome") == "rtsp_tunnel":
        sw = d.get("switched_to") or {}
        return (f"RTSP/{rtsp_port} blocked; auto-switched to RTSP over HTTP "
                f"tunnel on port {sw.get('port')} (full video preserved).")
    if d.get("outcome") == "switched":
        sw = d.get("switched_to") or {}
        return (f"RTSP/{rtsp_port} blocked; auto-switched to HTTP snapshot "
                f"on port {sw.get('port')} ({sw.get('vendor')}).")
    if d.get("outcome") == "no_endpoint_found":
        attempts = d.get("http_attempts") or []
        any_tcp = any(a.get("tcp") for a in attempts)
        if not any_tcp:
            return (f"RTSP/{rtsp_port} blocked AND no HTTP port on {host} "
                    f"answered at all. The store router likely isn't "
                    f"forwarding any port to the NVR. Fix port-forwarding "
                    f"at the router (forward 80, 8080, or 7000 to the "
                    f"NVR's LAN IP), then click 'Re-probe'.")
        return (f"RTSP/{rtsp_port} blocked. HTTP port(s) are reachable but "
                f"none returned a JPEG via Dahua CGI / Hikvision ISAPI / "
                f"ONVIF paths. The NVR may need its CGI/ISAPI service "
                f"enabled in its admin UI, or the credentials may be "
                f"wrong. See the per-attempt detail below.")
    return "Probe ran but result is inconclusive — see detail below."


class AutoFailoverIn(BaseModel):
    # If provided, only failover cameras in this store. Omitted = all.
    store_id: int | None = None
    # If provided, try this HTTP port first (e.g. 7000 for Dahua NVRs
    # that put their CGI on a non-default port).
    http_port: int | None = None


@router.post("/auto-failover")
async def auto_failover(payload: AutoFailoverIn,
                        db: Session = Depends(get_db),
                        _u: User = Depends(require_role("admin", "operator"))):
    """One-click: probe every RTSP camera in scope, switch to
    http_snapshot for any whose port 554 is unreachable but whose
    HTTP snapshot endpoint returns a JPEG.

    Use this after fixing a store's router or when adding a new store
    where you don't know which transport will work. Returns a
    per-camera report so operators can see which ones flipped, which
    are still broken, and which are fine.
    """
    from app.stream.auto_transport import negotiate, forget
    from app.utils.crypto import decrypt

    q = db.query(Camera)
    if payload.store_id is not None:
        q = q.filter(Camera.store_id == payload.store_id)
    cams = q.all()

    report: list[dict] = []
    for cam in cams:
        # Clear the per-process cache so this call always re-probes.
        try:
            forget(cam.host, cam.rtsp_port or 554)
        except Exception:
            pass
        try:
            pw = decrypt(cam.password_encrypted or "")
        except Exception:
            pw = ""
        camera_dict = {
            "host": cam.host,
            "rtsp_port": cam.rtsp_port,
            # Caller-supplied port takes priority if given.
            "http_port": payload.http_port or cam.http_port,
            "transport": "rtsp",   # force a probe even if already http_snapshot
            "username":  cam.username,
            "channel_number": cam.channel_number,
        }
        updates = negotiate(camera_dict, pw)
        if updates:
            for k, v in updates.items():
                setattr(cam, k, v)
            db.add(cam)
            report.append({"camera_id": cam.id, "name": cam.name,
                           "result": "switched", "updates": updates})
        else:
            # If RTSP works, that's already a win. We can't tell from
            # here which branch ran inside negotiate(); look at the
            # status field for context.
            report.append({"camera_id": cam.id, "name": cam.name,
                           "result": "no_change",
                           "hint": "RTSP reachable, or no HTTP snapshot "
                                   "port responded. If still failing, "
                                   "verify port-forward at the store router."})
    db.commit()
    return {"checked": len(cams),
            "switched": sum(1 for r in report if r["result"] == "switched"),
            "report": report}


@router.patch("/{camera_id}", response_model=CameraOut)
def update_camera(camera_id: int, patch: CameraUpdate, db: Session = Depends(get_db),
                  _u: User = Depends(require_role("admin", "operator"))):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    for k, v in patch.model_dump(exclude_unset=True).items():
        setattr(cam, k, v)
    db.commit()
    db.refresh(cam)
    return cam


@router.delete("/{camera_id}")
def delete_camera(camera_id: int, db: Session = Depends(get_db),
                  _u: User = Depends(require_role("admin"))):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    db.delete(cam)
    db.commit()
    return {"deleted": camera_id}


# ---------- test connection (no DB write) -----------------------------

@router.post("/test-connection", response_model=TestConnectionOut)
async def test_connection(payload: TestConnectionIn,
                          _u: User = Depends(require_role("admin", "operator"))):
    """Used by the Add Camera wizard's *Test Connection* button."""
    # Build RTSP URL from inputs (override wins).
    rtsp = build_rtsp_url(
        brand=payload.brand,
        host=payload.host,
        port=payload.rtsp_port,
        username=payload.username,
        password=payload.password,
        channel=payload.channel_number,
        subtype=1,                       # use substream for the test (faster)
        override=payload.rtsp_url_override,
    )

    # 1) Quick HTTP-side device probe to identify model (best effort).
    device_model: str | None = None
    detected_channels = 0
    try:
        if payload.brand == "dahua" and payload.username:
            api = DahuaHTTP(payload.host, payload.http_port, payload.username, payload.password or "")
            info = await api.device_info()
            device_model = info.get("deviceType") or info.get("model")
            detected_channels = await api.channel_count()
        elif payload.brand == "hikvision" and payload.username:
            api = HikvisionISAPI(payload.host, payload.http_port, payload.username, payload.password or "")
            info = await api.device_info()
            device_model = info.get("model")
            detected_channels = await api.channel_count()
    except Exception as e:
        # Non-fatal — RTSP probe below is the source of truth.
        device_model = device_model or f"<HTTP probe failed: {e}>"

    # 2) RTSP probe.
    ok, err = await probe_rtsp(rtsp, timeout=12)
    snap_b64: Optional[str] = None
    if ok:
        snap_b64 = await grab_thumbnail(rtsp, timeout=15)

    return TestConnectionOut(
        ok=ok,
        rtsp_url=rtsp,
        snapshot_jpeg_b64=snap_b64,
        detected_channels=detected_channels,
        device_model=device_model,
        error=err if not ok else None,
    )


# ---------- ONVIF LAN discovery --------------------------------------

@router.get("/discover-onvif", response_model=list[ONVIFDiscoverEntry])
def discover(_u: User = Depends(require_role("admin", "operator"))):
    """Broadcast WS-Discovery on the local network. Returns up to a few
    seconds' worth of replies."""
    return discover_onvif(timeout=3.0)


# ---------- snapshot / stream URL -------------------------------------

@router.get("/{camera_id}/stream-url")
def stream_url(camera_id: int, subtype: int = 0,
               db: Session = Depends(get_db),
               _u: User = Depends(get_current_user)):
    from app.utils.crypto import decrypt
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    pw = decrypt(cam.password_encrypted or "")
    return {"rtsp_url": _camera_rtsp_url(cam, subtype=subtype, password_plain=pw)}


@router.get("/{camera_id}/snapshot")
async def snapshot(camera_id: int, db: Session = Depends(get_db),
                   _u: User = Depends(get_current_user)):
    from app.utils.crypto import decrypt
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    pw = decrypt(cam.password_encrypted or "")
    rtsp = _camera_rtsp_url(cam, subtype=1, password_plain=pw)
    img, err = await grab_thumbnail_verbose(rtsp, timeout=15)
    if not img:
        # IMPORTANT: do NOT mutate cam.status here. The streamer is the
        # source of truth for online/offline. A failed one-shot snapshot
        # from THIS container could just mean a transient network blip,
        # a slower path than the streamer takes, or the operator's
        # request raced with an ffmpeg restart. Operators were seeing
        # every camera flip to 'offline' after opening Camera Setup.
        raise HTTPException(503, detail=(err or "snapshot grab failed")[:1000])
    # Same on the success path — leave cam.status alone. The CamerasPage
    # already shows the streamer-derived live state separately.
    return {"jpeg_b64": img}


# Operator-visible diagnostic: tries the substream and the mainstream and
# reports the full FFmpeg stderr for each. Use from the UI when a camera
# refuses to connect.
@router.get("/{camera_id}/diagnose")
async def diagnose(camera_id: int, db: Session = Depends(get_db),
                   _u: User = Depends(get_current_user)):
    from app.utils.crypto import decrypt
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")
    pw = decrypt(cam.password_encrypted or "")
    results = []
    for sub in (1, 0):    # try substream first, then main
        rtsp = _camera_rtsp_url(cam, subtype=sub, password_plain=pw)
        # Hide credentials in the response so we can paste safely.
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(rtsp)
        redacted_netloc = (u.hostname or "") + (f":{u.port}" if u.port else "")
        if u.username:
            redacted_netloc = f"{u.username}:****@{redacted_netloc}"
        redacted = urlunsplit((u.scheme, redacted_netloc, u.path, u.query, u.fragment))

        _img, err = await grab_thumbnail_verbose(rtsp, timeout=12)
        results.append({
            "subtype": sub,
            "stream_label": "substream" if sub == 1 else "mainstream",
            "rtsp_url": redacted,
            "ok": _img is not None,
            "ffmpeg_stderr": err,
        })
    return {
        "camera_id": cam.id,
        "name": cam.name,
        "brand": cam.brand,
        "connection_type": cam.connection_type,
        "host": cam.host,
        "rtsp_port": cam.rtsp_port,
        "channel_number": cam.channel_number,
        "results": results,
    }
