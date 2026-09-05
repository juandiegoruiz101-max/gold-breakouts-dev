"""Cross-currency 'double surprise' scanner.

The biggest forex moves usually aren't one currency's data surprising -- they
are two currencies releasing at the *exact same instant* with surprises that
reinforce the same pair (e.g. a hot US NFP + a soft Canada jobs report, both
at 12:30 UTC, both pushing USD/CAD up). This scans the full ForexFactory day
(every currency, not just USD) and flags those clusters.

Modes:
    python3 -m movers.scan brief   -- today's schedule of high/medium events,
                                       grouped by simultaneous release time
    python3 -m movers.scan check   -- re-fetch today, print clusters released
                                       in the last LOOKBACK_MIN minutes

`check` is meant to be run repeatedly (e.g. every 30 min from a cron) by an
agent that has the PushNotification tool: if it prints any "MOVER:" line,
notify the user with that line; if it prints "NOTHING NEW", stay silent.
Deliberately stateless -- no "already notified" file -- so it works the same
from a fresh cloud checkout as it does locally; LOOKBACK_MIN just needs to be
a bit longer than the cron interval so nothing falls in the gap between runs.
"""

from __future__ import annotations

import argparse
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from calendar_pipeline.schema import CanonicalEvent
from calendar_pipeline.sources.forexfactory import ForexFactorySource

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "01 Data" / "raw" / "forexfactory"

MTY = ZoneInfo("America/Monterrey")  # fixed UTC-6, no DST since Mexico's 2022 reform
MIN_IMPACT = ("high", "medium")
STRENGTH_MIN = 0.15   # ignore near-in-line prints
CLUSTER_MIN = 0.35    # combined |strength_i - strength_j| needed to flag a pair
LOOKBACK_MIN = 40     # keep above the check cadence (30 min) so nothing is missed


def _bw_sign(ev: CanonicalEvent) -> int:
    return {0: 0, 1: 1, 2: -1}.get(ev.source_better_worse, 0)


def _strength(ev: CanonicalEvent) -> float:
    """Signed, currency-relative surprise size. 0 = in line / no data."""
    sign = _bw_sign(ev)
    if sign == 0:
        return 0.0
    mag = abs(ev.surprise_pct) if ev.surprise_pct is not None else 0.25
    return sign * min(mag, 3.0)


def fetch_today() -> list[CanonicalEvent]:
    now = datetime.now(timezone.utc)
    src = ForexFactorySource(RAW_DIR)
    raw = src.fetch_day_raw(now.year, now.month, now.day, refresh=True)
    return [src.to_canonical(e) for e in raw if e.get("currency")]


def clusters(events: list[CanonicalEvent]) -> dict[str, list[CanonicalEvent]]:
    groups: dict[str, list[CanonicalEvent]] = {}
    for e in events:
        if e.impact not in MIN_IMPACT:
            continue
        groups.setdefault(e.datetime_utc, []).append(e)
    return groups


def compound_pairs(evs: list[CanonicalEvent]) -> list[tuple[CanonicalEvent, CanonicalEvent, float]]:
    """Pairs of distinct-currency events in one cluster, ranked by how hard they
    reinforce a move in that currency pair (both released, opposite/strong signs)."""
    released = [e for e in evs if e.actual_raw]
    scored = [(e, _strength(e)) for e in released if abs(_strength(e)) >= STRENGTH_MIN]
    out = []
    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            ei, si = scored[i]
            ej, sj = scored[j]
            if ei.currency == ej.currency:
                continue
            diff = si - sj
            if abs(diff) >= CLUSTER_MIN:
                out.append((ei, ej, diff))
    out.sort(key=lambda t: -abs(t[2]))
    return out


def notify(message: str, *, title: str = "Forex") -> None:
    """Push straight to ntfy.sh -- no cloud round-trip, no Claude session needed.
    No-op unless NTFY_TOPIC is set (kept out of the repo; passed by the local
    launchd wrapper)."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        print(f"ntfy send failed: {exc}")


def describe_pair(ei: CanonicalEvent, ej: CanonicalEvent, diff: float) -> str:
    strong, weak = (ei, ej) if diff > 0 else (ej, ei)
    pair = f"{strong.currency}/{weak.currency}"
    return (f"{pair} hacia ARRIBA -- {strong.currency} {strong.title}: {strong.actual_raw} "
            f"vs {strong.forecast_raw} esperado + {weak.currency} {weak.title}: {weak.actual_raw} "
            f"vs {weak.forecast_raw} esperado (mismo instante)")


def cmd_brief() -> None:
    events = fetch_today()
    groups = clusters(events)
    print(f"Calendario de hoy ({datetime.now(MTY):%Y-%m-%d}) -- impacto alto/medio, hora Monterrey\n")
    flagged = []
    for ts, evs in sorted(groups.items()):
        t_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        t_mty = t_utc.astimezone(MTY)
        cur = sorted({e.currency for e in evs})
        multi = len(cur) > 1
        flag = "  <-- varias monedas, vigilar este horario" if multi else ""
        print(f"{t_mty:%H:%M} MTY  {'/'.join(cur):<12} " + ", ".join(e.title for e in evs) + flag)
        if multi:
            flagged.append(f"{t_mty:%H:%M} {'/'.join(cur)}")

    summary = ("Hoy vigilar: " + "; ".join(flagged)) if flagged else "Hoy sin cruces de monedas -- dia tranquilo."
    notify(summary[:200], title="Forex - brief del dia")


def cmd_check() -> None:
    """Stateless on purpose: each run (incl. a fresh cloud checkout with no
    disk history) only reports clusters whose release fell in the last
    LOOKBACK_MIN minutes, instead of tracking "already notified" on disk."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=LOOKBACK_MIN)

    events = fetch_today()
    groups = clusters(events)
    found, seen_pairs = [], set()
    for ts, evs in groups.items():
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not (cutoff <= t <= now):
            continue
        for ei, ej, diff in compound_pairs(evs):
            key = tuple(sorted([ei.source_event_id, ej.source_event_id]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            found.append((ei, ej, diff))

    if not found:
        print("NOTHING NEW")
        return
    descriptions = []
    for ei, ej, diff in found:
        desc = describe_pair(ei, ej, diff)
        print("MOVER:", desc)
        descriptions.append(desc)
    notify(" | ".join(descriptions)[:200], title="Forex - movimiento grande")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="movers.scan", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["brief", "check"])
    args = ap.parse_args(argv)
    (cmd_brief if args.mode == "brief" else cmd_check)()


if __name__ == "__main__":
    main()
