"""Persistence layer for daily closing prices.

The fetching code only depends on the :class:`Storage` protocol, so a future
``SupabaseStorage`` can be dropped in without touching ``fetch_sp500.py``.
"""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_PATH = PROJECT_ROOT / "data" / "sp500_daily.csv"

CSV_FIELDS = ("date", "close")


@dataclass(frozen=True)
class DailyClose:
    """One trading day's closing price."""

    date: date
    close: float


@dataclass(frozen=True)
class UpsertResult:
    """Summary of an upsert operation."""

    inserted: int
    updated: int
    unchanged: int
    total: int


class Storage(Protocol):
    """Anything that can idempotently upsert daily closes keyed by date."""

    def upsert(self, rows: Sequence[DailyClose]) -> UpsertResult:
        """Insert new dates and replace existing ones. Must be idempotent."""
        ...

    def describe(self) -> str:
        """Human-readable destination, used in CLI output."""
        ...


class CsvStorage:
    """Stores daily closes in a CSV file with one row per date."""

    def __init__(self, path: Path = DEFAULT_CSV_PATH) -> None:
        self.path = Path(path)

    def describe(self) -> str:
        return str(self.path)

    def load(self) -> dict[date, float]:
        """Return existing rows keyed by date (empty if the file is missing)."""
        if not self.path.exists():
            return {}
        rows: dict[date, float] = {}
        with self.path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = set(CSV_FIELDS) - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{self.path} is missing columns: {sorted(missing)}")
            for line_no, record in enumerate(reader, start=2):
                try:
                    rows[date.fromisoformat(record["date"])] = float(record["close"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{self.path}:{line_no}: malformed row {record!r}") from exc
        return rows

    def upsert(self, rows: Sequence[DailyClose]) -> UpsertResult:
        existing = self.load()
        inserted = updated = unchanged = 0
        for row in rows:
            previous = existing.get(row.date)
            if previous is None:
                inserted += 1
            elif previous != row.close:
                updated += 1
            else:
                unchanged += 1
            existing[row.date] = row.close

        self._write(existing)
        return UpsertResult(inserted, updated, unchanged, total=len(existing))

    def _write(self, rows: dict[date, float]) -> None:
        """Write all rows sorted by date, atomically replacing the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, lineterminator="\n")
                writer.writerow(CSV_FIELDS)
                for day in sorted(rows):
                    writer.writerow([day.isoformat(), f"{rows[day]:.2f}"])
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
