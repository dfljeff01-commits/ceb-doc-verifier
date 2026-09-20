# -*- coding: utf-8 -*-
"""
一次性数据迁移工具：mobile_results/*.json（旧JSON文件存储）→ PostgreSQL。

用法：
  python migrate_json_to_pg.py                     # 迁移 + 校验 + 数据量对比
  python migrate_json_to_pg.py --verify-only       # 只做迁移后校验，不写入
  python migrate_json_to_pg.py --source /path/dir  # 指定旧JSON目录（默认
                                                   # mobile_results/，可用
                                                   # MOBILE_RESULTS_DIR 覆盖）
  python migrate_json_to_pg.py --quiet             # 容器启动时静默自动迁移

口径：
  - 幂等：按批次编号 upsert，重跑不产生重复数据；
  - 保真：created_at / source 原值保留，documents/verification/lite 逐字段
    迁入正式表；迁移后对每个批次从数据库回读重建记录，与源JSON做深度比对
    （canonical JSON），任何差异都会导致退出码 1；
  - 旧JSON文件只读不删（迁移前备份，验证无误后由运维决定是否归档清理）；
  - 电脑端网页旧版核验历史为会话级临时状态（不落盘），无历史数据需要迁移；
    网页新建批次自本版本起直接入库存档（source=web）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import db
import mobile_store

PROJECT_ROOT = Path(__file__).parent


def legacy_dir(source: str | None) -> Path:
    if source:
        return Path(source)
    env_dir = Path(__import__("os").environ.get("MOBILE_RESULTS_DIR", "")).strip() \
        if __import__("os").environ.get("MOBILE_RESULTS_DIR", "") else None
    return env_dir or (PROJECT_ROOT / mobile_store.LEGACY_JSON_DIR_NAME)


def load_legacy_records(directory: Path) -> list[dict]:
    records = []
    if not directory.exists():
        return records
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".tmp"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[migrate] 跳过损坏文件 {path.name}: {exc}", file=sys.stderr)
            continue
        if isinstance(record, dict) and record.get("batch_id"):
            records.append(record)
    return records


def migrate(records: list[dict]) -> int:
    for record in records:
        created_at = None
        raw_time = record.get("created_at")
        if raw_time:
            try:
                created_at = datetime.fromisoformat(str(raw_time))
            except ValueError:
                created_at = None  # 时间不可解析则回退为迁移时刻
        mobile_store.save_batch(
            record.get("documents") or [],
            record.get("verification") or {},
            source=str(record.get("source") or "mobile_app"),
            created_by=record.get("created_by") or None,
            created_at=created_at)
    return len(records)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def verify(records: list[dict]) -> tuple[bool, dict]:
    """逐批次回读比对：documents/verification/lite/identity_numbers/元信息。"""
    problems = []
    for record in records:
        batch_id = record["batch_id"]
        migrated = mobile_store.get_batch(batch_id)
        if migrated is None:
            problems.append(f"{batch_id}: 数据库中未找到")
            continue
        for key in ("documents", "verification", "lite", "identity_numbers"):
            left = canonical(record.get(key))
            right = canonical(migrated.get(key))
            if left != right:
                problems.append(f"{batch_id}: 字段 {key} 不一致")
        if str(record.get("created_at")) != migrated["created_at"]:
            problems.append(
                f"{batch_id}: created_at 不一致 "
                f"({record.get('created_at')} -> {migrated['created_at']})")
        if str(record.get("source") or "mobile_app") != migrated["source"]:
            problems.append(f"{batch_id}: source 不一致")
    return (not problems), {"batches": len(records), "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser(description="JSON → PostgreSQL 数据迁移")
    parser.add_argument("--source", default=None, help="旧JSON目录")
    parser.add_argument("--verify-only", action="store_true",
                        help="只校验，不写入")
    parser.add_argument("--quiet", action="store_true", help="精简输出")
    args = parser.parse_args()

    directory = legacy_dir(args.source)
    records = load_legacy_records(directory)
    if not args.quiet:
        print(f"[migrate] 源目录：{directory}（旧JSON文件，保留为迁移前备份）")
        print(f"[migrate] 发现批次JSON：{len(records)} 个")

    db.init_schema()
    if not args.verify_only:
        migrated = migrate(records)
        if not args.quiet:
            print(f"[migrate] 已迁入数据库批次：{migrated} 个")

    ok, result = verify(records)
    stats = mobile_store.counts()
    if not args.quiet:
        print("[migrate] —— 数据量对比 ——")
        print(f"[migrate] 源JSON批次       : {result['batches']}")
        print(f"[migrate] 库内批次总数     : {stats['batches']}")
        print(f"[migrate] 库内单据行       : {stats['documents']}")
        print(f"[migrate] 库内核验问题行   : {stats['issues']}")
        if ok:
            print("[migrate] 逐批次回读深度比对：全部一致，迁移无损 ✔")
        else:
            print(f"[migrate] 校验失败，{len(result['problems'])} 处不一致：")
            for p in result["problems"]:
                print(f"[migrate]   - {p}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
