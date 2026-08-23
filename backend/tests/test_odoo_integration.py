from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.models import (
    AssuranceCase, Camera, DetectionEvent, MetricSnapshot, OdooPosActivityBucket,
    OdooPosSession, OdooRosterWindow, OdooStoreMap, OdooStoreSalesHourly,
    OdooSyncState, OdooTillConflict, Store,
    StoreBusinessHours, Zone,
)
from app.operations.odoo_assurance import (
    compute_conversion_metrics, compute_till_conflicts, create_changing_room_reviews,
    effective_business_hours, pseudonymise_employee, roster_advisory,
)
from app.tasks.odoo_sync import _sync_sales, sync_store_master
from app.utils.business_hours import is_open_with_default


def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_store(db: Session, *, name: str = "Vivo Kigali") -> Store:
    store = Store(name=name, country="Rwanda", timezone="Africa/Kigali",
                  business_hours_json={"sun": []})
    db.add(store)
    db.flush()
    return store


def add_camera(db: Session, store: Store) -> Camera:
    camera = Camera(name="Channel 1", site=store.name, brand="dahua",
                    connection_type="lan_rtsp", host="127.0.0.1", store_id=store.id)
    db.add(camera)
    db.flush()
    return camera


def test_feature_flag_defaults_off_and_does_not_call_network(monkeypatch):
    monkeypatch.setattr(settings, "odoo_sync_enabled", False)
    assert sync_store_master() == {"status": "disabled", "stream": "store_master"}


def test_hours_precedence_fresh_odoo_then_manual_then_store():
    with db_session() as db:
        store = add_store(db)
        monday = datetime(2026, 8, 24, 7, 30, tzinfo=timezone.utc)  # 09:30 Kigali
        db.add(StoreBusinessHours(store_id=store.id, day_of_week=0,
                                  open_time=time(8), close_time=time(18), source="manual",
                                  synced_at=monday - timedelta(days=10)))
        db.add(StoreBusinessHours(store_id=store.id, day_of_week=0,
                                  open_time=time(9), close_time=time(20), source="odoo",
                                  synced_at=monday - timedelta(hours=1)))
        db.commit()
        hours, source = effective_business_hours(db, store, monday)
        assert source == "odoo"
        assert is_open_with_default(hours, monday.astimezone())

        odoo = db.query(StoreBusinessHours).filter_by(source="odoo").one()
        odoo.synced_at = monday - timedelta(hours=49)
        db.commit()
        hours, source = effective_business_hours(db, store, monday)
        assert source == "manual"
        assert hours == {"mon": ["08:00-18:00"]}


def test_sunday_closed_and_unmapped_default_are_safe():
    with db_session() as db:
        store = add_store(db)
        sunday = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
        hours, source = effective_business_hours(db, store, sunday)
        assert source == "store"
        assert not is_open_with_default(hours, sunday)
        store.business_hours_json = None
        hours, source = effective_business_hours(db, store, sunday)
        assert source == "default" and hours is None


def test_roster_is_opaque_advisory_and_never_suppresses():
    with db_session() as db:
        store = add_store(db)
        at = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
        opaque = pseudonymise_employee(123, "deployment-secret")
        assert opaque != "123" and len(opaque) == 64
        db.add(OdooRosterWindow(store_id=store.id, work_day=at.date(),
                                employee_ref=opaque, shift_start=at - timedelta(hours=1),
                                shift_end=at + timedelta(hours=1), synced_at=at))
        db.commit()
        advice = roster_advisory(db, store.id, at)
        assert advice["expected_staff_window"] == "expected"
        assert advice["alert_suppressed"] is False


def test_till_conflict_keeps_both_signal_times():
    with db_session() as db:
        store = add_store(db)
        camera = add_camera(db, store)
        opened = datetime(2026, 8, 23, 6, tzinfo=timezone.utc)
        db.add(DetectionEvent(camera_id=camera.id, detection_type="shop_open_close",
                              confidence=1, bbox_json=[0, 0, 1, 1],
                              timestamp=opened, extra={"rule": "shop_opened"}))
        db.add(OdooPosSession(odoo_session_id=77, store_id=store.id,
                              odoo_config_id=11, state="opened",
                              opened_at=opened + timedelta(hours=2)))
        db.commit()
        assert compute_till_conflicts(db, date(2026, 8, 23)) == 1
        db.commit()
        rows = db.query(OdooTillConflict).all()
        opening = next(row for row in rows if row.conflict_type == "opening_signal_conflict")
        assert opening.camera_event_at is not None and opening.till_event_at is not None


def test_conversion_over_sixty_percent_is_data_quality_flag_not_alert():
    with db_session() as db:
        store = add_store(db)
        hour = datetime(2026, 8, 23, 9, tzinfo=timezone.utc)
        db.add(OdooStoreSalesHourly(store_id=store.id, period_start=hour,
                                    transaction_count=7, amount_total=1000))
        db.add(MetricSnapshot(store_id=store.id, metric_type="unique_visitors",
                              period_start=hour, period_end=hour + timedelta(hours=1), value=10))
        db.commit()
        assert compute_conversion_metrics(db, hour, maximum=0.60) == 1
        db.commit()
        from app.models import OdooConversionMetric
        row = db.query(OdooConversionMetric).one()
        assert row.conversion_rate == 0.7
        assert row.data_quality_flag is True


def test_changing_room_no_sale_creates_neutral_review_but_sale_does_not():
    now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    with db_session() as db:
        store = add_store(db)
        camera = add_camera(db, store)
        zone = Zone(camera_id=camera.id, name="Changing-room exit", shape="line",
                    polygon_coords_json=[[0, 0], [1, 1]],
                    detection_types_json=["entry_exit", "changing_room"])
        db.add(zone); db.flush()
        event = DetectionEvent(camera_id=camera.id, zone_id=zone.id,
                               detection_type="entry_exit", confidence=.9,
                               bbox_json=[0, 0, 1, 1], timestamp=now - timedelta(minutes=20),
                               extra={"direction": "out"})
        db.add(event); db.commit()
        assert create_changing_room_reviews(db, now, grace_minutes=15) == 1
        db.commit()
        case = db.query(AssuranceCase).filter_by(case_type="changing_room_review").one()
        assert case.evidence["pos_status"] == "pos_unverified"
        assert "not evidence of theft" in case.description

    with db_session() as db:
        store = add_store(db)
        camera = add_camera(db, store)
        zone = Zone(camera_id=camera.id, name="Changing-room exit", shape="line",
                    polygon_coords_json=[[0, 0], [1, 1]],
                    detection_types_json=["entry_exit", "changing_room"])
        db.add(zone); db.flush()
        db.add(DetectionEvent(camera_id=camera.id, zone_id=zone.id,
                              detection_type="entry_exit", confidence=.9,
                              bbox_json=[0, 0, 1, 1], timestamp=now - timedelta(minutes=20),
                              extra={"direction": "out"}))
        db.add(OdooPosActivityBucket(
            store_id=store.id,
            period_start=(now - timedelta(minutes=15)).replace(second=0, microsecond=0),
            transaction_count=1, synced_at=now - timedelta(minutes=1)))
        db.commit()
        assert create_changing_room_reviews(db, now, grace_minutes=15) == 0
        assert db.query(AssuranceCase).count() == 0


def test_sales_sync_reconciles_changed_hour_instead_of_double_counting(monkeypatch):
    ordered_at = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)

    class Client:
        calls = 0

        def search_read(self, model, domain, fields):
            self.calls += 1
            if "write_date" in fields:
                return [{"config_id": [90, "Junction"],
                         "date_order": ordered_at.strftime("%Y-%m-%d %H:%M:%S"),
                         "write_date": ordered_at.strftime("%Y-%m-%d %H:%M:%S")}]
            return [
                {"config_id": [90, "Junction"],
                 "date_order": ordered_at.strftime("%Y-%m-%d %H:%M:%S"),
                 "amount_total": 100},
                {"config_id": [90, "Junction"],
                 "date_order": (ordered_at + timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S"),
                 "amount_total": 200},
            ]

    with db_session() as db:
        store = add_store(db, name="Vivo Junction")
        db.add(OdooStoreMap(store_id=store.id, odoo_model="pos.config",
                            odoo_res_id=90, odoo_pos_config_id=90,
                            name="Junction", timezone="Africa/Nairobi"))
        state = OdooSyncState(stream="sales")
        db.add(state); db.commit()
        monkeypatch.setattr(settings, "odoo_txn_minutes", 15)
        first = _sync_sales(db, Client(), state)
        db.commit()
        assert first["orders_reconciled"] == 2
        hourly = db.query(OdooStoreSalesHourly).one()
        buckets = db.query(OdooPosActivityBucket).all()
        assert hourly.transaction_count == 2 and hourly.amount_total == 300
        assert len(buckets) == 2
        assert sum(bucket.transaction_count for bucket in buckets) == 2

        # A second incremental pass replaces the hour aggregate; it does not
        # add the same two orders again.
        _sync_sales(db, Client(), state)
        db.commit()
        assert db.query(OdooStoreSalesHourly).one().transaction_count == 2
