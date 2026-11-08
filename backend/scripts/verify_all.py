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
    ("memory 会话与记忆实证", [sys.executable, "backend/scripts/memory_evidence.py"]),
    ("eval  行为评测门禁", [sys.executable, "backend/scripts/run_eval.py"]),
    # 数据核对：把「简历上的亮点」逐条量一遍。不是又一个测试，而是防止
    # 文档里的百分比与代码实际行为悄悄脱钩（改了阈值却没人更新文档）。
    ("evidence 企业级数据核对", [sys.executable, "backend/scripts/enterprise_evidence.py", "--fast"]),
    # 检索 A/B：vector（旧）与 hybrid（新）跑同一批标注查询，hybrid 变差就红
    ("retrieval 检索质量 A/B", [sys.executable, "backend/scripts/retrieval_eval.py",
                                "--json", "backend/evals/retrieval_eval.json"]),
    # token 治理：按字符裁 vs 按 token 裁的真实 token 离散度（tiktoken 可选，没装则跳过断言）
    ("token  token 治理实测", [sys.executable, "backend/scripts/token_governance.py",
                               "--json", "backend/evals/token_governance.json"]),
    # 并发压测：先查后写 vs SQL 版本守卫，守卫必须 0 陈旧覆盖
    ("sync   同步并发压测", [sys.executable, "backend/scripts/sync_stress.py",
                             "--json", "backend/evals/sync_stress.json"]),
    ("smoke 端到端冒烟", [sys.executable, "backend/scripts/smoke_scenarios.py", "--serve"]),
    ("chaos 崩溃注入", [sys.executable, "backend/scripts/chaos_resume.py"]),
    # 最后一步查文档：所有「文档里的数字」必须与代码/用例/脚本的真实数字一致。
    # 放在最后是因为前面的步骤会改变真实数字（用例数、组数），先查等于白查。
    ("docs  文档一致性", [sys.executable, "backend/scripts/verify_docs.py"]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="只跑 lint + test")
    parser.add_argument("--skip", nargs="*", default=[], help="跳过指定环节：memory / eval / evidence / retrieval / token / sync / smoke / chaos / docs")
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
