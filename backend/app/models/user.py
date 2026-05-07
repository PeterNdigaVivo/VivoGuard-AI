"""User accounts (single-tenant, JWT auth)."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id:           Mapped[int]      = mapped_column(primary_key=True)
    email:        Mapped[str]      = mapped_column(String(255), unique=True, index=True)
    password_hash:Mapped[str]      = mapped_column(String(255))
    full_name:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Roles: "admin" (full), "operator" (acks alerts, edits cameras), "viewer".
    role:         Mapped[str]      = mapped_column(String(32), default="viewer")
    is_active:    Mapped[bool]     = mapped_column(Boolean, default=True)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at:Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
