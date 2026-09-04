"""CSV-backed store for canonical calendar events (stdlib only).

One row per release, keyed on ``event_key``. Re-merging a month overwrites its
rows, so revised actuals and late-posted forecasts are picked up simply by
fetching that month again.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from .schema import CSV_COLUMNS, CanonicalEvent


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return repr(round(v, 6))
    return str(v)


class CalendarStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def merge(self, events: Iterable[CanonicalEvent]) -> dict:
        rows: dict[str, dict] = {r["event_key"]: r for r in self.load()}
        before = len(rows)

        seen = 0
        for ev in events:
            seen += 1
            rows[ev.event_key] = {k: _fmt(v) for k, v in ev.to_row().items()}

        ordered = sorted(rows.values(), key=lambda r: (r["datetime_utc"], r["event_key"]))
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(ordered)

        added = len(rows) - before
        return {"seen": seen, "added": added, "refreshed": seen - added, "total": len(rows)}
