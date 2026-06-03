from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Depth = Literal["quick", "normal", "deep"]


@dataclass
class SearchResult:
    rank: int
    title: str
    url: str
    snippet: str
    page_age_days: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FetchHint:
    needs_login: bool
    cache_status: str  # "warm" | "cold"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnnotatedResult:
    rank: int
    title: str
    url: str
    snippet: str
    page_age_days: int | None
    fetch_hint: FetchHint

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "page_age_days": self.page_age_days,
            "fetch_hint": self.fetch_hint.to_dict(),
        }


DEPTH_DEFAULTS: dict[Depth, int] = {"quick": 5, "normal": 10, "deep": 20}
