# Gold Dashboard upgrade notes

This update focuses on market-data monitoring and software transparency, not personalized purchase recommendations.


## Changes

- Separate indicator strength from data quality and freshness.
- Per-series data-quality reporting: latest observation date, calendar-day age, last-10-row availability, overall coverage, and FRESH / DELAYED / STALE / MISSING status.
- Explicit warnings for missing or stale critical inputs (gold, spot, USD/INR) and fetch failures.
- Signal transparency counts: supportive, bearish, neutral, unavailable, excluded, and confirmed groups; warns when evidence conflicts.
- Historical signal-score correlation report to flag indicators that may be redundant.
- Workflow now validates `docs/data.json`, stages optional CSVs only when present, serializes scheduled runs, and avoids a missing-path `git add` failure.
- Added an IBJA CSV header file and automated unit tests.
- Dashboard wording reframed as a market monitor; individual purchase schedule outputs are no longer generated/displayed.

## Files

- `fetch_data.py`
- `index.html`
- `.github/workflows/update.yml`
- `docs/ibja_history.csv`
- `tests/test_data_quality.py`

## Checks run

- Python syntax compilation: passed.
- JavaScript syntax check (`node --check`): passed.
- HTML parser check: passed.
- Unit tests: passed (5 tests).
- Synthetic backtest smoke test: passed, returned 21 signal rows and correlation pairs.

## Before merging

Run the GitHub Actions workflow once manually after applying these files. A live data run was not performed in this environment, so upstream source parsing, API availability, and the Pages deployment have not been end-to-end verified.
