"""FXStreet calendar source -- placeholder.

The user's preferred / manually-watched calendar is FXStreet (fxstreet.com).
Its official Calendar API (docs.fxstreet.com/api/calendar/) is OAuth2 and
sales-led: credentials come from emailing FXStreet sales, there is no
self-serve signup, and the old v4 API is deprecated.

Once access exists, implement ``iter_canonical`` here. Nothing downstream
(store, analysis, backtest) needs to change -- it only sees ``CanonicalEvent``.

Expected FXStreet -> CanonicalEvent mapping (from the public docs; verify on
access):

    id / eventId                  -> source_event_id
    <series / event-type id>      -> source_series_id
    dateUtc                       -> datetime_utc        (already UTC)
    countryCode                   -> country
    currencyCode                  -> currency
    name                          -> title  (+ categorize())
    volatility  (HIGH/MEDIUM/LOW) -> impact
    actual / consensus / previous -> actual / forecast / previous
    revised                       -> revised_previous
    unit / potency                -> unit
"""

from __future__ import annotations

from collections.abc import Iterator

from ..schema import CanonicalEvent
from .base import CalendarSource


class FXStreetSource(CalendarSource):
    name = "fxstreet"

    def __init__(self, *args, **kwargs):
        pass

    def iter_canonical(
        self, start: str, end: str, *, refresh: bool = False
    ) -> Iterator[CanonicalEvent]:
        raise NotImplementedError(
            "FXStreet API access is sales-led (OAuth2). Obtain credentials from "
            "FXStreet sales, then implement this source -- see the module docstring "
            "for the field mapping."
        )
