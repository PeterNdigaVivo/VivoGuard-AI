"""Process-wide logging setup. Imported once from main.py."""
import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout, force=True)
    # Quiet down third-party chatter.
    for noisy in ("urllib3", "asyncio", "sqlalchemy.engine", "ultralytics"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
