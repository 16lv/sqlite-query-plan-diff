from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_plan_diff import (
    PlanSnapshot,
    QuerySpec,
    capture_snapshots,
    diff_snapshots,
)
from sqlite_plan_diff.checker import main


def _create_sample_db(db_path: Path, with_index: bool) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE enrolments (
                student_id INTEGER NOT NULL,
                module_code TEXT NOT NULL,
                mark INTEGER NOT NULL
            );
            """
        )
        if with_index:
            conn.execute(
                "CREATE INDEX idx_enrolments_mod_mark ON enrolments(module_code, mark);"
            )
        conn.commit()
    finally:
        conn.close()


def test_capture_classifies_covering_index_vs_full_scan(tmp_path: Path) -> None:
    indexed_db = tmp_path / "indexed.db"
    scan_db = tmp_path / "scan.db"
    _create_sample_db(indexed_db, with_index=True)
    _create_sample_db(scan_db, with_index=False)

    queries = [
        QuerySpec(
            name="module_mark_threshold",
            sql="SELECT student_id, mark FROM enrolments WHERE module_code = ? AND mark >= ?",
            params=["CM2102", 70],
        )
    ]

    good_snaps = capture_snapshots(indexed_db, queries)
    bad_snaps = capture_snapshots(scan_db, queries)

    assert good_snaps[0].access_kind in ("COVERING_INDEX", "INDEX_SEARCH")
    assert bad_snaps[0].access_kind == "FULL_SCAN"


def test_diff_detects_index_to_scan_regression_and_missing_queries() -> None:
    baseline = [
        PlanSnapshot(
            name="lookup_user",
            sql="SELECT id FROM users WHERE email = ?",
            steps=["SEARCH users USING COVERING INDEX idx_email (email=?)"],
            access_kind="COVERING_INDEX",
            uses_temp_btree=False,
        ),
        PlanSnapshot(
            name="list_orders",
            sql="SELECT id FROM orders WHERE status = ?",
            steps=["SEARCH orders USING INDEX idx_status (status=?)"],
            access_kind="INDEX_SEARCH",
            uses_temp_btree=False,
        ),
    ]
    current = [
        PlanSnapshot(
            name="lookup_user",
            sql="SELECT id FROM users WHERE email = ?",
            steps=["SCAN users"],
            access_kind="FULL_SCAN",
            uses_temp_btree=False,
        )
    ]

    report = diff_snapshots(baseline, current)
    assert report.passed is False
    reasons = {r.query_name: r.severity for r in report.regressions}
    assert reasons == {"lookup_user": "REGRESSION", "list_orders": "MISSING"}


def test_diff_flags_new_temp_btree_when_strict_mode_enabled() -> None:
    base = [
        PlanSnapshot(
            name="sorted_events",
            sql="SELECT id FROM events ORDER BY created_at",
            steps=["SCAN events USING COVERING INDEX idx_events_created"],
            access_kind="INDEX_SEARCH",
            uses_temp_btree=False,
        )
    ]
    curr = [
        PlanSnapshot(
            name="sorted_events",
            sql="SELECT id FROM events ORDER BY created_at",
            steps=["SEARCH events USING INDEX idx_other", "USE TEMP B-TREE FOR ORDER BY"],
            access_kind="INDEX_SEARCH",
            uses_temp_btree=True,
        )
    ]

    assert diff_snapshots(base, curr, strict_temp_btree=False).passed is True
    strict_report = diff_snapshots(base, curr, strict_temp_btree=True)
    assert strict_report.passed is False
    assert "temporary B-tree" in strict_report.regressions[0].reason


def test_diff_flags_covering_index_downgrade_to_table_lookup() -> None:
    base = [
        PlanSnapshot(
            name="fast_count",
            sql="SELECT id FROM items WHERE sku = ?",
            steps=["SEARCH items USING COVERING INDEX idx_sku (sku=?)"],
            access_kind="COVERING_INDEX",
            uses_temp_btree=False,
        )
    ]
    curr = [
        PlanSnapshot(
            name="fast_count",
            sql="SELECT id, price FROM items WHERE sku = ?",
            steps=["SEARCH items USING INDEX idx_sku (sku=?)"],
            access_kind="INDEX_SEARCH",
            uses_temp_btree=False,
        )
    ]

    report = diff_snapshots(base, curr)
    assert report.passed is False
    assert report.regressions[0].baseline_kind == "COVERING_INDEX"
    assert report.regressions[0].current_kind == "INDEX_SEARCH"


def test_capture_rejects_blank_name_and_empty_sql(tmp_path: Path) -> None:
    db_file = tmp_path / "empty.db"
    _create_sample_db(db_file, with_index=False)

    with pytest.raises(ValueError, match="must not be blank"):
        capture_snapshots(db_file, [QuerySpec(name="   ", sql="SELECT 1", params=[])])
    with pytest.raises(ValueError, match="non-empty SQL"):
        capture_snapshots(db_file, [QuerySpec(name="bad_sql", sql="EXPLAIN QUERY PLAN ;", params=[])])


def test_cli_capture_and_check_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queries_json = tmp_path / "queries.json"
    baseline_json = tmp_path / "baseline.json"
    schema_good = tmp_path / "good.sql"
    schema_bad = tmp_path / "bad.sql"

    schema_good.write_text(
        "CREATE TABLE students(id INTEGER PRIMARY KEY, email TEXT); "
        "CREATE UNIQUE INDEX idx_email ON students(email);",
        encoding="utf-8",
    )
    schema_bad.write_text(
        "CREATE TABLE students(id INTEGER PRIMARY KEY, email TEXT);",
        encoding="utf-8",
    )
    queries_json.write_text(
        json.dumps([{"name": "find_by_email", "sql": "SELECT id FROM students WHERE email = ?", "params": ["a@b.c"]}]),
        encoding="utf-8",
    )

    assert main(["capture", ":memory:", str(queries_json), "--schema", str(schema_good), "-o", str(baseline_json)]) == 0
    capsys.readouterr()

    assert main(["check", ":memory:", str(queries_json), "--schema", str(schema_good), "--baseline", str(baseline_json)]) == 0
    ok_payload = json.loads(capsys.readouterr().out)
    assert ok_payload["passed"] is True

    assert main(["check", ":memory:", str(queries_json), "--schema", str(schema_bad), "--baseline", str(baseline_json)]) == 1
    fail_payload = json.loads(capsys.readouterr().out)
    assert fail_payload["passed"] is False
    assert fail_payload["regressions"][0]["severity"] == "REGRESSION"
