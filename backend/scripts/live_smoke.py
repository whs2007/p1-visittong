"""真模型链路自检：确认「接上真模型之后，这件东西真的能跑」。

为什么单独一个脚本、并且**不进 CI**：CI 没有密钥也不该花钱；而 mock 模式永远
验证不了三件事——请求体字段是否被 provider 接受、流式 `tool_calls` 分片能否拼对、
真模型能否按固定段落产出摘要。这三件事恰恰是「本地绿、线上 500」的高发区。

跑的检查（共 5 项）：
  1. 普通补全：能拿到非空文本
  2. 工具调用：能给出一条 `tool_calls`（function calling 通）
  3. 意图分类：`classify()` 返回合法 skill
  4. 压缩摘要：输出含 Pi 的六段结构（至少 4 段）
  5. 回传格式：带 `tool_calls` 的 assistant 消息能被 provider 接受（v0.8 的 500 就是这个）

用法（需要 .env 里配好 LLM_MODE=live 与 LLM_API_KEY）：
    python backend/scripts/live_smoke.py
    python backend/scripts/live_smoke.py --model qwen-plus     # 换个模型对比
没有密钥时打印「跳过」并以 0 退出，不会把「没配」当成失败。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.agent.summarize import llm_summary  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm.client import OpenAICompatLLM  # noqa: E402
from app.sessions.tree import _openai_tool_calls  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "todo_query",
            "description": "查询某天的拜访待办",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            },
        },
    }
]

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


async def collect(llm: OpenAICompatLLM, messages, tools=None) -> tuple[str, list, dict]:
    text: list[str] = []
    calls: list = []
    usage: dict = {}
    async for chunk in llm.stream(messages, tools):
        if chunk.type == "text_delta":
            text.append(chunk.text)
        elif chunk.type == "tool_calls":
            calls = chunk.tool_calls
        elif chunk.type == "usage":
            usage = chunk.usage
    return "".join(text), calls, usage


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    args = parser.parse_args()

    settings = Settings()
    if args.model:
        settings.llm_model = args.model
    if not settings.llm_api_key:
        print("未配置 LLM_API_KEY，跳过真模型自检（mock 模式已由 pytest 覆盖）")
        return 0

    llm = OpenAICompatLLM(settings)
    print(f"真模型自检 → {settings.llm_base_url} · {settings.llm_model}")

    text, _, usage = await collect(llm, [{"role": "user", "content": "用一句话介绍你自己"}])
    check("普通补全", bool(text.strip()), f"{len(text)} 字 / usage={usage}")

    text, calls, usage = await collect(
        llm,
        [
            {"role": "system", "content": "需要数据时必须调用工具，不要凭空回答。"},
            {"role": "user", "content": "帮我看看今天的待办"},
        ],
        TOOLS,
    )
    name = calls[0].get("name") if calls else ""
    check("工具调用", name == "todo_query", f"tool={name or '-'} usage={usage}")

    result = await llm.classify("帮我约一下望京便利店明天下午三点", ["plan_create", "todo_query", "chat"])
    check("意图分类", bool(result and result.get("skill")), str(result)[:120])

    entries = [
        {"role": "user", "content": "帮我约望京便利店明天下午三点"},
        {"role": "assistant", "content": "", "tool_calls": '[{"id":"c1","name":"create_visit_plan","arguments":"{}"}]'},
        {"role": "tool", "name": "create_visit_plan", "content": '{"status":"need_input","output":"请确认门店"}'},
        {"role": "user", "content": "那就望京 SOHO 店"},
    ]
    summary = await llm_summary(llm, entries, timeout_s=settings.llm_timeout_s)
    sections = sum(1 for title in ("目标", "约束", "进展", "关键决策", "下一步", "关键上下文") if title in summary)
    check("压缩摘要六段结构", sections >= 4, f"命中 {sections}/6 段")

    # 关键回归：把 assistant(tool_calls) 原样回传，provider 必须接受
    messages = [
        {"role": "system", "content": "需要数据时必须调用工具。"},
        {"role": "user", "content": "帮我看看今天的待办"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": _openai_tool_calls(
                [{"id": "call_smoke", "name": "todo_query", "arguments": '{"date": "2026-11-08"}'}]
            ),
        },
        {"role": "tool", "tool_call_id": "call_smoke", "name": "todo_query",
         "content": '{"status":"ok","output":"今天有 1 条待办"}'},
    ]
    text, _, usage = await collect(llm, messages, TOOLS)
    check("tool_calls 回传格式", bool(text.strip()), f"{len(text)} 字 / usage={usage}")

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("真模型链路 5/5 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
