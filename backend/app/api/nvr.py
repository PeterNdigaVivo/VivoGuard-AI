"""NVR endpoints — connect, list channels, materialise channels as cameras."""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.connectors.dahua_http import DahuaHTTP
from app.connectors.hikvision_isapi import HikvisionISAPI
from app.connectors.nvr_manager import enumerate_dahua, enumerate_hikvision
from app.database import get_db
from app.deps import require_role
from app.models import Camera, NVRDevice, User
from app.schemas.camera import (
    CameraOut, NVRChannelOut, NVRConnectIn, NVRConnectOut,
)
from app.utils.crypto import encrypt

router = APIRouter(prefix="/nvr", tags=["nvr"])


@router.post("/connect", response_model=NVRConnectOut)
async def connect_nvr(payload: NVRConnectIn, db: Session = Depends(get_db),
                      _u: User = Depends(require_role("admin", "operator"))):
    """Probe an NVR, enumerate channels, persist NVR row, return channels."""
    # Identify device model.
    device_model: str | None = None
    try:
        if payload.brand == "dahua":
            info = await DahuaHTTP(payload.host, payload.http_port,
                                   payload.username, payload.password).device_info()
            device_model = info.get("deviceType") or info.get("model")
            channels = await enumerate_dahua(payload.host, payload.http_port, payload.rtsp_port,
                                             payload.username, payload.password)
        else:  # hikvision
            info = await HikvisionISAPI(payload.host, payload.http_port,
                                        payload.username, payload.password).device_info()
            device_model = info.get("model")
            channels = await enumerate_hikvision(payload.host, payload.http_port, payload.rtsp_port,
                                                 payload.username, payload.password)
    except Exception as e:
        raise HTTPException(502, f"could not reach NVR: {e}") from e

    # Persist NVR.
    nvr = NVRDevice(
        name=payload.name,
        brand=payload.brand,
        host=payload.host,
        sdk_port=payload.sdk_port,
        rtsp_port=payload.rtsp_port,
        http_port=payload.http_port,
        username=payload.username,
        password_encrypted=encrypt(payload.password),
        total_channels=len(channels),
        connected_channels=len(channels),
        status="online",
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(nvr)
    db.commit()
    db.refresh(nvr)

    return NVRConnectOut(
        nvr_id=nvr.id,
        device_model=device_model,
        channels=[NVRChannelOut(channel=c.channel, name=c.name,
                                rtsp_main=c.rtsp_main, rtsp_sub=c.rtsp_sub)
                  for c in channels],
    )


@router.get("/{nvr_id}/channels", response_model=list[NVRChannelOut])
async def list_channels(nvr_id: int, db: Session = Depends(get_db),
                        _u: User = Depends(require_role("admin", "operator"))):
    nvr = db.get(NVRDevice, nvr_id)
    if not nvr:
        raise HTTPException(404, "nvr not found")
    from app.utils.crypto import decrypt
    pw = decrypt(nvr.password_encrypted)
    if nvr.brand == "dahua":
        chans = await enumerate_dahua(nvr.host, nvr.http_port, nvr.rtsp_port, nvr.username, pw)
    else:
        chans = await enumerate_hikvision(nvr.host, nvr.http_port, nvr.rtsp_port, nvr.username, pw)
    return [NVRChannelOut(channel=c.channel, name=c.name, rtsp_main=c.rtsp_main, rtsp_sub=c.rtsp_sub)
            for c in chans]


from pydantic import BaseModel


class AddChannelsIn(BaseModel):
    """Selected channels + the destination store. The store is mandatory
    so every channel-camera created here lands attached to a store."""
    nvr_id: int | None = None
    store_id: int
    channels: list[NVRChannelOut]


@router.post("/{nvr_id}/add-channels", response_model=list[CameraOut])
async def add_channels(nvr_id: int, payload: AddChannelsIn, db: Session = Depends(get_db),
                       _u: User = Depends(require_role("admin", "operator"))):
    """Materialise the selected channels as Camera rows attached to a store."""
    nvr = db.get(NVRDevice, nvr_id)
    if not nvr:
        raise HTTPException(404, "nvr not found")
    from app.models import Store
    if not db.get(Store, payload.store_id):
        raise HTTPException(400, f"store_id={payload.store_id} not found")
    out: list[Camera] = []
    for ch in payload.channels:
        cam = Camera(
            name=f"{nvr.name} - {ch.name}",
            brand=nvr.brand,
            connection_type="nvr_dahua" if nvr.brand == "dahua" else "nvr_hik",
            host=nvr.host,
            rtsp_port=nvr.rtsp_port,
            http_port=nvr.http_port,
            username=nvr.username,
            password_encrypted=nvr.password_encrypted,
            nvr_id=nvr.id,
            channel_number=ch.channel,
            rtsp_url_override=ch.rtsp_main,
            network_type="lan",
            store_id=payload.store_id,
            status="online",
        )
        db.add(cam)
        out.append(cam)
    db.commit()
    for c in out:
        db.refresh(c)
    return out
