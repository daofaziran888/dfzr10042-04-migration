"""演示数据：两份同名查询（Q-1001）的旧导出快照 + 一份迁移前的历史日报。

- Q-1001_2026-11-21：字段摘要 + 旧数据文件（8 元、12 元两行，可核对）；
- Q-1001_2026-11-22：同名查询，只有字段摘要，没有数据文件——用来验证
  迁移后不会借用前一份文件给后一份伪造两行；
- 历史日报 daily-2026-11-21：迁移前由旧管线生成，证据指向旧文件（合计 20 元）。
"""
from __future__ import annotations

import shutil
from typing import Any

from app.snapshot_migration.core import State, atomic_write_json, sha256_file

QUERY_NO = "Q-1001"
SNAP_WITH_FILE = "Q-1001_2026-11-21"
SNAP_SUMMARY_ONLY = "Q-1001_2026-11-22"
LEGACY_REPORT_ID = "daily-2026-11-21"

FIELDS = [
    {"name": "day", "type": "date"},
    {"name": "amount", "type": "number"},
]

DATA_CSV = "day,amount\n2026-11-21,8\n2026-11-21,12\n"


def _write_summary(export_dir, snapshot_date: str) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(export_dir / "query.json", {
        "query_no": QUERY_NO,
        "snapshot_date": snapshot_date,
        "fields": FIELDS,
        "claimed_rows": 2,
        "claimed_total_amount": 20,
    })


def seed_demo(state: State, *, reset: bool = False) -> dict[str, Any]:
    """写入演示旧导出与历史日报；reset=True 时先清空状态目录。"""
    if reset and state.root.exists():
        shutil.rmtree(state.root)
    if state.legacy_exports.is_dir():
        return {"status": "ok", "seeded": False, "detail": "演示数据已存在，未重复写入"}

    export_a = state.legacy_exports / SNAP_WITH_FILE
    _write_summary(export_a, "2026-11-21")
    (export_a / "data.csv").write_text(DATA_CSV, encoding="utf-8")

    _write_summary(state.legacy_exports / SNAP_SUMMARY_ONLY, "2026-11-22")

    atomic_write_json(state.reports_legacy / f"{LEGACY_REPORT_ID}.json", {
        "report_id": LEGACY_REPORT_ID,
        "query_no": QUERY_NO,
        "snapshot_date": "2026-11-21",
        "fields": ["day", "amount"],
        "field_types": {"day": "date", "amount": "number"},
        "row_count": 2,
        "total_amount": 20,
        "evidence": {
            "kind": "legacy_file",
            "path": f"legacy_exports/{SNAP_WITH_FILE}/data.csv",
            "sha256": sha256_file(export_a / "data.csv"),
            "recorded_at": "2026-11-21T23:59:00+00:00",
        },
        "generated_by": "legacy-pipeline",
    })
    return {
        "status": "ok",
        "seeded": True,
        "snapshots": [SNAP_WITH_FILE, SNAP_SUMMARY_ONLY],
        "legacy_report": LEGACY_REPORT_ID,
    }


def ensure_seeded(state: State) -> None:
    """状态目录为空时补上演示数据，让服务启动后即可走通完整故事。"""
    if not state.legacy_exports.is_dir():
        seed_demo(state)
