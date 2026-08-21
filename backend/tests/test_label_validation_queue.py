from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.labels import queue
from app.database import Base
from app.models import Alert, Camera, DetectionEvent, Store, User


def test_validation_queue_is_pinned_to_exact_camera_and_counts_legacy_clip():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    store = Store(name="Vivo Junction", country="Kenya")
    user = User(email="reviewer@vivo", password_hash="x", role="operator")
    db.add_all([store, user])
    db.flush()
    cameras = [
        Camera(name=f"Junction Ch{channel}", brand="dahua",
               connection_type="nvr_dahua", host="127.0.0.1",
               store_id=store.id)
        for channel in (1, 5)
    ]
    db.add_all(cameras)
    db.flush()
    for camera in cameras:
        event = DetectionEvent(
            camera_id=camera.id, detection_type="trespass", confidence=.8,
            bbox_json=[0, 0, 1, 1], timestamp=datetime.now(timezone.utc),
            extra={"alert_clip_path": f"/clips/{camera.id}.mp4"},
        )
        db.add(event)
        db.flush()
        db.add(Alert(event_id=event.id, status="new"))
    db.commit()

    rows = queue(
        db=db, _user=user, limit=20, detection_type="trespass",
        store_id=store.id, camera_id=cameras[1].id,
    )

    assert len(rows) == 1
    assert rows[0]["camera_id"] == cameras[1].id
    assert rows[0]["clip_available"] is True
    db.close()
