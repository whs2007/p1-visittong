"""一条命令跑完发布门禁（跨平台，不依赖 GNU make）。

为什么有这一层：`Makefile` 只是入口之一，Windows 上不一定有 `make`（本机就没有）。
把「门禁包含哪几步、按什么顺序、带什么参数」写在 Python 里，`make verify` 调它；
CI 为便于在 GitHub 上定位失败环节而**分步执行**，步骤、顺序与参数必须与本文件一致——
两边不一致时以本文件为准（改门禁先改这里，再同步 workflow）。

门禁顺序是**故意的，从便宜到贵**：静态检查先挡语法与未使用导入，再跑单测，
再跑行为评测（最慢但最能发现行为漂移），最后才是端到端与崩溃注入。
早失败 = 早结束，不让人等十分钟才看到第一行红字。

用法：
    python backend/scripts/verify_all.py            # 全跑
    python backend/scripts/verify_all.py --fast     # 只跑 lint + test
    python backend/scripts/verify_all.py --skip chaos

退出码 0 = 全过。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

STEPS: list[tuple[str, list[str]]] = [
    ("lint  静态检查", [sys.executable, "-m", "ruff", "check", "backend"]),
    ("test  单元/集成", [sys.executable, "-m", "pytest"]),
    ("eval  行为评测门禁", [sys.executable, "backend/scripts/run_eval.py"]),
    ("smoke 端到端冒烟", [sys.executable, "backend/scripts/smoke_scenarios.py", "--serve"]),
    ("chaos 崩溃注入", [sys.executable, "backend/scripts/chaos_resume.py"]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="只跑 lint + test")
    parser.add_argument("--skip", nargs="*", default=[], help="跳过指定环节：eval / smoke / chaos")
    args = parser.parse_args()

    steps = STEPS[:2] if args.fast else STEPS
    if args.skip:
        steps = [s for s in steps if s[0].split()[0] not in args.skip]

    results: list[tuple[str, bool, float]] = []
    for name, cmd in steps:
        print(f"\n=== {name}")
        started = time.monotonic()
        # 不用 shell：避免 Windows / POSIX 引号差异；输出直通，失败时人能看到原因
        code = subprocess.call(cmd, cwd=str(ROOT))
        elapsed = time.monotonic() - started
        results.append((name, code == 0, elapsed))
        if code != 0:
            print(f"--- 失败：{name}（{elapsed:.0f}s）")
            break

    print("\n=== 门禁结果")
    for name, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {elapsed:6.0f}s")

    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"\n未通过：{', '.join(failed)}")
        return 1
    print("\nrelease gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
