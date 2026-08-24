from datetime import datetime, timezone
import hashlib
import hmac
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.operations import (
    MissedEventIn, _verify_odoo_signature, report_missed_event,
)
from app.database import Base
from app.integrations.odoo_pos import normalise_odoo_event
from app.models import AssuranceCase, Camera, Store, Zone
from app.operations.assurance import (
    assess_coverage, create_alert_quality_cases, create_lone_worker_cases, risk_band,
    score_operational_event, store_is_open,
)


def test_pos_score_is_transparent_and_review_only():
    score, factors = score_operational_event("no_sale", 75_000,
                                             after_hours=True, camera_evidence=False)
    assert score == 0.9
    assert risk_band(score) == "high_review"
    assert {f["signal"] for f in factors} == {
        "no_sale", "high_value_amount", "outside_business_hours", "camera_evidence_unavailable"}


def test_store_hours_are_timezone_aware():
    store = SimpleNamespace(timezone="Africa/Nairobi",
                            business_hours_json={"fri": ["09:00-20:00"]})
    assert store_is_open(store, datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc))
    assert not store_is_open(store, datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc))


def test_odoo_normaliser_minimises_payload():
    result = normalise_odoo_event({"event": "pos.refund", "id": 42, "store_id": 3,
                                   "occurred_at": "2026-08-21T10:00:00Z", "amount_total": 2500,
                                   "employee_id": "EMP-7", "customer_name": "must-not-pass"})
    assert result["event_type"] == "refund"
    assert result["source_event_id"] == "42"
    assert "customer_name" not in result["payload"]


def test_odoo_webhook_signature_and_replay_window():
    raw = b'{"event":"pos.refund","id":42}'
    timestamp = "1787306400"
    secret = "test-service-secret"
    signature = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    now = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)

    _verify_odoo_signature(raw, timestamp, signature, secret=secret,
                           max_age_seconds=300, now=now)

    with pytest.raises(HTTPException, match="invalid Odoo webhook signature"):
        _verify_odoo_signature(raw, timestamp, "sha256=wrong", secret=secret,
                               max_age_seconds=300, now=now)
    with pytest.raises(HTTPException, match="expired Odoo webhook timestamp"):
        _verify_odoo_signature(
            raw, timestamp, signature, secret=secret, max_age_seconds=300,
            now=datetime.fromtimestamp(int(timestamp) + 301, tz=timezone.utc),
        )


def test_assurance_agents_resolve_cases_when_conditions_clear():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        coverage = AssuranceCase(
            dedup_key="coverage-config:999", case_type="coverage_gap",
            severity="critical", status="open", title="Old coverage gap",
        )
        lone_worker = AssuranceCase(
            dedup_key="lone-worker:999:2026-08-21", case_type="lone_worker",
            severity="high", status="open", title="Old lone-worker review",
        )
        alert_quality = AssuranceCase(
            dedup_key="alert-quality:999:evidence_missing", case_type="alert_quality",
            severity="high", status="open", title="Old alert-quality exception",
        )
        db.add_all([coverage, lone_worker, alert_quality])
        db.commit()

        assess_coverage(db, datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
                        persist=True)
        create_lone_worker_cases(
            db, datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))
        create_alert_quality_cases(
            db, datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))

        assert coverage.status == "resolved"
        assert coverage.resolved_at is not None
        assert lone_worker.status == "resolved"
        assert lone_worker.resolved_at is not None
        assert alert_quality.status == "resolved"
        assert alert_quality.resolved_at is not None


def test_missed_event_rejects_camera_from_another_store():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = Store(name="First", country="Kenya")
        second = Store(name="Second", country="Kenya")
        db.add_all([first, second])
        db.flush()
        camera = Camera(
            name="Second Channel 1", site="Second", brand="dahua",
            connection_type="nvr_dahua", host="127.0.0.1",
            store_id=second.id,
        )
        db.add(camera)
        db.commit()

        body = MissedEventIn(
            source_ref="manual-test-1", store_id=first.id,
            camera_id=camera.id,
            occurred_at=datetime.now(timezone.utc),
            report_text="A person entered the protected stockroom.",
            label="intrusion",
        )
        with pytest.raises(HTTPException, match="does not belong") as exc:
            report_missed_event(
                body, db=db,
                user=SimpleNamespace(id=1, email="reviewer@example.test"),
            )

        assert exc.value.status_code == 422
        assert db.query(AssuranceCase).count() == 0


def test_missed_event_without_identified_camera_stays_pending_investigation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(name="Unmapped Incident Store", country="Kenya")
        db.add(store)
        db.commit()

        result = report_missed_event(
            MissedEventIn(
                source_ref="manual-test-unmapped", store_id=store.id,
                occurred_at=datetime.now(timezone.utc),
                report_text="A stockroom incident had no corresponding alert.",
                label="stockroom_access",
            ),
            db=db,
            user=SimpleNamespace(id=1, email="reviewer@example.test"),
        )

        assert result["root_cause"] == "camera_unconfirmed"
        assert result["training_status"] == (
            "blocked_pending_camera_identification"
        )
        case = db.get(AssuranceCase, result["case_id"])
        assert case.camera_id is None
        assert case.root_cause == "camera_unconfirmed"


def test_missed_event_with_evidence_ignores_stale_current_camera_status():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(name="Evidence Store", country="Kenya")
        db.add(store)
        db.flush()
        camera = Camera(
            name="Evidence Camera", site="Evidence Store", brand="dahua",
            connection_type="nvr_dahua", host="127.0.0.1",
            status="offline", store_id=store.id,
        )
        camera.zones.append(Zone(
            name="Entrance", shape="polygon",
            polygon_coords_json=[[0, 0], [1, 0], [1, 1]],
            detection_types_json=["shutter"],
        ))
        db.add(camera)
        db.commit()

        result = report_missed_event(
            MissedEventIn(
                source_ref="evidence-backed-historical-miss",
                store_id=store.id,
                camera_id=camera.id,
                occurred_at=datetime.now(timezone.utc),
                report_text="A retained frame shows a missed shutter event.",
                label="shutter",
                evidence_path="/data/recordings/missed-events/frame.jpg",
            ),
            db=db,
            user=SimpleNamespace(id=1, email="reviewer@example.test"),
        )

        assert result["root_cause"] == "detector_false_negative"
        assert result["training_status"] == (
            "sample_created_pending_bbox_verification"
        )
