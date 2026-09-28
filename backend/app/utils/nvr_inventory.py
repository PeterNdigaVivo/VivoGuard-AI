"""Validation helpers for credential-free NVR inventory CSV files."""
from __future__ import annotations

import csv
import ipaddress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InventoryRow:
    line: int
    store_name: str
    host: str
    rtsp_port: int
    http_port: int | None = None
    brand: str | None = None

    @property
    def port(self) -> int:
        """Backward-compatible alias for older inventory tooling."""
        return self.rtsp_port


def load_inventory(path: Path) -> list[InventoryRow]:
    required = {"store_name", "public_ip", "rtsp_port"}
    rows: list[InventoryRow] = []
    seen_stores: set[str] = set()
    seen_hosts: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"missing CSV columns: {', '.join(sorted(missing))}")
        for line, raw in enumerate(reader, start=2):
            store = (raw.get("store_name") or "").strip()
            host = (raw.get("public_ip") or "").strip()
            port_text = (raw.get("rtsp_port") or "").strip()
            http_text = (raw.get("http_port") or "").strip()
            brand = (raw.get("brand") or "").strip().casefold() or None
            if not store or not host or not port_text:
                raise ValueError(f"line {line}: store_name, public_ip and rtsp_port are required")
            ipaddress.ip_address(host)
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError(f"line {line}: invalid port {port}")
            store_key, host_key = store.casefold(), host.casefold()
            if store_key in seen_stores:
                raise ValueError(f"line {line}: duplicate store {store}")
            if host_key in seen_hosts:
                raise ValueError(f"line {line}: duplicate host {host}")
            seen_stores.add(store_key)
            seen_hosts.add(host_key)
            http_port = int(http_text) if http_text else None
            if http_port is not None and not 1 <= http_port <= 65535:
                raise ValueError(f"line {line}: invalid HTTP port {http_port}")
            if brand not in {None, "dahua", "hikvision"}:
                raise ValueError(f"line {line}: brand must be dahua or hikvision")
            rows.append(InventoryRow(line, store, host, port, http_port, brand))
    if not rows:
        raise ValueError("inventory is empty")
    return rows


def country_for(store_name: str) -> str:
    return "Rwanda" if "kigali" in store_name.casefold() else "Kenya"
