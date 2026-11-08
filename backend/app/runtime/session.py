"""AgentSession：一次装配的结果，Orchestrator 只通过它取能力。

对齐 Pi 的 createAgentSession()：会话持有本次运行可用的 skills / tools /
prompt，而不是让编排器直接读模块级全局字典。这样同一套 Runtime 可以装配出
能力集完全不同的 Agent，且越权能力在装配期就被裁掉。
"""
from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from app.agent.prompt import build_system_prompt, skill_path
from app.skills.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from app.runtime.contract import ExecutionContract
    from app.runtime.loader import ResourceLoader
    from app.runtime.spec import AgentSpec
    from app.skills.visit_skills import SkillSpec


def authorized_tool_names(spec: "AgentSpec", skill_names: Any, registry: ToolRegistry) -> list[str]:
    """装配期授权的工具全集：**唯一**定义，配置校验与能力指纹共用。

    刻意不看 `ToolSpec.enabled`：enabled 是运行期操作开关（主管可随时启停），
    由 PolicyEngine 在每次调用时硬校验（`tool_unavailable`）。若把它算进能力
    指纹，主管关掉一个工具就会让所有在途 Turn 的检查点失效，等于把「可用性
    开关」误当成「权限边界变更」。装配授权集 = 归属已授权 Skill + 已注册 +
    （若配置了白名单）在白名单内。
    """
    allowed = set(spec.tools) if spec.tools is not None else None
    names: list[str] = []
    for tool in registry.all():
        if tool.skill not in skill_names:
            continue
        if allowed is not None and tool.name not in allowed:
            continue
        if tool.name not in names:
            names.append(tool.name)
    return names


class AgentSession:
    """已装配的 Agent 能力视图（不可变语义：刷新需由 ResourceLoader 重建）。"""

    def __init__(
        self,
        *,
        spec: "AgentSpec",
        loader: "ResourceLoader",
        registry: ToolRegistry,
        skills: dict[str, "SkillSpec"],
    ) -> None:
        self.spec = spec
        self.loader = loader
        self.registry = registry
        # 真不可变：避免「文档说 session 不可变、实际能被任意改 _skills」的口实。
        # 能力集变更只能由 ResourceLoader 重建 session（原子替换）。
        self._skills = MappingProxyType(dict(skills))

    # ------------------------------------------------------------------ #
    # 能力视图
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def skills(self) -> dict[str, "SkillSpec"]:
        """当前 Agent 可见的技能（供 build_system_prompt 使用）。"""
        return self._skills

    def skill_names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def route_labels(self) -> list[str]:
        """交给 LLM 分类器的候选标签：只含本 Agent 可见技能。"""
        return [*self._skills, "chat"]

    def allows_skill(self, name: str) -> bool:
        return name == "chat" or name in self._skills

    def authorized_tools(self) -> frozenset[str]:
        """装配期授权的工具全集（已注册 + 归属已授权 Skill + 白名单内）。"""
        return frozenset(authorized_tool_names(self.spec, self._skills, self.registry))

    def allows_tool(self, name: str) -> bool:
        """Agent 级能力边界。

        v0.7 起不再是「配置里没写白名单就一律放行」：判定集合统一为
        `authorized_tool_names()`，因此默认 `visit` 下**根本不存在**的工具名
        （模型幻觉 / 提示词注入造出来的名字）会被拒绝。之前
        `spec.tools is None -> True` 让默认装配的 guard 对未知工具恒真，
        这是审计里点名的一条「名不副实」。
        """
        return name in self.authorized_tools()

    def guard(self, contract: "ExecutionContract") -> tuple[bool, str, str]:
        """执行期硬校验（凭证版）：三条同时成立才放行。

        见 `guard_tool` 的说明；这里只是把凭证里的两个字段取出来。
        """
        return self.guard_tool(contract.tool_name, contract.active_skill)

    def guard_tool(self, tool_name: str, active_skill: str) -> tuple[bool, str, str]:
        """执行期硬校验：三条同时成立才放行。

        (1) 当前 Skill 已授权给本 Agent；
        (2) 工具**归属**当前 Skill（不是只看 Agent 级白名单 —— 否则模型在
            `visit_log` 技能里调 `cancel_visit_plan` 也能通过，审计 §4.2 第 3 点）；
        (3) 工具在装配授权集内（含已注册）且当前未被主管停用。

        返回 (ok, rule, reason)。rule 会写进 Trace 与治理台账，便于事后统计
        「拦住了什么」，而不是只留一句自然语言。
        """
        if not self.allows_skill(active_skill):
            return (
                False,
                "skill_not_authorized",
                f"技能 {active_skill} 未授权给 Agent {self.name}",
            )
        tool = self.registry.get(tool_name)
        if tool is None:
            return (
                False,
                "tool_not_registered",
                f"工具 {tool_name} 不存在（未注册）",
            )
        if tool.skill and tool.skill != active_skill:
            return (
                False,
                "cross_skill_denied",
                f"工具 {tool.name} 归属技能 {tool.skill}，当前技能是 {active_skill}",
            )
        if tool.name not in self.authorized_tools():
            return (
                False,
                "not_in_agent_capability",
                f"工具 {tool.name} 不在 Agent {self.name} 的能力白名单内",
            )
        if not tool.enabled:
            return (
                False,
                "tool_disabled",
                f"工具 {tool.name} 已被主管停用",
            )
        return True, "allowed", ""

    def label_of(self, skill: str) -> str:
        spec = self._skills.get(skill)
        return spec.label if spec else "通用对话"

    def resolve_route(self, skill: str) -> str:
        """能力边界：路由到未授权技能时降级为通用对话，而不是让它执行。"""
        return skill if self.allows_skill(skill) else "chat"

    # ------------------------------------------------------------------ #
    # 渐进式披露（Pi：启动只注入 name + description + path，正文按需读取）
    # ------------------------------------------------------------------ #
    def skills_index(self) -> list[dict[str, str]]:
        return [
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "path": skill_path(spec.name),
                "tools": ", ".join(spec.tools) if spec.tools else "",
            }
            for spec in self._skills.values()
        ]

    def load_skill_body(self, name: str) -> str | None:
        """按需读取技能正文；未授权技能返回 None。"""
        spec = self._skills.get(name)
        return spec.prompt.strip() if spec else None

    # ------------------------------------------------------------------ #
    # 工具白名单
    # ------------------------------------------------------------------ #
    def tools_for(self, skill: str) -> list[ToolSpec]:
        if not self.allows_skill(skill):
            return []
        base = self.registry.for_skill(skill)
        if self.spec.tools is None:
            return base
        allowed = set(self.spec.tools)
        return [tool for tool in base if tool.name in allowed]

    def openai_tools(self, skill: str) -> list[dict[str, Any]]:
        return self.registry.openai_tools([tool.name for tool in self.tools_for(skill)])

    def exposed_tool_names(self) -> list[str]:
        names: list[str] = []
        for skill in self._skills:
            for tool in self.tools_for(skill):
                if tool.name not in names:
                    names.append(tool.name)
        return names

    # ------------------------------------------------------------------ #
    # 能力快照（Durable Execution 的一致性边界）
    # ------------------------------------------------------------------ #
    def capability(self) -> dict[str, Any]:
        """本次装配的授权能力集，与 ResourceLoader._validate 用同一个集合定义。

        不用配置里的 spec.tools 字面值，也不用受 enabled 影响的 exposed_tool_names()，
        而是 authorized_tool_names()：归属已授权 Skill 且已注册的工具全集。
        这样「配置校验保证的东西」和「checkpoint 绑定的东西」是同一件事。
        """
        return {
            "agent": self.spec.name,
            "skills": sorted(self._skills),
            "tools": sorted(authorized_tool_names(self.spec, self._skills, self.registry)),
            "preamble": self.spec.preamble or "",
        }

    def capability_fingerprint(self) -> str:
        """能力集摘要。checkpoint 绑定它，跨能力集续跑一律拒绝。

        用内容哈希而非手填版本号：改了配置却忘记改版本号，内容哈希照样变。
        完整快照另存 checkpoint.capability_json，供事后举证「这个 Turn
        当时到底有哪些能力」，避免只留一个不可反查的哈希。
        """
        payload = json.dumps(
            self.capability(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # 提示词
    # ------------------------------------------------------------------ #
    def system_prompt(
        self,
        *,
        active_skill: str,
        slots: dict[str, Any] | None = None,
        entity: dict[str, Any] | None = None,
        memory_block: str = "",
    ) -> str:
        return build_system_prompt(
            active_skill=active_skill,
            slots=slots,
            entity=entity,
            skills=self._skills,
            preamble=self.spec.preamble,
            tool_names=[tool.name for tool in self.tools_for(active_skill)],
            skill_body=self.load_skill_body(active_skill),
            memory_block=memory_block,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "label": self.spec.label,
            "description": self.spec.description,
            "skills": list(self._skills),
            "tools": self.exposed_tool_names(),
            "tools_mode": "derived" if self.spec.tools is None else "allowlist",
            "preamble_overridden": bool(self.spec.preamble),
            "capability_fingerprint": self.capability_fingerprint(),
        }
