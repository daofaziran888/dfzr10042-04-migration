"""迁移编排：build → verify →（独立升级确认）enable_reader。

- 每个阶段完成后写 journal（JSONL），进程中断重启后从断点接续；
- 清单文件原子写，崩溃不会留下半份清单；
- resume 只补做 build/verify；启用新版读入是独立的升级确认步骤，
  与清单构建、文件核验分开验证；
- 环境变量 SNAPSHOT_MIGRATION_CRASH_AFTER="verify:<snapshot_id>" 用于
  演练/测试在核验落盘后、写 journal 前的硬中断。
"""
from __future__ import annotations

import os
from typing import Any

from app.snapshot_migration.core import (
    State,
    append_jsonl,
    atomic_write_json,
    now_iso,
    read_json,
    read_jsonl,
)
from app.snapshot_migration.manifests import (
    build_manifest,
    list_manifests,
    verify_manifest,
)

PHASE_BUILD = "build"
PHASE_VERIFY = "verify"
PHASE_ENABLE_READER = "enable_reader"

CRASH_ENV = "SNAPSHOT_MIGRATION_CRASH_AFTER"


class MigrationInterrupted(RuntimeError):
    """模拟迁移进程被硬中断（落盘完成、journal 未写）。"""


def completed_phases(state: State) -> list[str]:
    phases: list[str] = []
    for entry in read_jsonl(state.journal):
        phase = entry.get("phase")
        if phase and phase not in phases:
            phases.append(phase)
    return phases


def _record_phase(state: State, phase: str, **extra: Any) -> None:
    append_jsonl(state.journal, {"phase": phase, "ts": now_iso(), **extra})


def load_config(state: State) -> dict[str, Any]:
    if state.config.is_file():
        return read_json(state.config)
    return {"new_reader_enabled": False}


def new_reader_enabled(state: State) -> bool:
    return bool(load_config(state).get("new_reader_enabled"))


def build(state: State) -> dict[str, Any]:
    """阶段一：扫描旧导出，写出分析快照字段清单（核验状态 pending/缺文件）。"""
    done = completed_phases(state)
    if PHASE_VERIFY in done:
        return {
            "status": "error",
            "detail": "文件核验已完成，重新构建会重置核验状态；如需重来请换新的状态目录",
        }
    if not state.legacy_exports.is_dir():
        return {"status": "error", "detail": f"旧导出目录不存在：{state.legacy_exports}"}
    built = []
    for export_dir in sorted(p for p in state.legacy_exports.iterdir() if p.is_dir()):
        manifest = build_manifest(state, export_dir)
        atomic_write_json(state.manifest_path(manifest["snapshot_id"]), manifest)
        built.append({
            "snapshot_id": manifest["snapshot_id"],
            "verification": manifest["verification"]["status"],
            "row_count": manifest["row_count"],
        })
    if PHASE_BUILD not in done:
        _record_phase(state, PHASE_BUILD, manifests=[b["snapshot_id"] for b in built])
    return {"status": "ok", "phase": PHASE_BUILD, "manifests": built}


def verify(state: State) -> dict[str, Any]:
    """阶段二：独立核验文件校验值与行数，幂等，可从断点重入。"""
    done = completed_phases(state)
    if PHASE_BUILD not in done:
        return {"status": "error", "detail": "清单尚未构建，请先执行 build 阶段"}
    crash_after = os.environ.get(CRASH_ENV, "")
    results = []
    for manifest in list_manifests(state):
        verified = verify_manifest(state, manifest)
        atomic_write_json(state.manifest_path(verified["snapshot_id"]), verified)
        results.append({
            "snapshot_id": verified["snapshot_id"],
            "verification": verified["verification"]["status"],
        })
        if crash_after == f"verify:{verified['snapshot_id']}":
            # 清单已原子落盘、journal 未写：模拟进程在此刻被杀
            raise MigrationInterrupted(
                f"核验 {verified['snapshot_id']} 落盘后、写 journal 前进程中断"
            )
    if PHASE_VERIFY not in done:
        _record_phase(state, PHASE_VERIFY, manifests=[r["snapshot_id"] for r in results])
    return {"status": "ok", "phase": PHASE_VERIFY, "manifests": results}


def enable_reader(state: State) -> dict[str, Any]:
    """阶段三（独立升级确认）：文件核验完成后才允许启用新版读入。"""
    done = completed_phases(state)
    if PHASE_VERIFY not in done:
        return {"status": "error", "detail": "文件核验未完成，不能启用新版读入"}
    if new_reader_enabled(state):
        return {"status": "ok", "phase": PHASE_ENABLE_READER, "new_reader_enabled": True,
                "detail": "新版读入此前已启用"}
    atomic_write_json(state.config, {**load_config(state), "new_reader_enabled": True})
    _record_phase(state, PHASE_ENABLE_READER)
    return {"status": "ok", "phase": PHASE_ENABLE_READER, "new_reader_enabled": True}


def resume(state: State) -> dict[str, Any]:
    """断点接续：只补做未完成的 build/verify；启用新版读入必须单独确认。"""
    done = completed_phases(state)
    ran = []
    if PHASE_BUILD not in done:
        result = build(state)
        if result["status"] != "ok":
            return result
        ran.append(PHASE_BUILD)
    if PHASE_VERIFY not in done:
        result = verify(state)
        if result["status"] != "ok":
            return result
        ran.append(PHASE_VERIFY)
    return {
        "status": "ok",
        "resumed_phases": ran,
        "new_reader_enabled": new_reader_enabled(state),
        "detail": "断点接续只完成清单构建与文件核验；启用新版读入是独立的升级确认步骤",
    }


def status(state: State) -> dict[str, Any]:
    return {
        "completed_phases": completed_phases(state),
        "new_reader_enabled": new_reader_enabled(state),
        "manifests": [
            {
                "snapshot_id": m["snapshot_id"],
                "verification": m["verification"]["status"],
                "row_count": m["row_count"],
            }
            for m in list_manifests(state)
        ],
    }
