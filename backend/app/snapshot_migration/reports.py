"""日报取数与回算。

- 历史日报回算：只读旧证据文件，与迁移进度无关（旧证据下的 20 元）；
- 新版取数 fetch_new：只信核验通过的清单；待核/缺文件/核验失败都明确拒绝，
  取数当下重算文件校验值与行数，保证真实文件来源；
- 新版日报 generate_daily_new：新版读入启用后才允许生成，且只引用核验过的
  文件；日报钉住当次采用的一个投影、列类型、行数和文件校验值；
- 回算 recompute_new_report：按日报钉住的投影与核验文件重算，投影漂移只标记、
  不影响按当次口径复查；
- audit：复查缺文件记录与全部报告证据。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.snapshot_migration.core import (
    State,
    atomic_write_json,
    json_value,
    now_iso,
    read_json,
    read_projection_rows,
    sha256_file,
    sum_amount,
)
from app.snapshot_migration.manifests import (
    FAILED,
    MISSING_FILE,
    PENDING,
    VERIFIED,
    list_manifests,
    load_manifest,
)
from app.snapshot_migration.migration import completed_phases, new_reader_enabled


def recompute_legacy_report(state: State, report_id: str) -> dict[str, Any]:
    """历史日报回算：只按旧证据文件重算，不触碰清单与迁移状态。"""
    report_path = state.reports_legacy / f"{report_id}.json"
    if not report_path.is_file():
        return {"status": "error", "detail": f"历史日报不存在：{report_id}"}
    report = read_json(report_path)
    evidence = report["evidence"]
    evidence_path = state.root / evidence["path"]
    if not evidence_path.is_file():
        return {
            "status": "evidence_missing",
            "report_id": report_id,
            "detail": f"旧证据文件丢失：{evidence['path']}",
        }
    actual_sha = sha256_file(evidence_path)
    sha_ok = actual_sha == evidence.get("sha256")
    rows = read_projection_rows(evidence_path, report["fields"], report["field_types"])
    total = sum_amount(rows)
    ok = (
        sha_ok
        and json_value(total) == report["total_amount"]
        and len(rows) == report["row_count"]
    )
    return {
        "status": "ok" if ok else "mismatch",
        "report_id": report_id,
        "source": "legacy_file",
        "evidence_path": evidence["path"],
        "evidence_sha256_ok": sha_ok,
        "row_count": len(rows),
        "total_amount": json_value(total),
        "expected_total_amount": report["total_amount"],
        "detail": "按旧证据文件回算，与迁移/清单状态无关",
    }


def fetch_new(state: State, snapshot_id: str) -> dict[str, Any]:
    """新版取数：只从核验通过的清单读取真实投影和字段数据。"""
    manifest = load_manifest(state, snapshot_id)
    if manifest is None:
        return {"status": "no_manifest", "snapshot_id": snapshot_id,
                "detail": "清单尚未构建"}
    verification = manifest["verification"]
    status = verification["status"]
    if status == PENDING:
        return {
            "status": "pending_verification",
            "snapshot_id": snapshot_id,
            "detail": f"清单已构建但文件核验未完成（明确待核）：{verification['detail']}",
        }
    if status == MISSING_FILE:
        return {
            "status": "missing_file",
            "snapshot_id": snapshot_id,
            "claimed_rows": manifest.get("claimed_rows"),
            "detail": f"缺文件：{verification['detail']}",
        }
    if status == FAILED:
        return {"status": "verification_failed", "snapshot_id": snapshot_id,
                "detail": verification["detail"]}

    # verified：取数当下重算校验值与行数，确认真实文件来源没有被换
    source = manifest["source_file"]
    source_path = state.root / source["path"]
    if not source_path.is_file():
        return {"status": "source_file_lost", "snapshot_id": snapshot_id,
                "detail": "清单已核验但源文件当前不存在"}
    actual_sha = sha256_file(source_path)
    if actual_sha != source["sha256"]:
        return {"status": "source_file_changed", "snapshot_id": snapshot_id,
                "detail": "源文件校验值与已核验清单不一致，拒绝取数"}
    col_types = {c["name"]: c["type"] for c in manifest["columns"]}
    try:
        rows = read_projection_rows(source_path, manifest["projection"], col_types)
    except ValueError as exc:
        return {"status": "projection_unreadable", "snapshot_id": snapshot_id,
                "detail": str(exc)}
    if len(rows) != manifest["row_count"]:
        return {"status": "row_count_changed", "snapshot_id": snapshot_id,
                "detail": f"行数 {len(rows)} 与已核验清单 {manifest['row_count']} 不一致，拒绝取数"}
    return {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "query_no": manifest["query_no"],
        "snapshot_date": manifest["snapshot_date"],
        "projection": manifest["projection"],
        "columns": manifest["columns"],
        "row_count": len(rows),
        "rows": [{k: json_value(v) for k, v in row.items()} for row in rows],
        "evidence": {
            "kind": "verified_file",
            "path": source["path"],
            "sha256": actual_sha,
            "manifest": state.manifest_path(snapshot_id).name,
            "verified_at": verification["checked_at"],
        },
    }


def generate_daily_new(state: State, snapshot_id: str) -> dict[str, Any]:
    """生成新版日报：新版读入已启用 + 文件已核验，两者缺一不可。"""
    if not new_reader_enabled(state):
        return {
            "status": "reader_not_enabled",
            "detail": "新版读入尚未启用（升级确认前，生产日报不切换取数路径）",
        }
    fetched = fetch_new(state, snapshot_id)
    if fetched["status"] != "ok":
        return {
            "status": "refused",
            "reason": fetched["status"],
            "detail": "新生成的日报只能引用核验过的文件",
            "fetch": fetched,
        }
    total = sum((Decimal(str(row["amount"])) for row in fetched["rows"]), Decimal(0))
    report = {
        "report_id": f"daily-{fetched['snapshot_date']}",
        "query_no": fetched["query_no"],
        "snapshot_id": fetched["snapshot_id"],
        "snapshot_date": fetched["snapshot_date"],
        # 一份日报只钉住一个有效投影和一个真实文件来源
        "projection": fetched["projection"],
        "columns": fetched["columns"],
        "row_count": fetched["row_count"],
        "total_amount": json_value(total),
        "evidence": fetched["evidence"],
        "generated_by": "new-reader v1",
        "generated_at": now_iso(),
    }
    atomic_write_json(state.reports_new / f"{report['report_id']}.json", report)
    return {"status": "generated", "report": report}


def recompute_new_report(state: State, report_id: str) -> dict[str, Any]:
    """按日报当次采用的投影回算：数据源以清单核验的文件为准。"""
    report_path = state.reports_new / f"{report_id}.json"
    if not report_path.is_file():
        return {"status": "error", "detail": f"新版日报不存在：{report_id}"}
    report = read_json(report_path)
    evidence = report["evidence"]

    projection_drift = False
    manifest = load_manifest(state, report["snapshot_id"])
    if manifest is not None:
        source = manifest.get("source_file")
        if source is None or source["sha256"] != evidence["sha256"]:
            return {"status": "evidence_broken",
                    "detail": "清单源文件校验值与日报证据不一致"}
        evidence_path = state.root / source["path"]
        projection_drift = manifest.get("projection") != report["projection"]
    else:
        evidence_path = state.root / evidence["path"]  # 清单缺失时仍可按日报内证据复查

    if not evidence_path.is_file():
        return {"status": "evidence_missing",
                "detail": f"证据文件丢失：{evidence['path']}"}
    actual_sha = sha256_file(evidence_path)
    if actual_sha != evidence["sha256"]:
        return {"status": "evidence_broken", "detail": "证据文件校验值已变化"}

    col_types = {c["name"]: c["type"] for c in report["columns"]}
    rows = read_projection_rows(evidence_path, report["projection"], col_types)
    total = sum_amount(rows)
    ok = json_value(total) == report["total_amount"] and len(rows) == report["row_count"]
    return {
        "status": "ok" if ok else "mismatch",
        "report_id": report_id,
        "projection_used": report["projection"],
        "projection_drift": projection_drift,
        "row_count": len(rows),
        "total_amount": json_value(total),
        "expected_total_amount": report["total_amount"],
        "evidence_sha256": actual_sha,
        "detail": "按日报当次采用的投影与核验文件回算",
    }


def audit(state: State) -> dict[str, Any]:
    """复查：迁移阶段、清单状态、缺文件记录、全部报告证据。"""
    manifests = []
    missing_records = []
    for m in list_manifests(state):
        verification = m["verification"]
        entry = {
            "snapshot_id": m["snapshot_id"],
            "query_no": m["query_no"],
            "snapshot_date": m["snapshot_date"],
            "verification": verification["status"],
            "row_count": m["row_count"],
            "claimed_rows": m.get("claimed_rows"),
            "source_sha256": (m.get("source_file") or {}).get("sha256"),
            "detail": verification["detail"],
        }
        manifests.append(entry)
        if verification["status"] == MISSING_FILE:
            missing_records.append(entry)

    def _reports(report_dir) -> list[dict[str, Any]]:
        if not report_dir.is_dir():
            return []
        return [
            {
                "report_id": r["report_id"],
                "total_amount": r["total_amount"],
                "evidence": r["evidence"],
            }
            for r in (read_json(p) for p in sorted(report_dir.glob("*.json")))
        ]

    return {
        "completed_phases": completed_phases(state),
        "new_reader_enabled": new_reader_enabled(state),
        "manifests": manifests,
        "missing_file_records": missing_records,
        "reports": {
            "legacy": _reports(state.reports_legacy),
            "new": _reports(state.reports_new),
        },
    }
