"""SQLAlchemy engine, session factory, and declarative Base.

Synchronous engine (psycopg v3). FastAPI dependency `get_db` yields a
session and ensures cleanup. Alembic imports `Base` to autogenerate
migrations.
"""
from collections.abc import Iterator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base. All ORM models inherit from this."""


# Pool sized for a single API container; bump for high-RPS deployments.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session, guarantee close-on-exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
