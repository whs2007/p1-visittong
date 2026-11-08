"""清理本地测试残留目录。

背景：`backend/tests/conftest.py` 刻意不在 teardown 删 `tmp_path`，因为一次性
删除几十个目录会命中宿主的批量删除保护，导致整批测试被取消。代价是目录会累积。
CI 在容器里没有这个限制，由 workflow 直接清理；本地需要时跑本脚本。

安全约束：只删 `backend/data/_pytest/` 下的**直接子目录**，不递归跟随符号链接，
不触碰其他任何路径。
"""
from __future__ import annotations

import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "data" / "_pytest"


def main() -> int:
    if not BASE.exists():
        print("nothing to clean")
        return 0
    removed = 0
    for child in sorted(BASE.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    print(f"cleaned {removed} test dir(s) under {BASE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
