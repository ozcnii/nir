"""Published cards and the continuation of one verified listing page."""

from dataclasses import dataclass, field

from app.collection.domclick_normalization import DailyListingQuote


@dataclass(frozen=True)
class SearchPage:
    number: int
    filters: dict[str, object]
    urls: dict[str, str]
    next_url: str | None
    incomplete: tuple[str, ...]
    next_control: str | None = None
    quotes: dict[str, DailyListingQuote] = field(default_factory=dict)
    group_urls: tuple[str, ...] = ()
