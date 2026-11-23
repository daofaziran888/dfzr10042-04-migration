"""迁移子系统的文件状态布局与确定性 IO 工具。

所有写盘操作都是原子写（临时文件 + fsync + rename），保证迁移进程在任意
时刻被中断都不会留下写了一半的清单或日报，重启后从 journal 断点接续。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

MANIFEST_SUFFIX = ".manifest.json"


class State:
    """迁移子系统的文件状态目录布局。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def legacy_exports(self) -> Path:
        return self.root / "legacy_exports"

    @property
    def manifests(self) -> Path:
        return self.root / "manifests"

    @property
    def reports_legacy(self) -> Path:
        return self.root / "reports" / "legacy"

    @property
    def reports_new(self) -> Path:
        return self.root / "reports" / "new"

    @property
    def journal(self) -> Path:
        return self.root / "migration_journal.jsonl"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    def manifest_path(self, snapshot_id: str) -> Path:
        return self.manifests / f"{snapshot_id}{MANIFEST_SUFFIX}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON：先写临时文件并 fsync，再 rename 覆盖目标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def parse_typed(raw: str, col_type: str) -> Any:
    """按列类型解析 CSV 单元格；number 用 Decimal 保证金额精确。"""
    if col_type == "number":
        return Decimal(raw)
    if col_type == "date":
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    if col_type == "string":
        return raw
    raise ValueError(f"未知列类型：{col_type!r}")


def json_value(value: Any) -> Any:
    """把计算值转成 JSON 可序列化形式；Decimal 整数转 int，其余转 float。"""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def read_projection_rows(
    csv_path: Path,
    projection: list[str],
    col_types: dict[str, str],
) -> list[dict[str, Any]]:
    """按投影顺序从 CSV 读出指定列并解析类型。

    投影是清单/日报钉住的列子集与顺序；文件里多出的列不读，缺少投影列则报错。
    """
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [col for col in projection if col not in header]
        if missing:
            raise ValueError(f"{csv_path.name} 缺少投影列：{missing}")
        rows = []
        for raw in reader:
            rows.append({col: parse_typed(raw[col], col_types[col]) for col in projection})
        return rows


def count_csv_rows(csv_path: Path) -> int:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def sum_amount(rows: list[dict[str, Any]], column: str = "amount") -> Decimal:
    total = Decimal(0)
    for row in rows:
        value = row[column]
        total += value if isinstance(value, Decimal) else Decimal(str(value))
    return total
