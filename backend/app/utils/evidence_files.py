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
