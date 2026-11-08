"""Agent 能力声明：把「谁能做什么」从代码里搬到配置里。

对齐 Pi 的 AgentSession / DefaultResourceLoader：一次 Agent 运行的能力集
（技能白名单、工具白名单、系统提示词覆写）是装配期决定的，不是运行时全局常量。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent 的静态能力声明。

    skills: 允许激活的子 Skill 白名单；顺序即提示词索引顺序。
    tools:  允许暴露的工具白名单；None 表示由所选 Skill 的 tools 声明推导。
    preamble: 覆写系统提示词的角色段落；None 表示用内置 PREAMBLE。
    """

    name: str
    label: str
    description: str = ""
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] | None = None
    preamble: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentSpec":
        if not isinstance(raw, dict):
            raise ValueError("Agent 配置必须是 JSON 对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("Agent 配置缺少 name")
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Agent name 非法：{name}")
        raw_skills = raw.get("skills")
        if not isinstance(raw_skills, list) or not raw_skills:
            raise ValueError(f"Agent {name} 的 skills 必须是非空数组")
        skills = tuple(str(item) for item in raw_skills)
        if len(set(skills)) != len(skills):
            raise ValueError(f"Agent {name} 的 skills 存在重复项")
        raw_tools = raw.get("tools")
        tools: tuple[str, ...] | None
        if raw_tools is None:
            tools = None
        elif isinstance(raw_tools, list):
            tools = tuple(str(item) for item in raw_tools)
        else:
            raise ValueError(f"Agent {name} 的 tools 必须是数组或 null")
        preamble = raw.get("preamble")
        return cls(
            name=name,
            label=str(raw.get("label") or name),
            description=str(raw.get("description") or ""),
            skills=skills,
            tools=tools,
            preamble=str(preamble) if preamble else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "skills": list(self.skills),
            "tools": list(self.tools) if self.tools is not None else None,
            "preamble": self.preamble,
        }
