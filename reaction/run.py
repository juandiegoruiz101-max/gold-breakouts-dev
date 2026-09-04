"""Join the economic calendar to the 6B tape and measure each release's reaction.

    python3 -m reaction.run                       # CPI PPI NFP FOMC PCE
    python3 -m reaction.run --categories CPI NFP
    python3 -m reaction.run --charts              # also write an event-study PNG

Thesis check (USD leg): a surprise that is strong-for-USD (better_worse=1)
should push 6B = GBP/USD **down**; weak-for-USD (better_worse=2) should push it
**up**. `thesis` column marks match / miss on the +15 min move.
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
from pathlib import Path

from .measure import HORIZONS, measure
from .tape import TAPE_END, TAPE_START, load_session, utc_to_ny_naive

REPO = Path(__file__).resolve().parent.parent
CAL = REPO / "01 Data" / "calendar" / "calendar_events.csv"
OUT = REPO / "01 Data" / "reaction" / "6b_reactions.csv"
CHART = REPO / "01 Data" / "reaction" / "6b_event_study.png"

DEFAULT_CATS = ["CPI", "PPI", "NFP", "FOMC", "PCE"]


def _abs_surprise_pct(r: dict) -> float:
    try:
        return abs(float(r["surprise_pct"]))
    except (ValueError, KeyError):
        return -1.0


def pick_driver(rows: list[dict]) -> dict:
    """Among the sub-series of one release, the row carrying the surprise signal."""
    headline = [r for r in rows
                if r["title"].lower().endswith("m/m") and "core" not in r["title"].lower()]
    return max(headline or rows, key=_abs_surprise_pct)


def load_events(categories: set[str]):
    rows = list(csv.DictReader(CAL.open()))
    keep = [r for r in rows
            if r["currency"] == "USD" and r["impact"] == "high"
            and r["category"] in categories
            and TAPE_START <= r["datetime_utc"][:10] <= TAPE_END]
    groups: dict[tuple, list] = {}
    for r in keep:
        groups.setdefault((r["datetime_utc"], r["category"]), []).append(r)
    return [(key, pick_driver(rs), rs) for key, rs in sorted(groups.items())]


def thesis_match(better_worse: str, direction: int) -> str:
    if better_worse == "1":   # strong-for-USD  -> 6B down
        return "match" if direction < 0 else "miss"
    if better_worse == "2":   # weak-for-USD    -> 6B up
        return "match" if direction > 0 else "miss"
    return "n/a"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="reaction.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--categories", nargs="+", default=DEFAULT_CATS)
    ap.add_argument("--charts", action="store_true")
    args = ap.parse_args(argv)

    events = load_events(set(args.categories))
    print(f"{len(events)} releases in the 6B window ({TAPE_START} .. {TAPE_END})\n")

    out_rows, measured = [], []
    for (dt_utc, cat), drv, _subs in events:
        anchor = utc_to_ny_naive(dt_utc)
        trades = load_session(anchor.date())
        r = measure(trades, anchor)
        if r is None:
            print(f"  {dt_utc}  {cat:<5}  -- no tape for {anchor.date()}")
            continue
        verdict = thesis_match(drv["source_better_worse"], r.direction)
        out_rows.append({
            "datetime_utc": dt_utc, "category": cat, "title": drv["title"],
            "actual": drv["actual_raw"], "forecast": drv["forecast_raw"],
            "surprise": drv["surprise"], "surprise_pct": drv["surprise_pct"],
            "better_worse": drv["source_better_worse"],
            "t0_ny": r.t0, "t0_offset_min": r.t0_offset_min, "t0_source": r.t0_source,
            "reaction_ratio": r.reaction_ratio,
            "range5_pips": r.range5_pips, "range_ratio": r.range_ratio,
            **{f"move_{h}m_pips": r.move_pips[h] for h in HORIZONS},
            "mfe_pips": r.mfe_pips, "mae_pips": r.mae_pips,
            "direction": r.direction, "thesis": verdict,
        })
        measured.append((drv, r, verdict))
        flag = "*" if r.t0_source == "spike" else "?"
        print(f"  {dt_utc}  {cat:<5} {drv['title'][:22]:<22} "
              f"surp={str(drv['surprise'] or '-'):>6} bw={drv['source_better_worse']}  "
              f"t0{flag}{r.t0_offset_min:+.0f}m x{r.reaction_ratio}  "
              f"move +5m={r.move_pips[5]:+6.1f}  +30m={r.move_pips[30]:+6.1f}  {verdict}")

    if not out_rows:
        print("nothing measured")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\n-> {OUT}  ({len(out_rows)} rows)")

    _summary(out_rows)
    if args.charts:
        _charts(measured)


def _summary(rows: list[dict]) -> None:
    graded = [r for r in rows if r["thesis"] in ("match", "miss")]
    print("\n--- summary ---")
    if graded:
        hits = sum(r["thesis"] == "match" for r in graded)
        print(f"directional hit-rate (real surprises, +15m): {hits}/{len(graded)} = {hits / len(graded):.0%}")
        by_cat: dict[str, list] = {}
        for r in graded:
            by_cat.setdefault(r["category"], []).append(r["thesis"] == "match")
        for c, v in sorted(by_cat.items()):
            print(f"    {c:<5} {sum(v)}/{len(v)}")

    surp = [abs(r["move_30m_pips"]) for r in graded if r["move_30m_pips"] == r["move_30m_pips"]]
    flat = [abs(r["move_30m_pips"]) for r in rows
            if r["better_worse"] == "0" and r["move_30m_pips"] == r["move_30m_pips"]]
    if surp:
        line = f"median |move +30m|  -- surprises: {st.median(surp):.1f} pips"
        if flat:
            line += f"   in-line: {st.median(flat):.1f} pips"
        print(line)
    rr = [r["range_ratio"] for r in rows if r["range_ratio"] == r["range_ratio"]]
    if rr:
        print(f"median 5-min range vs pre-release: x{st.median(rr):.1f}")


def _charts(measured) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from .tape import PIP, load_session, window

    from datetime import datetime as _dt

    items = [(d, r, v) for d, r, v in measured if v in ("match", "miss")]
    n = len(items)
    cols = 4
    rows_ = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows_, cols, figsize=(cols * 3.3, rows_ * 2.4), squeeze=False)
    flat_axes = [a for row in axes for a in row]

    for ax, (drv, r, verdict) in zip(flat_axes, items):
        t0 = _dt.fromisoformat(r.t0)
        trades = load_session(t0.date())
        w = window(trades, t0, 30, 60)
        px = (w["price"] - r.baseline_px) / PIP
        ax.plot(px.index, px.values, lw=0.8)
        ax.axvline(t0, color="k", lw=0.8, ls="--")
        ax.axhline(0, color="0.6", lw=0.5)
        colour = {"match": "tab:green", "miss": "tab:red"}.get(verdict, "tab:gray")
        ax.set_title(f"{drv['category']} {r.t0[:10]}\nsurp {drv['surprise'] or '-'} "
                     f"bw{drv['source_better_worse']} -> {r.move_pips[30]:+.0f}p",
                     fontsize=7, color=colour)
        ax.tick_params(labelsize=6)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))

    for ax in flat_axes[n:]:
        ax.axis("off")
    fig.suptitle("6B (GBP/USD) reaction vs US data surprise -- price in pips from pre-release baseline", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(CHART, dpi=130)
    print(f"-> {CHART}")


if __name__ == "__main__":
    main()
