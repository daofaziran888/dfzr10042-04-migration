"""分析快照字段清单迁移子系统。

把"只有字段摘要和查询编号"的旧导出迁移为带投影顺序、列类型、实际行数和
文件校验值的分析快照字段清单，并保证：

- 迁移分 build → verify → enable_reader 三段，journal 断点可接续；
- 历史日报始终可按旧证据文件回算，与迁移进度无关；
- 新版取数只引用核验过的文件，待核/缺文件状态明确可说明；
- 一份日报只钉住一个有效投影和一个真实文件来源，取数后可按当次投影回算；
- 缺文件的同名快照停留在可说明状态，不借用其他快照的文件伪造数据行。
"""
from __future__ import annotations

from app.snapshot_migration.core import State
from app.snapshot_migration.fixtures import (
    LEGACY_REPORT_ID,
    QUERY_NO,
    SNAP_SUMMARY_ONLY,
    SNAP_WITH_FILE,
    seed_demo,
)
from app.snapshot_migration.manifests import (
    FAILED,
    MISSING_FILE,
    PENDING,
    VERIFIED,
    build_manifest,
    verify_manifest,
)
from app.snapshot_migration.migration import (
    MigrationInterrupted,
    build,
    completed_phases,
    enable_reader,
    resume,
    status,
    verify,
)
from app.snapshot_migration.reports import (
    audit,
    fetch_new,
    generate_daily_new,
    recompute_legacy_report,
    recompute_new_report,
)

__all__ = [
    "State",
    "seed_demo",
    "QUERY_NO",
    "SNAP_WITH_FILE",
    "SNAP_SUMMARY_ONLY",
    "LEGACY_REPORT_ID",
    "PENDING",
    "VERIFIED",
    "MISSING_FILE",
    "FAILED",
    "build_manifest",
    "verify_manifest",
    "MigrationInterrupted",
    "build",
    "verify",
    "enable_reader",
    "resume",
    "status",
    "completed_phases",
    "recompute_legacy_report",
    "fetch_new",
    "generate_daily_new",
    "recompute_new_report",
    "audit",
]
