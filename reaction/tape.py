"""Load the 6B (GBP/USD futures) tick tape and slice windows around a timestamp.

Source: Databento L2 trades CSV, one file per session:

    ~/Desktop/Data Center/6B/L2 Trades/6B-YYYY-MM-DD trades.csv
    columns: ts_event, side (A=buy aggressor, B=sell aggressor, N=none), price, size

``ts_event`` is **US Eastern wall-clock** (America/New_York, DST-aware) -- verified
against NFP / CPI release spikes landing exactly on 08:30 local. Every timestamp
is kept as naive New-York local; calendar UTC anchors are converted the same way
(:func:`utc_to_ny_naive`), so there is no tz arithmetic downstream.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

TAPE_DIR = Path.home() / "Desktop" / "Data Center" / "6B" / "L2 Trades"
NY = ZoneInfo("America/New_York")
PIP = 0.0001  # GBP/USD: 1 pip = 0.0001 price units

TAPE_START = "2026-01-02"
TAPE_END = "2026-07-03"


def utc_to_ny_naive(iso_utc: str) -> datetime:
    """``'2026-03-11T12:30:00Z'`` -> naive datetime in New-York local time."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(NY).replace(tzinfo=None)


def session_path(session_date) -> Path:
    return TAPE_DIR / f"6B-{session_date:%Y-%m-%d} trades.csv"


def load_session(session_date) -> pd.DataFrame | None:
    """Trades for one session, indexed by naive NY timestamp. ``None`` if no file."""
    path = session_path(session_date)
    if not path.exists():
        return None
    df = pd.read_csv(path, usecols=["ts_event", "side", "price", "size"])
    df["ts"] = pd.to_datetime(df["ts_event"])  # already NY wall-clock, naive
    return df.drop(columns="ts_event").set_index("ts").sort_index()


def window(trades: pd.DataFrame, t0: datetime, before_min: int, after_min: int) -> pd.DataFrame:
    lo = t0 - pd.Timedelta(minutes=before_min)
    hi = t0 + pd.Timedelta(minutes=after_min)
    return trades.loc[lo:hi]


def minute_bars(trades: pd.DataFrame) -> pd.DataFrame:
    """1-minute OHLC + trade count + volume from tick trades."""
    px = trades["price"].resample("1min")
    bars = px.ohlc()
    bars["trades"] = px.count()
    bars["volume"] = trades["size"].resample("1min").sum()
    return bars.dropna(subset=["open"])
