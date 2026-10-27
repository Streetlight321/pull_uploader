"""Fetch the previous calendar month of S&P 500 daily closes and persist them.

Run from the repository root with::

    python -m src.fetch_sp500                 # previous calendar month (UTC)
    python -m src.fetch_sp500 --month 2026-09 # explicit month (backfill)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd
import yfinance as yf

if __package__ in (None, ""):  # allow `python path/to/src/fetch_sp500.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage import CsvStorage, DailyClose, Storage

TICKER = "^GSPC"
log = logging.getLogger("fetch_sp500")


class FetchError(RuntimeError):
    """Raised when market data cannot be downloaded or is unusable."""


def month_range(year: int, month: int) -> tuple[date, date]:
    """Return ``(first_day, first_day_of_next_month)`` for a month.

    The end date is exclusive, which matches yfinance's ``end`` semantics.
    """
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def previous_month_range(today: date | None = None) -> tuple[date, date]:
    """Return the ``[start, end)`` range for the calendar month before ``today`` (UTC)."""
    today = today or datetime.now(timezone.utc).date()
    if today.month == 1:
        return month_range(today.year - 1, 12)
    return month_range(today.year, today.month - 1)


def parse_month(value: str) -> tuple[date, date]:
    """Parse ``YYYY-MM`` into a ``[start, end)`` range; raises ``ValueError``."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"invalid month {value!r}; expected YYYY-MM")
    return month_range(int(match.group(1)), int(match.group(2)))


Downloader = Callable[..., pd.DataFrame]


def fetch_daily_closes(
    start: date,
    end_exclusive: date,
    ticker: str = TICKER,
    downloader: Downloader = yf.download,
) -> list[DailyClose]:
    """Download daily closes for ``[start, end_exclusive)``, sorted by date.

    Raises :class:`FetchError` on network failures or empty/malformed data.
    """
    try:
        df = downloader(
            ticker,
            start=start.isoformat(),
            end=end_exclusive.isoformat(),
            interval="1d",
            auto_adjust=False,  # raw "Close", not adjusted
            actions=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:
        raise FetchError(f"download of {ticker} failed: {exc}") from exc

    if df is None or df.empty:
        raise FetchError(f"no data returned for {ticker} between {start} and {end_exclusive}")

    if "Close" not in df.columns.get_level_values(0):
        raise FetchError(f"response for {ticker} has no 'Close' column: {list(df.columns)}")

    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # MultiIndex columns: (field, ticker)
        if ticker in close.columns:
            close = close[ticker]
        elif close.shape[1] == 1:
            close = close.iloc[:, 0]
        else:
            raise FetchError(f"ambiguous 'Close' columns for {ticker}: {list(close.columns)}")

    close = pd.to_numeric(close, errors="coerce").dropna()
    rows: dict[date, float] = {}
    for ts, value in close.items():
        day = pd.Timestamp(ts).date()
        if start <= day < end_exclusive:  # guard against out-of-range rows
            rows[day] = round(float(value), 2)

    if not rows:
        raise FetchError(f"no valid closing prices for {ticker} between {start} and {end_exclusive}")

    return [DailyClose(day, rows[day]) for day in sorted(rows)]


def run(start: date, end_exclusive: date, storage: Storage, downloader: Downloader = yf.download) -> None:
    """Fetch one month and upsert it into ``storage``."""
    last_day = end_exclusive - timedelta(days=1)
    log.info("Fetching %s from %s through %s", TICKER, start, last_day)
    rows = fetch_daily_closes(start, end_exclusive, downloader=downloader)
    log.info("Retrieved %d trading days (%s .. %s)", len(rows), rows[0].date, rows[-1].date)

    result = storage.upsert(rows)
    log.info(
        "Inserted %d, updated %d, unchanged %d; %d total rows in %s",
        result.inserted, result.updated, result.unchanged, result.total, storage.describe(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Collect {TICKER} daily closes for one month.")
    parser.add_argument("--month", help="month to collect as YYYY-MM (default: previous month, UTC)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # yfinance itself triggers pandas deprecation warnings; they are not actionable here.
    warnings.filterwarnings("ignore", message=".*inplace keyword.*", module=r"yfinance\.")

    if args.month:
        try:
            start, end_exclusive = parse_month(args.month)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        start, end_exclusive = previous_month_range()

    try:
        run(start, end_exclusive, CsvStorage())
    except Exception:
        log.exception("Collection failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
