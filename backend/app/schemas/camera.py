"""Pydantic request/response schemas for cameras and NVRs."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class CameraCreate(BaseModel):
    # Store-first onboarding: required on every new camera created via
    # POST /cameras/add. The API rejects the call with 400 otherwise.
    store_id: int
    name: str
    site: str | None = None
    brand: str = Field(pattern=r"^(dahua|hikvision|onvif|generic)$")
    connection_type: str
    host: str
    public_ip: str | None = None
    sdk_port: int | None = None
    rtsp_port: int = 554
    http_port: int = 80
    username: str | None = None
    password: str | None = None
    nvr_id: int | None = None
    channel_number: int | None = None
    rtsp_url_override: str | None = None
    ddns_hostname: str | None = None
    network_type: str = "lan"
    ai_enabled: bool = True
    inference_fps: int = 5


class CameraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    site: str | None
    brand: str
    connection_type: str
    host: str
    public_ip: str | None
    rtsp_port: int
    http_port: int
    nvr_id: int | None
    channel_number: int | None
    network_type: str
    ai_model_id: int | None
    ai_enabled: bool
    inference_fps: int
    status: str
    last_seen_at: datetime | None
    last_error: str | None
    # Store-first onboarding (Alembic 0002+). NULL allowed for legacy rows.
    store_id: int | None = None
    transport: str = "rtsp"
    snapshot_url_override: str | None = None
    created_at: datetime


class CameraUpdate(BaseModel):
    name: str | None = None
    site: str | None = None
    ai_enabled: bool | None = None
    ai_model_id: int | None = None
    inference_fps: int | None = None
    rtsp_url_override: str | None = None
    # Attach/detach in one PATCH. Pass an int to attach; pass null to
    # detach. The UI's "Store" dropdown uses this — see CamerasPage.
    store_id: int | None = None
    # Transport switch — 'rtsp' (default) or 'http_snapshot'.
    transport: str | None = None
    snapshot_url_override: str | None = None
    rtsp_port: int | None = None
    http_port: int | None = None
    channel_number: int | None = None


class TestConnectionIn(BaseModel):
    brand: str
    connection_type: str
    host: str
    rtsp_port: int = 554
    http_port: int = 80
    username: str | None = None
    password: str | None = None
    channel_number: int | None = None
    rtsp_url_override: str | None = None


class TestConnectionOut(BaseModel):
    ok: bool
    rtsp_url: str | None = None
    snapshot_jpeg_b64: str | None = None
    detected_channels: int = 0
    device_model: str | None = None
    error: str | None = None


class NVRConnectIn(BaseModel):
    name: str
    brand: str = Field(pattern=r"^(dahua|hikvision)$")
    host: str
    sdk_port: int | None = None
    rtsp_port: int = 554
    http_port: int = 80
    username: str
    password: str
    network_type: str = "lan"


class NVRChannelOut(BaseModel):
    channel: int
    name: str
    rtsp_main: str
    rtsp_sub: str


class NVRConnectOut(BaseModel):
    nvr_id: int
    device_model: str | None = None
    channels: list[NVRChannelOut]


class ONVIFDiscoverEntry(BaseModel):
    endpoint: str
    scopes: list[str]
    types: list[str]
