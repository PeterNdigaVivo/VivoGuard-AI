from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Store


SCRIPT = Path(__file__).parents[2] / "scripts" / "odoo_store_map.py"
SPEC = importlib.util.spec_from_file_location("odoo_store_map", SCRIPT)
assert SPEC and SPEC.loader
odoo_store_map = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(odoo_store_map)


def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def write_mapping(path: Path, store_name: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=odoo_store_map.FIELDS)
        writer.writeheader()
        writer.writerow({
            "vivoguard_store_name": store_name,
            "odoo_model": "pos.config",
            "odoo_res_id": "97",
            "odoo_pos_config_id": "97",
            "odoo_code": "WHFIN",
            "odoo_name": "Mombasa CBD",
            "timezone": "Africa/Nairobi",
        })


def test_import_matches_legacy_store_name_with_outer_whitespace(tmp_path, monkeypatch) -> None:
    with db_session() as db:
        db.add(Store(name=" Vivo DIGO RD MSA ", country="Kenya",
                     timezone="Africa/Nairobi", business_hours_json={}))
        db.commit()
        monkeypatch.setattr(odoo_store_map, "SessionLocal", lambda: db)
        mapping = tmp_path / "mapping.csv"
        write_mapping(mapping, "Vivo DIGO RD MSA")

        applied, errors = odoo_store_map.import_csv(mapping, dry_run=True)

        assert applied == 1
        assert errors == []


def test_import_rejects_ambiguous_normalised_store_names(tmp_path, monkeypatch) -> None:
    with db_session() as db:
        db.add_all([
            Store(name="Vivo CBD", country="Kenya", timezone="Africa/Nairobi",
                  business_hours_json={}),
            Store(name=" Vivo CBD ", country="Kenya", timezone="Africa/Nairobi",
                  business_hours_json={}),
        ])
        db.commit()
        monkeypatch.setattr(odoo_store_map, "SessionLocal", lambda: db)
        mapping = tmp_path / "mapping.csv"
        write_mapping(mapping, "Vivo CBD")

        applied, errors = odoo_store_map.import_csv(mapping, dry_run=True)

        assert applied == 0
        assert errors == ["line 2: ambiguous VivoGuard store name: Vivo CBD"]

