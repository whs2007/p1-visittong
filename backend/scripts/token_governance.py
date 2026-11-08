"""token 治理实测：为什么把「按字符裁」换成「按 token 裁」，以及估算器有多准。

三组数字，都能当场复现（tiktoken 是可选依赖，没装时降级为只报估算值）：

1. 估算器 vs 真实 tokenizer：7 类真实输出形态，报每条误差与 MAE。
2. 固定 1600 字符上限 → 各形态真实 token 数（这就是 1.79 倍误差的来源）。
3. 改成 900 token 上限 → 各形态真实 token 数（误差被压到 ~1.1 倍以内）。

用法：
    python backend/scripts/token_governance.py
    python backend/scripts/token_governance.py --json backend/evals/token_governance.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.output_guard import OutputGovernor  # noqa: E402
from app.agent.tokens import estimate_tokens  # noqa: E402
from app.domain.db import Database  # noqa: E402

CHAR_LIMIT = 1600
TOKEN_LIMIT = 900


def samples() -> dict[str, str]:
    """7 类真实工具输出形态（客户档案 / 任务列表 / 日志 / 代码 / 混合 / 英文 / 长句）。"""
    customers = [
        {
            "customer_id": f"c_{index:03d}",
            "name": f"北京示例科技{index}有限公司",
            "alias": f"示例{index}",
            "industry": "互联网",
            "level": "A",
            "address": f"北京市海淀区中关村大街{index}号院{index + 1}号楼",
        }
        for index in range(60)
    ]
    todos = [
        {"todo_id": f"todo_{index}", "title": f"准备第{index}家客户的季度复盘材料", "due_at": "2026-11-09T10:00:00+08:00", "status": "open"}
        for index in range(80)
    ]
    return {
        "中文客户档案 JSON": json.dumps(customers, ensure_ascii=False, indent=2),
        "中文待办 JSON": json.dumps(todos, ensure_ascii=False, indent=2),
        "中文长文（知识片段）": (
            "客户档案：北京云启数智科技有限公司成立于二零一五年，主营业务为数据中台与智能报表，"
            "上一财年采购金额约一百二十万元，决策链为技术总监与采购经理双签，"
            "上次拜访记录显示对交付周期敏感，需要在方案里明确实施排期。"
        ) * 8,
        "英文 JSON": json.dumps(
            [{"id": index, "name": f"acme-{index}", "status": "active", "tags": ["alpha", "beta"]} for index in range(60)]
        ),
        "单行日志": "\n".join(
            f"2026-11-08T10:{index:02d}:00 INFO tool=plan_create user=sales-001 ok=True latency_ms={index * 7}"
            for index in range(60)
        ),
        "代码片段": "\n".join(
            [
                "def route_text_hybrid(db, text, settings):",
                "    rule = route_text(db, text)",
                "    if rule.confidence >= 0.9:",
                "        return rule",
                "    return RouteResult('x', 0.0, 'llm', {}, 'fallback')",
            ]
            * 12
        ),
        "混合（中文 + 日志）": (
            "客户档案：北京云启数智科技有限公司，主营数据中台。" * 6
        ) + "\n" + "\n".join(
            f"2026-11-08T11:{index:02d}:00 INFO trace=tr_{index:04d} status=success"
            for index in range(40)
        ),
    }


def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base"), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"未安装 tiktoken（{exc}），只报估算值"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="", help="结果写入 JSON")
    args = parser.parse_args()

    encoder, note = _encoder()
    workdir = Path(tempfile.mkdtemp(prefix="token_gov_"))
    db = Database(str(workdir / "gov.db"))
    db.init()

    def truth(text: str) -> int | None:
        return len(encoder.encode(text)) if encoder else None

    rows = []
    for name, text in samples().items():
        by_char = OutputGovernor(db, max_chars=CHAR_LIMIT, max_tokens=0).govern(text, tool="probe")
        by_token = OutputGovernor(db, max_tokens=TOKEN_LIMIT).govern(text, tool="probe")
        rows.append(
            {
                "sample": name,
                "chars": len(text),
                "est_tokens": estimate_tokens(text),
                "true_tokens": truth(text),
                "char_cap": {
                    "kept_chars": by_char.kept_chars,
                    "true_tokens": truth(by_char.text),
                    "est_tokens": by_char.kept_tokens,
                },
                "token_cap": {
                    "kept_chars": by_token.kept_chars,
                    "true_tokens": truth(by_token.text),
                    "est_tokens": by_token.kept_tokens,
                },
            }
        )

    print("\n[1] 估算器 vs 真实 tokenizer（tiktoken cl100k_base）\n")
    print(f"{'输出形态':<22}{'字符':>7}{'真实':>7}{'估算':>7}{'误差':>9}")
    print("-" * 56)
    errors = []
    for row in rows:
        if row["true_tokens"]:
            error = (row["est_tokens"] - row["true_tokens"]) / row["true_tokens"]
            errors.append(abs(error))
            print(f"{row['sample']:<22}{row['chars']:>7}{row['true_tokens']:>7}{row['est_tokens']:>7}{error * 100:>8.1f}%")
        else:
            print(f"{row['sample']:<22}{row['chars']:>7}{'-':>7}{row['est_tokens']:>7}{'-':>9}")
    mae = statistics.fmean(errors) if errors else 0.0
    print(f"\n平均绝对误差 MAE = {mae * 100:.1f}%" + (f"；{note}" if note else ""))

    def spread(key: str) -> tuple[float, float, float]:
        values = [row[key]["true_tokens"] for row in rows if row[key]["true_tokens"]]
        if not values:
            return 0.0, 0.0, 0.0
        return float(min(values)), float(max(values)), round(max(values) / max(min(values), 1), 2)

    char_low, char_high, char_ratio = spread("char_cap")
    tok_low, tok_high, tok_ratio = spread("token_cap")
    print(f"\n[2] 固定字符上限 {CHAR_LIMIT} 字符 → 真实 token {char_low:.0f} ~ {char_high:.0f}（最大/最小 = {char_ratio}×）")
    print(f"[3] 固定 token 上限 {TOKEN_LIMIT} token → 真实 token {tok_low:.0f} ~ {tok_high:.0f}（最大/最小 = {tok_ratio}×）")
    print(f"\n结论：同一份预算，按字符裁会在不同工具输出上产生 {char_ratio}× 的 token 误差；按 token 裁收敛到 {tok_ratio}×。")

    payload = {
        "char_limit": CHAR_LIMIT,
        "token_limit": TOKEN_LIMIT,
        "estimator_mae": round(mae, 4) if errors else None,
        "tiktoken_available": bool(encoder),
        "note": note,
        "rows": rows,
        "char_cap_spread": {"min": char_low, "max": char_high, "ratio": char_ratio},
        "token_cap_spread": {"min": tok_low, "max": tok_high, "ratio": tok_ratio},
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    # 门禁：按 token 裁的离散度必须明显优于按字符裁
    if not encoder:
        return 0
    return 0 if tok_ratio < char_ratio else 1


if __name__ == "__main__":
    raise SystemExit(main())
