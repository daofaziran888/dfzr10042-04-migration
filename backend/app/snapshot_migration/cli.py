"""迁移与日报取数命令行。

用法（在 backend/ 下）：
    python -m app.snapshot_migration.cli --state <目录> seed-demo
    python -m app.snapshot_migration.cli --state <目录> migrate build|verify|enable-reader|resume|status
    python -m app.snapshot_migration.cli --state <目录> report recompute-legacy|fetch-new|daily-new|recompute-new <目标>
    python -m app.snapshot_migration.cli --state <目录> audit
    python -m app.snapshot_migration.cli --state <目录> story     # 端到端演练

story 子命令把迁移阶段放到真实子进程里执行：核验落盘后、写 journal 前把进程
杀掉，再以新进程从断点接续，并在中断点同时发起历史回算和新版取数。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.snapshot_migration import fixtures, migration, reports
from app.snapshot_migration.core import State
from app.snapshot_migration.migration import CRASH_ENV, MigrationInterrupted

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STATE = BACKEND_DIR / "var" / "snapshot_migration"


# ---------------------------------------------------------------------------
# 单步命令
# ---------------------------------------------------------------------------

def _dispatch(state: State, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "seed-demo":
        return fixtures.seed_demo(state, reset=True)
    if args.command == "migrate":
        return {
            "build": migration.build,
            "verify": migration.verify,
            "enable-reader": migration.enable_reader,
            "resume": migration.resume,
            "status": migration.status,
        }[args.phase](state)
    if args.command == "report":
        return {
            "recompute-legacy": lambda: reports.recompute_legacy_report(state, args.target),
            "fetch-new": lambda: reports.fetch_new(state, args.target),
            "daily-new": lambda: reports.generate_daily_new(state, args.target),
            "recompute-new": lambda: reports.recompute_new_report(state, args.target),
        }[args.action]()
    if args.command == "audit":
        return reports.audit(state)
    raise ValueError(f"未知命令：{args.command}")


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# story：端到端演练
# ---------------------------------------------------------------------------

class _Story:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state = State(state_dir)
        self.failures: list[str] = []

    def check(self, cond: bool, label: str) -> None:
        mark = "✔" if cond else "✘"
        print(f"    {mark} {label}")
        if not cond:
            self.failures.append(label)

    def section(self, title: str) -> None:
        print()
        print("=" * 74)
        print(title)
        print("=" * 74)

    def run_cli(self, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "app.snapshot_migration.cli",
             "--state", str(self.state_dir), *args],
            capture_output=True, text=True, cwd=str(BACKEND_DIR), env=env,
        )

    def run_json(self, *args: str) -> dict[str, Any]:
        proc = self.run_cli(*args)
        if proc.returncode != 0:
            raise RuntimeError(f"命令失败 {args}: {proc.stderr or proc.stdout}")
        return json.loads(proc.stdout)

    # -- 各幕 ------------------------------------------------------------

    def act_seed(self) -> None:
        self.section("第 1 幕 · 准备两份同名查询快照（Q-1001）的旧导出")
        result = fixtures.seed_demo(self.state, reset=True)
        self.check(result["seeded"], "演示数据已写入")
        print(f"    快照 {fixtures.SNAP_WITH_FILE}：字段摘要 + 旧数据文件（8 元、12 元）")
        print(f"    快照 {fixtures.SNAP_SUMMARY_ONLY}：只有字段摘要，没有数据文件")
        print(f"    历史日报 {fixtures.LEGACY_REPORT_ID}：旧管线产物，证据指向旧文件（20 元）")

    def act_build(self) -> None:
        self.section("第 2 幕 · migrate build：写出分析快照字段清单（子进程）")
        result = self.run_json("migrate", "build")
        _print(result)
        manifest_a = json.loads(
            self.state.manifest_path(fixtures.SNAP_WITH_FILE).read_text(encoding="utf-8"))
        manifest_b = json.loads(
            self.state.manifest_path(fixtures.SNAP_SUMMARY_ONLY).read_text(encoding="utf-8"))
        self.check(manifest_a["projection"] == ["day", "amount"], "A 清单记录投影顺序 [day, amount]")
        self.check([c["type"] for c in manifest_a["columns"]] == ["date", "number"],
                   "A 清单记录列类型 date/number")
        self.check(manifest_a["row_count"] == 2, "A 清单记录实际行数 2")
        self.check(len(manifest_a["source_file"]["sha256"]) == 64, "A 清单记录文件 sha256 校验值")
        self.check(manifest_a["verification"]["status"] == "pending", "A 清单待核（pending）")
        self.check(manifest_b["verification"]["status"] == "missing_file", "B 清单缺文件（missing_file）")
        self.check(manifest_b["source_file"] is None and manifest_b["row_count"] is None,
                   "B 清单不借用 A 的文件：无来源文件、实际行数留空")
        self.check(fixtures.SNAP_WITH_FILE not in json.dumps(manifest_b),
                   "B 清单全文不引用 A 快照的任何路径")

    def act_breakpoint_conflict(self) -> None:
        self.section("第 3 幕 · 中断点并发冲突：历史回算 vs 新版取数（清单已写出、文件未核验）")
        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_legacy = pool.submit(
                reports.recompute_legacy_report, self.state, fixtures.LEGACY_REPORT_ID)
            fut_fetch_a = pool.submit(reports.fetch_new, self.state, fixtures.SNAP_WITH_FILE)
            fut_fetch_b = pool.submit(reports.fetch_new, self.state, fixtures.SNAP_SUMMARY_ONLY)
            legacy, fetch_a, fetch_b = fut_legacy.result(), fut_fetch_a.result(), fut_fetch_b.result()
        print("  历史日报回算（旧证据路径）：")
        _print(legacy)
        print("  新版取数 A（清单路径）：")
        _print(fetch_a)
        print("  新版取数 B（清单路径）：")
        _print(fetch_b)
        self.check(legacy["status"] == "ok" and legacy["total_amount"] == 20,
                   "历史回算看到旧证据下的 20 元")
        self.check(fetch_a["status"] == "pending_verification",
                   "新版取数 A 明确待核（pending_verification），不偷用未核验文件")
        self.check(fetch_b["status"] == "missing_file",
                   "新版取数 B 明确缺文件（missing_file），可说明")

    def act_crash_and_resume(self) -> None:
        self.section("第 4 幕 · 迁移进程硬中断（核验落盘后、写 journal 前被杀）")
        proc = self.run_cli("migrate", "verify",
                            env_extra={CRASH_ENV: f"verify:{fixtures.SNAP_WITH_FILE}"})
        print(f"    子进程退出码 {proc.returncode}；输出: {proc.stdout.strip()}")
        self.check(proc.returncode != 0 and "interrupted" in proc.stdout,
                   "verify 子进程按预期中断")
        phases = migration.completed_phases(self.state)
        self.check(phases == ["build"], f"journal 只有 build（实际 {phases}）")
        manifest_a = json.loads(
            self.state.manifest_path(fixtures.SNAP_WITH_FILE).read_text(encoding="utf-8"))
        self.check(manifest_a["verification"]["status"] == "verified",
                   "A 清单已原子落盘为 verified（崩溃不留半文件）")

        self.section("第 5 幕 · 重启后 migrate resume：从文件核验前的断点接续（新子进程）")
        result = self.run_json("migrate", "resume")
        _print(result)
        self.check(result["resumed_phases"] == ["verify"], "断点接续只补做 verify")
        self.check(result["new_reader_enabled"] is False,
                   "接续不擅自启用新版读入（清单构建与启用新版读入分开验证）")
        phases = migration.completed_phases(self.state)
        self.check(phases == ["build", "verify"], f"journal 记录 build+verify（实际 {phases}）")

    def act_after_verify(self) -> None:
        self.section("第 6 幕 · 核验完成、升级确认前：取数放行，生产日报仍不切换")
        fetch_a = reports.fetch_new(self.state, fixtures.SNAP_WITH_FILE)
        self.check(fetch_a["status"] == "ok" and fetch_a["row_count"] == 2,
                   "新版取数 A 放行：8 元、12 元两行")
        daily = reports.generate_daily_new(self.state, fixtures.SNAP_WITH_FILE)
        self.check(daily["status"] == "reader_not_enabled",
                   "未做升级确认前，新版日报拒绝生成（reader_not_enabled）")
        legacy = reports.recompute_legacy_report(self.state, fixtures.LEGACY_REPORT_ID)
        self.check(legacy["status"] == "ok" and legacy["total_amount"] == 20,
                   "历史日报仍按旧证据回算 20 元")

    def act_enable_and_daily(self) -> None:
        self.section("第 7 幕 · migrate enable-reader：升级确认（子进程）")
        result = self.run_json("migrate", "enable-reader")
        _print(result)
        self.check(result["status"] == "ok" and result["new_reader_enabled"], "新版读入已启用")

        self.section("第 8 幕 · 升级确认后的新版日报：引用核验文件，钉住当次投影")
        daily = reports.generate_daily_new(self.state, fixtures.SNAP_WITH_FILE)
        _print(daily)
        report = daily.get("report", {})
        self.check(daily["status"] == "generated" and report.get("total_amount") == 20,
                   "新版日报生成，合计 20 元")
        self.check(report.get("projection") == ["day", "amount"],
                   "日报只钉住一个有效投影 [day, amount]")
        evidence = report.get("evidence", {})
        self.check(evidence.get("kind") == "verified_file" and len(evidence.get("sha256", "")) == 64,
                   "日报只引用一个真实文件来源（核验文件 + sha256）")
        recompute = reports.recompute_new_report(self.state, report.get("report_id", ""))
        self.check(recompute["status"] == "ok" and recompute["total_amount"] == 20,
                   "取数之后按当次投影回算：20 元，一致")

    def act_missing_file(self) -> None:
        self.section("第 9 幕 · 缺文件快照 B：停留在可说明状态，不伪造两行")
        daily_b = reports.generate_daily_new(self.state, fixtures.SNAP_SUMMARY_ONLY)
        _print(daily_b)
        self.check(daily_b["status"] == "refused" and daily_b["reason"] == "missing_file",
                   "B 的新版日报被拒绝：缺文件")
        new_reports = list(self.state.reports_new.glob("*.json")) if self.state.reports_new.is_dir() else []
        leaked = [p for p in new_reports
                  if fixtures.SNAP_SUMMARY_ONLY in p.read_text(encoding="utf-8")]
        self.check(not leaked, "B 没有生成任何日报文件（新版目录无 B 的记录）")
        manifest_b = json.loads(
            self.state.manifest_path(fixtures.SNAP_SUMMARY_ONLY).read_text(encoding="utf-8"))
        self.check(manifest_b["row_count"] is None and manifest_b["source_file"] is None,
                   "迁移后 B 仍无行数、无来源文件，未借用 A 的 8/12 两行")

    def act_audit(self) -> None:
        self.section("第 10 幕 · audit：复查缺文件记录与全部证据")
        result = reports.audit(self.state)
        _print(result["missing_file_records"])
        self.check(len(result["missing_file_records"]) == 1
                   and result["missing_file_records"][0]["snapshot_id"] == fixtures.SNAP_SUMMARY_ONLY,
                   "缺文件记录只有 B，且带可说明的原因")
        self.check(result["completed_phases"] == ["build", "verify", "enable_reader"],
                   "迁移三阶段全部完成")
        legacy = reports.recompute_legacy_report(self.state, fixtures.LEGACY_REPORT_ID)
        self.check(legacy["status"] == "ok" and legacy["total_amount"] == 20,
                   "迁移全部结束后，历史运算与文件证据仍可复查（20 元）")

    def run(self) -> int:
        self.act_seed()
        self.act_build()
        self.act_breakpoint_conflict()
        self.act_crash_and_resume()
        self.act_after_verify()
        self.act_enable_and_daily()
        self.act_missing_file()
        self.act_audit()

        self.section("总结 · 中断点上三种视角的核对结果")
        print("    · 历史日报回算      → 旧证据下的 20 元")
        print("    · 新版日报取数      → 明确待核（pending_verification）")
        print("    · 升级确认后        → 新版本：核验文件取数 20 元，可按当次投影回算")
        print("    · 缺文件快照        → 可说明的 missing_file，未伪造 8/12 两行")
        if self.failures:
            print(f"\n    失败 {len(self.failures)} 项：")
            for label in self.failures:
                print(f"      ✘ {label}")
            return 1
        print("\n    全部检查通过。")
        return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot_migration", description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="状态目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed-demo", help="重置并写入演示数据")

    p_migrate = sub.add_parser("migrate", help="迁移阶段")
    p_migrate.add_argument("phase",
                           choices=["build", "verify", "enable-reader", "resume", "status"])

    p_report = sub.add_parser("report", help="日报取数/回算")
    p_report.add_argument("action", choices=["recompute-legacy", "fetch-new",
                                             "daily-new", "recompute-new"])
    p_report.add_argument("target", help="report_id 或 snapshot_id")

    sub.add_parser("audit", help="复查缺文件记录与证据")
    sub.add_parser("story", help="端到端演练（含真实子进程中断/重启）")

    args = parser.parse_args(argv)
    state = State(args.state)

    if args.command == "story":
        return _Story(Path(args.state)).run()

    try:
        result = _dispatch(state, args)
    except MigrationInterrupted as exc:
        print(json.dumps({"status": "interrupted", "detail": str(exc)}, ensure_ascii=False))
        return 1
    _print(result)
    return 0 if result.get("status") not in {"error"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
