"""ForexFactory calendar watcher.

Two things, per run:
  1. a heads-up BEFORE a high-impact release ("UPCOMING") so there is time to
     get ready, and
  2. a direction call the moment a high-impact number lands off forecast
     ("MOVER"): which way the currency leans, up or down.

Modes:
    python3 -m movers.scan brief   -- once a day: the full schedule for today
    python3 -m movers.scan watch   -- every ~5 min: UPCOMING + MOVER lines

Direction comes from ForexFactory's own actual-vs-forecast flag
(``actualBetterWorse``: 1 = stronger than expected for that currency,
2 = weaker), which already accounts for "lower is better" indicators like the
unemployment rate. This is a lean, not a guarantee -- small surprises are
noisier than big ones.

Stateless by design (no "already notified" file), so it behaves the same from
GitHub Actions, cron, or a local run. LOOKBACK_MIN just needs to stay >= the
cron interval so nothing slips through the gap between runs.
"""

from __future__ import annotations

import argparse
import os
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from calendar_pipeline.schema import CanonicalEvent
from calendar_pipeline.sources.forexfactory import ForexFactorySource

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "01 Data" / "raw" / "forexfactory"

MTY = ZoneInfo("America/Monterrey")  # fixed UTC-6, no DST since Mexico's 2022 reform

BRIEF_IMPACT = ("high", "medium")   # what the daily brief lists
LEAD_MIN = 12       # "UPCOMING" fires for high-impact events releasing within this window
LOOKBACK_MIN = 8    # "MOVER" fires for releases at most this old (keep >= cron interval)

# stronger double-surprise cluster (bonus, higher-conviction tag)
STRENGTH_MIN = 0.15
CLUSTER_MIN = 0.35

# how the pair is quoted, to turn "currency strengthens" into an up/down lean
_INVERSE = {"EUR", "GBP", "AUD", "NZD"}   # quoted X/USD  -> X strong = pair up
_DIRECT = {"JPY", "CAD", "CHF"}           # quoted USD/X  -> X strong = pair down


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _bw_sign(ev: CanonicalEvent) -> int:
    return {0: 0, 1: 1, 2: -1}.get(ev.source_better_worse, 0)


def _strength(ev: CanonicalEvent) -> float:
    sign = _bw_sign(ev)
    if sign == 0:
        return 0.0
    mag = abs(ev.surprise_pct) if ev.surprise_pct is not None else 0.25
    return sign * min(mag, 3.0)


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def lean(currency: str, stronger: bool) -> str:
    """Turn 'this currency came in stronger/weaker than forecast' into an up/down
    call on the common pairs."""
    up = "ARRIBA" if stronger else "ABAJO"
    inv = "ABAJO" if stronger else "ARRIBA"
    if currency == "USD":
        return f"USD se inclina {up} -> EUR/USD, GBP/USD y oro {inv}; USD/JPY, USD/CAD {up}"
    if currency in _INVERSE:
        return f"{currency}/USD se inclina {up}"
    if currency in _DIRECT:
        return f"USD/{currency} se inclina {inv}"
    return f"{currency} {'mas fuerte' if stronger else 'mas debil'} de lo esperado"


def fetch_today() -> list[CanonicalEvent]:
    now = datetime.now(timezone.utc)
    src = ForexFactorySource(RAW_DIR)
    raw = src.fetch_day_raw(now.year, now.month, now.day, refresh=True)
    return [src.to_canonical(e) for e in raw if e.get("currency")]


def clusters(events: list[CanonicalEvent], impacts=BRIEF_IMPACT) -> dict[str, list[CanonicalEvent]]:
    groups: dict[str, list[CanonicalEvent]] = {}
    for e in events:
        if e.impact in impacts:
            groups.setdefault(e.datetime_utc, []).append(e)
    return groups


def compound_pairs(evs: list[CanonicalEvent]):
    """Distinct-currency pairs in one cluster whose surprises reinforce a move in
    that pair -- the high-conviction 'double surprise' case."""
    released = [e for e in evs if e.actual_raw]
    scored = [(e, _strength(e)) for e in released if abs(_strength(e)) >= STRENGTH_MIN]
    out = []
    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            ei, si = scored[i]
            ej, sj = scored[j]
            if ei.currency != ej.currency and abs(si - sj) >= CLUSTER_MIN:
                out.append((ei, ej, si - sj))
    out.sort(key=lambda t: -abs(t[2]))
    return out


def notify(message: str, *, title: str = "Forex") -> None:
    """POST to ntfy.sh. No-op unless NTFY_TOPIC is set (kept out of the repo --
    it is a bearer secret; injected by the GitHub Actions secret / local env)."""
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


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #

def cmd_brief() -> None:
    events = fetch_today()
    groups = clusters(events)
    print(f"Calendario de hoy ({datetime.now(MTY):%Y-%m-%d}) -- impacto alto/medio, hora Monterrey\n")
    flagged = []
    for ts, evs in sorted(groups.items()):
        t_mty = _ts(ts).astimezone(MTY)
        cur = sorted({e.currency for e in evs})
        multi = len(cur) > 1
        mark = "  <-- varias monedas, vigilar este horario" if multi else ""
        print(f"{t_mty:%H:%M} MTY  {'/'.join(cur):<12} " + ", ".join(e.title for e in evs) + mark)
        if multi:
            flagged.append(f"{t_mty:%H:%M} {'/'.join(cur)}")

    summary = ("Hoy vigilar: " + "; ".join(flagged)) if flagged else "Hoy sin cruces de monedas -- dia tranquilo."
    notify(summary[:200], title="Forex - brief del dia")


def cmd_watch() -> None:
    now = datetime.now(timezone.utc)
    recent = now - timedelta(minutes=LOOKBACK_MIN)
    events = fetch_today()
    high = [e for e in events if e.impact == "high"]

    lines: list[str] = []

    # 1) heads-up: high-impact events about to release
    for e in sorted(high, key=lambda e: e.datetime_utc):
        if e.actual_raw:
            continue
        mins = (_ts(e.datetime_utc) - now).total_seconds() / 60
        if 0 < mins <= LEAD_MIN:
            others = sorted({x.currency for x in high
                             if x.datetime_utc == e.datetime_utc and x.currency != e.currency})
            extra = f" (+ {', '.join(others)} al mismo tiempo)" if others else ""
            lines.append(f"UPCOMING: en ~{round(mins)} min "
                         f"({_ts(e.datetime_utc).astimezone(MTY):%H:%M} MTY) "
                         f"{e.currency} {e.title}{extra} -- preparate")

    # 2) direction: high-conviction double-surprise clusters first
    clustered_ids: set[str] = set()
    for ts, evs in clusters(events, impacts=("high",)).items():
        if not (recent <= _ts(ts) <= now):
            continue
        for ei, ej, diff in compound_pairs(evs):
            strong, weak = (ei, ej) if diff > 0 else (ej, ei)
            clustered_ids.update([ei.source_event_id, ej.source_event_id])
            lines.append(
                f"MOVER (DOBLE): {strong.currency} {strong.title} {strong.actual_raw} vs "
                f"{strong.forecast_raw} + {weak.currency} {weak.title} {weak.actual_raw} vs "
                f"{weak.forecast_raw} -> {strong.currency}/{weak.currency} se inclina ARRIBA"
            )

    # 3) direction: single high-impact releases off forecast, grouped so the
    #    four CPI sub-series (m/m, y/y, core...) become one line, not four
    buckets: dict[tuple, list[CanonicalEvent]] = defaultdict(list)
    for e in high:
        if not e.actual_raw or e.source_better_worse == 0 or e.source_event_id in clustered_ids:
            continue
        if recent <= _ts(e.datetime_utc) <= now:
            buckets[(e.currency, e.datetime_utc, e.source_better_worse)].append(e)

    for (cur, _dt, bw), evs in buckets.items():
        stronger = bw == 1
        detail = "; ".join(f"{e.title} {e.actual_raw} vs {e.forecast_raw}" for e in evs[:3])
        lines.append(f"MOVER: {cur} ({detail}) {'mas fuerte' if stronger else 'mas debil'} "
                     f"de lo esperado -> {lean(cur, stronger)}")

    if not lines:
        print("NOTHING NEW")
        return
    for ln in lines[:6]:
        print(ln)
        title = "Forex - por salir" if ln.startswith("UPCOMING") else "Forex - direccion"
        notify(ln[:200], title=title)
    for ln in lines[6:]:
        print(ln)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="movers.scan", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["brief", "watch", "check"])
    args = ap.parse_args(argv)
    cmd_brief() if args.mode == "brief" else cmd_watch()


if __name__ == "__main__":
    main()
