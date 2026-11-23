"""分析快照字段清单迁移的 HTTP 接口：迁移阶段、日报取数/回算、缺文件复查。

状态目录默认 backend/var/snapshot_migration，可用环境变量
SNAPSHOT_MIGRATION_STATE 覆盖；目录为空时自动写入演示数据（两份同名查询
快照 + 一份历史日报），服务启动后即可走通完整迁移故事。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.snapshot_migration import fixtures, migration, reports
from app.snapshot_migration.core import State
from app.snapshot_migration.manifests import load_manifest
from app.snapshot_migration.migration import MigrationInterrupted

router = APIRouter(prefix="/api/snapshot-migration", tags=["快照清单迁移"])

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "var" / "snapshot_migration"


def _state() -> State:
    root = Path(os.environ.get("SNAPSHOT_MIGRATION_STATE", str(DEFAULT_STATE_DIR)))
    state = State(root)
    fixtures.ensure_seeded(state)
    return state


def _run(fn, *args) -> dict[str, Any]:
    try:
        return fn(*args)
    except MigrationInterrupted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/status")
def migration_status() -> dict[str, Any]:
    return migration.status(_state())


@router.post("/build")
def migrate_build() -> dict[str, Any]:
    return _run(migration.build, _state())


@router.post("/verify")
def migrate_verify() -> dict[str, Any]:
    return _run(migration.verify, _state())


@router.post("/resume")
def migrate_resume() -> dict[str, Any]:
    return _run(migration.resume, _state())


@router.post("/enable-reader")
def migrate_enable_reader() -> dict[str, Any]:
    return _run(migration.enable_reader, _state())


@router.get("/audit")
def audit() -> dict[str, Any]:
    return reports.audit(_state())


@router.get("/manifests/{snapshot_id}")
def get_manifest(snapshot_id: str) -> dict[str, Any]:
    manifest = load_manifest(_state(), snapshot_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"清单不存在：{snapshot_id}")
    return manifest


@router.post("/reports/legacy/{report_id}/recompute")
def recompute_legacy(report_id: str) -> dict[str, Any]:
    return reports.recompute_legacy_report(_state(), report_id)


@router.get("/snapshots/{snapshot_id}/fetch")
def fetch_new(snapshot_id: str) -> dict[str, Any]:
    return reports.fetch_new(_state(), snapshot_id)


@router.post("/snapshots/{snapshot_id}/daily-report")
def daily_report(snapshot_id: str) -> dict[str, Any]:
    return reports.generate_daily_new(_state(), snapshot_id)


@router.post("/reports/new/{report_id}/recompute")
def recompute_new(report_id: str) -> dict[str, Any]:
    return reports.recompute_new_report(_state(), report_id)


@router.post("/seed-demo")
def seed_demo() -> dict[str, Any]:
    return fixtures.seed_demo(_state(), reset=True)
