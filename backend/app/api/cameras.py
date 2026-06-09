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
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(status_code=400, detail=f"store_id={store_id} not found")

    # Inherit the store's default RTSP port unless the operator
    # explicitly set a non-default one in the wizard. Most Vivo
    # stores are Dahua-on-7000, so once an admin sets the store
    # default, every camera added afterwards gets the right port.
    effective_rtsp_port = payload.rtsp_port
    if payload.rtsp_port == 554 and store.default_rtsp_port:
        effective_rtsp_port = store.default_rtsp_port

    cam = Camera(
        name=payload.name,
        site=payload.site,
        brand=payload.brand,
        connection_type=payload.connection_type,
        host=payload.host,
        public_ip=payload.public_ip,
        sdk_port=payload.sdk_port,
        rtsp_port=effective_rtsp_port,
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


# Per-brand RTSP port defaults. Dahua-on-7000 is the dominant pattern
# across the 26 Vivo stores; Hikvision typically sits on 554 with the
# tunnel fallbacks. The Add Camera wizard reads this map to prefill
# the port field whenever the brand is changed.
_BRAND_RTSP_DEFAULTS: dict[str, int] = {
    "dahua":     7000,
    "hikvision": 554,
    "uniview":   554,
    "onvif":     554,
    "generic":   554,
}


@router.get("/brand-defaults")
def brand_defaults(_u: User = Depends(get_current_user)):
    """Per-brand default ports + fallback list, served to the wizard
    so the form prefills sensibly when the operator picks a brand."""
    return {
        "rtsp_port_defaults": _BRAND_RTSP_DEFAULTS,
        # Probe order used by Test Connection and auto-failover —
        # 7000 first because Dahua-on-7000 is the dominant pattern.
        "fallback_ports": [7000, 554, 80, 800, 8000, 8080],
    }


class BulkUpdatePortIn(BaseModel):
    """Body for /cameras/bulk-update-port. At least one filter is
    required so this can't accidentally rewrite every camera in the
    fleet."""
    host_filter: str | None = None     # exact match on `cameras.host`
    store_id: int | None = None        # all cameras in a store
    new_port: int                      # new RTSP port to set
    # When True, also flips rtsp_transport to 'http' (most cameras
    # that need a non-554 port are reached via HTTP tunnel).
    use_http_tunnel: bool = True


@router.post("/bulk-update-port")
def bulk_update_port(payload: BulkUpdatePortIn,
                     db: Session = Depends(get_db),
                     _u: User = Depends(require_role("admin", "operator"))):
    """Bulk-rewrite cameras.rtsp_port (and optionally rtsp_transport)
    for a set of cameras matched by host OR by store.

    Typical use:
      { host_filter: "197.155.67.50", new_port: 7000 } — set all
        cameras at that public IP to port 7000 with HTTP tunnel.
      { store_id: 5, new_port: 7000 } — same for every camera
        attached to store 5.

    Refuses to run with neither filter set (prevents accidental
    fleet-wide rewrites). Returns the per-camera before/after report.
    """
    if not payload.host_filter and payload.store_id is None:
        raise HTTPException(
            400,
            "Specify at least one of host_filter or store_id — bulk "
            "port update with no filter would rewrite every camera.",
        )

    q = db.query(Camera)
    if payload.host_filter:
        q = q.filter(Camera.host == payload.host_filter)
    if payload.store_id is not None:
        q = q.filter(Camera.store_id == payload.store_id)
    cams = q.all()

    report: list[dict] = []
    for cam in cams:
        before = {"rtsp_port": cam.rtsp_port,
                  "rtsp_transport": cam.rtsp_transport,
                  "transport": cam.transport}
        cam.rtsp_port = payload.new_port
        if payload.use_http_tunnel:
            cam.rtsp_transport = "http"
            # If this camera had been demoted to http_snapshot, give
            # it back full RTSP video.
            if cam.transport == "http_snapshot":
                cam.transport = "rtsp"
                cam.snapshot_url_override = None
        db.add(cam)
        report.append({
            "camera_id": cam.id, "name": cam.name, "host": cam.host,
            "before": before,
            "after": {"rtsp_port": cam.rtsp_port,
                      "rtsp_transport": cam.rtsp_transport,
                      "transport": cam.transport},
        })
    db.commit()
    # Bust the streamer's probe cache for every affected host so the
    # next reconcile picks up the new settings immediately.
    try:
        from app.stream.auto_transport import forget
        for cam in cams:
            forget(cam.host, cam.rtsp_port)
    except Exception:
        pass
    return {
        "matched": len(cams),
        "updated": len(report),
        "report": report,
    }


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
    # Probe order: Dahua-on-7000 is the most common pattern across
    # the 26 Vivo stores, so 7000 is tried first when the primary
    # RTSP port fails. Then standard 554, then the long tail.
    fallback_ports: list[int] = [7000, 554, 80, 800, 8000, 8080]


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
    """Return a JPEG snapshot as base64.

    Two-tier strategy:
      1. Prefer the streamer's most recent frame from Redis. The
         inference worker already has a live RTSP/HTTP-snapshot pull
         going for every active camera — that's a fresh frame within
         seconds, and it's cached in `vg:frame:{camera_id}`. Reading
         from Redis is fast, never times out, and avoids opening a
         second RTSP connection per snapshot request.
      2. Fall back to a one-shot ffmpeg pull when Redis has nothing
         (camera just attached / streamer down / ai_enabled=false).
         Use the camera's per-row `rtsp_transport` so HTTP-tunnel
         cameras work too — `rtsp_url_override` is honored via
         build_rtsp_url() inside _camera_rtsp_url().
    """
    import base64
    from app.utils.crypto import decrypt
    from app.stream.frame_buffer import FrameBuffer
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "camera not found")

    # Tier 1: streamer's cached JPEG.
    cached = FrameBuffer().latest_jpeg(camera_id)
    if cached:
        return {"jpeg_b64": base64.b64encode(cached).decode()}

    # Tier 2: one-shot ffmpeg pull. Use the row's transport setting
    # so tunnel-mode cameras don't fail with "[rtsp @ ...] Failed
    # reading RTSP data" the way they did with the hard-coded TCP
    # flag.
    pw = decrypt(cam.password_encrypted or "")
    rtsp = _camera_rtsp_url(cam, subtype=1, password_plain=pw)
    transport = (cam.rtsp_transport or "tcp")
    img, err = await grab_thumbnail_verbose(rtsp, timeout=15,
                                              rtsp_transport=transport)
    if not img:
        # The full ffmpeg stderr — "[rtsp @ 0x...] Failed reading RTSP
        # data", "Connection refused", etc. — is operationally useful
        # but not user-facing. We log it in the API container and
        # return a sanitised 503 with `code` set so the UI can render
        # a clean placeholder instead of the raw error string.
        import logging as _l
        _l.getLogger(__name__).info(
            "snapshot grab failed for camera %s (transport=%s): %s",
            camera_id, transport, (err or "")[:300],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "snapshot_unavailable",
                "camera_id": cam.id,
                "camera_name": cam.name,
                "message": "Snapshot temporarily unavailable.",
            },
        )
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


@router.get("/activity/top")
def activity_top(limit: int = 15, minutes: int = 15,
                 db: Session = Depends(get_db),
                 _u: User = Depends(get_current_user)):
    """Top cameras across the chain by detection_event count in the
    last `minutes`. Powers the Live View "Activity" toggle.

    Aggregated in a single Postgres query using FILTER clauses so the
    response is one round-trip; hits the existing
    (camera_id, timestamp) index on detection_events. Rounds out the
    response with camera + store metadata for the tile labels.

    Detection-type buckets (mapped to the spec's three activity icons):
      person                      → 👤 person
      dwell                       → 🛍 browsing (aisle zones)
      staff_present, queue,
      queue_length, entry_exit    → 🧾 checkout / entry
    sales_floor_insight is the bookkeeping heartbeat and is
    excluded so it can't dominate the ranking on a quiet store."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, and_
    from app.models import DetectionEvent

    limit   = max(1, min(100, int(limit)))
    minutes = max(1, min(180, int(minutes)))
    cutoff  = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    person_types   = ("person",)
    browsing_types = ("dwell",)
    checkout_types = ("staff_present", "queue", "queue_length", "entry_exit")
    counted_types  = person_types + browsing_types + checkout_types

    rows = (
        db.query(
            DetectionEvent.camera_id.label("camera_id"),
            func.count(DetectionEvent.id).label("total"),
            func.count(DetectionEvent.id).filter(
                DetectionEvent.detection_type.in_(person_types)
            ).label("person"),
            func.count(DetectionEvent.id).filter(
                DetectionEvent.detection_type.in_(browsing_types)
            ).label("browsing"),
            func.count(DetectionEvent.id).filter(
                DetectionEvent.detection_type.in_(checkout_types)
            ).label("checkout"),
            func.max(DetectionEvent.timestamp).label("last_at"),
        )
        .filter(DetectionEvent.timestamp >= cutoff,
                DetectionEvent.camera_id.isnot(None),
                DetectionEvent.detection_type.in_(counted_types))
        .group_by(DetectionEvent.camera_id)
        .order_by(func.count(DetectionEvent.id).desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return {"window_minutes": minutes, "cameras": []}

    cam_ids = [r.camera_id for r in rows]
    cams = {c.id: c for c in db.query(Camera).filter(Camera.id.in_(cam_ids)).all()}
    store_ids = {c.store_id for c in cams.values() if c.store_id}
    from app.models import Store as _Store
    stores = ({s.id: s for s in db.query(_Store).filter(_Store.id.in_(store_ids)).all()}
              if store_ids else {})

    out = []
    for r in rows:
        cam = cams.get(r.camera_id)
        if cam is None:
            continue
        store = stores.get(cam.store_id) if cam.store_id else None
        last_at = r.last_at
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        out.append({
            "camera_id":   cam.id,
            "camera_name": cam.name,
            "store_id":    cam.store_id,
            "store_name":  store.name if store else None,
            "status":      cam.status,
            "total":       int(r.total or 0),
            "counts": {
                "person":   int(r.person or 0),
                "browsing": int(r.browsing or 0),
                "checkout": int(r.checkout or 0),
            },
            "last_detection_at": last_at.isoformat() if last_at else None,
        })
    return {"window_minutes": minutes, "cameras": out}
