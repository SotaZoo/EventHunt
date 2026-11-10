from __future__ import annotations

from abc import ABC, abstractmethod

from api.models.events import RawEvent
from api.models.occurrences import provider_identity


class BaseScraper(ABC):
    name: str
    excluded_sources: frozenset[tuple[str, str]] = frozenset()

    def should_skip(self, url: str) -> bool:
        """Exclude persisted terminal source identifiers from detail-page visits."""
        return provider_identity(url) in self.excluded_sources

    @abstractmethod
    def scrape(self, limit: int = 50) -> list[RawEvent]:
        """Return raw events scraped from the source."""
        raise NotImplementedError
