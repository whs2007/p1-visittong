"""系统提示词组装：对齐 Pi 的「分层注入 + 技能渐进披露」。

Pi 启动时只把 skill 的 name + description + path 注入 system prompt，
正文按需读取（/skill:name 强制加载）。P1 用总控路由替代模型自主加载，
但注入结构保持一致：
  1) preamble：角色与硬规则
  2) skills：全部技能的 name + description（决定路由）
  3) 当前技能：完整 prompt + 工具白名单（单轮只注入一个）
  4) runtime：当前时间 / 槽位 / 最近实体

最后一行保留 [当前子 Skill: xxx] [槽位: {...}] 标记，Mock LLM 与既有 Trace
解析依赖它（契约见 docs/04-数据模型与接口契约.md）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.skills.visit_skills import SKILLS, SkillSpec

PREAMBLE = """你是「拜访通」销售助手，帮助 BD 完成拜访计划、到店签到与拜访日志。
硬规则：
1. 只做当前技能范围内的事；跨技能需求先说明，让用户分多轮完成。
2. 客户、门店、时间、计划 ID 必须来自工具返回，不要编造。
3. 工具报错 / 被策略拦截 / 审批被拒时如实转述，不要尝试绕过。
4. 修改或取消计划前，先回显"要改哪个对象"。
5. 用户信息不全时只追问缺的槽位，不要重复追问已确认的信息。"""

SLOT_KEYS = ("customer", "planned_at", "store", "topic", "plan_id", "distance_m")


def skill_path(name: str) -> str:
    """虚拟路径：Pi 的 skill 以文件路径披露，便于模型按需读取。"""
    return f"skill://{name}"


def build_system_prompt(
    *,
    active_skill: str,
    slots: dict[str, Any] | None = None,
    entity: dict[str, Any] | None = None,
    skills: dict[str, SkillSpec] | None = None,
    now: datetime | None = None,
) -> str:
    skills = skills or SKILLS
    slots = slots or {}
    now = now or datetime.now().astimezone()

    lines: list[str] = [PREAMBLE, "", "# 技能索引（单轮只激活一个）"]
    for spec in skills.values():
        mark = " ← 当前" if spec.name == active_skill else ""
        lines.append(f"- {spec.name}（{skill_path(spec.name)}）：{spec.description}{mark}")

    spec = skills.get(active_skill)
    if spec is not None:
        lines += [
            "",
            f"# 当前技能：{spec.label}（{spec.name}）",
            spec.prompt.strip(),
            f"可用工具：{', '.join(spec.tools) if spec.tools else '无（纯对话）'}",
        ]

    hint = json.dumps({k: v for k, v in slots.items() if k in SLOT_KEYS}, ensure_ascii=False)
    lines += [
        "",
        "# 运行时状态",
        f"- 当前时间：{now.isoformat(timespec='seconds')}",
    ]
    if entity:
        lines.append(f"- 最近实体：{json.dumps(entity, ensure_ascii=False)}")
    lines.append(f"[当前子 Skill: {active_skill}] [槽位: {hint}]")
    return "\n".join(lines)