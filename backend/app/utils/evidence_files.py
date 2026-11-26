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


def alert_clip_file(path: object, *, recordings_dir: str) -> Path | None:
    """Resolve a playable alert clip within the recorder's evidence directory."""
    if not isinstance(path, str) or not path:
        return None
    try:
        directory = (Path(recordings_dir) / "alert_clips").resolve()
        resolved = Path(path).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None
    if resolved.is_relative_to(directory) and evidence_file_available(str(resolved)):
        return resolved
    return None
