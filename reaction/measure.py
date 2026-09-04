"""Per-release reaction metrics for the 6B tape around a scheduled US release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from .tape import PIP, minute_bars, window

HORIZONS = (1, 5, 15, 30, 60)  # minutes after t0 to measure the move


@dataclass(slots=True)
class Reaction:
    t0_anchor: str            # calendar-derived release time (NY local)
    t0: str                   # detected release instant (NY local)
    t0_offset_min: float      # detected - anchor, minutes
    t0_source: str            # "spike" (snapped to volume burst) | "anchor" (no clear burst)
    spike_trades: int         # trades in the busiest minute
    baseline_trades: float    # median trades/min in the quiet pre-window
    reaction_ratio: float     # spike_trades / baseline_trades

    baseline_px: float        # median trade price in [t0-10m, t0)
    move_pips: dict           # {horizon_min: signed pips vs baseline}
    mfe_pips: float           # max favourable excursion vs baseline, [t0, t0+30]
    mae_pips: float           # max adverse excursion vs baseline, [t0, t0+30]
    range5_pips: float        # high-low in [t0, t0+5]
    pre_range5_pips: float    # median rolling 5-min high-low in the quiet pre-window
    range_ratio: float        # range5 / pre_range5
    direction: int            # sign(move at +15m)


def detect_t0(trades: pd.DataFrame, anchor: datetime):
    """Snap to the busiest minute within +/-75 min of the calendar anchor.

    Falls back to the anchor itself when no minute clearly stands out (the
    calendar time is then used as-is and flagged).
    """
    w = window(trades, anchor, 90, 90)
    if w.empty:
        return anchor, "anchor", 0, float("nan"), float("nan")

    per_min = w["price"].resample("1min").count()
    quiet = per_min[(per_min.index < anchor - pd.Timedelta(minutes=15)) |
                    (per_min.index > anchor + pd.Timedelta(minutes=45))]
    baseline = float(quiet.median()) if len(quiet) else float(per_min.median())

    cand = per_min[(per_min.index >= anchor - pd.Timedelta(minutes=75)) &
                   (per_min.index <= anchor + pd.Timedelta(minutes=75))]
    ratio = (cand.max() / baseline) if (len(cand) and baseline) else float("nan")
    if cand.empty or cand.max() < max(5.0 * baseline, baseline + 20):
        return anchor, "anchor", int(cand.max()) if len(cand) else 0, baseline, ratio
    t0 = cand.idxmax().to_pydatetime()
    return t0, "spike", int(cand.max()), baseline, ratio


def measure(trades: pd.DataFrame | None, anchor: datetime) -> Reaction | None:
    if trades is None or trades.empty:
        return None

    t0, src, spike, baseline_tr, ratio = detect_t0(trades, anchor)

    pre = window(trades, t0, 10, 0)["price"]
    pre = pre.iloc[:-1] if len(pre) > 1 else pre  # drop the t0 tick itself
    if pre.empty:
        return None
    baseline_px = float(pre.median())

    def px_at(minutes: int) -> float:
        seg = trades.loc[: t0 + pd.Timedelta(minutes=minutes), "price"]
        return float(seg.iloc[-1]) if len(seg) else float("nan")

    move = {h: (px_at(h) - baseline_px) / PIP for h in HORIZONS}

    post30 = window(trades, t0, 0, 30)["price"]
    mfe = (post30.max() - baseline_px) / PIP if len(post30) else float("nan")
    mae = (post30.min() - baseline_px) / PIP if len(post30) else float("nan")

    post5 = window(trades, t0, 0, 5)["price"]
    range5 = (post5.max() - post5.min()) / PIP if len(post5) else float("nan")

    pre_bars = minute_bars(window(trades, t0, 35, 0))
    if len(pre_bars) >= 6:
        roll = ((pre_bars["high"] - pre_bars["low"]).rolling(5).sum().dropna()) / PIP
        pre_range5 = float(roll.median()) if len(roll) else float("nan")
    else:
        pre_range5 = float("nan")

    def r1(x):
        return round(x, 1) if x == x else float("nan")

    return Reaction(
        t0_anchor=anchor.isoformat(sep=" "),
        t0=t0.isoformat(sep=" "),
        t0_offset_min=round((t0 - anchor).total_seconds() / 60, 1),
        t0_source=src,
        spike_trades=spike,
        baseline_trades=r1(baseline_tr),
        reaction_ratio=r1(ratio),
        baseline_px=round(baseline_px, 5),
        move_pips={h: r1(v) for h, v in move.items()},
        mfe_pips=r1(mfe),
        mae_pips=r1(mae),
        range5_pips=r1(range5),
        pre_range5_pips=r1(pre_range5),
        range_ratio=(r1(range5 / pre_range5) if pre_range5 == pre_range5 and pre_range5 else float("nan")),
        direction=int(np.sign(move[15])) if move[15] == move[15] else 0,
    )
