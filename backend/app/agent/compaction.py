"""上下文压缩：对齐 Pi 的 compaction 语义。

Pi 的规则（docs/compaction.md）：
  - 触发：contextTokens > contextWindow - reserveTokens；
  - 保留：从 firstKeptEntryId 开始保留最近的 keepRecentTokens；
  - 切点：只能落在 user / assistant 边界，绝不在 toolResult 处切
    （toolCall 与 toolResult 必须同留）；
  - 摘要：固定段落（目标 / 进展 / 关键决策 / 下一步 / 关键上下文），
    旧条目仍保留在会话文件里，只是不再进入模型上下文。

P1 是请求-响应式的小规模会话，用「估算 token + 保留条数」控制预算；
摘要默认用确定性摘要（可注入 LLM 摘要实现，接口不变）。
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from typing import Any, Callable

from app.config import Settings
from app.sessions.tree import SessionTree

# 摘要器允许同步（确定性摘要）或异步（真模型），由 compact_if_needed 统一 await
Summarizer = Callable[[list[dict[str, Any]]], str | Awaitable[str]]


def estimate_tokens(text: str) -> int:
    """近似 token：CJK 1 字≈1 token，其余 4 字符≈1 token（Pi 用 chars/4）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return cjk + max(0, len(text) - cjk) // 4 + 1


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(str(m.get("content") or "")) + 8 for m in messages)


def _short(text: str, limit: int) -> str:
    return (text or "").strip().replace("\n", " ")[:limit]


def digest_entries(entries: list[dict[str, Any]]) -> str:
    """确定性摘要，段落与 Pi 的 compaction summary 对齐。"""
    goals: list[str] = []
    progress: list[str] = []
    decisions: list[str] = []
    next_steps: list[str] = []
    context: list[str] = []

    for item in entries:
        role = item.get("role")
        content = _short(item.get("content") or "", 120)
        if role == "user":
            goals.append(content[:80])
        elif role == "assistant" and item.get("tool_calls"):
            try:
                names = [c.get("name", "?") for c in json.loads(item["tool_calls"])]
            except (json.JSONDecodeError, TypeError):
                names = []
            if names:
                progress.append(f"调用工具 {'、'.join(names)}")
        elif role == "tool":
            try:
                payload = json.loads(item.get("content") or "{}")
            except json.JSONDecodeError:
                payload = {}
            status = payload.get("status") or "ok"
            if status in ("need_input", "denied", "rejected", "timeout", "error"):
                decisions.append(f"工具返回 {status}：{content[:80]}")
            else:
                progress.append(f"工具结果 {status}")
        elif role == "summary":
            context.append(f"此前摘要：{content[:100]}")
        if item.get("tool_call_id") and item.get("name"):
            context.append(f"关联工具 {item['name']}")

    def block(title: str, rows: list[str], fallback: str) -> str:
        if not rows:
            return f"## {title}\n- {fallback}"
        return f"## {title}\n" + "\n".join(f"- {row}" for row in rows[-8:])

    return "\n".join(
        [
            block("目标", goals, "（无）"),
            block("进展", progress, "（无）"),
            block("关键决策", decisions, "（无）"),
            block("下一步", next_steps, "（按用户最新输入继续）"),
            block("关键上下文", context, "（无）"),
        ]
    )


def _cut_index(path: list[dict[str, Any]], keep_recent: int) -> int:
    """返回保留窗口的起始下标；保证不把 tool 结果和它的 assistant 拆开。"""
    start = max(0, len(path) - max(1, keep_recent))
    while start > 0 and path[start].get("role") == "tool":
        start -= 1
    return start


async def compact_if_needed(
    *,
    tree: SessionTree,
    session_id: str,
    settings: Settings,
    summarizer: Summarizer | None = None,
) -> dict[str, Any] | None:
    """超预算时折叠早期条目，返回压缩信息；不需要压缩返回 None。"""
    if not settings.enable_compaction:
        return None

    path = tree.path(session_id, limit=100_000)
    if len(path) <= settings.compaction_keep_recent:
        return None

    messages = tree.messages(session_id, limit=100_000)
    tokens_before = estimate_messages(messages)
    if tokens_before <= settings.context_token_budget:
        return None

    start = _cut_index(path, settings.compaction_keep_recent)
    if start <= 0:
        return None
    older = path[:start]
    text = (summarizer or digest_entries)(older)
    if inspect.isawaitable(text):
        text = await text
    info = tree.compact(session_id, summary_text=text, first_kept_entry_id=path[start]["entry_id"])
    if info is None:
        return None
    info["tokens_before"] = tokens_before
    info["tokens_after"] = estimate_messages(tree.messages(session_id, limit=100_000))
    info["removed"] = len(older)
    return info