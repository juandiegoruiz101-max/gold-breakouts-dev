"""ForexFactory calendar watcher.

Covers every high AND medium impact event (anything that can nudge price).
Two things, per run:
  1. a heads-up BEFORE a release ("UPCOMING") so there is time to get ready, and
  2. a direction call the moment a number lands off forecast ("MOVER"): which
     way the currency leans, up or down. Every line -- calendar or news -- is
     tagged [alto] / [medio] / [bajo], so it's always clear how much weight to
     put on the call (a technically on-topic headline that the market usually
     shrugs off, like a current-account print, still gets a direction call but
     tagged [bajo] instead of reading as confidently as a CPI/NFP release).

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
import html
import json
import os
import re
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
_IMPACT_ES = {"high": "alto", "medium": "medio", "low": "bajo"}
LEAD_MIN = 12       # "UPCOMING" fires for events releasing within this window
LOOKBACK_MIN = 8    # "MOVER" fires for releases at most this old (keep >= cron interval)

# stronger double-surprise cluster (bonus, higher-conviction tag)
STRENGTH_MIN = 0.15
CLUSTER_MIN = 0.35

# how the pair is quoted, to turn "currency strengthens" into an up/down lean
_INVERSE = {"EUR", "GBP", "AUD", "NZD"}   # quoted X/USD  -> X strong = pair up
_DIRECT = {"JPY", "CAD", "CHF"}           # quoted USD/X  -> X strong = pair down
_GOLD = {"XAU", "GOLD", "ORO"}            # gold: inverse of USD, or its own safe-haven call

# --- unscheduled news headlines (central-bank talk, geopolitics, ...) --------
# Financial Juice = a real breaking-headline wire (squawk style). FXStreet's
# feed is analyst opinion pieces published non-stop -> too noisy for alerts.
NEWS_FEEDS = (
    "https://www.financialjuice.com/feed.ashx?xy=rss",
)
NEWS_LOOKBACK_MIN = 25  # GH Actions cron can lag well past its 5-min schedule;
                        # a 25-min window survives that instead of silently
                        # dropping a headline that ages out before any run sees it

# Gemini (free tier) reads each headline that passes the keyword filter and
# calls the direction -- this is what a keyword match alone can't do.
_CLASSIFY_MODEL = "gemini-flash-lite-latest"
_CLASSIFY_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_CLASSIFY_MODEL}:generateContent"
_CLASSIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "relevant": {"type": "BOOLEAN",
                     "description": "true if this could move a major FX pair or gold today"},
        "currency": {"type": "STRING",
                     "description": "the single asset most affected: USD, EUR, GBP, JPY, "
                                    "AUD, CAD, CHF, NZD, or XAU for gold; empty string if "
                                    "none fits. Use XAU for safe-haven flows, real-yield "
                                    "moves, or Fed-policy headlines that mainly matter "
                                    "through gold rather than a specific FX pair"},
        "direction": {"type": "STRING", "enum": ["up", "down", "neutral"],
                      "description": "does that asset strengthen, weaken, or neither -- "
                                     "for XAU this means gold's own price, not USD"},
        "impact": {"type": "STRING", "enum": ["alto", "medio", "bajo"],
                   "description": "how much this can actually move price, same three tiers "
                                  "ForexFactory itself uses for scheduled events. 'alto' = "
                                  "CPI/NFP/PPI-tier, a central bank rate decision, a war "
                                  "outbreak or big military escalation -- traders drop what "
                                  "they're doing for this. 'medio' = genuinely market-moving "
                                  "but not decisive on its own (a central banker's remarks, "
                                  "a notable but not critical geopolitical update). 'bajo' = "
                                  "on-topic and technically relevant, but the kind of release "
                                  "the market historically shrugs off even when the textbook "
                                  "direction is clear -- e.g. a current-account balance, a "
                                  "minor revision, routine remarks with nothing new in them"},
        "reason": {"type": "STRING", "description": "why, in Spanish, under 12 words"},
    },
    "required": ["relevant", "currency", "direction", "impact", "reason"],
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
    "gold", "xau", "bullion", "safe haven", "safe-haven", "safehaven",
    "real yield", "treasury", "10-year", "10 year", "risk-off", "risk off",
)
# word-boundary match, not substring -- otherwise short keywords like "pmi" or
# "war" false-positive inside unrelated words ("DeepMind" contains "pmi",
# "software" contains "war"). Only the safety net for when Gemini is
# unavailable (no GEMINI_API_KEY, or the call fails), so worth getting right.
_NEWS_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in NEWS_KEYWORDS) + r")\b",
    re.IGNORECASE,
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


_NAMES = {
    "EUR": "el euro", "GBP": "la libra", "AUD": "el dolar australiano",
    "NZD": "el dolar neozelandes", "JPY": "el yen", "CAD": "el dolar canadiense",
    "CHF": "el franco suizo",
}


def lean(currency: str, stronger: bool) -> str:
    """Turn 'this currency came in stronger/weaker than forecast' into a plain
    Spanish sentence -- 'sube'/'baja', no arrows or symbols. Always describes
    the CURRENCY's own value (the natural reading of "el yen sube"), never the
    USD/X pair's chart direction -- those move opposite for JPY/CAD/CHF, and
    mixing the two conventions produced self-contradicting messages (Gemini's
    "reason" text saying "debilita al yen" right next to a "sube" call)."""
    sube = "sube" if stronger else "baja"
    baja = "baja" if stronger else "sube"
    bajan = "bajan" if stronger else "suben"
    if currency == "USD":
        return (f"el dolar {sube}, por eso el euro y la libra {bajan}, el oro {baja}, "
                f"y el yen y el dolar canadiense {bajan}")
    if currency in _GOLD:
        # gold quoted XAU/USD: "stronger" here means gold itself is stronger (bid)
        return f"el oro {sube}"
    if currency in _INVERSE or currency in _DIRECT:
        return f"{_NAMES[currency]} {sube}"
    return f"{currency} salio {'mas fuerte' if stronger else 'mas debil'} de lo esperado"


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
    """Ask Gemini whether this headline matters for FX and which way it leans.
    Returns None if GEMINI_API_KEY is unset or the call fails -- callers fall
    back to forwarding the raw headline (no direction) in that case."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    body = {
        "contents": [{"parts": [{"text": (
            f"Financial news-wire headline: {title!r}\n\n"
            "Is this likely to move a major FX pair or gold (XAU/USD) today? "
            "Gold trades on US real yields, Fed policy expectations, and "
            "safe-haven demand during geopolitical stress or market risk-off "
            "-- flag it as XAU when that is the main channel, even if no FX "
            "pair is named. Which single asset is most affected, does it "
            "strengthen or weaken, and how big a deal is this headline? "
            "Answer the 'reason' field in Spanish, under 12 words."
        )}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _CLASSIFY_SCHEMA,
        },
    }
    try:
        req = urllib.request.Request(
            f"{_CLASSIFY_URL}?key={key}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        raw = urllib.request.urlopen(req, timeout=20).read()
        text = json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]
        # Gemini sometimes HTML-entity-encodes accented characters (e.g. "d&#243;lar")
        return {k: (html.unescape(v) if isinstance(v, str) else v)
                for k, v in json.loads(text).items()}
    except Exception as exc:
        print(f"gemini classify failed: {exc}")
        return None


def news_lines() -> list[tuple[str, bool]]:
    """Fresh market-moving headlines from the news feeds, as (text, important)
    pairs. Each one that passes the keyword pre-filter gets read by Gemini,
    which drops it if it isn't really FX/gold-relevant, adds a direction call
    when it is, and tags it [alto]/[medio]/[bajo] -- only [alto] (CPI/NFP/
    rate-decision/war tier) is marked important. Without GEMINI_API_KEY,
    falls back to forwarding the plain headline untagged and not-important
    (impact can't be judged without the model)."""
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
            if not _NEWS_KEYWORD_RE.search(title):
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
            out.append((f"NOTICIA {stamp}: {title}", False))
            continue
        if not verdict.get("relevant"):
            continue  # Gemini read it and it doesn't actually matter for FX/gold
        cur, direction, reason = verdict.get("currency", ""), verdict.get("direction", "neutral"), verdict.get("reason", "")
        impact = verdict.get("impact", "medio")
        important = impact == "alto"
        call = f" {lean(cur, direction == 'up').capitalize()}." if cur and direction in ("up", "down") else ""
        out.append((f"NOTICIA [{impact}] {stamp}: {title} [{reason}]{call}", important))
    return out


def notify(message: str, *, title: str = "Forex", important: bool = False) -> None:
    """POST to ntfy.sh. No-op unless NTFY_TOPIC is set (kept out of the repo --
    it is a bearer secret; injected by the GitHub Actions secret / local env).

    important=True is for things on the scale of CPI/NFP/PPI, a rate decision,
    or a war headline -- the ones the user wants impossible to miss. A push
    notification can't render colored or large text (that's the OS's call,
    not the app's), so this is the closest equivalent: urgent priority (shows
    with a red bar in the ntfy app, can break through silent mode) plus
    alert-siren/red-circle tags, which ntfy renders as emoji. Everything else
    stays at default priority with no tags."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    headers = {"Title": f"IMPORTANTE - {title}" if important else title}
    if important:
        headers["Priority"] = "urgent"
        headers["Tags"] = "rotating_light,red_circle"
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
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

    lines: list[tuple[str, bool]] = []  # (text, important)

    # 1) heads-up: events about to release. High impact = important (CPI/NFP/
    #    rate-decision tier); medium = normal.
    for e in sorted(watched, key=lambda e: e.datetime_utc):
        if e.actual_raw:
            continue
        mins = (_ts(e.datetime_utc) - now).total_seconds() / 60
        if 0 < mins <= LEAD_MIN:
            others = sorted({x.currency for x in watched
                             if x.datetime_utc == e.datetime_utc and x.currency != e.currency})
            extra = f" (+ {', '.join(others)} al mismo tiempo)" if others else ""
            text = (f"UPCOMING [{_IMPACT_ES.get(e.impact, e.impact)}]: en ~{round(mins)} min "
                    f"({_ts(e.datetime_utc).astimezone(MTY):%H:%M} MTY) "
                    f"{e.currency} {e.title}{extra} -- preparate")
            lines.append((text, e.impact == "high"))

    # 2) direction: high-conviction double-surprise clusters first -- two
    #    currencies surprising in the same instant is always a big-mover case
    clustered_ids: set[str] = set()
    for ts, evs in clusters(events, impacts=WATCH_IMPACT).items():
        if not (recent <= _ts(ts) <= now):
            continue
        for ei, ej, diff in compound_pairs(evs):
            strong, weak = (ei, ej) if diff > 0 else (ej, ei)
            clustered_ids.update([ei.source_event_id, ej.source_event_id])
            lines.append((
                f"MOVER (DOBLE) [alto]: {strong.currency} {strong.title} {strong.actual_raw} vs "
                f"{strong.forecast_raw}, y al mismo tiempo {weak.currency} {weak.title} "
                f"{weak.actual_raw} vs {weak.forecast_raw}. {strong.currency} sube.",
                True,
            ))
            if "USD" in (strong.currency, weak.currency):
                usd_stronger = strong.currency == "USD"
                lines.append((f"MOVER (DOBLE) [alto]: ORO. {lean('XAU', not usd_stronger).capitalize()}.", True))

    # 3) direction: single releases off forecast, grouped so the four CPI
    #    sub-series (m/m, y/y, core...) become one line, not four. Important
    #    iff any sub-series in the group is high impact.
    buckets: dict[tuple, list[CanonicalEvent]] = defaultdict(list)
    for e in watched:
        if not e.actual_raw or e.source_better_worse == 0 or e.source_event_id in clustered_ids:
            continue
        if recent <= _ts(e.datetime_utc) <= now:
            buckets[(e.currency, e.datetime_utc, e.source_better_worse)].append(e)

    for (cur, _dt, bw), evs in buckets.items():
        stronger = bw == 1
        is_high = any(e.impact == "high" for e in evs)
        imp = "alto" if is_high else "medio"
        detail = "; ".join(f"{e.title} {e.actual_raw} vs {e.forecast_raw}" for e in evs[:3])
        lines.append((f"MOVER [{imp}]: {cur} ({detail}) salio {'mas fuerte' if stronger else 'mas debil'} "
                     f"de lo esperado. {lean(cur, stronger).capitalize()}.", is_high))
        if cur == "USD":
            # gold's own line -- inverse of USD (real-yield / safe-haven thesis),
            # called out on its own instead of buried in the USD cross-list
            lines.append((f"MOVER [{imp}]: ORO. {cur} salio {'mas fuerte' if stronger else 'mas debil'} "
                         f"de lo esperado. {lean('XAU', not stronger).capitalize()}.", is_high))

    # 3b) high-impact releases that landed exactly on forecast (bw == 0, so
    # skipped above -- no surprise, no direction to call). An [alto] UPCOMING
    # promised something worth prepping for; leaving that hanging with no
    # follow-up reads as the watcher having failed, so send a closure line
    # instead -- unimportant, since "nothing happened" isn't urgent.
    inline_high: dict[tuple, list[CanonicalEvent]] = defaultdict(list)
    for e in watched:
        if (not e.actual_raw or e.source_better_worse != 0
                or e.impact != "high" or e.source_event_id in clustered_ids):
            continue
        if recent <= _ts(e.datetime_utc) <= now:
            inline_high[(e.currency, e.datetime_utc)].append(e)

    for (cur, _dt), evs in inline_high.items():
        detail = "; ".join(f"{e.title} {e.actual_raw} vs {e.forecast_raw}" for e in evs[:3])
        lines.append((f"MOVER [alto]: {cur} ({detail}) salio en linea con lo esperado -- "
                     f"sin sorpresa, no se espera movimiento fuerte por esto.", False))

    # 4) unscheduled headlines (central-bank talk, geopolitics, oil, ...)
    news = news_lines()

    if not lines and not news:
        print("NOTHING NEW")
        return

    for text, important in lines[:8]:
        print(text)
        title = "Forex - por salir" if text.startswith("UPCOMING") else "Forex - direccion"
        notify(text[:200], title=title, important=important)
    for text, _important in lines[8:]:
        print(text)
    for text, important in news[:6]:
        print(text)
        notify(text[:200], title="Forex - noticia", important=important)
    for text, _important in news[6:]:
        print(text)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="movers.scan", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["brief", "watch", "check"])
    args = ap.parse_args(argv)
    cmd_brief() if args.mode == "brief" else cmd_watch()


if __name__ == "__main__":
    main()
