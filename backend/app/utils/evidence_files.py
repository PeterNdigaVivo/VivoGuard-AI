"""Shared checks for local evidence files."""
from pathlib import Path
from stat import S_ISREG


def evidence_file_available(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    try:
        info = Path(path).stat()
    except (OSError, ValueError):
        return False
    return S_ISREG(info.st_mode) and info.st_size > 0


def alert_clip_file(
    path: object, *, alert_id: int, recordings_dir: str,
) -> Path | None:
    """Resolve the clip written by the recorder for this alert ID."""
    if not isinstance(path, str) or not path:
        return None
    try:
        directory = (Path(recordings_dir) / "alert_clips").resolve()
        resolved = Path(path).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None
    if resolved == directory / f"{alert_id}.mp4" and evidence_file_available(str(resolved)):
        return resolved
    return None


def alert_snapshot_file(
    path: object, *, recordings_dir: str, filmstrip_alert_id: int | None = None,
) -> Path | None:
    """Resolve a thumbnail or an alert-indexed filmstrip from evidence storage."""
    if not isinstance(path, str) or not path:
        return None
    try:
        root = Path(recordings_dir).resolve()
        resolved = Path(path).resolve(strict=True)
        parts = resolved.relative_to(root).parts
    except (OSError, ValueError, RuntimeError):
        return None
    if resolved.suffix.lower() not in {".jpg", ".jpeg"}:
        return None

    if filmstrip_alert_id is None:
        matches = len(parts) >= 2 and parts[0] == "snapshots"
    else:
        # The checkout recorder and store filmstrips use different layouts,
        # but both put indexed evidence inside the owning alert's directory.
        matches = len(parts) == 4 and parts[2] == str(filmstrip_alert_id) and (
            parts[:2] == ("checkout_snaps", "alerts")
            or (parts[0] in {"alert_snaps", "after_hours_snaps"} and parts[1].isdigit())
        )
    return resolved if matches and evidence_file_available(str(resolved)) else None
