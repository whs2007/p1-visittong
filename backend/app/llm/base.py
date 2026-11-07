"""LLM 适配层统一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


class LLMError(RuntimeError):
    """模型调用失败（超时/5xx/协议错误）。"""

    def __init__(self, message: str, code: str = "LLM_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class LLMChunk:
    type: str  # text_delta | tool_calls | usage | done
    text: str = ""
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class BaseLLM(ABC):
    name: str = "base"

    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """产出增量事件；实现方保证最后一条是 done。"""
        raise NotImplementedError
        yield  # pragma: no cover  (让 async generator 类型成立)

    async def classify(self, text: str, labels: list[str]) -> dict[str, Any] | None:
        """轻量意图分类（总控路由用）：返回 {skill, confidence, slots?, reason?}。

        默认不实现（返回 None）→ 路由层回退到规则通道。
        """
        return None