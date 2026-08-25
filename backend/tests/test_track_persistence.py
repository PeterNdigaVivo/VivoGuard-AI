from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.detectors.retail_p1 import _insert_unique_visitor
from app.database import Base
from app.models import StaffTrack, VisitorTrack


def test_unique_visitor_insert_is_idempotent_across_workers():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    values = {
        "store_id": 7,
        "camera_id": 11,
        "day": date(2026, 8, 25),
        "track_signature": "cam11:tr42",
        "global_person_id": None,
    }

    _insert_unique_visitor(db, **values)
    _insert_unique_visitor(db, **values)
    db.flush()

    assert db.query(VisitorTrack).count() == 1
    db.close()


def test_staff_source_accepts_operational_provenance_tags():
    assert StaffTrack.__table__.c.source.type.length == 32
    assert len("afterhours_present") <= StaffTrack.__table__.c.source.type.length
    assert len("uniform_dual_black") <= StaffTrack.__table__.c.source.type.length
