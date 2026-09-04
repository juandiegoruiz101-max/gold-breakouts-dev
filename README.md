# Gold Breakouts Dev

Event-driven research on **spot XAU/USD**: trade the volatility reaction when a
scheduled US macro release (CPI, PPI, NFP, FOMC, …) comes in away from
consensus. Thesis: gold is driven by the interaction of US inflation
expectations and bond yields, so a downside inflation surprise → market prices
future rate cuts → fast upward candles in gold.

Roughly 60–65 % of the largest single-candle moves on gold line up with these
scheduled releases, and the release times are known in advance — so the strategy
is timed off the economic calendar rather than off the chart.

## Layout

```
calendar_pipeline/        economic-calendar ingestion (this milestone)
  schema.py               CanonicalEvent — the source-agnostic record
  store.py                CSV store, dedup on event_key
  sources/
    forexfactory.py       working source (cloudscraper, month pages)
    fxstreet.py           stub — preferred source, sales-led API
  cli.py                  backfill / update / show / stats
01 Data/
  calendar/calendar_events.csv    the normalised store
  raw/forexfactory/               cached raw pages (gitignored)
Docs/calendar_schema.md   column-by-column schema + source notes
```

## Setup

```sh
python3 -m pip install -r requirements.txt
```

## Usage

Run from the repo root:

```sh
# one-off history load (ForexFactory has data back to ~2010)
python3 -m calendar_pipeline.cli backfill --start 2015-01

# keep it current — re-fetches this + last month to catch revisions
python3 -m calendar_pipeline.cli update

# inspect
python3 -m calendar_pipeline.cli stats
python3 -m calendar_pipeline.cli show --currency USD --impact high --category CPI
```

Raw month pages are cached under `01 Data/raw/`; re-running `backfill` is cheap.
Use `--refresh` to force a re-download.

## Status / next

- [x] Calendar pipeline — ForexFactory source, canonical schema, CSV store
- [ ] Price data — Dukascopy XAU/USD tick / 1 s bars around each release timestamp
- [ ] Join — reaction-candle metrics (pre-release baseline, impulse, drift) keyed on `datetime_utc`
- [ ] Backtest — surprise (`surprise_pct` / `source_better_worse`) vs. gold reaction
- [ ] Swap in FXStreet once API access is sorted (no downstream changes)
