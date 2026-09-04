"""ForexFactory calendar source.

FF ships a whole month of events as a JS blob (``calendarComponentStates[1]``)
embedded in the page HTML. Each event carries an absolute UTC ``dateline``
epoch, so we read that blob rather than scraping the rendered table (which is
rendered in whatever timezone the session defaults to).

Cloudflare fronts forexfactory.com; ``cloudscraper`` clears the challenge with
no real browser. Raw pages are cached under ``01 Data/raw/forexfactory/`` so
re-runs do not re-hit the site.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Iterator
from pathlib import Path

import cloudscraper

from ..schema import CanonicalEvent, categorize, iso_utc, now_utc_iso, parse_value
from .base import CalendarSource

_MONTH_ABBR = ("jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec")

# FF impactName -> canonical impact
_IMPACT = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "holiday": "holiday",
    "non-economic": "holiday",
}


def month_range(start: str, end: str) -> Iterator[tuple[int, int]]:
    """Yield (year, month) for every month in the inclusive range 'YYYY-MM'..'YYYY-MM'."""
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class ForexFactorySource(CalendarSource):
    name = "forexfactory"
    BASE = "https://www.forexfactory.com/calendar"

    def __init__(self, cache_dir: Path, *, min_delay: float = 2.0, max_delay: float = 4.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_delay, self.max_delay = min_delay, max_delay
        self._scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )

    # ----------------------------------------------------------------- fetch

    def _month_url(self, year: int, month: int) -> str:
        return f"{self.BASE}?month={_MONTH_ABBR[month - 1]}.{year}"

    def _day_url(self, year: int, month: int, day: int) -> str:
        return f"{self.BASE}?day={_MONTH_ABBR[month - 1]}{day}.{year}"

    def _fetch_html(self, url: str, cache_path: Path, refresh: bool) -> str:
        if cache_path.exists() and not refresh:
            return cache_path.read_text(encoding="utf-8")

        last_err = None
        for attempt in range(3):
            time.sleep(random.uniform(self.min_delay, self.max_delay))
            try:
                resp = self._scraper.get(url, timeout=45)
                if resp.status_code == 200 and "calendarComponentStates[1]" in resp.text:
                    cache_path.write_text(resp.text, encoding="utf-8")
                    return resp.text
                last_err = f"status={resp.status_code} len={len(resp.text)}"
            except Exception as exc:  # network / cloudflare hiccup
                last_err = repr(exc)
            time.sleep(5 * (3 ** attempt))  # 5s, 15s, 45s
        raise RuntimeError(f"ForexFactory fetch failed for {url}: {last_err}")

    @staticmethod
    def _extract_days(html: str) -> list[dict]:
        anchor = html.find("calendarComponentStates[1] = {")
        if anchor == -1:
            raise ValueError("calendar state blob not found in page")
        arr_start = html.index("[", html.index("days:", anchor))
        depth = 0
        for i in range(arr_start, len(html)):
            c = html[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[arr_start : i + 1])
        raise ValueError("unterminated days array in calendar state blob")

    def fetch_month_raw(self, year: int, month: int, *, refresh: bool = False) -> list[dict]:
        """Raw FF event dicts for one calendar-month page (tags each with its URL)."""
        url = self._month_url(year, month)
        cache_path = self.cache_dir / f"month-{year:04d}-{month:02d}.html"
        html = self._fetch_html(url, cache_path, refresh)
        days = self._extract_days(html)
        return [dict(ev, _source_url=url) for day in days for ev in day["events"]]

    def fetch_day_raw(self, year: int, month: int, day: int, *, refresh: bool = True) -> list[dict]:
        """Raw FF event dicts for a single day. Defaults to no cache -- actuals
        keep being posted through the day, so a same-day scan should re-fetch."""
        url = self._day_url(year, month, day)
        cache_path = self.cache_dir / f"day-{year:04d}-{month:02d}-{day:02d}.html"
        html = self._fetch_html(url, cache_path, refresh)
        days = self._extract_days(html)
        return [dict(ev, _source_url=url) for d in days for ev in d["events"]]

    # ------------------------------------------------------------- normalise

    def to_canonical(self, ev: dict) -> CanonicalEvent:
        actual, a_unit = parse_value(ev.get("actual"))
        forecast, f_unit = parse_value(ev.get("forecast"))
        previous, p_unit = parse_value(ev.get("previous"))
        revised_previous, _ = parse_value(ev.get("revision"))
        surprise, surprise_pct = CanonicalEvent.compute_surprise(actual, a_unit, forecast, f_unit)

        title = (ev.get("name") or "").strip()
        time_label = ev.get("timeLabel") or ""
        exact = (
            not ev.get("timeMasked", False)
            and time_label not in ("", "All Day", "Tentative")
            and not time_label.lower().startswith("day ")
        )

        return CanonicalEvent(
            event_key=f"forexfactory:{ev['id']}",
            source=self.name,
            source_event_id=str(ev["id"]),
            source_series_id=str(ev.get("ebaseId", "")),
            datetime_utc=iso_utc(int(ev["dateline"])),
            date_label=ev.get("date", ""),
            time_label=time_label,
            time_is_exact=exact,
            country=ev.get("country", ""),
            currency=ev.get("currency", ""),
            title=title,
            category=categorize(title),
            impact=_IMPACT.get((ev.get("impactName") or "").lower(), ev.get("impactName") or ""),
            actual=actual,
            forecast=forecast,
            previous=previous,
            revised_previous=revised_previous,
            unit=a_unit or f_unit or p_unit,
            actual_raw=ev.get("actual") or "",
            forecast_raw=ev.get("forecast") or "",
            previous_raw=ev.get("previous") or "",
            revised_previous_raw=ev.get("revision") or "",
            surprise=surprise,
            surprise_pct=surprise_pct,
            source_better_worse=int(ev.get("actualBetterWorse", 0) or 0),
            source_url=ev.get("_source_url", self.BASE),
            fetched_at_utc=now_utc_iso(),
        )

    def iter_canonical(
        self, start: str, end: str, *, refresh: bool = False
    ) -> Iterator[CanonicalEvent]:
        for year, month in month_range(start, end):
            for ev in self.fetch_month_raw(year, month, refresh=refresh):
                yield self.to_canonical(ev)
