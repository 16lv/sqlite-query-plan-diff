from __future__ import annotations

import argparse
import json
import sqlite3
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


def classify_access_kind(steps: Sequence[str]) -> str:
    joined = " | ".join(step.upper() for step in steps)
    if not joined:
        return "UNKNOWN"
    if "SCAN " in joined or "SCAN TABLE" in joined:
        return "FULL_SCAN"
    if "USING COVERING INDEX" in joined:
        return "COVERING_INDEX"
    if "USING INDEX" in joined or "USING PRIMARY KEY" in joined or "SEARCH " in joined:
        return "INDEX_SEARCH"
    return "OTHER"


def _strip_explain_prefix(sql: str) -> str:
    cleaned = sql.strip().rstrip(";")
    upper = cleaned.upper()
    if upper.startswith("EXPLAIN QUERY PLAN "):
        return cleaned[len("EXPLAIN QUERY PLAN ") :].strip()
    return cleaned


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


def capture_snapshots(
    db_path: str | Path,
    queries: Sequence[QuerySpec],
    init_sql: str | None = None,
) -> list[PlanSnapshot]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
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
    baseline_by_name = {item.name: item for item in baseline}
    current_by_name = {item.name: item for item in current}
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


def load_query_specs(path: Path) -> list[QuerySpec]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("Queries JSON file must contain a top-level array.")
    specs: list[QuerySpec] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TypeError(f"Query entry at index {idx} must be a JSON object.")
        specs.append(
            QuerySpec(
                name=str(entry.get("name", "")),
                sql=str(entry.get("sql", "")),
                params=list(entry.get("params", [])),
            )
        )
    return specs


def save_baseline(snapshots: Sequence[PlanSnapshot], output_path: Path) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([asdict(s) for s in snapshots], indent=2),
        encoding="utf-8",
    )


def load_baseline(path: Path) -> list[PlanSnapshot]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture and diff SQLite EXPLAIN QUERY PLAN baselines to catch index regressions."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="Capture EXPLAIN QUERY PLAN snapshots to a JSON file")
    cap.add_argument("db", help="Path to SQLite database file (or :memory:)")
    cap.add_argument("queries", type=Path, help="Path to JSON array of query specifications")
    cap.add_argument("--schema", type=Path, default=None, help="Optional SQL schema file to execute")
    cap.add_argument("-o", "--output", type=Path, required=True, help="Output baseline JSON path")

    chk = sub.add_parser("check", help="Compare current SQLite query plans against a saved baseline")
    chk.add_argument("db", help="Path to SQLite database file (or :memory:)")
    chk.add_argument("queries", type=Path, help="Path to JSON array of query specifications")
    chk.add_argument("--baseline", type=Path, required=True, help="Saved baseline JSON file")
    chk.add_argument("--schema", type=Path, default=None, help="Optional SQL schema file to execute")
    chk.add_argument(
        "--strict-temp-btree",
        action="store_true",
        help="Treat newly introduced TEMP B-TREE sorting steps as regressions",
    )

    args = parser.parse_args(argv)
    init_sql = args.schema.read_text(encoding="utf-8") if args.schema else None
    specs = load_query_specs(args.queries)
    snapshots = capture_snapshots(args.db, specs, init_sql=init_sql)

    if args.command == "capture":
        save_baseline(snapshots, args.output)
        print(json.dumps({"captured": len(snapshots), "baseline": str(args.output)}, indent=2))
        return 0

    baseline = load_baseline(args.baseline)
    report = diff_snapshots(baseline, snapshots, strict_temp_btree=args.strict_temp_btree)
    print(json.dumps(asdict(report), indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
