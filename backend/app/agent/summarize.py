"""压缩摘要：把 Pi 的固定段落交给模型生成，失败回退确定性摘要。

Pi 的 compaction 用模型生成摘要，段落是固定的六段（Goal / Constraints /
Progress / Key Decisions / Next Steps / Critical Context）；本项目的
`digest_entries` 一直是确定性摘要（为了离线可测），这里补上「有真模型时用真模型」。

为什么必须带兜底：摘要失败会让上下文压缩整个失败——那等于在「上下文已经接近
上限」时再叠一次故障。所以策略是 **模型摘要失败就退回确定性摘要**，宁可摘要
质量差一点，也不能让 Turn 挂掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent.compaction import Summarizer, digest_entries
from app.config import Settings
from app.llm.base import BaseLLM

log = logging.getLogger(__name__)

_SYSTEM = """你在为一次销售拜访对话做上下文压缩。请只输出 Markdown，严格包含以下六节，不要额外解释：
## 目标
## 约束
## 进展
## 关键决策
## 下一步
## 关键上下文

要求：
1. 只写对话里出现过的事实，不要推测、不要补充业务常识；
2. 计划 ID、客户名、门店名、时间等标识原样保留（它们是后续工具调用的参数）；
3. 被拒绝 / 待审批 / 参数不足这类状态必须写进「关键决策」或「下一步」；
4. 总长度不超过 400 字。"""


def _render(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in entries:
        role = item.get("role")
        content = str(item.get("content") or "")[:600]
        if role == "assistant" and item.get("tool_calls"):
            try:
                names = [call.get("name", "?") for call in json.loads(item["tool_calls"])]
            except (json.JSONDecodeError, TypeError):
                names = []
            lines.append(f"assistant: （调用工具 {', '.join(names)}）{content}")
        elif role == "tool":
            lines.append(f"tool({item.get('name') or '?'}): {content}")
        elif role == "summary":
            lines.append(f"summary: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def llm_summary(llm: BaseLLM, entries: list[dict[str, Any]], *, timeout_s: float = 20.0) -> str:
    """让模型按固定段落摘要；任何异常都回退到确定性摘要。"""
    transcript = _render(entries)
    if not transcript.strip():
        return digest_entries(entries)
    chunks: list[str] = []
    try:
        async with asyncio.timeout(timeout_s):
            async for chunk in llm.stream(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"以下是需要压缩的历史对话：\n\n{transcript}"},
                ]
            ):
                if chunk.type == "text_delta":
                    chunks.append(chunk.text)
        text = "".join(chunks).strip()
        # 太短通常意味着模型没按要求输出（或只回了一句客套话），不如用确定性摘要
        if len(text) < 60 or "##" not in text:
            raise ValueError(f"摘要不合规（{len(text)} 字）")
        return text
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("llm_summary timeout, fallback to digest")
    except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响 Turn
        log.warning("llm_summary failed (%s), fallback to digest", type(exc).__name__)
    return digest_entries(entries)


def build_summarizer(llm: BaseLLM, settings: Settings) -> Summarizer | None:
    """有真模型才返回摘要器；mock 直接返回 None（mock 的「摘要」是假的）。"""
    if getattr(llm, "name", "") == "mock":
        return None
    timeout_s = max(5.0, min(settings.llm_timeout_s, 30.0))

    async def summarizer(entries: list[dict[str, Any]]) -> str:
        return await llm_summary(llm, entries, timeout_s=timeout_s)

    return summarizer
