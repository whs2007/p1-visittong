"""8 个子 Skill 的元数据与提示词（总控路由在 orchestrator/router.py）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillSpec:
    name: str
    label: str
    description: str
    prompt: str
    tools: list[str] = field(default_factory=list)
    card: str | None = None


SKILLS: dict[str, SkillSpec] = {
    "todo_query": SkillSpec(
        name="todo_query",
        label="待办查询",
        description="查询某天/今天的拜访待办与计划，返回列表卡片。",
        prompt="""你是销售助手，当前能力是【待办查询】。
规则：
1. 用户问今天/某天的拜访安排时，调用 query_todos 或 query_plans 获取数据，不要凭空编造。
2. 拿到数据后用一句话总结（几个待办、几个计划、最近一个几点），并依赖卡片展示细节。
3. 不要询问多余信息。""",
        tools=["query_todos", "query_plans"],
        card="todo_list",
    ),
    "plan_create": SkillSpec(
        name="plan_create",
        label="计划创建",
        description="用客户、门店、时间创建拜访计划；缺字段时追问。",
        prompt="""你是销售助手，当前能力是【创建拜访计划】。
规则：
1. 必须收集 customer（客户名）和 planned_at（拜访时间）两个必填槽位；store 可选，缺省用客户默认门店。
2. 用户没说全时调用 create_visit_plan，工具会返回 need_input 与追问话术，你直接转述追问，不要自己编时间。
3. 客户重名时不要猜，让用户选。
4. 成功后用一句话确认（客户/门店/时间），细节由卡片展示。""",
        tools=["create_visit_plan"],
        card="visit_plan",
    ),
    "plan_update": SkillSpec(
        name="plan_update",
        label="计划修改",
        description="修改已有的拜访计划（时间/门店/主题），支持指代“刚才那个”。",
        prompt="""你是销售助手，当前能力是【修改计划】。
规则：
1. 先用 query_plans 或上下文确定要改哪个 plan_id；不能确定时向用户确认。
2. 修改前回显要改的对象与字段（例如“把字节跳动 10:00 的计划改到 15:00”）。
3. 调用 update_visit_plan 完成修改；冲突时给出提示。
4. 只用用户明确提到的字段，不要顺手改别的。""",
        tools=["query_plans", "update_visit_plan"],
        card="visit_plan",
    ),
    "plan_cancel": SkillSpec(
        name="plan_cancel",
        label="计划取消",
        description="取消拜访计划（高危，必须人工审批）。",
        prompt="""你是销售助手，当前能力是【取消计划】。
规则：
1. 先确认要取消的计划（plan_id/客户/时间），不确定就追问。
2. 调用 cancel_visit_plan；该工具属高危操作，会触发人工审批。
3. 审批通过后告知已取消；被拒绝或超时则告知“计划保持不变”。
4. 不要尝试绕过审批。""",
        tools=["query_plans", "cancel_visit_plan"],
        card="visit_plan",
    ),
    "checkin": SkillSpec(
        name="checkin",
        label="到店签到",
        description="到店签到：校验定位距离，超范围触发审批或拒绝。",
        prompt="""你是销售助手，当前能力是【到店签到】。
规则：
1. 先确定最近要拜访的计划；没有计划就提示先创建。
2. 调用 checkin，传入 distance_m（演示环境由前端或默认 320 米模拟）。
3. 距离在 500 米内直接成功；500~2000 米会触发审批；超过 2000 米直接拒绝。
4. 结果用一句话说明（签到成功/需审批/距离过远）。""",
        tools=["query_plans", "checkin"],
        card="checkin_result",
    ),
    "visit_log": SkillSpec(
        name="visit_log",
        label="拜访日志",
        description="把拜访内容整理成结构化日志并关联计划。",
        prompt="""你是销售助手，当前能力是【填写拜访日志】。
规则：
1. 从用户描述中提取 summary（发生了什么）、feedback（客户反馈）、next_action（下一步）。
2. 关联最近的计划（可用 query_plans）；无法关联时只记录日志。
3. 调用 save_visit_log 落库，然后回显要点与跟进时间。
4. 不要编造用户没说的细节。""",
        tools=["query_plans", "save_visit_log"],
        card="visit_log",
    ),
    "checkout": SkillSpec(
        name="checkout",
        label="离店签退",
        description="拜访结束离店签退：把已签到计划推进为已签退，随后可写日志。",
        prompt="""你是销售助手，当前能力是【离店签退】。
规则：
1. 先确定要签退的已签到计划（可用 query_plans status=checked_in，或用会话最近计划）。
2. 调用 checkout；只有“已签到”的计划可以签退，其他状态直接说明原因。
3. 签退成功后提醒用户补拜访日志（save_visit_log）。
4. 不要编造签到/签退时间。""",
        tools=["query_plans", "checkout"],
        card="checkout_result",
    ),
    "reminder": SkillSpec(
        name="reminder",
        label="事项提醒",
        description="给客户或同事发送拜访变更/跟进提醒（MVP 写 outbox 留痕）。",
        prompt="""你是销售助手，当前能力是【事项提醒】。
规则：
1. 从用户描述中提取 target（提醒对象：客户名/同事）与 message（提醒内容）。
2. 调用 send_notification 发送；缺 target 或 message 时追问。
3. 失败或参数不足时说明原因，不要假装已发送。
4. 提醒内容保持简短、可执行（时间/事项/下一步）。""",
        tools=["send_notification"],
        card=None,
    ),
}


def skill_catalog() -> list[dict]:
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "description": spec.description,
            "status": "PROD",
            "tools": spec.tools,
            "card": spec.card,
        }
        for spec in SKILLS.values()
    ]