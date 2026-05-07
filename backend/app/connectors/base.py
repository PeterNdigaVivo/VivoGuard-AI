"""Connector base classes and shared dataclasses.

A *connector* knows how to talk to one brand/connection_type. The system
uses connectors to:
  - validate that a host is alive and credentials work
  - enumerate channels (NVR case)
  - fetch a snapshot
  - construct an RTSP URL the streamer can hand to FFmpeg

Connectors are *stateless* — they take parameters per call so they're safe
to share across requests and don't leak SDK sessions.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CameraEndpoint:
    """Inputs needed to reach a single camera or NVR channel."""

    brand: str                    # "dahua" | "hikvision" | "onvif" | "generic"
    host: str                     # IP or DDNS hostname
    rtsp_port: int = 554
    http_port: int = 80
    username: Optional[str] = None
    password: Optional[str] = None
    channel: Optional[int] = None # NVR channel, 1-indexed
    rtsp_url_override: Optional[str] = None
    network_type: str = "lan"     # lan | wan | vpn


@dataclass
class ConnectionTestResult:
    """What `test_connection` returns to the API layer."""

    ok: bool
    rtsp_url: Optional[str] = None        # URL we'd actually stream
    snapshot_jpeg_b64: Optional[str] = None
    detected_channels: int = 0
    device_model: Optional[str] = None
    error: Optional[str] = None
    diagnostics: dict = field(default_factory=dict)
