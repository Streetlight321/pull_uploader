# pull_uploader — S&P 500 daily closes

A small pipeline that downloads daily closing prices for the S&P 500 index
(`^GSPC`) from Yahoo Finance via [`yfinance`](https://pypi.org/project/yfinance/)
and keeps a growing, de-duplicated history in `data/sp500_daily.csv`.
A GitHub Actions workflow runs it once a month and commits the updated CSV.

## Project structure

```text
pull_uploader/
├── .github/workflows/monthly_sp500.yml  # monthly schedule + manual trigger
├── data/sp500_daily.csv                 # the dataset (date,close)
├── src/
│   ├── fetch_sp500.py                   # date logic, yfinance download, CLI
│   └── storage.py                       # Storage protocol + CsvStorage
├── tests/test_fetch_sp500.py            # offline unit tests (yfinance mocked)
├── requirements.txt
└── README.md
```

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running locally

```bash
python -m src.fetch_sp500                  # collect the previous calendar month
python -m src.fetch_sp500 --month 2026-09  # collect a specific month (backfill/debug)
python -m unittest discover -s tests -v    # run the tests
```

Example output:

```text
Fetching ^GSPC from 2026-09-01 through 2026-09-30
Retrieved 21 trading days (2026-09-01 .. 2026-09-30)
Inserted 21, updated 0, unchanged 0; 21 total rows in .../data/sp500_daily.csv
```

The script exits non-zero on any failure (network error, empty or malformed
response, bad `--month`), so CI fails loudly. File paths are resolved relative
to the source files, so the output always lands in this repo's `data/`
directory regardless of the current working directory.

## Data

`data/sp500_daily.csv`, one row per trading day, sorted by date:

```csv
date,close
2026-09-01,7631.47
```

`close` is the raw (unadjusted) daily close, rounded to 2 decimals.

## How "previous month" works

The current date is taken in **UTC**, and the range is the whole previous
calendar month: running on 2026-10-27 or 2026-10-02 collects 2026-09-01 →
2026-09-30; running in January collects December of the prior year.

yfinance treats `end` as **exclusive**, so the request uses
`start=<first of month>` and `end=<first of next month>`. Weekends and market
holidays simply don't appear in the response; nothing assumes the first or
last day of the month is a trading day.

## Idempotency

Rows are keyed by `date`. On each run the existing CSV is loaded, fetched rows
are **upserted** (new dates inserted, existing dates replaced with the latest
value), and the whole file is rewritten sorted by date via an atomic
temp-file-and-rename. Rerunning the same month therefore yields an identical
file and never creates duplicates.

## GitHub Actions automation

`.github/workflows/monthly_sp500.yml`:

- Runs at **06:00 UTC on the 2nd** of every month (the 2nd rather than the 1st
  gives Yahoo time to finalise the last trading day of the month).
- Installs dependencies, runs the tests, then runs the collector.
- Commits `data/sp500_daily.csv` as `github-actions[bot]` with message
  `data: update S&P 500 daily prices` — **only if the file changed**.
- Has `contents: write` permission so the built-in `GITHUB_TOKEN` can push.
- Can't loop: it has no `push` trigger, and pushes made with `GITHUB_TOKEN`
  don't trigger workflows anyway.

**Manual run:** GitHub → *Actions* → *Monthly S&P 500 collection* →
*Run workflow*. Optionally enter a month (`YYYY-MM`) to backfill; leave blank
for the previous month.

## Future: Supabase

Supabase persistence is **planned but intentionally not implemented yet**.

`fetch_sp500.py` only produces a list of `DailyClose(date, close)` records and
hands them to any object satisfying the `Storage` protocol in `storage.py`:

```python
class Storage(Protocol):
    def upsert(self, rows: Sequence[DailyClose]) -> UpsertResult: ...
    def describe(self) -> str: ...
```

To switch to Supabase:

1. Create a table with `date` as the primary key (e.g. `date date primary key,
   close double precision not null`).
2. Add `SupabaseStorage` to `storage.py` implementing `upsert` with
   `on_conflict="date"`, reading the URL/key from environment variables.
3. Add `supabase` to `requirements.txt` and the credentials as GitHub Actions
   secrets (never commit them).
4. Choose the storage in `main()` (e.g. via an env var) and drop the
   commit-back step from the workflow once CSV is retired.

The fetching code and tests for it don't change.
