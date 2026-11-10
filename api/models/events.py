from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from api.models.availability import RegistrationAvailability

Source = Literal[
    "luma", "eventbrite", "meetup", "partiful",
    "cerebral_valley", "ny_tech", "shack15", "betaworks",
    "founders_inc", "a16z", "yc", "twitter", "other",
]

Category = Literal[
    "conference", "meetup", "hackathon", "workshop",
    "panel", "social", "demo", "pitch", "other",
]

ClassifyStatus = Literal["unclassified", "classified", "classify_failed"]
AttendanceMode = Literal["in-person", "online", "mixed"]


class RawEvent(BaseModel):
    """What a scraper returns."""
    source: Source
    url: str
    title: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue: str | None = None
    neighborhood: str | None = None
    description: str | None = None
    host: str | None = None
    cover_image: str | None = None
    raw_html: str | None = None
    attendance_mode: AttendanceMode | None = None
    registration_url: str | None = None
    registration_availability: RegistrationAvailability = "unknown"
    availability_evidence: str | None = None
    availability_checked_at: datetime | None = None


class ClassifiedEvent(BaseModel):
    """Classifier output per event. `id` is the row id in our DB."""
    id: int
    ai_relevance: int = Field(ge=0, le=10)
    category: Category
    tags: list[str] = Field(default_factory=list, max_length=5)
    summary: str = Field(max_length=200)
    is_nyc: bool


class Event(BaseModel):
    """Full row as stored."""
    id: int | None = None
    source: Source
    url: str
    title: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue: str | None = None
    neighborhood: str | None = None
    description: str | None = None
    host: str | None = None
    cover_image: str | None = None
    ai_relevance: int | None = None
    category: Category | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    is_nyc: bool | None = None
    classify_status: ClassifyStatus = "unclassified"
    first_seen: datetime | None = None
    last_seen: datetime | None = None
