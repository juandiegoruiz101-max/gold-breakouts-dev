"""Economic-calendar data pipeline for the Gold Breakouts strategy.

Fetches scheduled US macro releases (CPI, PPI, NFP, FOMC, ...) with their exact
UTC release timestamp and actual / forecast / previous values, normalised to a
source-agnostic schema so the calendar provider can be swapped without touching
the backtest.
"""

__all__ = ["schema", "store", "sources"]
