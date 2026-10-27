"""Unit tests. No network access: yfinance is replaced with fake downloaders."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from src.fetch_sp500 import (
    TICKER,
    FetchError,
    fetch_daily_closes,
    main,
    month_range,
    parse_month,
    previous_month_range,
)
from src.storage import CsvStorage, DailyClose


def fake_frame(closes: dict[str, float], multiindex: bool = False) -> pd.DataFrame:
    """Build a DataFrame shaped like a yf.download() response."""
    index = pd.DatetimeIndex(list(closes), name="Date")
    values = list(closes.values())
    data = {"Open": values, "High": values, "Low": values, "Close": values, "Volume": [0] * len(values)}
    df = pd.DataFrame(data, index=index)
    if multiindex:
        df.columns = pd.MultiIndex.from_product([df.columns, [TICKER]], names=["Price", "Ticker"])
    return df


class DateRangeTests(unittest.TestCase):
    def test_previous_month_normal(self) -> None:
        self.assertEqual(previous_month_range(date(2026, 10, 27)), (date(2026, 9, 1), date(2026, 10, 1)))

    def test_previous_month_on_first_day(self) -> None:
        self.assertEqual(previous_month_range(date(2026, 11, 1)), (date(2026, 10, 1), date(2026, 11, 1)))

    def test_january_resolves_to_previous_december(self) -> None:
        self.assertEqual(previous_month_range(date(2027, 1, 2)), (date(2026, 12, 1), date(2027, 1, 1)))

    def test_leap_year_february(self) -> None:
        start, end = previous_month_range(date(2028, 3, 2))
        self.assertEqual((start, end), (date(2028, 2, 1), date(2028, 3, 1)))
        self.assertEqual((end - start).days, 29)

    def test_month_range_end_is_exclusive(self) -> None:
        self.assertEqual(month_range(2026, 12), (date(2026, 12, 1), date(2027, 1, 1)))

    def test_parse_month(self) -> None:
        self.assertEqual(parse_month("2026-09"), (date(2026, 9, 1), date(2026, 10, 1)))
        for bad in ("2026-13", "2026-00", "2026-9", "26-09", "2026/09", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_month(bad)


class FetchTests(unittest.TestCase):
    def test_passes_exclusive_end_and_sorts(self) -> None:
        calls: list[dict] = []

        def downloader(ticker: str, **kwargs) -> pd.DataFrame:
            calls.append({"ticker": ticker, **kwargs})
            return fake_frame({"2026-09-02": 6502.456, "2026-09-01": 6500.0})

        rows = fetch_daily_closes(date(2026, 9, 1), date(2026, 10, 1), downloader=downloader)
        self.assertEqual(calls[0]["ticker"], TICKER)
        self.assertEqual((calls[0]["start"], calls[0]["end"]), ("2026-09-01", "2026-10-01"))
        self.assertEqual(calls[0]["interval"], "1d")
        self.assertEqual(rows, [DailyClose(date(2026, 9, 1), 6500.0), DailyClose(date(2026, 9, 2), 6502.46)])

    def test_handles_multiindex_columns(self) -> None:
        rows = fetch_daily_closes(
            date(2026, 9, 1), date(2026, 10, 1),
            downloader=lambda *a, **k: fake_frame({"2026-09-01": 6500.0}, multiindex=True),
        )
        self.assertEqual(rows, [DailyClose(date(2026, 9, 1), 6500.0)])

    def test_drops_out_of_range_and_nan_rows(self) -> None:
        frame = fake_frame({"2026-08-29": 1.0, "2026-09-01": float("nan"), "2026-09-02": 2.0, "2026-10-01": 3.0})
        rows = fetch_daily_closes(date(2026, 9, 1), date(2026, 10, 1), downloader=lambda *a, **k: frame)
        self.assertEqual(rows, [DailyClose(date(2026, 9, 2), 2.0)])

    def test_empty_response_raises(self) -> None:
        with self.assertRaises(FetchError):
            fetch_daily_closes(date(2026, 9, 1), date(2026, 10, 1), downloader=lambda *a, **k: pd.DataFrame())

    def test_missing_close_column_raises(self) -> None:
        frame = fake_frame({"2026-09-01": 1.0}).drop(columns="Close")
        with self.assertRaises(FetchError):
            fetch_daily_closes(date(2026, 9, 1), date(2026, 10, 1), downloader=lambda *a, **k: frame)

    def test_network_error_wrapped(self) -> None:
        def boom(*a, **k):
            raise ConnectionError("offline")

        with self.assertRaises(FetchError):
            fetch_daily_closes(date(2026, 9, 1), date(2026, 10, 1), downloader=boom)


class CsvStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "sp500_daily.csv"
        self.storage = CsvStorage(self.path)
        self.rows = [DailyClose(date(2026, 9, 2), 6502.46), DailyClose(date(2026, 9, 1), 6500.0)]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creates_file_sorted(self) -> None:
        result = self.storage.upsert(self.rows)
        self.assertEqual((result.inserted, result.total), (2, 2))
        self.assertEqual(
            self.path.read_text(),
            "date,close\n2026-09-01,6500.00\n2026-09-02,6502.46\n",
        )

    def test_rerun_is_idempotent(self) -> None:
        self.storage.upsert(self.rows)
        first = self.path.read_text()
        result = self.storage.upsert(self.rows)
        self.assertEqual((result.inserted, result.updated, result.unchanged, result.total), (0, 0, 2, 2))
        self.assertEqual(self.path.read_text(), first)

    def test_no_duplicate_dates(self) -> None:
        self.storage.upsert(self.rows)
        self.storage.upsert(self.rows + [DailyClose(date(2026, 9, 3), 6510.0)])
        dates = [line.split(",")[0] for line in self.path.read_text().splitlines()[1:]]
        self.assertEqual(dates, ["2026-09-01", "2026-09-02", "2026-09-03"])

    def test_existing_date_is_updated(self) -> None:
        self.storage.upsert(self.rows)
        result = self.storage.upsert([DailyClose(date(2026, 9, 1), 6499.99)])
        self.assertEqual((result.inserted, result.updated, result.total), (0, 1, 2))
        self.assertEqual(self.storage.load()[date(2026, 9, 1)], 6499.99)


class CliTests(unittest.TestCase):
    def test_invalid_month_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            main(["--month", "2026-13"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
