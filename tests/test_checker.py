from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_plan_diff import (
    PlanSnapshot,
    QuerySpec,
    capture_snapshots,
    classify_access_kind,
    diff_snapshots,
    load_query_specs,
    parse_sql_queries,
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


def test_classify_handles_covering_index_scan_and_mixed_join_scans() -> None:
    assert (
        classify_access_kind(["SCAN events USING COVERING INDEX idx_events_created"])
        == "COVERING_INDEX"
    )
    assert (
        classify_access_kind(
            ["SEARCH orders USING INDEX idx_status", "SCAN customers"]
        )
        == "FULL_SCAN"
    )
    assert (
        classify_access_kind(
            [
                "SEARCH a USING COVERING INDEX idx_a",
                "SEARCH b USING INDEX idx_b",
            ]
        )
        == "INDEX_SEARCH"
    )


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
            access_kind="COVERING_INDEX",
            uses_temp_btree=False,
        )
    ]
    curr = [
        PlanSnapshot(
            name="sorted_events",
            sql="SELECT id FROM events ORDER BY created_at",
            steps=["SCAN events USING COVERING INDEX idx_events_created", "USE TEMP B-TREE FOR ORDER BY"],
            access_kind="COVERING_INDEX",
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


def test_capture_rejects_blank_duplicate_and_missing_db(tmp_path: Path) -> None:
    db_file = tmp_path / "empty.db"
    _create_sample_db(db_file, with_index=False)

    with pytest.raises(ValueError, match="must not be blank"):
        capture_snapshots(db_file, [QuerySpec(name="   ", sql="SELECT 1", params=[])])
    with pytest.raises(ValueError, match="non-empty SQL"):
        capture_snapshots(db_file, [QuerySpec(name="bad_sql", sql="EXPLAIN QUERY PLAN ;", params=[])])
    with pytest.raises(ValueError, match="Duplicate QuerySpec name"):
        capture_snapshots(
            db_file,
            [
                QuerySpec(name="dup", sql="SELECT 1", params=[]),
                QuerySpec(name="dup", sql="SELECT 2", params=[]),
            ],
        )

    missing_db = tmp_path / "does_not_exist.db"
    with pytest.raises(FileNotFoundError):
        capture_snapshots(missing_db, [QuerySpec(name="q", sql="SELECT 1", params=[])])
    assert not missing_db.exists()


def test_parse_annotated_sql_queries_and_validation(tmp_path: Path) -> None:
    sql_file = tmp_path / "queries.sql"
    sql_file.write_text(
        """
        -- Leading doc comment ignored
        -- name: lookup_mark
        -- params: ["CM2102", 65]
        SELECT student_id
        FROM enrolments
        WHERE module_code = ? AND mark >= ?;

        -- name: count_rows
        SELECT COUNT(*) FROM enrolments;
        """,
        encoding="utf-8",
    )

    specs = load_query_specs(sql_file)
    assert len(specs) == 2
    assert specs[0].name == "lookup_mark"
    assert specs[0].params == ["CM2102", 65]
    assert specs[1].name == "count_rows"
    assert specs[1].params == []

    with pytest.raises(ValueError, match="Duplicate query name"):
        parse_sql_queries("-- name: a\nSELECT 1;\n-- name: a\nSELECT 2;")
    with pytest.raises(ValueError, match="JSON array"):
        parse_sql_queries('-- name: a\n-- params: {"k": 1}\nSELECT 1;')


def test_cli_capture_explain_and_check_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queries_sql = tmp_path / "queries.sql"
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
    queries_sql.write_text(
        '-- name: find_by_email\n-- params: ["a@b.c"]\nSELECT id FROM students WHERE email = ?;\n',
        encoding="utf-8",
    )

    assert main(["explain", ":memory:", str(queries_sql), "--schema", str(schema_good)]) == 0
    explain_out = capsys.readouterr().out
    assert "find_by_email |" in explain_out

    assert main(["capture", ":memory:", str(queries_sql), "--schema", str(schema_good), "-o", str(baseline_json)]) == 0
    capsys.readouterr()

    assert main(["check", ":memory:", str(queries_sql), "--schema", str(schema_good), "--baseline", str(baseline_json)]) == 0
    ok_payload = json.loads(capsys.readouterr().out)
    assert ok_payload["passed"] is True

    assert (
        main(
            [
                "check",
                ":memory:",
                str(queries_sql),
                "--schema",
                str(schema_bad),
                "--baseline",
                str(baseline_json),
                "--format",
                "text",
            ]
        )
        == 1
    )
    fail_text = capsys.readouterr().out
    assert "FAILED" in fail_text
    assert "find_by_email" in fail_text


def test_cli_returns_exit_code_2_on_missing_input_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_queries = tmp_path / "missing.json"
    assert main(["explain", ":memory:", str(missing_queries)]) == 2
    assert "error:" in capsys.readouterr().err
