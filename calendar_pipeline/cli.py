"""Command-line entry point for the calendar pipeline.

Run from the repo root:

    python3 -m calendar_pipeline.cli backfill --start 2015-01 --end 2026-09
    python3 -m calendar_pipeline.cli update
    python3 -m calendar_pipeline.cli show --currency USD --impact high --category CPI
    python3 -m calendar_pipeline.cli stats
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .sources.forexfactory import ForexFactorySource, month_range
from .store import CalendarStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "01 Data"
RAW_DIR = DATA_DIR / "raw" / "forexfactory"
STORE_PATH = DATA_DIR / "calendar" / "calendar_events.csv"


def _this_month() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def _print_table(rows: list[dict], cols: list[str], limit: int) -> None:
    rows = rows[-limit:]

    def cell(r, c):
        v = str(r.get(c, ""))
        return v if len(v) <= 40 else v[:37] + "..."

    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(cell(r, c)))
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(cell(r, c).ljust(widths[c]) for c in cols))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_backfill(args) -> None:
    src = ForexFactorySource(RAW_DIR)
    store = CalendarStore(STORE_PATH)
    end = args.end or _this_month()
    months = list(month_range(args.start, end))
    print(f"Backfill {args.start} .. {end}  ({len(months)} months) via {src.name}")

    events = []
    for i, (y, m) in enumerate(months, 1):
        raw = src.fetch_month_raw(y, m, refresh=args.refresh)
        canon = [src.to_canonical(ev) for ev in raw]
        events.extend(canon)
        usd_high = sum(1 for e in canon if e.currency == "USD" and e.impact == "high")
        print(f"  [{i:>3}/{len(months)}] {y}-{m:02d}: {len(canon):>3} events  ({usd_high} USD-high)")

    stats = store.merge(events)
    print(f"\nseen={stats['seen']}  added={stats['added']}  refreshed={stats['refreshed']}  total={stats['total']}")
    print(f"store: {STORE_PATH}")


def cmd_update(args) -> None:
    src = ForexFactorySource(RAW_DIR)
    store = CalendarStore(STORE_PATH)
    this = _this_month()
    months = [_prev_month(this), this]
    print(f"Update (force refresh): {', '.join(months)}")

    events = []
    for ym in months:
        y, m = map(int, ym.split("-"))
        canon = [src.to_canonical(ev) for ev in src.fetch_month_raw(y, m, refresh=True)]
        events.extend(canon)
        print(f"  {ym}: {len(canon)} events")

    stats = store.merge(events)
    print(f"seen={stats['seen']}  added={stats['added']}  refreshed={stats['refreshed']}  total={stats['total']}")


def cmd_show(args) -> None:
    rows = CalendarStore(STORE_PATH).load()
    if not rows:
        print("store is empty -- run `backfill` first")
        return
    if args.currency:
        rows = [r for r in rows if r["currency"] == args.currency]
    if args.impact:
        rows = [r for r in rows if r["impact"] == args.impact]
    if args.category:
        rows = [r for r in rows if r["category"] == args.category.upper()]

    cols = ["datetime_utc", "currency", "impact", "category", "title",
            "actual_raw", "forecast_raw", "previous_raw", "surprise", "source_better_worse"]
    _print_table(rows, cols, args.limit)
    print(f"\n{len(rows)} rows match")


def cmd_stats(args) -> None:
    rows = CalendarStore(STORE_PATH).load()
    if not rows:
        print("store is empty -- run `backfill` first")
        return
    dts = sorted(r["datetime_utc"] for r in rows)
    print(f"rows:       {len(rows)}")
    print(f"date range: {dts[0]}  ..  {dts[-1]}")

    print("\nby currency (top 12):")
    for cur, n in Counter(r["currency"] for r in rows).most_common(12):
        print(f"  {cur or '(blank)':<8} {n}")

    usd_high = [r for r in rows if r["currency"] == "USD" and r["impact"] == "high"]
    print(f"\nUSD high-impact events: {len(usd_high)}   by category:")
    for cat, n in Counter(r["category"] for r in usd_high).most_common():
        print(f"  {cat:<22} {n}")

    exact = sum(1 for r in usd_high if r["time_is_exact"] == "True")
    print(f"\nUSD high-impact with an exact release time: {exact} / {len(usd_high)}")


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="calendar_pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="fetch a month range and merge into the store")
    b.add_argument("--start", default="2015-01", help="YYYY-MM inclusive (default 2015-01)")
    b.add_argument("--end", default=None, help="YYYY-MM inclusive (default: current month)")
    b.add_argument("--refresh", action="store_true", help="bypass the raw HTML cache")
    b.set_defaults(func=cmd_backfill)

    u = sub.add_parser("update", help="re-fetch current + previous month for revisions")
    u.set_defaults(func=cmd_update)

    s = sub.add_parser("show", help="print rows from the store")
    s.add_argument("--currency")
    s.add_argument("--impact", choices=["high", "medium", "low", "holiday"])
    s.add_argument("--category")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_show)

    st = sub.add_parser("stats", help="coverage summary of the store")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
