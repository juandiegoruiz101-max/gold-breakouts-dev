# Canonical calendar-event schema

Every calendar source normalises to `CanonicalEvent` (see
[`calendar_pipeline/schema.py`](../calendar_pipeline/schema.py)). Analysis and
backtest code only ever touch this shape, so swapping the calendar provider
(ForexFactory → FXStreet) does not ripple downstream.

Store: `01 Data/calendar/calendar_events.csv`, one row per release, deduplicated
on `event_key`.

## Columns

| column | type | notes |
|---|---|---|
| `event_key` | str | globally unique, stable — `"<source>:<id>"`. Merge key. |
| `source` | str | `forexfactory` (later: `fxstreet`) |
| `source_event_id` | str | id of this single release |
| `source_series_id` | str | id shared by every release of one indicator (FF `ebaseId`). Join key for "all CPI m/m prints". |
| `datetime_utc` | str | ISO8601 `…Z`, **always UTC**. This is the join key to price data. |
| `date_label` | str | source's own day label, for eyeballing only |
| `time_label` | str | `"8:30am"` / `"All Day"` / `"Tentative"` (as shown by the source) |
| `time_is_exact` | bool | `False` for All Day / Tentative / multi-day. Only trade `True`. |
| `country` | str | source country code (FF: `US`, `GE`, `JN` — **not ISO**) |
| `currency` | str | `USD`, … |
| `title` | str | e.g. `CPI m/m` |
| `category` | str | coarse bucket: `CPI` `PPI` `NFP` `ADP` `PCE` `GDP` `FOMC` `RETAIL_SALES` `ISM_MFG` `ISM_SVC` `JOBLESS_CLAIMS` `AVG_HOURLY_EARNINGS` `UNEMPLOYMENT_RATE` `OTHER`. A filtering convenience, not authoritative — always keep `title` too. |
| `impact` | str | `high` / `medium` / `low` / `holiday` |
| `actual` `forecast` `previous` `revised_previous` | float / empty | **face value as printed** — `3.4` for `"3.4%"`, `175.0` for `"175K"`. Empty when missing or unparseable. |
| `unit` | str | `%` / `K` / `M` / `B` / `T` / empty. Applies to all four value columns of the row. |
| `actual_raw` `forecast_raw` `previous_raw` `revised_previous_raw` | str | original strings, never lost |
| `surprise` | float | `actual − forecast` in face units (when `actual`/`forecast` share a unit) |
| `surprise_pct` | float | `(actual − forecast) / |forecast|`, unit-invariant. Prefer this for cross-indicator comparison. |
| `source_better_worse` | int | the source's own surprise sign, **currency-centric**. FF: `0` in-line, `1` better-for-USD (e.g. hot CPI, strong NFP), `2` worse-for-USD. For the gold thesis, `2` (USD-negative) is the gold-bullish case. |
| `source_url` | str | page the row came from |
| `fetched_at_utc` | str | when this row was scraped |

## ForexFactory specifics

- Source of truth in the page is the JS blob `calendarComponentStates[1].days[].events[]`, **not** the rendered HTML table (the table renders in the session's default timezone).
- `dateline` (unix seconds) is an **absolute UTC instant** — verified against known releases (US CPI 8:30am ET → `13:30Z` in winter). This is what `datetime_utc` comes from; timezone handling is therefore a non-issue.
- `revision` = revised value of the *previous* period → `revised_previous`.
- History is available at least back to **2010** via `?month=<mmm.yyyy>`.
- Cloudflare-fronted; fetched with `cloudscraper` (no browser). Be polite: 2–4 s between requests (built in), pages cached under `01 Data/raw/`.
- FF value quirks to watch: occasional unit switch within a series (`998K` → `1.0M`); `surprise_pct` stays correct, `surprise` may not for those rows.

## FXStreet (future)

Sales-led OAuth2 API (`docs.fxstreet.com/api/calendar/`). Expected mapping is in
[`calendar_pipeline/sources/fxstreet.py`](../calendar_pipeline/sources/fxstreet.py).
`dateUtc` is already UTC; `volatility` → `impact`; `consensus` → `forecast`.
