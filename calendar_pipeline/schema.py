"""Canonical, source-agnostic economic-calendar event schema.

Every calendar source (ForexFactory now, FXStreet later) normalises its rows
into `CanonicalEvent`. Analysis code should only ever see this shape, so a
source swap does not ripple past this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# value parsing
# --------------------------------------------------------------------------- #

_VALUE_RE = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)\s*([KkMmBbTt%]?)$")
_SUFFIX_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_value(raw: str | None) -> tuple[float | None, str | None]:
    """Parse a calendar value string into (face_number, unit).

    ``"3.4%"`` -> ``(3.4, "%")``   ``"175K"`` -> ``(175.0, "K")``
    ``"-40.9B"`` -> ``(-40.9, "B")``   ``""`` / unparseable -> ``(None, None)``

    The face number is kept as printed (not expanded) so the CSV stays
    readable; use :func:`to_absolute` when arithmetic must cross units.
    """
    if raw is None:
        return None, None
    s = raw.strip().replace(",", "").replace(" ", "").replace("\xa0", "")
    if not s or s == "-":
        return None, None
    m = _VALUE_RE.match(s)
    if not m:
        return None, None
    sign, digits, suffix = m.groups()
    val = float(digits) * (-1.0 if sign == "-" else 1.0)
    if suffix in ("", "%"):
        return val, (suffix or None)
    return val, suffix.upper()


def to_absolute(value: float | None, unit: str | None) -> float | None:
    """Expand a (face value, unit) pair to an absolute number for arithmetic."""
    if value is None:
        return None
    if unit in (None, "%"):
        return value
    return value * _SUFFIX_MULT[unit.lower()]


# --------------------------------------------------------------------------- #
# category bucketing  (a convenience for filtering, not an authoritative tag)
# --------------------------------------------------------------------------- #

_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ADP",                 ("adp",)),
    ("NFP",                 ("non-farm employment", "nonfarm employment", "non-farm payroll")),
    ("AVG_HOURLY_EARNINGS", ("average hourly earnings",)),
    ("UNEMPLOYMENT_RATE",   ("unemployment rate",)),
    ("JOBLESS_CLAIMS",      ("unemployment claims", "jobless claims")),
    ("CPI",                 ("cpi",)),
    ("PPI",                 ("ppi",)),
    ("PCE",                 ("pce",)),
    ("GDP",                 ("gdp",)),
    ("RETAIL_SALES",        ("retail sales",)),
    ("ISM_MFG",             ("ism manufacturing",)),
    ("ISM_SVC",             ("ism services", "ism non-manufacturing")),
    ("FOMC",                ("fomc", "federal funds rate", "fed chair", "fed interest rate")),
]


def categorize(title: str) -> str:
    t = title.lower()
    for cat, needles in _CATEGORY_RULES:
        if any(n in t for n in needles):
            return cat
    return "OTHER"


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #

CSV_COLUMNS = [
    "event_key", "source", "source_event_id", "source_series_id",
    "datetime_utc", "date_label", "time_label", "time_is_exact",
    "country", "currency", "title", "category", "impact",
    "actual", "forecast", "previous", "revised_previous", "unit",
    "actual_raw", "forecast_raw", "previous_raw", "revised_previous_raw",
    "surprise", "surprise_pct", "source_better_worse",
    "source_url", "fetched_at_utc",
]


@dataclass(slots=True)
class CanonicalEvent:
    # --- identity ---
    event_key: str            # globally unique, stable: "<source>:<id>"
    source: str               # "forexfactory"
    source_event_id: str      # id of this single release
    source_series_id: str     # id shared by every release of the same indicator

    # --- timing (always UTC) ---
    datetime_utc: str         # ISO8601 "....Z"
    date_label: str           # source's own day label, for eyeballing
    time_label: str           # "8:30am" / "All Day" / "Tentative"
    time_is_exact: bool       # False for All Day / Tentative / multi-day

    # --- classification ---
    country: str              # source's country code (FF: "US", "GE", "JN"...)
    currency: str             # "USD"
    title: str                # "CPI m/m"
    category: str             # CPI / PPI / NFP / FOMC / ... / OTHER  (see categorize)
    impact: str               # high / medium / low / holiday

    # --- values (face number + unit; raw strings kept alongside) ---
    actual: float | None
    forecast: float | None
    previous: float | None
    revised_previous: float | None
    unit: str | None          # "%" / "K" / "M" / "B" / "T" / None

    actual_raw: str
    forecast_raw: str
    previous_raw: str
    revised_previous_raw: str

    # --- surprise ---
    surprise: float | None        # actual - forecast (face units when they match)
    surprise_pct: float | None    # (actual - forecast) / |forecast|
    source_better_worse: int      # source's own surprise sign, currency-centric
                                  # (FF: 0 in-line, 1 better-for-USD, 2 worse-for-USD)

    # --- provenance ---
    source_url: str
    fetched_at_utc: str

    @staticmethod
    def compute_surprise(actual, a_unit, forecast, f_unit):
        a, f = to_absolute(actual, a_unit), to_absolute(forecast, f_unit)
        if a is None or f is None:
            return None, None
        surprise_pct = (a - f) / abs(f) if f != 0 else None
        if a_unit == f_unit and actual is not None and forecast is not None:
            return actual - forecast, surprise_pct
        return a - f, surprise_pct

    def to_row(self) -> dict:
        return {k: getattr(self, k) for k in CSV_COLUMNS}


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def iso_utc(dt_or_epoch) -> str:
    if isinstance(dt_or_epoch, (int, float)):
        dt = datetime.fromtimestamp(dt_or_epoch, tz=timezone.utc)
    else:
        dt = dt_or_epoch.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
