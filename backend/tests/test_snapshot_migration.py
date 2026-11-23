"""分析快照字段清单迁移 · 端到端测试

覆盖需求主线：
1. 旧导出（字段摘要 + 查询编号）→ 带投影顺序、列类型、实际行数、文件校验的清单；
2. 历史日报按旧文件回算；新版日报只引用核验过的文件；
3. 迁移可从文件核验前的断点接续；清单构建与启用新版读入分开验证；
4. 取数之后按当次采用的投影回算；
5. 同名查询的两份快照：缺文件的那份不借用另一份的文件伪造两行；
6. 中断点并发：历史回算看到旧证据下的 20 元，新版取数明确待核；
7. 缺文件快照停留在可说明状态，证据可复查。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.snapshot_migration import fixtures, migration, reports
from app.snapshot_migration.core import State, atomic_write_json, read_json
from app.snapshot_migration.fixtures import (
    LEGACY_REPORT_ID,
    SNAP_SUMMARY_ONLY,
    SNAP_WITH_FILE,
    seed_demo,
)
from app.snapshot_migration.manifests import (
    MISSING_FILE,
    PENDING,
    VERIFIED,
    load_manifest,
)
from app.snapshot_migration.migration import CRASH_ENV, MigrationInterrupted


@pytest.fixture
def state(tmp_path) -> State:
    st = State(tmp_path / "state")
    seed_demo(st)
    return st


@pytest.fixture
def built(state) -> State:
    result = migration.build(state)
    assert result["status"] == "ok"
    return state


@pytest.fixture
def verified(built) -> State:
    result = migration.verify(built)
    assert result["status"] == "ok"
    return built


@pytest.fixture
def upgraded(verified) -> State:
    result = migration.enable_reader(verified)
    assert result["status"] == "ok"
    return verified


# ---------------------------------------------------------------------------
# 1. 清单构建：投影顺序、列类型、实际行数、文件校验
# ---------------------------------------------------------------------------

def test_build_writes_manifest_with_projection_types_rowcount_checksum(built):
    manifest = load_manifest(built, SNAP_WITH_FILE)
    assert manifest["projection"] == ["day", "amount"]
    assert [c["type"] for c in manifest["columns"]] == ["date", "number"]
    assert manifest["row_count"] == 2  # 实际行数来自文件，不是摘要里的声称值
    assert len(manifest["source_file"]["sha256"]) == 64
    assert manifest["verification"]["status"] == PENDING


def test_build_marks_summary_only_snapshot_missing_file(built):
    manifest = load_manifest(built, SNAP_SUMMARY_ONLY)
    assert manifest["verification"]["status"] == MISSING_FILE
    assert manifest["source_file"] is None
    assert manifest["row_count"] is None
    # 摘要里的"声称 2 行"只作未证实声明保留
    assert manifest["claimed_rows"] == 2
    # 不得借用同名快照的文件：清单全文不出现另一份快照的标识
    assert SNAP_WITH_FILE not in json.dumps(manifest)


def test_verify_marks_file_backed_manifest_verified(built):
    migration.verify(built)
    manifest = load_manifest(built, SNAP_WITH_FILE)
    assert manifest["verification"]["status"] == VERIFIED
    assert manifest["verification"]["checked_at"]
    # 缺文件快照复核后仍停留在可说明的缺文件状态
    assert load_manifest(built, SNAP_SUMMARY_ONLY)["verification"]["status"] == MISSING_FILE


def test_build_after_verify_is_refused(verified):
    result = migration.build(verified)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# 2. 中断点并发冲突：旧证据 20 元 vs 明确待核
# ---------------------------------------------------------------------------

def test_breakpoint_legacy_recompute_and_new_fetch_run_concurrently(built):
    """清单已写出、文件未核验的中断点：两路同时发起，互不干扰。"""
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_legacy = pool.submit(reports.recompute_legacy_report, built, LEGACY_REPORT_ID)
        fut_fetch_a = pool.submit(reports.fetch_new, built, SNAP_WITH_FILE)
        fut_fetch_b = pool.submit(reports.fetch_new, built, SNAP_SUMMARY_ONLY)
        legacy = fut_legacy.result()
        fetch_a = fut_fetch_a.result()
        fetch_b = fut_fetch_b.result()

    # 历史日报回算：旧证据下的 20 元
    assert legacy["status"] == "ok"
    assert legacy["source"] == "legacy_file"
    assert legacy["total_amount"] == 20
    assert legacy["evidence_sha256_ok"] is True

    # 新版取数：明确待核，不偷用未核验文件
    assert fetch_a["status"] == "pending_verification"
    assert "rows" not in fetch_a

    # 缺文件快照：明确缺文件
    assert fetch_b["status"] == "missing_file"
    assert fetch_b["claimed_rows"] == 2  # 声称值仅作说明，不变成数据


# ---------------------------------------------------------------------------
# 3. 断点接续：核验落盘后、写 journal 前崩溃，重启后续接
# ---------------------------------------------------------------------------

def test_crash_during_verify_then_resume_from_breakpoint(built, monkeypatch):
    monkeypatch.setenv(CRASH_ENV, f"verify:{SNAP_WITH_FILE}")
    with pytest.raises(MigrationInterrupted):
        migration.verify(built)

    # journal 只有 build：核验阶段未记完成，断点在文件核验前
    assert migration.completed_phases(built) == ["build"]
    # 原子写保证 A 清单已完整落盘为 verified，没有半文件
    assert load_manifest(built, SNAP_WITH_FILE)["verification"]["status"] == VERIFIED

    # 重启接续：幂等重跑 verify，不擅自启用新版读入
    monkeypatch.delenv(CRASH_ENV)
    result = migration.resume(built)
    assert result["status"] == "ok"
    assert result["resumed_phases"] == ["verify"]
    assert result["new_reader_enabled"] is False
    assert migration.completed_phases(built) == ["build", "verify"]

    # 接续后 A 已核验可取数，B 仍缺文件
    assert reports.fetch_new(built, SNAP_WITH_FILE)["status"] == "ok"
    assert reports.fetch_new(built, SNAP_SUMMARY_ONLY)["status"] == "missing_file"


def test_verify_is_idempotent_after_crash(built, monkeypatch):
    monkeypatch.setenv(CRASH_ENV, f"verify:{SNAP_WITH_FILE}")
    with pytest.raises(MigrationInterrupted):
        migration.verify(built)
    monkeypatch.delenv(CRASH_ENV)
    first = read_json(built.manifest_path(SNAP_WITH_FILE))
    migration.verify(built)
    second = read_json(built.manifest_path(SNAP_WITH_FILE))
    assert first["source_file"]["sha256"] == second["source_file"]["sha256"]
    assert second["verification"]["status"] == VERIFIED


# ---------------------------------------------------------------------------
# 4. 新版日报：启用确认 + 核验文件，缺一不可；取数后按当次投影回算
# ---------------------------------------------------------------------------

def test_daily_report_refused_before_reader_enabled(verified):
    result = reports.generate_daily_new(verified, SNAP_WITH_FILE)
    assert result["status"] == "reader_not_enabled"


def test_enable_reader_requires_verify(built):
    result = migration.enable_reader(built)
    assert result["status"] == "error"
    assert migration.new_reader_enabled(built) is False


def test_daily_report_pins_single_projection_and_real_file_source(upgraded):
    result = reports.generate_daily_new(upgraded, SNAP_WITH_FILE)
    assert result["status"] == "generated"
    report = result["report"]
    assert report["total_amount"] == 20
    assert report["projection"] == ["day", "amount"]  # 一个有效投影
    assert report["evidence"]["kind"] == "verified_file"  # 一个真实文件来源
    assert len(report["evidence"]["sha256"]) == 64

    recompute = reports.recompute_new_report(upgraded, report["report_id"])
    assert recompute["status"] == "ok"
    assert recompute["total_amount"] == 20
    assert recompute["projection_used"] == ["day", "amount"]
    assert recompute["projection_drift"] is False


def test_recompute_uses_pinned_projection_even_if_manifest_drifts(upgraded):
    result = reports.generate_daily_new(upgraded, SNAP_WITH_FILE)
    report_id = result["report"]["report_id"]

    # 清单投影后来被调整（例如新增列导致顺序变化），日报仍按当次投影回算
    manifest = load_manifest(upgraded, SNAP_WITH_FILE)
    drifted = {**manifest, "projection": ["amount", "day"]}
    atomic_write_json(upgraded.manifest_path(SNAP_WITH_FILE), drifted)

    recompute = reports.recompute_new_report(upgraded, report_id)
    assert recompute["status"] == "ok"
    assert recompute["total_amount"] == 20
    assert recompute["projection_used"] == ["day", "amount"]
    assert recompute["projection_drift"] is True  # 漂移被标记，供复查


def test_fetch_rechecks_real_file_source_at_read_time(upgraded):
    data_csv = upgraded.root / "legacy_exports" / SNAP_WITH_FILE / "data.csv"
    original = data_csv.read_bytes()
    try:
        data_csv.write_text("day,amount\n2026-11-21,8\n2026-11-21,99\n", encoding="utf-8")
        fetched = reports.fetch_new(upgraded, SNAP_WITH_FILE)
        assert fetched["status"] == "source_file_changed"
        # 历史回算同样发现证据不一致，旧证据仍可复查
        legacy = reports.recompute_legacy_report(upgraded, LEGACY_REPORT_ID)
        assert legacy["status"] == "mismatch"
        assert legacy["evidence_sha256_ok"] is False
    finally:
        data_csv.write_bytes(original)


# ---------------------------------------------------------------------------
# 5. 缺文件快照：不伪造、可说明、可复查
# ---------------------------------------------------------------------------

def test_missing_file_snapshot_never_fabricates_rows(upgraded):
    result = reports.generate_daily_new(upgraded, SNAP_SUMMARY_ONLY)
    assert result["status"] == "refused"
    assert result["reason"] == "missing_file"

    # 新版目录里没有 B 的日报
    new_reports = list(upgraded.reports_new.glob("*.json"))
    assert all(SNAP_SUMMARY_ONLY not in p.read_text(encoding="utf-8") for p in new_reports)

    # B 的清单仍无行数、无来源文件，没有借用 A 的 8/12 两行
    manifest_b = load_manifest(upgraded, SNAP_SUMMARY_ONLY)
    assert manifest_b["row_count"] is None
    assert manifest_b["source_file"] is None
    assert manifest_b["verification"]["status"] == MISSING_FILE


def test_audit_reviews_missing_file_records(upgraded):
    reports.generate_daily_new(upgraded, SNAP_WITH_FILE)
    result = reports.audit(upgraded)

    assert result["completed_phases"] == ["build", "verify", "enable_reader"]
    assert result["new_reader_enabled"] is True

    missing = result["missing_file_records"]
    assert len(missing) == 1
    record = missing[0]
    assert record["snapshot_id"] == SNAP_SUMMARY_ONLY
    assert record["row_count"] is None
    assert record["claimed_rows"] == 2
    assert "没有数据文件" in record["detail"] or "无数据文件" in record["detail"]

    # 历史日报与新版日报的证据都可复查
    assert result["reports"]["legacy"][0]["evidence"]["kind"] == "legacy_file"
    assert result["reports"]["new"][0]["evidence"]["kind"] == "verified_file"


# ---------------------------------------------------------------------------
# 6. 全故事：三种视角在时间线上各就各位
# ---------------------------------------------------------------------------

def test_full_story_three_viewpoints(state, monkeypatch):
    # 迁移前：历史日报按旧证据回算 20 元
    legacy = reports.recompute_legacy_report(state, LEGACY_REPORT_ID)
    assert legacy["status"] == "ok" and legacy["total_amount"] == 20

    # 清单写出、核验未完成的中断点
    migration.build(state)
    assert reports.fetch_new(state, SNAP_WITH_FILE)["status"] == "pending_verification"
    legacy = reports.recompute_legacy_report(state, LEGACY_REPORT_ID)
    assert legacy["status"] == "ok" and legacy["total_amount"] == 20  # 旧证据不变

    # 中断重启：从文件核验前的断点接续
    monkeypatch.setenv(CRASH_ENV, f"verify:{SNAP_WITH_FILE}")
    with pytest.raises(MigrationInterrupted):
        migration.verify(state)
    monkeypatch.delenv(CRASH_ENV)
    migration.resume(state)

    # 升级确认后：新版本取数 20 元，日报钉住投影可回算
    migration.enable_reader(state)
    daily = reports.generate_daily_new(state, SNAP_WITH_FILE)
    assert daily["status"] == "generated" and daily["report"]["total_amount"] == 20
    recompute = reports.recompute_new_report(state, daily["report"]["report_id"])
    assert recompute["status"] == "ok" and recompute["total_amount"] == 20

    # 缺文件快照全程停留在可说明状态
    assert reports.fetch_new(state, SNAP_SUMMARY_ONLY)["status"] == "missing_file"
    missing = reports.audit(state)["missing_file_records"]
    assert [r["snapshot_id"] for r in missing] == [SNAP_SUMMARY_ONLY]

    # 历史运算与文件证据仍可复查
    legacy = reports.recompute_legacy_report(state, LEGACY_REPORT_ID)
    assert legacy["status"] == "ok" and legacy["total_amount"] == 20
