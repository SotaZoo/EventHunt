from .base import BaseScraper
from .betaworks import BetaworksScraper
from .cerebral_valley import CerebralValleyScraper
from .eventbrite import EventbriteScraper
from .luma import LumaScraper
from .meetup import MeetupScraper
from .partiful import PartifulScraper

SCRAPERS = {
    "luma": LumaScraper,
    "eventbrite": EventbriteScraper,
    "meetup": MeetupScraper,
    "partiful": PartifulScraper,
    "betaworks": BetaworksScraper,
    "cerebral_valley": CerebralValleyScraper,
}

__all__ = [
    "SCRAPERS",
    "BaseScraper",
    "BetaworksScraper",
    "CerebralValleyScraper",
    "EventbriteScraper",
    "LumaScraper",
    "MeetupScraper",
    "PartifulScraper",
]
