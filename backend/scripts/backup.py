"""SQLite 备份：`python scripts/backup.py [--keep 7]`。

用 `sqlite3.Connection.backup()` 而不是复制文件：WAL 模式下直接 cp 可能拿到
「主库 + 未 checkpoint 的 WAL」不一致的快照。backup API 会做一次一致性快照。

保留策略：按时间戳命名，默认只保留最近 7 份（`--keep` 可调）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=7)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    settings = get_settings()
    source = Path(settings.sqlite_path)
    if not source.exists():
        print(f"数据库不存在：{source}")
        return 1
    target_dir = Path(args.out) if args.out else source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"

    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    backups = sorted(target_dir.glob(f"{source.stem}-*.db"))
    for old in backups[: max(0, len(backups) - args.keep)]:
        try:
            old.unlink()
        except OSError:
            pass
    print(f"backup ok -> {target} (kept {min(len(backups), args.keep)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
