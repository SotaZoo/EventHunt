"""Optional source-supported facts, separate from ranking and personal status."""
import json
import re
import warnings
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from dateutil.parser import ParserError, UnknownTimezoneWarning, isoparse, parse, parserinfo
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

AnnotationText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SourceEvidence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class _CompleteSourceTime(parserinfo):
    """Reject dateutil's implicit calendar/clock defaults before normalization."""

    # Preserve zone assertions until validation. Resolve aliases explicitly
    # through tzinfos instead of dateutil's implicit UTC/GMT rewriting.
    UTCZONE = ()

    def validate(self, res):
        if (any(getattr(res, field) is None for field in ("year", "month", "day", "hour"))
                or res.year < 100 or (res.tzname is None and res.tzoffset is None)):
            raise ParserError("source time requires an explicit year, date, hour and timezone")
        if res.tzname is not None and res.tzoffset is not None:
            # dateutil may reverse signed named-zone offsets or override one
            # timezone assertion with another. Do not choose between them.
            raise ParserError("source time has unsupported combined timezone assertions")
        if (res.weekday is not None
                and datetime(res.year, res.month, res.day).weekday() != res.weekday):
            raise ParserError("source weekday contradicts its calendar date")
        return super().validate(res)


def source_deadline(expression: str) -> datetime:
    """Normalize a complete source expression without a scrape/current-time anchor."""
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", expression):
        result = isoparse(expression)
        if result.tzinfo is None:
            raise ValueError("source time needs a timezone")
        return result
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnknownTimezoneWarning)
        try:
            # Precision below the stated hour defaults to zero, never the current clock.
            options = {"parserinfo": _CompleteSourceTime(), "default": datetime(2000, 1, 1),
                       "tzinfos": {"UTC": 0, "GMT": 0, "Z": 0, "EST": -18000, "EDT": -14400}}
            result = parse(expression, **options)
            alternative = parse(expression, dayfirst=True, **options)
        except (ValueError, OverflowError, UnknownTimezoneWarning) as exc:
            raise ValueError("source time is incomplete or unsupported") from exc
    if result.tzinfo is None or result != alternative:
        raise ValueError("source time is ambiguous")
    return result


class EventHighlight(BaseModel):
    """One concrete positive draw with its captured source evidence."""

    model_config = ConfigDict(extra="forbid")
    text: AnnotationText
    evidence: SourceEvidence


class EventUrgencyNote(BaseModel):
    """A registration deadline or scarcity observation, never event proximity."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["deadline", "scarcity"]
    text: AnnotationText
    evidence: SourceEvidence
    deadline_at: AwareDatetime | None = None
    deadline_evidence: SourceEvidence | None = None

    @model_validator(mode="after")
    def unambiguous_deadline(self) -> "EventUrgencyNote":
        """Require a usable instant for a deadline; scarcity carries no invented date."""
        if self.kind == "deadline":
            if self.deadline_evidence is None:
                raise ValueError("a deadline needs complete source time evidence")
            # The model selects evidence. The application owns the normalized fact.
            self.deadline_at = source_deadline(self.deadline_evidence)
            clock = self.deadline_at.strftime("%I:%M %p").lstrip("0").replace(":00", "")
            offset = self.deadline_at.strftime("%z")
            zone = self.deadline_at.tzname() or f"UTC{offset[:3]}:{offset[3:]}"
            self.text = (f"Register by {self.deadline_at:%b} {self.deadline_at.day}, "
                         f"{self.deadline_at.year} at {clock} {zone}")
        if self.kind == "scarcity" and (self.deadline_at is not None or self.deadline_evidence is not None):
            raise ValueError("scarcity must not invent a registration deadline")
        return self


Annotation = TypeVar("Annotation", EventHighlight, EventUrgencyNote)


def grounded_annotation(value: Any, model: type[Annotation], source: Mapping[str, Any]) -> Annotation | None:
    """Accept a bounded annotation only when its evidence occurs in a captured input."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = json.loads(value)
        annotation = model.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        return None
    quote = " ".join(annotation.evidence.split()).casefold()
    if isinstance(annotation, EventUrgencyNote) and annotation.kind == "deadline":
        time_quote = " ".join((annotation.deadline_evidence or "").split()).casefold()
        if time_quote not in quote:
            return None
    return annotation if any(
        quote in " ".join(str(source.get(field) or "").split()).casefold()
        for field in ("title", "description", "host", "venue")
    ) else None
