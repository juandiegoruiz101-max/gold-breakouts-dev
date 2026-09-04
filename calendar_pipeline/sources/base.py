from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..schema import CanonicalEvent


class CalendarSource(ABC):
    """A provider of economic-calendar events, normalised to CanonicalEvent."""

    name: str

    @abstractmethod
    def iter_canonical(
        self, start: str, end: str, *, refresh: bool = False
    ) -> Iterator[CanonicalEvent]:
        """Yield events for the inclusive month range ``start`` .. ``end``.

        ``start`` / ``end`` are ``"YYYY-MM"`` strings. ``refresh`` bypasses any
        local cache so revised actuals / late-posted forecasts are picked up.
        """
