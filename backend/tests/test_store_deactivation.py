from datetime import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.stores import (
    create_store, delete_store, list_stores, store_deactivation_impact,
    update_store,
)
from app.database import Base
from app.models import Camera, GovernanceAuditLog, OdooStoreMap, Shift, Store
from app.schemas.store import StoreIn


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _store(db, *, name="Test Store", active=True):
    store = Store(
        name=name,
        code=name.upper().replace(" ", "-")[:32],
        country="Kenya",
        timezone="Africa/Nairobi",
        is_active=active,
    )
    db.add(store)
    db.flush()
    return store


def test_delete_route_soft_deactivates_and_retains_related_history(db):
    store = _store(db)
    shift = Shift(
        store_id=store.id,
        name="opening",
        day_of_week=0,
        start_time=time(9, 30),
        end_time=time(17, 30),
    )
    db.add(shift)
    db.commit()

    actor = SimpleNamespace(id=None, email="admin@vivo.example")
    result = delete_store(store.id, db=db, _u=actor)

    assert result == {
        "deactivated": store.id,
        "already_inactive": False,
        "historical_data_retained": True,
    }
    retained = db.get(Store, store.id)
    assert retained is not None
    assert retained.is_active is False
    assert db.query(Shift).filter_by(store_id=store.id).count() == 1
    audit = db.query(GovernanceAuditLog).one()
    assert audit.actor_email == "admin@vivo.example"
    assert audit.action == "store.deactivated"
    assert audit.entity_id == str(store.id)
    assert audit.details["historical_data_retained"] is True


def test_deactivation_is_blocked_while_camera_is_linked(db):
    store = _store(db)
    camera = Camera(
        name="Store Channel 1",
        site=store.name,
        brand="dahua",
        connection_type="nvr_dahua",
        host="127.0.0.1",
        store_id=store.id,
    )
    db.add(camera)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        delete_store(store.id, db=db, _u=object())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["blockers"] == {
        "linked_cameras": 1,
        "odoo_store_mappings": 0,
    }
    assert db.get(Store, store.id).is_active is True
    assert db.get(Camera, camera.id).store_id == store.id
    assert db.query(GovernanceAuditLog).count() == 0


def test_deactivation_impact_is_a_read_only_preflight(db):
    store = _store(db)
    db.add(Camera(
        name="Store Channel 1",
        site=store.name,
        brand="dahua",
        connection_type="nvr_dahua",
        host="127.0.0.1",
        store_id=store.id,
    ))
    db.commit()

    impact = store_deactivation_impact(store.id, db=db, _u=object())

    assert impact["can_deactivate"] is False
    assert impact["blockers"] == {
        "linked_cameras": 1,
        "odoo_store_mappings": 0,
    }
    assert impact["historical_data_will_be_retained"] is True
    assert db.get(Store, store.id).is_active is True
    assert db.query(GovernanceAuditLog).count() == 0


def test_deactivation_is_blocked_while_odoo_mapping_is_linked(db):
    store = _store(db)
    db.add(OdooStoreMap(
        store_id=store.id,
        odoo_model="pos.config",
        odoo_res_id=123,
        odoo_pos_config_id=123,
        code="POS-123",
        name="Test Store POS",
        timezone="Africa/Nairobi",
    ))
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        delete_store(store.id, db=db, _u=object())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["blockers"] == {
        "linked_cameras": 0,
        "odoo_store_mappings": 1,
    }
    assert db.get(Store, store.id).is_active is True


def test_patch_cannot_bypass_deactivation_dependency_guard(db):
    store = _store(db)
    db.add(Camera(
        name="Store Channel 1",
        site=store.name,
        brand="dahua",
        connection_type="nvr_dahua",
        host="127.0.0.1",
        store_id=store.id,
    ))
    db.commit()

    patch = StoreIn(name=store.name, country=store.country, is_active=False)
    with pytest.raises(HTTPException) as exc_info:
        update_store(store.id, patch, db=db, _u=object())

    assert exc_info.value.status_code == 409
    assert db.get(Store, store.id).is_active is True


def test_repeated_deactivation_is_idempotent(db):
    store = _store(db, active=False)
    db.commit()

    result = delete_store(store.id, db=db, _u=object())

    assert result == {
        "deactivated": store.id,
        "already_inactive": True,
        "historical_data_retained": True,
    }
    assert db.get(Store, store.id).is_active is False


def test_inactive_stores_are_hidden_from_operational_list_by_default(db):
    active = _store(db, name="Active Store")
    inactive = _store(db, name="Retired Store", active=False)
    db.commit()

    default_rows = list_stores(db=db, _u=object())
    audit_rows = list_stores(include_inactive=True, db=db, _u=object())

    assert [row.id for row in default_rows] == [active.id]
    assert {row.id for row in audit_rows} == {active.id, inactive.id}


def test_create_rejects_case_and_whitespace_duplicate_name(db):
    _store(db, name="Vivo Sarit")
    db.commit()

    payload = StoreIn(name="  vivo sarit  ", code="OTHER", country="Kenya")
    with pytest.raises(HTTPException) as exc_info:
        create_store(payload, db=db, _u=object())

    assert exc_info.value.status_code == 409
    assert "normalising" in exc_info.value.detail
    assert db.query(Store).count() == 1


def test_create_rejects_normalised_duplicate_code_but_allows_distinct_name(db):
    _store(db, name="Vivo Sarit")
    db.commit()

    duplicate_code = StoreIn(
        name="Safari Sarit", code="  vivo-sarit  ", country="Kenya")
    with pytest.raises(HTTPException) as exc_info:
        create_store(duplicate_code, db=db, _u=object())
    assert exc_info.value.status_code == 409

    distinct = StoreIn(name="  Safari Sarit  ", code="SAFARI", country="Kenya")
    created = create_store(distinct, db=db, _u=object())
    assert created.name == "Safari Sarit"
    assert created.code == "SAFARI"
