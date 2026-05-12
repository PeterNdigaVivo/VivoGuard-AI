"""Cameras and NVR devices.

A camera row represents a single video source — either a standalone IP
camera (LAN or WAN) or one channel of a parent NVR. NVR-channel cameras
keep `nvr_id` populated and `channel_number` set.
"""
from datetime import datetime
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# Connection-type enum (kept as a plain string column for forward-compat;
# Alembic migrations wouldn't have to rewrite enum types when we add new
# brands later).
CONNECTION_TYPES = (
    "nvr_dahua",     # channel of a Dahua NVR (HTTP/CGI + RTSP)
    "nvr_hik",       # channel of a Hikvision NVR (ISAPI + RTSP)
    "nvr_onvif",     # channel of any ONVIF NVR
    "lan_rtsp",      # direct RTSP camera on LAN
    "wan_rtsp",      # direct RTSP camera reached via public IP
    "wan_dahua",     # Dahua camera reached via public IP (HTTP/CGI + RTSP)
    "wan_hik",       # Hikvision camera reached via public IP (ISAPI + RTSP)
    "onvif",         # generic ONVIF camera
)

NETWORK_TYPES = ("lan", "wan", "vpn")
CAMERA_STATUS = ("online", "offline", "degraded", "pending")


class NVRDevice(Base):
    """A network video recorder with N channels."""

    __tablename__ = "nvr_devices"

    id:             Mapped[int]      = mapped_column(primary_key=True)
    name:           Mapped[str]      = mapped_column(String(128))
    brand:          Mapped[str]      = mapped_column(String(32))   # dahua | hikvision | onvif | generic
    host:           Mapped[str]      = mapped_column(String(255))  # IP or DDNS hostname
    sdk_port:       Mapped[int | None] = mapped_column(Integer, nullable=True)   # 7000 (Dahua), 8000 (Hik) — informational
    rtsp_port:      Mapped[int]      = mapped_column(Integer, default=554)
    http_port:      Mapped[int]      = mapped_column(Integer, default=80)
    username:       Mapped[str]      = mapped_column(String(128))
    password_encrypted: Mapped[str]  = mapped_column(Text)
    total_channels: Mapped[int]      = mapped_column(Integer, default=0)
    connected_channels: Mapped[int]  = mapped_column(Integer, default=0)
    status:         Mapped[str]      = mapped_column(String(16), default="pending")
    last_seen_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cameras = relationship("Camera", back_populates="nvr", cascade="all, delete-orphan")


class Camera(Base):
    __tablename__ = "cameras"

    id:                Mapped[int]   = mapped_column(primary_key=True)
    name:              Mapped[str]   = mapped_column(String(128))
    site:              Mapped[str | None] = mapped_column(String(128), nullable=True)
    brand:             Mapped[str]   = mapped_column(String(32))   # dahua | hikvision | generic | onvif
    connection_type:   Mapped[str]   = mapped_column(String(32))   # see CONNECTION_TYPES

    # Network endpoint (some fields unused depending on connection_type).
    host:              Mapped[str]   = mapped_column(String(255))  # IP or DDNS hostname
    public_ip:         Mapped[str | None] = mapped_column(String(64), nullable=True)
    sdk_port:          Mapped[int | None] = mapped_column(Integer, nullable=True)
    rtsp_port:         Mapped[int]   = mapped_column(Integer, default=554)
    http_port:         Mapped[int]   = mapped_column(Integer, default=80)
    username:          Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_encrypted:Mapped[str | None] = mapped_column(Text, nullable=True)

    # NVR linkage (NULL for standalone cameras).
    nvr_id:            Mapped[int | None] = mapped_column(ForeignKey("nvr_devices.id", ondelete="SET NULL"), nullable=True)
    channel_number:    Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Stream URL override (when auto-template is wrong for an exotic device).
    rtsp_url_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    ddns_hostname:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    network_type:      Mapped[str]   = mapped_column(String(8), default="lan")

    # AI assignment.
    ai_model_id:       Mapped[int | None] = mapped_column(ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True)
    ai_enabled:        Mapped[bool]  = mapped_column(Boolean, default=True)
    inference_fps:     Mapped[int]   = mapped_column(Integer, default=5)

    # Multi-store retail rollout: optional formal Store FK. Cameras
    # without a store still work via the legacy `site` text field.
    store_id:          Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)

    # Health.
    status:            Mapped[str]   = mapped_column(String(16), default="pending")
    last_seen_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error:        Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    nvr = relationship("NVRDevice", back_populates="cameras")
    detection_configs = relationship("DetectionConfig", back_populates="camera", cascade="all, delete-orphan")
    zones = relationship("Zone", back_populates="camera", cascade="all, delete-orphan")
    events = relationship("DetectionEvent", back_populates="camera", cascade="all, delete-orphan")
