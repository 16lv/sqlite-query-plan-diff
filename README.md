# sqlite-query-plan-diff

SQLite `EXPLAIN QUERY PLAN` baseline capture and index-regression checker built with **Python 3.12** standard-library `sqlite3`.

## Overview

`sqlite-query-plan-diff` records query-planner strategies (`COVERING_INDEX`, `INDEX_SEARCH`, `FULL_SCAN`, `TEMP B-TREE`) for named parameterised SQL queries as JSON snapshots, then compares updated schemas in CI to fail builds cleanly whenever an index migration drops back to a table scan.

## Features

- **Zero runtime dependencies**: Built purely on Python 3.12 standard-library `sqlite3`, `json`, `argparse`, and `dataclasses`.
- **Plan classification & diffing**: Distinguishes covering indexes from secondary table lookups, catches `INDEX -> FULL_SCAN` and `COVERING_INDEX -> INDEX_SEARCH` regressions, and flags missing baseline queries.
- **Optional temp B-tree check**: Pass `--strict-temp-btree` when an `ORDER BY` or `GROUP BY` change introduces an unindexed temporary sort.
- **CI exit codes & JSON output**: Returns exit code `0` cleanly when plans hold and `1` on regressions.

## Technology Stack

- Python 3.11+ (tested on Python 3.12)
- SQLite3 (`sqlite3` standard library)
- pytest, Ruff

## Installation

```bash
pip install -e ".[dev]"
```

## Usage Examples

Capture a baseline against a schema file:

```bash
sqlite-plan-diff capture :memory: queries.json --schema schema.sql -o baseline.json
```

Verify a new migration against the saved baseline:

```bash
sqlite-plan-diff check :memory: queries.json --schema updated_schema.sql --baseline baseline.json --strict-temp-btree
```

## Testing

```bash
ruff check .
pytest -v
```

## Known Limitations & Future Improvements

- Built specifically for SQLite's `EXPLAIN QUERY PLAN` detail strings rather than PostgreSQL `EXPLAIN (FORMAT JSON)`.
- Adding an annotated `.sql` file parser (`-- name: lookup_user`) alongside JSON query lists would reduce boilerplate in larger SQL repositories.
