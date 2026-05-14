"""ORM model package — re-exports every model so Alembic and the rest of
the app can import them from one place."""
from app.models.user      import User
from app.models.camera    import Camera, NVRDevice, CONNECTION_TYPES, NETWORK_TYPES, CAMERA_STATUS
from app.models.detection import DetectionConfig, DETECTION_TYPES
from app.models.zone      import Zone
from app.models.event     import DetectionEvent
from app.models.alert     import Alert, ALERT_STATUSES
from app.models.ai_model  import AIModel
from app.models.training  import Dataset, TrainingImage, Annotation, TrainingJob
# Retail extension models (commit 0+ of the multi-store rollout).
from app.models.store     import Store, Shift
from app.models.metrics   import (
    MetricSnapshot, METRIC_TYPES, VisitorTrack, StockroomAccess, Campaign,
)
from app.models.scheduled import ScheduledReport, CustomerJourney

__all__ = [
    "User",
    "Camera", "NVRDevice", "CONNECTION_TYPES", "NETWORK_TYPES", "CAMERA_STATUS",
    "DetectionConfig", "DETECTION_TYPES",
    "Zone",
    "DetectionEvent",
    "Alert", "ALERT_STATUSES",
    "AIModel",
    "Dataset", "TrainingImage", "Annotation", "TrainingJob",
    # Retail extension.
    "Store", "Shift",
    "MetricSnapshot", "METRIC_TYPES", "VisitorTrack", "StockroomAccess", "Campaign",
    "ScheduledReport", "CustomerJourney",
]
