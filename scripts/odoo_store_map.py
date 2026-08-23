#!/usr/bin/env python3
"""Export or apply the governed Odoo-to-VivoGuard store mapping CSV."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.integrations.odoo_client import client_from_settings
from app.models import OdooStoreMap, Store
from sqlalchemy import func

FIELDS = (
    "vivoguard_store_name", "odoo_model", "odoo_res_id", "odoo_pos_config_id",
    "odoo_code", "odoo_name", "timezone",
)


def m2o_id(value) -> int | None:  # type: ignore[no-untyped-def]
    return int(value[0]) if isinstance(value, (list, tuple)) and value else None


def store_timezone(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in {"acacia", "oasis"}:
        return "Africa/Kampala"
    if "kigali" in lowered:
        return "Africa/Kigali"
    return "Africa/Nairobi"


def export_csv(path: Path) -> int:
    client = client_from_settings(settings)
    configs = client.search_read(
        "pos.config", [("active", "=", True)], ["id", "name", "warehouse_id", "company_id"])
    warehouse_ids = sorted({m2o_id(row.get("warehouse_id")) for row in configs} - {None})
    warehouses = client.search_read(
        "stock.warehouse", [("id", "in", warehouse_ids)], ["id", "name", "code"])
    wh_by_id = {int(row["id"]): row for row in warehouses}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for config in sorted(configs, key=lambda row: str(row.get("name") or "")):
            warehouse_id = m2o_id(config.get("warehouse_id"))
            warehouse = wh_by_id.get(warehouse_id or -1, {})
            # Vivo's Kenya POS configurations mostly point at the shared
            # finished-goods warehouse, so warehouse_id is not a valid store
            # key. The active pos.config is the authoritative till/location
            # dimension; the linked warehouse code remains useful metadata.
            name = str(config.get("name") or "")
            writer.writerow({
                "vivoguard_store_name": name,
                "odoo_model": "pos.config",
                "odoo_res_id": config["id"],
                "odoo_pos_config_id": config["id"],
                "odoo_code": warehouse.get("code") or "",
                "odoo_name": name,
                "timezone": store_timezone(name),
            })
    return len(configs)


def import_csv(path: Path, *, dry_run: bool) -> tuple[int, list[str]]:
    applied = 0
    errors: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as handle, SessionLocal() as db:
        for line, item in enumerate(csv.DictReader(handle), start=2):
            store_name = (item.get("vivoguard_store_name") or "").strip()
            stores = (db.query(Store)
                      .filter(func.trim(Store.name) == store_name)
                      .limit(2).all())
            if not stores:
                errors.append(f"line {line}: VivoGuard store not found: {store_name}")
                continue
            if len(stores) > 1:
                errors.append(f"line {line}: ambiguous VivoGuard store name: {store_name}")
                continue
            store = stores[0]
            try:
                res_id = int(item["odoo_res_id"])
                config_id = int(item["odoo_pos_config_id"]) if item.get("odoo_pos_config_id") else None
            except (TypeError, ValueError):
                errors.append(f"line {line}: invalid Odoo identifier")
                continue
            row = db.query(OdooStoreMap).filter_by(store_id=store.id).one_or_none()
            if row is None:
                row = OdooStoreMap(store_id=store.id, odoo_model="pos.config",
                                   odoo_res_id=res_id, name=item["odoo_name"])
                db.add(row)
            row.odoo_model = item.get("odoo_model") or "pos.config"
            row.odoo_res_id = res_id
            row.odoo_pos_config_id = config_id
            row.code = item.get("odoo_code") or None
            row.name = item["odoo_name"]
            row.timezone = item.get("timezone") or store.timezone or "Africa/Nairobi"
            applied += 1
        if not dry_run and not errors:
            db.commit()
        else:
            db.rollback()
    return applied, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("path", type=Path)
    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("path", type=Path)
    apply_cmd.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        print(f"exported={export_csv(args.path)}")
        return 0
    applied, errors = import_csv(args.path, dry_run=args.dry_run)
    print(f"validated={applied} errors={len(errors)} dry_run={args.dry_run}")
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
