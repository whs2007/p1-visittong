"""工具扩展点：对齐 Pi extension 的 tool_call / tool_result 拦截。

Pi 的扩展可以：
  - 在 tool_call 阶段拦截（block）、改写参数；
  - 在 tool_result 阶段审计、改写结果。
P1 保留两个等价扩展点（进程内注册，不做动态加载）：
  - before_tool_call(call) -> HookDecision | None：返回非 None 即短路；
  - after_tool_result(call, outcome) -> ToolOutcome | None：可替换结果（审计/脱敏）。
钩子异常只记日志，不影响主链路。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    call_id: str
    name: str
    args: dict[str, Any]
    spec: Any = None                 # ToolSpec | None
    label: str = ""
    risk: str = "low"
    result: "ToolOutcome | None" = None


@dataclass
class ToolOutcome:
    status: str                      # ok | error | need_input | denied | rejected | timeout
    output: str
    data: dict[str, Any] | None = None
    latency_ms: int = 0
    risk: str = "low"
    decision: str = "allow"          # allow | deny | approval
    approval_id: str | None = None
    approved: bool | None = None


@dataclass(frozen=True)
class HookDecision:
    """等价于 Pi tool_call 返回的 {block, reason}。"""

    action: str                      # allow | deny | approval
    reason: str = ""
    risk: str = "low"
    rule: str = "hook"


class ToolHook(Protocol):
    name: str

    async def before_tool_call(self, call: ToolCall) -> HookDecision | None: ...

    async def after_tool_result(
        self, call: ToolCall, outcome: ToolOutcome
    ) -> ToolOutcome | None: ...


class BaseToolHook:
    """默认实现：不拦截、不改写；子类按需覆盖。"""

    name = "base"

    async def before_tool_call(self, call: ToolCall) -> HookDecision | None:
        return None

    async def after_tool_result(
        self, call: ToolCall, outcome: ToolOutcome
    ) -> ToolOutcome | None:
        return None


class HookChain:
    """按注册顺序执行；before 第一个非 None 生效，after 依次链式改写。"""

    def __init__(self, hooks: list[ToolHook] | None = None) -> None:
        self.hooks: list[ToolHook] = list(hooks or [])

    def add(self, hook: ToolHook) -> None:
        self.hooks.append(hook)

    async def before_tool_call(self, call: ToolCall) -> HookDecision | None:
        for hook in self.hooks:
            try:
                decision = await hook.before_tool_call(call)
            except Exception:  # noqa: BLE001
                log.exception("hook before_tool_call failed: %s", getattr(hook, "name", hook))
                continue
            if decision is not None:
                return decision
        return None

    async def after_tool_result(
        self, call: ToolCall, outcome: ToolOutcome
    ) -> ToolOutcome:
        current = outcome
        for hook in self.hooks:
            try:
                replaced = await hook.after_tool_result(call, current)
            except Exception:  # noqa: BLE001
                log.exception("hook after_tool_result failed: %s", getattr(hook, "name", hook))
                continue
            if replaced is not None:
                current = replaced
        return current