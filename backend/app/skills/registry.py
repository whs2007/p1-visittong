"""Tool / Skill 注册表：装饰器注册、Schema 生成、运行时启停、按 Skill 裁剪。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.domain.db import Database
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree


@dataclass
class ToolResult:
    """工具统一返回：ok / error / need_input / denied。"""

    status: str
    output: str
    data: dict[str, Any] | None = None


@dataclass
class ToolContext:
    """工具执行上下文：不依赖 FastAPI，单测可直接构造。"""

    settings: Settings
    session_id: str
    trace_id: str
    user_id: str
    db: Database
    tree: SessionTree
    state: SessionState
    knowledge: Any = None

    async def remember(self, **entity: str) -> None:
        await self.state.set_entity(self.session_id, {k: v for k, v in entity.items() if v})


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk: str = "low"            # low | medium | high
    scope: str = "read"          # read | write（写操作会写操作审计）
    roles: tuple[str, ...] = ("sales", "manager", "admin")  # 允许调用的角色
    skill: str | None = None     # 归属的子 Skill（看板聚合用）
    enabled: bool = True
    slot_filling: bool = False   # True 时允许缺参数，由 handler 返回 need_input
    card: str | None = None      # 成功后产出的卡片类型


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def tool(
        self,
        *,
        name: str,
        label: str,
        description: str,
        parameters: dict[str, Any],
        risk: str = "low",
        skill: str | None = None,
        scope: str = "read",
        roles: tuple[str, ...] = ("sales", "manager", "admin"),
        slot_filling: bool = False,
        card: str | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(handler: ToolHandler) -> ToolHandler:
            self._tools[name] = ToolSpec(
                name=name, label=label, description=description, parameters=parameters,
                handler=handler, risk=risk, skill=skill, scope=scope, roles=roles,
                slot_filling=slot_filling, card=card,
            )
            return handler

        return decorator

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def for_skill(self, skill: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.skill == skill and t.enabled]

    def set_enabled(self, name: str, enabled: bool) -> ToolSpec | None:
        spec = self._tools.get(name)
        if spec:
            spec.enabled = enabled
        return spec

    def openai_tools(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        allow = set(names) if names is not None else None
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
            if spec.enabled and (allow is None or spec.name in allow)
        ]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "risk": spec.risk,
                "scope": spec.scope,
                "roles": list(spec.roles),
                "skill": spec.skill,
                "enabled": spec.enabled,
                "status": "ENABLED" if spec.enabled else "DISABLED",
                "params_schema": spec.parameters,
            }
            for spec in self._tools.values()
        ]


registry = ToolRegistry()