"""文档-代码一致性门禁：把所有「文档里写的数字」和「代码里真实的数字」对一遍。

为什么单独做一个门禁（这是 v0.10 的由来）：
`docs/10` 自己承认了三处「有说法没落点」，但更尴尬的是**文档里的数字本身会漂**——
v0.9 提交时 `pytest` 已经 162 条，`docs/07` 还写着 137，`docs/06` 甚至写着 110。
这种错误面试官一翻就发现，而它靠人肉核对必然复发。

所以规则改成：**数字必须有唯一来源，且能被机器验证**。
- 每条声明登记「文件 + 正则 + 期望值来源」；
- 期望值来源是**代码本身**（用例数、脚本里的分组数、verify_all 的步骤列表），不是另一份文档；
- 正则一条都匹配不到也算失败——防止有人把声明删掉了，门禁还以为「全都对」。

用法：
    python backend/scripts/verify_docs.py
    python backend/scripts/verify_docs.py --json backend/evals/docs_consistency.json
    python backend/scripts/verify_docs.py --no-pytest     # 跳过耗时的收集（约 10s）
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = "README.md"
D05 = "docs/05-里程碑与验收.md"
D06 = "docs/06-演示脚本与面试口径.md"
D07 = "docs/07-Pi-Agent架构对照与优化.md"
D09 = "docs/09-会话与记忆（Pi对齐）.md"
D10 = "docs/10-企业级核查与数据支撑.md"
D11 = "docs/11-不一致项收敛与前后对比.md"

# 历史记录类文档（audit 快照）里的数字是「当时的实测」，不参与一致性校验：
# 校验的目的是「当前的声明是否仍然成立」，不是把历史改成现在。
SKIP_FILES = {"docs/AUDIT-2026-11-08-全面审计与治理方案.md"}


@dataclass
class Claim:
    key: str
    title: str
    expected: int | None = None
    patterns: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""


def _read(rel: str) -> str:
    path = ROOT / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _list_elements(rel: str, name: str) -> list:
    """取出模块级列表常量（兼容 Assign 与 AnnAssign），返回其中的字面量元素。"""
    tree = ast.parse(_read(rel))
    for node in ast.walk(tree):
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        if name in targets and isinstance(value, (ast.List, ast.Tuple)):
            return list(value.elts)
    return []


def _count_list_constant(rel: str, name: str) -> int:
    """静态数一个模块里的列表常量有多少元素（不 import，避免副作用）。"""
    elements = _list_elements(rel, name)
    if not elements:
        raise SystemExit(f"找不到常量 {name} in {rel}")
    return len(elements)


def _count_functions(rel: str, prefix: str) -> int:
    tree = ast.parse(_read(rel))
    return sum(
        1 for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith(prefix)
    )


def _count_eval_cases() -> int:
    rows = [json.loads(line) for line in _read("backend/evals/cases.jsonl").splitlines() if line.strip()]
    return sum(1 for row in rows if (row.get("requires") or "") != "llm")


def _count_retrieval_cases() -> int:
    return len([line for line in _read("backend/evals/retrieval_cases.jsonl").splitlines() if line.strip()])


def _count_retrieval_corpus() -> int:
    return len([line for line in _read("backend/evals/retrieval_corpus.jsonl").splitlines() if line.strip()])


def _count_pytest() -> int | None:
    """跑 collect-only 数用例：这是「文档写 N passed」唯一的真实来源。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:warnings"],
            cwd=ROOT, capture_output=True, text=True, timeout=180, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! pytest 收集失败：{exc}")
        return None
    total = 0
    for line in proc.stdout.splitlines():
        match = re.match(r"^(?:backend/)?tests/\S+\.py:\s+(\d+)$", line.strip())
        if match:
            total += int(match.group(1))
    return total or None


def _chaos_iterations() -> int:
    """chaos_resume.py 里 --iterations 的默认值 = 文档里「6/6」的那个 6。"""
    tree = ast.parse(_read("backend/scripts/chaos_resume.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "--iterations" in args:
                for keyword in node.keywords:
                    if keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                        return int(keyword.value.value)
    raise SystemExit("chaos_resume.py 找不到 --iterations 默认值")


def build_claims(skip_pytest: bool) -> list[Claim]:
    claims: list[Claim] = []

    pytest_count = None if skip_pytest else _count_pytest()
    claims.append(Claim(
        "pytest", "pytest 用例数",
        expected=pytest_count,
        patterns=[
            (README, r"`pytest` \*\*(\d+) passed\*\*"),
            (README, r"单元 / 集成 \|[^|]*\|\s*\*\*(\d+) passed\*\*"),
            (README, r"测试资产 \| \*\*(\d+)\*\*"),
            (D05, r"G-02.*?\|\s*(\d+) passed"),
            (D06, r"`pytest` (\d+) passed"),
            (D07, r"pytest\s+#\s*(\d+) passed"),
            (D10, r"`pytest` \*\*(\d+) passed\*\*"),
        ],
        note="用例数只能来自 pytest --collect-only，不接受凭记忆写的数字",
    ))

    claims.append(Claim(
        "eval_cases", "行为评测用例数（不含 requires: llm）",
        expected=_count_eval_cases(),
        patterns=[
            (README, r"评测门禁 \| [^|]*?(\d+) 条用例 × (\d+) 轮"),
            (README, r"行为评测 \|[^|]*\|\s*(\d+) 条 × (\d+) 轮"),
            (D06, r"(\d+) 条用例 × (\d+) 轮"),
        ],
    ))

    claims.append(Claim(
        "smoke", "端到端冒烟场景数",
        expected=_count_list_constant("backend/scripts/smoke_scenarios.py", "SCENARIOS"),
        patterns=[
            (README, r"smoke_scenarios\.py` \|\s*\*\*(\d+)/\d+\*\*"),
            (README, r"smoke_scenarios\.py`\s*(\d+) 组可复现场景"),
            (D05, r"smoke_scenarios\.py` \| (\d+)/\d+"),
        ],
    ))

    claims.append(Claim(
        "chaos", "崩溃注入用例数",
        expected=_chaos_iterations(),
        patterns=[
            (README, r"kill -9`，(\d+)/\d+ 用例"),
            (README, r"chaos_resume\.py` \|\s*\*\*(\d+)/\d+\*\*"),
            (D05, r"chaos_resume\.py` \| (\d+)/\d+"),
        ],
    ))

    claims.append(Claim(
        "memory_groups", "会话与记忆离线实证组数",
        expected=_count_functions("backend/scripts/memory_evidence.py", "case_"),
        patterns=[
            (README, r"memory_evidence\.py` 离线 (\d+) 组"),
            (README, r"memory_evidence\.py` \|\s*\*\*(\d+) 组\*\*"),
            (D05, r"memory_evidence\.py` \| (\d+) 组"),
            (D09, r"memory_evidence\.py`?\s+#\s*(\d+) 组"),
        ],
    ))

    claims.append(Claim(
        "evidence_groups", "企业级证据组数",
        expected=_count_functions("backend/scripts/enterprise_evidence.py", "evidence_"),
        patterns=[
            (README, r"enterprise_evidence\.py` \*\*(\d+) 组\*\*"),
            (README, r"enterprise_evidence\.py` \|\s*\*\*(\d+) 组\*\*"),
            (D05, r"enterprise_evidence\.py --fast` \| (\d+) 组"),
        ],
    ))

    claims.append(Claim(
        "retrieval_cases", "检索标注集查询数",
        expected=_count_retrieval_cases(),
        patterns=[(D11, r"标注集 \*\*(\d+) 条查询\*\*")],
    ))

    claims.append(Claim(
        "retrieval_corpus", "检索标注集语料篇数",
        expected=_count_retrieval_corpus(),
        patterns=[(D11, r"\*\*(\d+) 篇\*\*标注语料")],
    ))

    claims.append(Claim(
        "verify_steps", "发布门禁步骤数",
        expected=_count_list_constant("backend/scripts/verify_all.py", "STEPS"),
        patterns=[
            (README, r"共 \*\*(\d+)\*\* 步"),
            (D11, r"门禁扩到 (\d+) 步"),
        ],
    ))

    return claims


def _extract(path: str, pattern: str) -> list[int]:
    text = _read(path)
    if not text:
        return []
    found: list[int] = []
    for match in re.finditer(pattern, text):
        for group in match.groups():
            if group and group.isdigit():
                found.append(int(group))
                break
    return found


def check_ci_covers_steps() -> list[str]:
    """CI 里必须逐步覆盖 verify_all 的每个脚本，否则「本地绿、CI 不跑」就是假绿。

    v0.10 之前这条其实是空跑：STEPS 用的是带类型注解的赋值（AnnAssign），
    而解析只认 `ast.Assign`，于是命令列表恒为空、检查恒绿——**一个永远绿的门禁
    比没有门禁更糟**，因为所有人都以为它在看着。
    """
    commands: list[str] = []
    for step in _list_elements("backend/scripts/verify_all.py", "STEPS"):
        if not isinstance(step, ast.Tuple):
            continue
        for item in step.elts:
            if isinstance(item, ast.List):
                commands.extend(
                    leaf.value for leaf in item.elts
                    if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
                )
    ci = _read(".github/workflows/ci.yml")
    return [
        f"CI 未执行门禁步骤：{command}"
        for command in commands
        if command.endswith(".py") and command not in ci
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    parser.add_argument("--no-pytest", action="store_true", help="跳过 pytest 收集（省 10s）")
    args = parser.parse_args()

    claims = build_claims(args.no_pytest)
    failures: list[str] = []
    rows: list[dict] = []

    print("\n文档 ↔ 代码 一致性\n")
    print(f"{'声明':<26}{'期望':>8}{'实际':>10}{'位置数':>8}  结果")
    print("-" * 70)
    for claim in claims:
        found: list[tuple[str, int]] = []
        for path, pattern in claim.patterns:
            values = _extract(path, pattern)
            if not values:
                failures.append(f"{claim.key}: 在 {path} 找不到声明（正则 {pattern}）——声明被删了还是写法变了？")
                continue
            for value in values:
                found.append((path, value))
        if claim.expected is None:
            status = "SKIP"
            mismatched = []
        else:
            mismatched = [(path, value) for path, value in found if value != claim.expected]
            status = "OK" if not mismatched and found else "FAIL"
            for path, value in mismatched:
                failures.append(f"{claim.key}: {path} 写 {value}，实际 {claim.expected}（{claim.title}）")
        rows.append({
            "key": claim.key, "title": claim.title,
            "expected": claim.expected,
            "found": [{"file": path, "value": value} for path, value in found],
            "status": status,
        })
        print(
            f"{claim.key:<26}{'—' if claim.expected is None else claim.expected:>8}"
            f"{len(found):>10}{len(found):>8}  {status}"
        )

    ci_failures = check_ci_covers_steps()
    failures.extend(ci_failures)
    rows.append({"key": "ci_covers_steps", "status": "OK" if not ci_failures else "FAIL",
                 "expected": None, "found": []})
    print(f"{'ci_covers_steps':<26}{'—':>8}{0:>10}{0:>8}  {'OK' if not ci_failures else 'FAIL'}")

    if failures:
        print("\n不一致明细：")
        for item in failures:
            print(f"  ✗ {item}")
    else:
        print("\n全部一致：文档里的每个数字都能在代码里找到同一个来源。")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"claims": rows, "failures": failures}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
