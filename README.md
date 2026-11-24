# sqlite-query-plan-diff

SQLite `EXPLAIN QUERY PLAN` baseline capture, query-strategy inspection (`explain`), and index-regression checker built with **Python 3.12** standard-library `sqlite3`.

## Overview

`sqlite-query-plan-diff` records SQLite query-planner strategies (`COVERING_INDEX`, `INDEX_SEARCH`, `FULL_SCAN`, `TEMP B-TREE`) for named parameterised queries from `.sql` or `.json` files, then checks updated schemas or database files in CI to fail builds cleanly whenever an index migration regresses to a table scan.

## Features

- **Zero runtime dependencies**: Built purely on Python 3.12 standard-library `sqlite3`, `json`, `argparse`, and `dataclasses`.
- **Annotated `.sql` & `.json` inputs**: Supports `-- name: <query_id>` and optional `-- params: [...]` comments inside standard `.sql` files alongside structured `.json` arrays.
- **Accurate scan vs index classification**: Distinguishes `SCAN ... USING COVERING INDEX` from unindexed `SCAN table` operations (including multi-table join steps where any unindexed scan flags `FULL_SCAN`).
- **Safe read-only database inspection**: Opens existing SQLite database files via URI `?mode=ro` when `--schema` is omitted, failing cleanly rather than creating empty files on typos.
- **`explain`, `capture`, and `check` subcommands**: Inspect live query plans interactively (`--format text` or `--format json`), capture JSON baselines, and enforce strict exit codes (`0` pass, `1` regression, `2` input/SQL error) with optional `--strict-temp-btree`.

## Technology Stack

- Python 3.11+ (tested on Python 3.12)
- SQLite3 (`sqlite3` standard library)
- pytest, Ruff

## Installation

```bash
pip install -e ".[dev]"
```

## Usage Examples

Inspect plans directly from an annotated `.sql` file against a schema:

```sql
-- queries.sql
-- name: lookup_by_email
-- params: ["alice@example.org"]
SELECT id FROM students WHERE email = ?;
```

```bash
sqlite-plan-diff explain :memory: queries.sql --schema schema.sql
```

Capture a JSON baseline:

```bash
sqlite-plan-diff capture :memory: queries.sql --schema schema.sql -o baseline.json
```

Verify an updated schema against the saved baseline (`--format text` or `json`):

```bash
sqlite-plan-diff check :memory: queries.sql --schema updated_schema.sql --baseline baseline.json --strict-temp-btree
```

## Project Structure

```text
sqlite-query-plan-diff/
├── src/sqlite_plan_diff/
│   ├── __init__.py
│   ├── __main__.py
│   └── checker.py
├── tests/
│   └── test_checker.py
├── pyproject.toml
└── requirements.txt
```

## Testing

```bash
ruff check .
pytest -v
sqlite-plan-diff --help
```

## Known Limitations & Future Improvements

- Targets SQLite's `EXPLAIN QUERY PLAN` output rather than PostgreSQL `EXPLAIN (FORMAT JSON)`.
- Parameter lists match positional `?` bindings sequentially per query; supporting `:named` parameter objects alongside positional arrays is a natural next extension.
