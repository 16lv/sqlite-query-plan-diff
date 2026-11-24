from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QuerySpec:
    name: str
    sql: str
    params: list[Any]


@dataclass(frozen=True)
class PlanSnapshot:
    name: str
    sql: str
    steps: list[str]
    access_kind: str
    uses_temp_btree: bool


@dataclass(frozen=True)
class PlanFinding:
    query_name: str
    severity: str
    reason: str
    baseline_kind: str | None
    current_kind: str | None
    baseline_steps: list[str]
    current_steps: list[str]


@dataclass(frozen=True)
class PlanReport:
    passed: bool
    checked_queries: int
    regressions: list[PlanFinding]


_SQL_NAME_RE = re.compile(r"^\s*--\s*name:\s*(.+?)\s*$", re.IGNORECASE)
_SQL_PARAMS_RE = re.compile(r"^\s*--\s*params:\s*(.+?)\s*$", re.IGNORECASE)
_EXPLAIN_PREFIX_RE = re.compile(r"^EXPLAIN\s+QUERY\s+PLAN\b", re.IGNORECASE)


def _is_unindexed_scan(upper_step: str) -> bool:
    if not ("SCAN " in upper_step or "SCAN TABLE" in upper_step):
        return False
    return not (
        "USING INDEX" in upper_step
        or "USING COVERING INDEX" in upper_step
        or "USING PRIMARY KEY" in upper_step
        or "USING INTEGER PRIMARY KEY" in upper_step
    )


def classify_access_kind(steps: Sequence[str]) -> str:
    upper_steps = [step.upper() for step in steps if step.strip()]
    if not upper_steps:
        return "UNKNOWN"
    if any(_is_unindexed_scan(step) for step in upper_steps):
        return "FULL_SCAN"

    has_covering = any("USING COVERING INDEX" in step for step in upper_steps)
    has_non_covering = any(
        ("USING INDEX" in step or "USING PRIMARY KEY" in step or "USING INTEGER PRIMARY KEY" in step)
        and "USING COVERING INDEX" not in step
        for step in upper_steps
    )
    if has_covering and not has_non_covering:
        return "COVERING_INDEX"

    joined = " | ".join(upper_steps)
    if (
        "USING INDEX" in joined
        or "USING COVERING INDEX" in joined
        or "USING PRIMARY KEY" in joined
        or "USING INTEGER PRIMARY KEY" in joined
        or "SEARCH " in joined
    ):
        return "INDEX_SEARCH"
    return "OTHER"


def _strip_explain_prefix(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    return _EXPLAIN_PREFIX_RE.sub("", cleaned, count=1).strip()


def _connect_sqlite(db_path: str | Path, read_only: bool) -> sqlite3.Connection:
    db_str = str(db_path)
    if db_str == ":memory:" or db_str.startswith("file:"):
        conn = sqlite3.connect(db_str, uri=db_str.startswith("file:"))
    elif read_only:
        resolved = Path(db_path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"SQLite database file not found: {db_path}")
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_str)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def capture_plan(conn: sqlite3.Connection, spec: QuerySpec) -> PlanSnapshot:
    clean_name = spec.name.strip()
    if not clean_name:
        raise ValueError("QuerySpec name must not be blank.")
    base_sql = _strip_explain_prefix(spec.sql)
    if not base_sql:
        raise ValueError(f"QuerySpec '{clean_name}' must contain non-empty SQL.")

    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"EXPLAIN QUERY PLAN {base_sql}", tuple(spec.params)).fetchall()
    steps = [str(row["detail"]) for row in rows]
    uses_temp_btree = any("TEMP B-TREE" in step.upper() for step in steps)
    return PlanSnapshot(
        name=clean_name,
        sql=base_sql,
        steps=steps,
        access_kind=classify_access_kind(steps),
        uses_temp_btree=uses_temp_btree,
    )


def _index_snapshots_unique(
    snapshots: Sequence[PlanSnapshot],
    label: str,
) -> dict[str, PlanSnapshot]:
    by_name: dict[str, PlanSnapshot] = {}
    for snap in snapshots:
        if snap.name in by_name:
            raise ValueError(f"Duplicate query name '{snap.name}' found in {label}.")
        by_name[snap.name] = snap
    return by_name


def capture_snapshots(
    db_path: str | Path,
    queries: Sequence[QuerySpec],
    init_sql: str | None = None,
) -> list[PlanSnapshot]:
    seen_names: set[str] = set()
    for q in queries:
        clean = q.name.strip()
        if clean and clean in seen_names:
            raise ValueError(f"Duplicate QuerySpec name: '{clean}'.")
        if clean:
            seen_names.add(clean)

    read_only = init_sql is None
    conn = _connect_sqlite(db_path, read_only=read_only)
    try:
        if init_sql:
            conn.executescript(init_sql)
        return [capture_plan(conn, q) for q in queries]
    finally:
        conn.close()


def diff_snapshots(
    baseline: Sequence[PlanSnapshot],
    current: Sequence[PlanSnapshot],
    strict_temp_btree: bool = False,
) -> PlanReport:
    baseline_by_name = _index_snapshots_unique(baseline, "baseline")
    current_by_name = _index_snapshots_unique(current, "current snapshots")
    findings: list[PlanFinding] = []

    for name, curr in current_by_name.items():
        prev = baseline_by_name.get(name)
        if prev is None:
            if curr.access_kind == "FULL_SCAN":
                findings.append(
                    PlanFinding(
                        query_name=name,
                        severity="REGRESSION",
                        reason="New query resolves via a full table scan.",
                        baseline_kind=None,
                        current_kind=curr.access_kind,
                        baseline_steps=[],
                        current_steps=curr.steps,
                    )
                )
            continue

        if prev.access_kind in ("COVERING_INDEX", "INDEX_SEARCH") and curr.access_kind == "FULL_SCAN":
            findings.append(
                PlanFinding(
                    query_name=name,
                    severity="REGRESSION",
                    reason="Indexed lookup regressed to a full table scan.",
                    baseline_kind=prev.access_kind,
                    current_kind=curr.access_kind,
                    baseline_steps=prev.steps,
                    current_steps=curr.steps,
                )
            )
        elif prev.access_kind == "COVERING_INDEX" and curr.access_kind == "INDEX_SEARCH":
            findings.append(
                PlanFinding(
                    query_name=name,
                    severity="REGRESSION",
                    reason="Covering index plan now requires table row fetches.",
                    baseline_kind=prev.access_kind,
                    current_kind=curr.access_kind,
                    baseline_steps=prev.steps,
                    current_steps=curr.steps,
                )
            )
        elif strict_temp_btree and not prev.uses_temp_btree and curr.uses_temp_btree:
            findings.append(
                PlanFinding(
                    query_name=name,
                    severity="REGRESSION",
                    reason="Execution plan introduced a temporary B-tree sort/grouping pass.",
                    baseline_kind=prev.access_kind,
                    current_kind=curr.access_kind,
                    baseline_steps=prev.steps,
                    current_steps=curr.steps,
                )
            )

    for name, prev in baseline_by_name.items():
        if name not in current_by_name:
            findings.append(
                PlanFinding(
                    query_name=name,
                    severity="MISSING",
                    reason="Query present in baseline was omitted from current check.",
                    baseline_kind=prev.access_kind,
                    current_kind=None,
                    baseline_steps=prev.steps,
                    current_steps=[],
                )
            )

    return PlanReport(
        passed=len(findings) == 0,
        checked_queries=len(current),
        regressions=findings,
    )


def parse_sql_queries(sql_text: str) -> list[QuerySpec]:
    """Parse named SQL queries annotated with '-- name: <id>' and optional '-- params: [...]'."""
    specs: list[QuerySpec] = []
    current_name: str | None = None
    current_params: list[Any] = []
    sql_lines: list[str] = []

    def _flush() -> None:
        if current_name is None:
            joined = "\n".join(sql_lines).strip()
            if joined:
                raise ValueError("SQL block found before any '-- name: <query_name>' header.")
            return
        body = "\n".join(sql_lines).strip()
        if not body:
            raise ValueError(f"Annotated query '{current_name}' has an empty SQL body.")
        specs.append(QuerySpec(name=current_name, sql=body, params=current_params))

    for raw_line in sql_text.splitlines():
        name_match = _SQL_NAME_RE.match(raw_line)
        if name_match:
            _flush()
            current_name = name_match.group(1).strip()
            if not current_name:
                raise ValueError("Encountered empty '-- name:' annotation.")
            current_params = []
            sql_lines = []
            continue

        params_match = _SQL_PARAMS_RE.match(raw_line)
        if params_match:
            if current_name is None:
                raise ValueError("Encountered '-- params:' before '-- name:'.")
            parsed_params = json.loads(params_match.group(1))
            if not isinstance(parsed_params, list):
                raise ValueError(f"Parameters for query '{current_name}' must be a JSON array.")
            current_params = parsed_params
            continue

        if raw_line.strip().startswith("--") or not raw_line.strip():
            continue
        sql_lines.append(raw_line)

    _flush()
    if not specs:
        raise ValueError("No '-- name:' queries found in SQL query file.")

    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"Duplicate query name '{spec.name}' in SQL query file.")
        seen.add(spec.name)
    return specs


def load_query_specs(path: Path) -> list[QuerySpec]:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Queries file not found: {target}")

    content = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".sql":
        return parse_sql_queries(content)

    raw = json.loads(content)
    if not isinstance(raw, list):
        raise TypeError("Queries JSON file must contain a top-level array.")
    specs: list[QuerySpec] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TypeError(f"Query entry at index {idx} must be a JSON object.")
        params = entry.get("params", [])
        if not isinstance(params, list):
            raise TypeError(f"Query entry '{entry.get('name', idx)}' params must be a JSON array.")
        name = str(entry.get("name", ""))
        clean_name = name.strip()
        if clean_name and clean_name in seen:
            raise ValueError(f"Duplicate query name '{clean_name}' in queries file.")
        if clean_name:
            seen.add(clean_name)
        specs.append(QuerySpec(name=name, sql=str(entry.get("sql", "")), params=list(params)))
    return specs


def save_baseline(snapshots: Sequence[PlanSnapshot], output_path: Path) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([asdict(s) for s in snapshots], indent=2),
        encoding="utf-8",
    )


def load_baseline(path: Path) -> list[PlanSnapshot]:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Baseline JSON file not found: {target}")
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("Baseline JSON file must contain a top-level array.")
    return [
        PlanSnapshot(
            name=str(item["name"]),
            sql=str(item["sql"]),
            steps=[str(step) for step in item["steps"]],
            access_kind=str(item["access_kind"]),
            uses_temp_btree=bool(item.get("uses_temp_btree", False)),
        )
        for item in raw
    ]


def _format_explain_text(snapshots: Sequence[PlanSnapshot]) -> str:
    lines: list[str] = []
    for snap in snapshots:
        temp_flag = " [TEMP B-TREE]" if snap.uses_temp_btree else ""
        lines.append(f"{snap.name} | {snap.access_kind}{temp_flag}")
        for step in snap.steps:
            lines.append(f"  -> {step}")
    return "\n".join(lines)


def _format_report_text(report: PlanReport) -> str:
    status = "PASSED" if report.passed else f"FAILED ({len(report.regressions)} issue(s))"
    lines = [f"SQLite Query Plan Check: {status} across {report.checked_queries} queries"]
    for item in report.regressions:
        lines.append(
            f"- [{item.severity}] {item.query_name}: {item.reason} "
            f"({item.baseline_kind or 'NEW'} -> {item.current_kind or 'MISSING'})"
        )
    return "\n".join(lines)


def _add_common_source_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("db", help="Path to SQLite database file (or :memory:)")
    subparser.add_argument(
        "queries",
        type=Path,
        help="Path to .json query array or '-- name:' annotated .sql file",
    )
    subparser.add_argument("--schema", type=Path, default=None, help="Optional SQL schema file to execute")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture, inspect, and diff SQLite EXPLAIN QUERY PLAN baselines to catch index regressions."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="Capture EXPLAIN QUERY PLAN snapshots to a JSON file")
    _add_common_source_args(cap)
    cap.add_argument("-o", "--output", type=Path, required=True, help="Output baseline JSON path")

    exp = sub.add_parser("explain", help="Inspect current EXPLAIN QUERY PLAN strategies without a baseline")
    _add_common_source_args(exp)
    exp.add_argument("--format", choices=("json", "text"), default="text", help="Output format")

    chk = sub.add_parser("check", help="Compare current SQLite query plans against a saved baseline")
    _add_common_source_args(chk)
    chk.add_argument("--baseline", type=Path, required=True, help="Saved baseline JSON file")
    chk.add_argument("--format", choices=("json", "text"), default="json", help="Output format")
    chk.add_argument(
        "--strict-temp-btree",
        action="store_true",
        help="Treat newly introduced TEMP B-TREE sorting steps as regressions",
    )

    args = parser.parse_args(argv)
    try:
        if args.schema and not args.schema.is_file():
            raise FileNotFoundError(f"Schema SQL file not found: {args.schema}")
        init_sql = args.schema.read_text(encoding="utf-8") if args.schema else None
        specs = load_query_specs(args.queries)
        snapshots = capture_snapshots(args.db, specs, init_sql=init_sql)

        if args.command == "capture":
            save_baseline(snapshots, args.output)
            print(json.dumps({"captured": len(snapshots), "baseline": str(args.output)}, indent=2))
            return 0

        if args.command == "explain":
            if args.format == "json":
                print(json.dumps([asdict(s) for s in snapshots], indent=2))
            else:
                print(_format_explain_text(snapshots))
            return 0

        baseline = load_baseline(args.baseline)
        report = diff_snapshots(baseline, snapshots, strict_temp_btree=args.strict_temp_btree)
        if args.format == "json":
            print(json.dumps(asdict(report), indent=2))
        else:
            print(_format_report_text(report))
        return 0 if report.passed else 1
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
