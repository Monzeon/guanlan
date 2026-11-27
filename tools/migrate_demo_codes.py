"""把存量库里早先灌进去的示例条目，从 C-2026-0001 式编号迁到 DEMO- 编号。

为什么只在库里改、不动代码：seed.py 已经改成用 DEMO- 前缀灌新库，
但已经灌过的库不会自己变 —— 用户的真实命题还排在示例后面（C-2026-0008 起）。

用法：
    python tools/migrate_demo_codes.py                # 只算方案，不动数据（默认）
    python tools/migrate_demo_codes.py --apply        # 先备份，再改
    python tools/migrate_demo_codes.py --db X.db --apply

判定依据是「编号 + 内容」双命中：编号对得上、原句也对得上才改。
用户自己写的条目即使占了同一个编号也不会被动到，工具会把这种冲突列出来。
改之前跟改之后都跑一遍 checks，任何一处对不上就整批不动。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import seed  # noqa: E402
import storage  # noqa: E402


def backup_db(db_path: Path, backup_dir: Path) -> Path:
    """用 sqlite3 自己的备份接口拷一份，WAL 里没落盘的页也会带上。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{db_path.stem}-pre-demo-codes-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(str(db_path))
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def residual_mentions(conn: sqlite3.Connection) -> list[str]:
    """迁移完还有哪些结构化的地方提到旧编号。

    ai_outputs.content 里是历史回答原文，文字中出现的 [C-2026-0001] 保留原样 ——
    那是当时的对话记录，改写它等于改历史，而且不影响功能。
    """
    ref_column = {"claims": "links", "predictions": "depends_on", "decisions": "depends_on"}
    leftovers = []
    for table, old, _new, _column, _needle in seed._LEGACY_ROWS:
        column = ref_column.get(table)
        if not column:
            continue                      # sources 是按 id 引用的，没有编号回指
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?",
            (f"%{old}%",)).fetchone()[0]
        if n:
            leftovers.append(f"{table}.{column} 仍有 {n} 行提到 {old}")
    return leftovers


def historical_text_mentions(conn: sqlite3.Connection) -> int:
    """历史回答正文里提到旧编号的处数（只报数，不改写）。"""
    total = 0
    for _table, old, _new, _column, _needle in seed._LEGACY_ROWS:
        total += conn.execute(
            "SELECT COUNT(*) FROM ai_outputs WHERE content LIKE ?",
            (f"%{old}%",)).fetchone()[0]
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="示例编号迁移到 DEMO- 前缀")
    ap.add_argument("--db", default=str(storage.DATA_DIR / "guanlan.db"))
    ap.add_argument("--apply", action="store_true", help="真的改库（默认只算方案）")
    ap.add_argument("--backup-dir", default=str(ROOT / "backups"))
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"找不到库：{db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        plan = seed.legacy_demo_plan(conn)
        print(f"库：{db_path}（{db_path.stat().st_size / 1024 / 1024:.1f} MB）")
        print(f"待改编号 {len(plan['renames'])} 条，待改引用 {len(plan['refs'])} 处")
        for table, old, new, _row_id in plan["renames"]:
            print(f"  {table:11s} {old} -> {new}")
        for table, row_id, old_raw, new_raw in plan["refs"]:
            print(f"  {table} #{row_id} 引用：{old_raw} -> {new_raw}")
        for problem in plan["problems"]:
            print(f"  [冲突] {problem}")

        if not plan["renames"] and not plan["refs"]:
            print("没有需要迁移的条目（要么早就迁过，要么这库本来就没灌示例）。")
            return 0
        if plan["problems"]:
            print("存在冲突，未改库。请先人工确认上面列出的条目。")
            return 2
        if not args.apply:
            print("以上为试算结果。加 --apply 才会真的改库（会先备份）。")
            return 0

        backup = backup_db(db_path, Path(args.backup_dir))
        print(f"已备份：{backup}（{backup.stat().st_size / 1024 / 1024:.1f} MB）")

        try:
            conn.execute("BEGIN IMMEDIATE")
            n_code, n_ref = seed.apply_legacy_demo_codes(conn, plan)
        except Exception:
            conn.rollback()
            print("改库失败，已回滚，库保持迁移前的状态。")
            raise
        print(f"已改：{n_code} 条编号、{n_ref} 处引用")

        leftovers = residual_mentions(conn)
        if leftovers:
            print("迁移后仍有旧编号残留（不阻塞）：")
            for line in leftovers:
                print(f"  {line}")
        else:
            print("结构化字段里已无旧编号。")
        n_text = historical_text_mentions(conn)
        if n_text:
            print(f"另有 {n_text} 条历史 AI 回答的正文提到旧编号，按原样保留（是当时的对话记录）。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
