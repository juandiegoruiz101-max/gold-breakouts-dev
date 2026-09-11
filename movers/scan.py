"""ForexFactory calendar watcher.

Covers every high AND medium impact event (anything that can nudge price).
Two things, per run:
  1. a heads-up BEFORE a release ("UPCOMING") so there is time to get ready, and
  2. a direction call the moment a number lands off forecast ("MOVER"): which
     way the currency leans, up or down. Lines are tagged [alto] / [medio].

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
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from calendar_pipeline.schema import CanonicalEvent
from calendar_pipeline.sources.forexfactory import ForexFactorySource

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "01 Data" / "raw" / "forexfactory"

MTY = ZoneInfo("America/Monterrey")  # fixed UTC-6, no DST since Mexico's 2022 reform

WATCH_IMPACT = ("high", "medium")   # brief + watch both cover high AND medium
LEAD_MIN = 12       # "UPCOMING" fires for events releasing within this window
LOOKBACK_MIN = 8    # "MOVER" fires for releases at most this old (keep >= cron interval)

# stronger double-surprise cluster (bonus, higher-conviction tag)
STRENGTH_MIN = 0.15
CLUSTER_MIN = 0.35

# how the pair is quoted, to turn "currency strengthens" into an up/down lean
_INVERSE = {"EUR", "GBP", "AUD", "NZD"}   # quoted X/USD  -> X strong = pair up
_DIRECT = {"JPY", "CAD", "CHF"}           # quoted USD/X  -> X strong = pair down

# --- unscheduled news headlines (central-bank talk, geopolitics, ...) --------
# Financial Juice = a real breaking-headline wire (squawk style). FXStreet's
# feed is analyst opinion pieces published non-stop -> too noisy for alerts.
NEWS_FEEDS = (
    "https://www.financialjuice.com/feed.ashx?xy=rss",
)
NEWS_LOOKBACK_MIN = 15

# Claude reads each headline that passes the keyword filter and calls the
# direction -- this is what a keyword match alone can't do (see below).
_CLASSIFY_MODEL = "claude-haiku-4-5"
_CLASSIFY_TOOL = {
    "name": "classify_headline",
    "description": "Classify whether a financial news headline is relevant to "
                    "major FX pairs and which way it leans.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean",
                         "description": "true if this could move a major FX pair today"},
            "currency": {"type": "string",
                         "description": "the single currency most affected: USD, EUR, GBP, "
                                        "JPY, AUD, CAD, CHF or NZD; empty string if none fits"},
            "direction": {"type": "string", "enum": ["up", "down", "neutral"],
                          "description": "does that currency strengthen, weaken, or neither"},
            "reason": {"type": "string", "description": "why, in Spanish, under 12 words"},
        },
        "required": ["relevant", "currency", "direction", "reason"],
        "additionalProperties": False,
    },
    "strict": True,
}
NEWS_KEYWORDS = (
    "fed", "fomc", "powell", "boj", "bank of japan", "ueda", "ecb", "lagarde",
    "boe", "bank of england", "bailey", "rba", "bank of canada", "boc", "snb",
    "pboc", "rbnz", "central bank", "rate decision", "rate hike", "rate cut",
    "rate-cut", "hike rates", "cut rates", "basis point", "hawkish", "dovish",
    "tightening", "easing", "intervention", "yield", "quantitative",
    "cpi", "inflation", "ppi", "payroll", "nonfarm", "non-farm", "jobless",
    "unemployment", "gdp", "recession", "pmi", "retail sales", "jobs report",
    "tariff", "trade war", "sanction", "war", "attack", "strike", "missile",
    "drone", "escalation", "ceasefire", "invasion", "opec", "crude", "oil price",
    "shutdown", "debt ceiling", "default",
    "dollar", "euro", "yen", "sterling", "pound", "swiss franc", "aussie",
    "loonie", "kiwi", "yuan", "peso", "usd/", "eur/", "gbp/", "/jpy", "/usd",
    "greenback", "currency",
)


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


def clusters(events: list[CanonicalEvent], impacts=WATCH_IMPACT) -> dict[str, list[CanonicalEvent]]:
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


def _recent_ntfy_bodies(hours: int = 12) -> set[str]:
    """Recently-sent ntfy message bodies -- used to not resend the same headline."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return set()
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}/json?poll=1&since={hours}h")
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception:
        return set()
    bodies = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("event") == "message":
            bodies.add(m.get("message", ""))
    return bodies


def classify_headline(title: str) -> dict | None:
    """Ask Claude whether this headline matters for FX and which way it leans.
    Returns None if ANTHROPIC_API_KEY is unset or the call fails -- callers
    fall back to forwarding the raw headline (no direction) in that case."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_CLASSIFY_MODEL,
            max_tokens=256,
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_headline"},
            messages=[{
                "role": "user",
                "content": (
                    f"Financial news-wire headline: {title!r}\n\n"
                    "Is this likely to move a major FX pair today? If so, which "
                    "single currency is most affected, and does it strengthen "
                    "or weaken?"
                ),
            }],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "classify_headline":
                return block.input
    except Exception as exc:
        print(f"claude classify failed: {exc}")
    return None


def news_lines() -> list[str]:
    """Fresh market-moving headlines from the news feeds. Each one that passes
    the keyword pre-filter gets read by Claude, which drops it if it isn't
    really FX-relevant and adds a direction call when it is. Without
    ANTHROPIC_API_KEY, falls back to forwarding the plain headline."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=NEWS_LOOKBACK_MIN)
    already = _recent_ntfy_bodies()
    hits: list[tuple[datetime, str]] = []
    for url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
        except Exception as exc:
            print(f"news feed failed {url}: {exc}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").replace("FinancialJuice:", "").strip()
            try:
                t = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t < cutoff or not title:
                continue
            low = title.lower()
            if not any(k in low for k in NEWS_KEYWORDS):
                continue
            if any(title in b for b in already):
                continue
            hits.append((t, title))
    hits.sort()
    seen: set[str] = set()
    out = []
    for t, title in hits:
        if title in seen:
            continue
        seen.add(title)
        stamp = f"({t.astimezone(MTY):%H:%M} MTY)"

        verdict = classify_headline(title)
        if verdict is None:
            out.append(f"NOTICIA {stamp}: {title}")
            continue
        if not verdict.get("relevant"):
            continue  # Claude read it and it doesn't actually matter for FX
        cur, direction, reason = verdict.get("currency", ""), verdict.get("direction", "neutral"), verdict.get("reason", "")
        call = f" -> {lean(cur, direction == 'up')}" if cur and direction in ("up", "down") else ""
        out.append(f"NOTICIA {stamp}: {title} [{reason}]{call}")
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
    watched = [e for e in events if e.impact in WATCH_IMPACT]  # high AND medium

    lines: list[str] = []

    # 1) heads-up: events about to release
    for e in sorted(watched, key=lambda e: e.datetime_utc):
        if e.actual_raw:
            continue
        mins = (_ts(e.datetime_utc) - now).total_seconds() / 60
        if 0 < mins <= LEAD_MIN:
            others = sorted({x.currency for x in watched
                             if x.datetime_utc == e.datetime_utc and x.currency != e.currency})
            extra = f" (+ {', '.join(others)} al mismo tiempo)" if others else ""
            lines.append(f"UPCOMING: en ~{round(mins)} min "
                         f"({_ts(e.datetime_utc).astimezone(MTY):%H:%M} MTY) "
                         f"[{e.impact}] {e.currency} {e.title}{extra} -- preparate")

    # 2) direction: high-conviction double-surprise clusters first
    clustered_ids: set[str] = set()
    for ts, evs in clusters(events, impacts=WATCH_IMPACT).items():
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

    # 3) direction: single releases off forecast, grouped so the four CPI
    #    sub-series (m/m, y/y, core...) become one line, not four
    buckets: dict[tuple, list[CanonicalEvent]] = defaultdict(list)
    for e in watched:
        if not e.actual_raw or e.source_better_worse == 0 or e.source_event_id in clustered_ids:
            continue
        if recent <= _ts(e.datetime_utc) <= now:
            buckets[(e.currency, e.datetime_utc, e.source_better_worse)].append(e)

    for (cur, _dt, bw), evs in buckets.items():
        stronger = bw == 1
        imp = "alto" if any(e.impact == "high" for e in evs) else "medio"
        detail = "; ".join(f"{e.title} {e.actual_raw} vs {e.forecast_raw}" for e in evs[:3])
        lines.append(f"MOVER [{imp}]: {cur} ({detail}) {'mas fuerte' if stronger else 'mas debil'} "
                     f"de lo esperado -> {lean(cur, stronger)}")

    # 4) unscheduled headlines (central-bank talk, geopolitics, oil, ...)
    news = news_lines()

    if not lines and not news:
        print("NOTHING NEW")
        return

    for ln in lines[:8]:
        print(ln)
        title = "Forex - por salir" if ln.startswith("UPCOMING") else "Forex - direccion"
        notify(ln[:200], title=title)
    for ln in lines[8:]:
        print(ln)
    for ln in news[:6]:
        print(ln)
        notify(ln[:200], title="Forex - noticia")
    for ln in news[6:]:
        print(ln)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="movers.scan", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["brief", "watch", "check"])
    args = ap.parse_args(argv)
    cmd_brief() if args.mode == "brief" else cmd_watch()


if __name__ == "__main__":
    main()
