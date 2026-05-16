"""Pydantic schemas for stores, shifts, metrics, and campaigns."""
from __future__ import annotations
from datetime import date as date_t, datetime, time as time_t
from pydantic import BaseModel, ConfigDict, Field


class StoreIn(BaseModel):
    name: str
    code: str | None = None
    country: str
    city: str | None = None
    address: str | None = None
    timezone: str = "Africa/Nairobi"
    # {"mon":["09:00-20:00"], "sat":["09:00-21:00"], "sun":[]}
    business_hours_json: dict | None = None
    capacity: int | None = None
    manager_name:  str | None = None
    manager_phone: str | None = None
    is_active: bool = True


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: str | None
    country: str
    city: str | None
    address: str | None
    timezone: str
    business_hours_json: dict | None
    capacity: int | None
    manager_name:  str | None
    manager_phone: str | None
    is_active: bool
    created_at: datetime


class ShiftIn(BaseModel):
    store_id: int
    name: str
    day_of_week: int = Field(ge=0, le=6)
    start_time: time_t
    end_time: time_t
    is_active: bool = True


class ShiftOut(ShiftIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class MetricSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_id: int | None
    camera_id: int | None
    zone_id: int | None
    metric_type: str
    period_start: datetime
    period_end: datetime
    value: float
    dims: dict | None


class CampaignIn(BaseModel):
    name: str
    store_id: int | None = None
    start_date: date_t
    end_date: date_t
    description: str | None = None


class CampaignOut(CampaignIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
