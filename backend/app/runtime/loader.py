"""ResourceLoader：显式装配 Agent 能力集（对齐 Pi DefaultResourceLoader）。

P1 原状：`import app.skills.tools` 的装饰器副作用填充全局 registry，
`SKILLS` 是模块级字典，编排器直接读这两者 —— 能力集进程内唯一、不可裁剪、
不可热更新。本模块把装配变成显式动作：

    loader = ResourceLoader(registry)
    loader.reload()                     # 读 agents/*.json + 校验（fail fast）
    session = loader.create_session("visit")   # 等价于 createAgentSession()

配置文件缺失时使用内置声明，保证零配置可运行；文件存在时同名覆盖。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.runtime.session import AgentSession, authorized_tool_names
from app.runtime.spec import AgentSpec
from app.skills.registry import ToolRegistry
from app.skills.visit_skills import SKILLS, SkillSpec

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


def _builtin_specs() -> dict[str, dict[str, Any]]:
    """内置声明：与 v0.4 的全局 SKILLS 完全等价，保证默认行为零回归。"""
    return {
        "visit": {
            "name": "visit",
            "label": "拜访通 · 销售助手",
            "description": "全能力销售 Agent：待办、计划、签到签退、日志、提醒。",
            "skills": list(SKILLS),
            "tools": None,
        },
        "ops_readonly": {
            "name": "ops_readonly",
            "label": "只读查询助手",
            "description": "仅可查询待办与计划，不暴露任何写工具，用于验证能力隔离。",
            "skills": ["todo_query"],
            "tools": ["query_todos", "query_plans"],
        },
    }


class ResourceLoader:
    """读取配置、校验、装配 AgentSession；支持 reload 热更新。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        agents_dir: Path | None = None,
        load_files: bool = True,
        skills: dict[str, SkillSpec] | None = None,
    ) -> None:
        self.registry = registry
        self.agents_dir = agents_dir if agents_dir is not None else AGENTS_DIR
        self.load_files = load_files
        self.skills_source = skills if skills is not None else SKILLS
        self._specs: dict[str, AgentSpec] = {}
        self.reload()

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def reload(self) -> list[str]:
        """重新读取配置并校验；任一项非法直接抛错，避免带病启动。"""
        raw_specs = _builtin_specs()
        if self.load_files and self.agents_dir.is_dir():
            for path in sorted(self.agents_dir.glob("*.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError(f"Agent 配置读取失败 {path.name}: {exc}") from exc
                spec = AgentSpec.from_dict(raw)
                raw_specs[spec.name] = spec.to_dict()
                log.info("loaded agent config: %s", path.name)

        specs: dict[str, AgentSpec] = {}
        for name, raw in raw_specs.items():
            spec = AgentSpec.from_dict(raw)
            self._validate(spec)
            specs[name] = spec
        self._specs = specs
        return list(specs)

    def _validate(self, spec: AgentSpec) -> None:
        unknown_skills = [name for name in spec.skills if name not in self.skills_source]
        if unknown_skills:
            raise ValueError(f"Agent {spec.name} 引用了未知 Skill: {', '.join(unknown_skills)}")
        if spec.tools is not None:
            unknown_tools = [name for name in spec.tools if self.registry.get(name) is None]
            if unknown_tools:
                raise ValueError(f"Agent {spec.name} 引用了未注册工具: {', '.join(unknown_tools)}")
            # 与 AgentSession.capability() 共用 authorized_tool_names：校验的是
            # 运行时真正授权的集合（registry 注册关系），而不是 SkillSpec.tools
            # 的静态声明。两者曾经不一致（query_plans 归属 todo_query 却被多个
            # SkillSpec 声明），会让 fail-fast 保证的东西和实际暴露的东西对不上。
            authorized = set(authorized_tool_names(spec, spec.skills, self.registry))
            orphan = [name for name in spec.tools if name not in authorized]
            if orphan:
                raise ValueError(
                    f"Agent {spec.name} 的工具白名单含未被任何已授权 Skill 实际注册的工具: {', '.join(orphan)}"
                )

    def spec(self, name: str) -> AgentSpec:
        if name not in self._specs:
            raise KeyError(f"未知 Agent: {name}（可用: {', '.join(self._specs)}）")
        return self._specs[name]

    def create_session(self, agent_name: str) -> AgentSession:
        spec = self.spec(agent_name)
        scoped: dict[str, SkillSpec] = {
            name: self.skills_source[name] for name in spec.skills
        }
        return AgentSession(
            spec=spec, loader=self, registry=self.registry, skills=scoped,
        )

    # ------------------------------------------------------------------ #
    # 渐进式披露
    # ------------------------------------------------------------------ #
    def catalog(self) -> list[dict[str, str]]:
        """只暴露 name / label / description / path，不含技能正文与工具细节。"""
        return [
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "path": f"agent://{spec.name}",
            }
            for spec in self._specs.values()
        ]

    def load_skill_body(self, name: str) -> str | None:
        spec = self.skills_source.get(name)
        return spec.prompt.strip() if spec else None

    def agent_names(self) -> list[str]:
        return list(self._specs)


def default_session(registry: ToolRegistry) -> AgentSession:
    """不读外部配置的兜底装配：供单测与未显式装配的调用方使用。"""
    return ResourceLoader(registry, load_files=False).create_session("visit")
