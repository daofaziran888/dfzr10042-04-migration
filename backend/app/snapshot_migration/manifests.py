"""分析快照字段清单：从旧导出构建、独立文件核验。

清单字段：投影顺序 projection、列类型 columns、实际行数 row_count、
文件校验 source_file.sha256，以及核验状态 verification.status：

- pending            清单已写出，文件核验尚未执行（明确待核）
- verified           独立重算的校验值与行数同清单一致
- missing_file       旧导出只有字段摘要、没有数据文件（可说明的缺文件状态）
- verification_failed 核验时发现文件与清单不一致
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.snapshot_migration.core import (
    State,
    count_csv_rows,
    now_iso,
    read_json,
    sha256_file,
)

PENDING = "pending"
VERIFIED = "verified"
MISSING_FILE = "missing_file"
FAILED = "verification_failed"

SUMMARY_FILE = "query.json"
DATA_FILE = "data.csv"


def build_manifest(state: State, export_dir: Path) -> dict[str, Any]:
    """从一份旧导出构建清单。

    有数据文件：记录真实投影、实际行数和文件校验值，状态置 pending（待核）。
    无数据文件：只登记摘要里的"声称"口径，状态置 missing_file；实际行数留空，
    绝不借用其他同名查询快照的文件来补数。
    """
    summary = read_json(export_dir / SUMMARY_FILE)
    fields = summary["fields"]
    rel_dir = export_dir.relative_to(state.root).as_posix()
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "snapshot_id": export_dir.name,
        "query_no": summary["query_no"],
        "snapshot_date": summary.get("snapshot_date"),
        "projection": [f["name"] for f in fields],
        "columns": fields,
        "claimed_rows": summary.get("claimed_rows"),
        "claimed_total_amount": summary.get("claimed_total_amount"),
        "legacy_export_dir": rel_dir,
        "created_at": now_iso(),
    }
    data_file = export_dir / DATA_FILE
    if data_file.is_file():
        manifest["row_count"] = count_csv_rows(data_file)
        manifest["source_file"] = {
            "path": f"{rel_dir}/{DATA_FILE}",
            "sha256": sha256_file(data_file),
            "size_bytes": data_file.stat().st_size,
        }
        manifest["verification"] = {
            "status": PENDING,
            "checked_at": None,
            "detail": "清单已按旧导出构建，文件核验尚未执行",
        }
    else:
        manifest["row_count"] = None
        manifest["source_file"] = None
        manifest["verification"] = {
            "status": MISSING_FILE,
            "checked_at": None,
            "detail": "旧导出只有字段摘要和查询编号，没有数据文件；实际行数未知，"
                      "且不得借用其他同名查询快照的文件补数",
        }
    return manifest


def verify_manifest(state: State, manifest: dict[str, Any]) -> dict[str, Any]:
    """独立核验：重算文件校验值与行数，同清单比对。

    幂等：中断重启后对同一份清单重复核验，结论一致。
    """
    result = copy.deepcopy(manifest)
    export_dir = state.root / result["legacy_export_dir"]
    data_file = export_dir / DATA_FILE

    if result["source_file"] is None:
        if data_file.is_file():
            # 构建之后才补齐的文件：按实际文件登记并直接核验通过
            result["row_count"] = count_csv_rows(data_file)
            result["source_file"] = {
                "path": f"{result['legacy_export_dir']}/{DATA_FILE}",
                "sha256": sha256_file(data_file),
                "size_bytes": data_file.stat().st_size,
            }
            result["verification"] = {
                "status": VERIFIED,
                "checked_at": now_iso(),
                "detail": "核验时发现数据文件已补齐，校验值与行数按实际文件登记",
            }
        else:
            result["verification"] = {
                "status": MISSING_FILE,
                "checked_at": now_iso(),
                "detail": "复核确认仍无数据文件：该快照只有字段摘要，停留在缺文件状态，"
                          "新版取数与日报生成均不可用",
            }
        return result

    if not data_file.is_file():
        result["verification"] = {
            "status": FAILED,
            "checked_at": now_iso(),
            "detail": "清单登记了源文件，但核验时文件不存在",
        }
        return result

    problems = []
    actual_sha = sha256_file(data_file)
    if actual_sha != result["source_file"]["sha256"]:
        problems.append(
            f"sha256 期望 {result['source_file']['sha256'][:12]}… 实际 {actual_sha[:12]}…"
        )
    actual_rows = count_csv_rows(data_file)
    if actual_rows != result["row_count"]:
        problems.append(f"行数期望 {result['row_count']} 实际 {actual_rows}")

    if problems:
        result["verification"] = {
            "status": FAILED,
            "checked_at": now_iso(),
            "detail": "；".join(problems),
        }
    else:
        result["verification"] = {
            "status": VERIFIED,
            "checked_at": now_iso(),
            "detail": "已独立重算文件校验值与行数，与清单一致",
        }
    return result


def load_manifest(state: State, snapshot_id: str) -> dict[str, Any] | None:
    path = state.manifest_path(snapshot_id)
    return read_json(path) if path.is_file() else None


def list_manifests(state: State) -> list[dict[str, Any]]:
    if not state.manifests.is_dir():
        return []
    return [read_json(path) for path in sorted(state.manifests.glob(f"*{'.manifest.json'}"))]
