"""Bulk-import NVR endpoints and materialise their discovered channels.

The inventory CSV contains no credentials. Passwords are collected with
``getpass`` at runtime, tried only against the detected vendor, encrypted
before persistence, and never written to the report.

Run inside the API image::

    python -m app.scripts.import_nvr_inventory \
        --inventory /tmp/nvr_inventory.csv --apply
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func

from app.connectors.dahua_http import DahuaHTTP
from app.connectors.hikvision_isapi import HikvisionISAPI
from app.connectors.nvr_manager import enumerate_dahua, enumerate_hikvision
from app.database import SessionLocal
from app.models import Camera, NVRDevice, Store
from app.utils.crypto import encrypt
from app.utils.nvr_inventory import InventoryRow, country_for, load_inventory


async def vendor_order(host: str, port: int) -> list[str]:
    """Probe vendor-specific paths without credentials to avoid lockouts."""
    paths = {
        "dahua": "/cgi-bin/magicBox.cgi?action=getDeviceType",
        "hikvision": "/ISAPI/System/deviceInfo",
    }
    scores = {name: 0 for name in paths}
    async with httpx.AsyncClient(timeout=5, verify=False, follow_redirects=True) as client:
        for name, path in paths.items():
            try:
                response = await client.get(f"http://{host}:{port}{path}")
                evidence = " ".join((
                    response.headers.get("server", ""),
                    response.headers.get("www-authenticate", ""),
                    response.text[:500],
                )).casefold()
                if response.status_code in {200, 401, 403}:
                    scores[name] += 1
                if name in evidence or (name == "dahua" and "realm=login" in evidence):
                    scores[name] += 3
            except (httpx.HTTPError, UnicodeError):
                continue
    # Stable fallback order keeps behaviour deterministic when probes are silent.
    return sorted(paths, key=lambda name: (-scores[name], name != "dahua"))


async def discover(row: InventoryRow, username: str, passwords: list[str]):
    errors: list[str] = []
    for brand in await vendor_order(row.host, row.port):
        for slot, password in enumerate(passwords, start=1):
            try:
                if brand == "dahua":
                    info = await DahuaHTTP(row.host, row.port, username, password).device_info()
                    known = {key.casefold() for key in info}
                    if not known.intersection({"devicetype", "deviceclass", "serialnumber", "hardwareversion"}):
                        raise RuntimeError("Dahua device signature missing")
                    channels = await enumerate_dahua(
                        row.host, row.port, row.port, username, password,
                    )
                else:
                    info = await HikvisionISAPI(row.host, row.port, username, password).device_info()
                    if not (info.get("model") or info.get("device_type")):
                        raise RuntimeError("Hikvision device signature missing")
                    channels = await enumerate_hikvision(
                        row.host, row.port, row.port, username, password,
                    )
                if not channels:
                    raise RuntimeError("device returned no channels")
                return brand, password, slot, channels
            except Exception as exc:  # endpoint/firmware failures vary by vendor
                errors.append(f"{brand}/credential-{slot}: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors))


def persist(row: InventoryRow, brand: str, username: str, password: str,
            channels, *, apply: bool) -> tuple[int, int]:
    """Return (created, existing); commit one NVR atomically when applying."""
    if not apply:
        return len(channels), 0
    with SessionLocal() as db:
        try:
            store = db.query(Store).filter(func.lower(Store.name) == row.store_name.lower()).first()
            if store is None:
                store = Store(
                    name=row.store_name,
                    country=country_for(row.store_name),
                    timezone="Africa/Nairobi",
                    default_rtsp_port=row.port,
                    is_active=True,
                )
                db.add(store)
                db.flush()
            else:
                store.default_rtsp_port = row.port

            encrypted = encrypt(password)
            nvr = db.query(NVRDevice).filter(
                NVRDevice.host == row.host,
                NVRDevice.http_port == row.port,
            ).first()
            if nvr is None:
                nvr = NVRDevice(
                    name=f"{row.store_name} NVR",
                    brand=brand,
                    host=row.host,
                    rtsp_port=row.port,
                    http_port=row.port,
                    username=username,
                    password_encrypted=encrypted,
                )
                db.add(nvr)
                db.flush()
            nvr.name = f"{row.store_name} NVR"
            nvr.brand = brand
            nvr.rtsp_port = row.port
            nvr.http_port = row.port
            nvr.username = username
            nvr.password_encrypted = encrypted
            nvr.total_channels = len(channels)
            nvr.connected_channels = len(channels)
            nvr.status = "online"
            nvr.last_seen_at = datetime.now(timezone.utc)

            existing = {
                camera.channel_number: camera
                for camera in db.query(Camera).filter(
                    Camera.nvr_id == nvr.id,
                    Camera.store_id == store.id,
                    Camera.is_deleted.is_(False),
                ).all()
                if camera.channel_number is not None
            }
            created = 0
            connection_type = "nvr_dahua" if brand == "dahua" else "nvr_hik"
            for channel in channels:
                camera = existing.get(channel.channel)
                if camera is None:
                    camera = Camera(
                        name=f"{row.store_name} - Channel {channel.channel}",
                        host=row.host,
                        channel_number=channel.channel,
                        nvr_id=nvr.id,
                        store_id=store.id,
                    )
                    db.add(camera)
                    created += 1
                camera.site = row.store_name
                camera.brand = brand
                camera.connection_type = connection_type
                camera.public_ip = row.host
                camera.rtsp_port = row.port
                camera.http_port = row.port
                camera.username = username
                camera.password_encrypted = encrypted
                camera.network_type = "wan"
                camera.rtsp_url_override = None
                camera.rtsp_transport = "http"
                camera.ai_enabled = True
                camera.inference_fps = 5
                camera.status = "pending"
            db.commit()
            return created, len(channels) - created
        except Exception:
            db.rollback()
            raise


def write_report(path: Path, results: list[dict]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["store_name", "public_ip", "port", "status", "brand",
              "channels", "created", "existing", "detail"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)


async def run(args) -> int:
    rows = load_inventory(args.inventory)
    primary = getpass.getpass("Primary NVR password: ")
    fallback = getpass.getpass("Fallback NVR password (Enter to skip): ")
    passwords = list(dict.fromkeys(value for value in (primary, fallback) if value))
    if not passwords:
        raise ValueError("at least one password is required")

    results: list[dict] = []
    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] {row.store_name} ({row.host}:{row.port})", flush=True)
        result = {
            "store_name": row.store_name, "public_ip": row.host, "port": row.port,
            "status": "failed", "brand": "", "channels": 0,
            "created": 0, "existing": 0, "detail": "",
        }
        try:
            brand, password, slot, channels = await discover(
                row, args.username, passwords,
            )
            created, existing = persist(
                row, brand, args.username, password, channels, apply=args.apply,
            )
            result.update(
                status="imported" if args.apply else "verified",
                brand=brand,
                channels=len(channels),
                created=created,
                existing=existing,
                detail=f"credential-{slot}",
            )
            print(f"  {brand}: {len(channels)} channel(s); created={created}, existing={existing}")
        except Exception as exc:
            result["detail"] = str(exc)[:300]
            print(f"  FAILED: {result['detail']}")
        results.append(result)
        write_report(args.report, results)

    ok = sum(result["status"] != "failed" for result in results)
    print(f"Completed: {ok}/{len(results)} NVRs succeeded. Report: {args.report}")
    return 0 if ok == len(results) else 2


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--report", type=Path,
                        default=Path("/data/datasets/nvr_import_report.csv"))
    parser.add_argument("--apply", action="store_true",
                        help="persist stores, NVRs and camera channels")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
