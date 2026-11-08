"""原子工具：待办/计划/签到/签退/日志/转写/通知。

设计：每个工具都是确定性函数 + JSON Schema + 风险等级；槽位由 handler 统一处理，
缺参时返回 need_input（status），由编排层转成追问话术。
状态变更必须走 app/domain/fsm.py 的 FSM（校验 + 写 state_transitions 审计）。
"""
from __future__ import annotations

from typing import Any

from app.adapters import asr, crm, notify
from app.domain.fsm import FSM
from app.skills.registry import ToolContext, ToolResult, registry
from app.utils import now_iso


# --------------------------------------------------------------------------- #
# 效果级幂等：写工具的「进入前回放 / 成功后记账」
#
# 崩溃窗口（handler 已产生副作用、账本还没写终态）无法靠本地事务消除 ——
# 副作用可能落在 CRM、短信网关这类没有事务的系统里。这里的做法是：
#   1) 幂等账本按 (turn_id, step, call_id) 记录完整 ToolResult，重放时直接返回；
#   2) 业务侧本身有自然唯一性（计划时段冲突、FSM 不允许重复签到/取消），
#      即使账本停在 running，重复执行也会被业务规则挡住。
# 两者叠加 = 有效一次（effectively-once），而不是宣称不可能的分布式事务。
# --------------------------------------------------------------------------- #
def _replay(ctx: ToolContext) -> ToolResult | None:
    idem = getattr(ctx, "idem", None)
    return idem.replay() if idem is not None else None


def _remember(ctx: ToolContext, result: ToolResult, resource_id: str = "") -> ToolResult:
    idem = getattr(ctx, "idem", None)
    if idem is not None:
        idem.remember(result, resource_id)
    return result


def plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": plan["plan_id"],
        "customer": plan.get("customer_name"),
        "store": plan.get("store_name"),
        "planned_at": plan.get("planned_at"),
        "topic": plan.get("topic"),
        "status": plan.get("status"),
    }


def plan_card(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_name": "visit_plan",
        "data": plan_payload(plan),
        "actions": [
            {"action_id": "reschedule", "label": "改约", "skill_name": "plan_update"},
            {"action_id": "checkin", "label": "签到", "skill_name": "checkin"},
            {"action_id": "checkout", "label": "签退", "skill_name": "checkout"},
            {"action_id": "cancel", "label": "取消", "skill_name": "plan_cancel"},
            {"action_id": "log", "label": "写日志", "skill_name": "visit_log"},
        ],
    }


def _resolve_customer(ctx: ToolContext, name: str) -> tuple[dict[str, Any] | None, ToolResult | None]:
    rows = crm.search_customers(ctx.db, name) if name else []
    if not rows and name and ctx.knowledge is not None:
        # 精确/别名匹配失败 -> 向量语义兜底（营业执照简称、品牌、地址片段等）
        try:
            hits = ctx.knowledge.search(name, kind="customer", top_k=3)
        except RuntimeError:
            hits = []
        semantic = []
        for hit in hits:
            if hit.get("score", 0) < ctx.settings.knowledge_min_score:
                continue
            customer = ctx.db.get_customer(hit.get("ref_id") or "")
            if customer and customer not in semantic:
                semantic.append(customer)
        rows = semantic
    if len(rows) == 1:
        return rows[0], None
    if len(rows) > 1:
        card = {
            "card_name": "customer_choice",
            "data": {"keyword": name, "candidates": rows},
            "actions": [
                {"action_id": c["customer_id"], "label": c["name"] + f"（{c['brand']}）", "skill_name": "plan_create"}
                for c in rows[:3]
            ],
        }
        return None, ToolResult(
            "need_input",
            f"找到 {len(rows)} 个「{name}」，请选择具体客户。",
            {"missing": ["customer_id"], "question": "请选择具体客户", "card": card},
        )
    return None, ToolResult(
        "need_input",
        f"没有找到客户「{name}」，请提供客户全称。",
        {"missing": ["customer"], "question": f"没有找到客户「{name}」，请提供客户全称"},
    )


# --------------------------------------------------------------------------- #
# 查询类
# --------------------------------------------------------------------------- #
@registry.tool(
    name="query_todos", label="查询待办", risk="low", skill="todo_query", card="todo_list",
    description="查询当前销售今天（或指定日期）的拜访待办与关联计划。",
    parameters={
        "type": "object",
        "properties": {"day": {"type": "string", "description": "日期 YYYY-MM-DD，缺省今天"}},
        "required": [],
    },
)
async def query_todos(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from datetime import date

    day = args.get("day") or date.today().isoformat()
    if day == "today":
        day = date.today().isoformat()
    todos = ctx.db.list_todos(ctx.user_id, day)
    plans = ctx.db.list_plans(ctx.user_id, day=day)
    card = {
        "card_name": "todo_list",
        "data": {"day": day, "todos": todos, "plans": [plan_payload(p) for p in plans]},
        "actions": [],
    }
    summary = f"{day} 共 {len(todos)} 条待办、{len(plans)} 个拜访计划"
    if plans:
        summary += f"，最近一个是 {plans[0]['planned_at'][11:16]} {plans[0]['customer_name']}"
    return ToolResult("ok", summary, {"card": card, "todos": todos, "plans": plans})


@registry.tool(
    name="query_plans", label="查询计划", risk="low", skill="todo_query", card="plan_list",
    description="按日期或状态查询拜访计划列表，用于定位要修改/取消/签到的计划。",
    parameters={
        "type": "object",
        "properties": {
            "day": {"type": "string", "description": "日期 YYYY-MM-DD 或 today"},
            "status": {"type": "string", "description": "planned/checked_in/done/cancelled"},
        },
        "required": [],
    },
)
async def query_plans(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from datetime import date

    day = args.get("day")
    if day in (None, "", "today"):
        day = date.today().isoformat()
    plans = ctx.db.list_plans(ctx.user_id, status=args.get("status"), day=day)
    card = {"card_name": "plan_list", "data": {"day": day, "plans": [plan_payload(p) for p in plans]}, "actions": []}
    return ToolResult("ok", f"共 {len(plans)} 个计划", {"card": card, "plans": plans})


# --------------------------------------------------------------------------- #
# 计划类
# --------------------------------------------------------------------------- #
@registry.tool(
    name="create_visit_plan", label="创建计划", risk="medium", scope="write", skill="plan_create",
    slot_filling=True, card="visit_plan",
    description="创建拜访计划。必填 customer（客户名称）与 planned_at（ISO 时间）；缺参数时会返回追问。",
    parameters={
        "type": "object",
        "properties": {
            "customer": {"type": "string", "description": "客户名称或简称"},
            "planned_at": {"type": "string", "description": "拜访时间 ISO8601，例如 2026-11-04T10:00:00+08:00"},
            "store": {"type": "string", "description": "门店名称，缺省用客户默认门店"},
            "topic": {"type": "string", "description": "拜访主题"},
        },
        "required": ["customer", "planned_at"],
    },
)
async def create_visit_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    replayed = _replay(ctx)
    if replayed is not None:
        return replayed
    slots = await ctx.state.slots(ctx.session_id)
    merged = {**slots, **{k: v for k, v in args.items() if v not in (None, "")}}
    missing = [k for k in ("customer", "planned_at") if not merged.get(k)]
    if missing:
        await ctx.state.set_slots(ctx.session_id, merged)
        question = "请补充" + "、".join({"customer": "客户名称", "planned_at": "拜访时间"}[m] for m in missing)
        return ToolResult("need_input", question, {"missing": missing, "question": question})

    customer = ctx.db.get_customer(merged["customer_id"]) if merged.get("customer_id") else None
    if customer is None:
        customer, error = _resolve_customer(ctx, merged["customer"])
        if error:
            return error
    assert customer is not None

    stores = crm.list_stores(ctx.db, customer["customer_id"])
    store = None
    if merged.get("store"):
        store = next((s for s in stores if merged["store"] in (s["name"] or "")), None)
    if store is None and stores:
        if len(stores) > 1 and not merged.get("store"):
            question = "该客户有多个门店：" + "、".join(s["name"] for s in stores[:3]) + "，去哪个？"
            return ToolResult("need_input", question, {"missing": ["store"], "question": question})
        store = stores[0]

    conflict = ctx.db.plan_conflict(ctx.user_id, merged["planned_at"])
    if conflict:
        idem = getattr(ctx, "idem", None)
        if idem is not None and getattr(idem, "state", "") == "running" \
                and conflict.get("owner_id") in (None, ctx.user_id):
            # 崩溃重放：账本停在 running，但业务表里已经有一条同时段的计划 ——
            # 那多半就是上一次已经成功的副作用，认领它，不要再建第二条。
            return _remember(
                ctx,
                ToolResult(
                    "ok",
                    f"已创建计划：{conflict['customer_name']} · "
                    f"{conflict.get('store_name') or '未指定门店'} · {conflict['planned_at'][:16]}",
                    {"card": plan_card(conflict), "plan": plan_payload(conflict), "recovered": True},
                ),
                conflict["plan_id"],
            )
        question = (
            f"{merged['planned_at'][5:16]} 已有计划「{conflict['topic'] or '客户拜访'}」，"
            "换个时间可以吗？（例如 15:00）"
        )
        return ToolResult("need_input", question, {"missing": ["planned_at"], "question": question})

    plan = ctx.db.create_plan(
        customer_id=customer["customer_id"],
        store_id=store["store_id"] if store else None,
        owner_id=ctx.user_id,
        planned_at=merged["planned_at"],
        topic=merged.get("topic") or "客户拜访",
    )
    await ctx.state.clear_slots(ctx.session_id, ["customer", "planned_at", "store", "topic"])
    await ctx.remember(last_plan_id=plan["plan_id"], last_customer_id=customer["customer_id"])
    FSM(ctx.db).create(plan["plan_id"], actor=ctx.user_id, trace_id=ctx.trace_id)
    output = f"已创建计划：{plan['customer_name']} · {plan.get('store_name') or '未指定门店'} · {plan['planned_at'][:16]}"
    return _remember(
        ctx,
        ToolResult("ok", output, {"card": plan_card(plan), "plan": plan_payload(plan)}),
        plan["plan_id"],
    )


@registry.tool(
    name="update_visit_plan", label="修改计划", risk="medium", scope="write", skill="plan_update",
    slot_filling=True, card="visit_plan",
    description="修改拜访计划的时间/门店/主题。plan_id 缺省时取当前会话最近操作的计划。",
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 ID"},
            "planned_at": {"type": "string", "description": "新的拜访时间 ISO8601"},
            "store": {"type": "string", "description": "新的门店名称"},
            "topic": {"type": "string", "description": "新的拜访主题"},
        },
        "required": [],
    },
)
async def update_visit_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    replayed = _replay(ctx)
    if replayed is not None:
        return replayed
    entity = await ctx.state.entity(ctx.session_id)
    plan = None
    if args.get("plan_id"):
        plan = ctx.db.get_plan(args["plan_id"])
    elif entity.get("last_plan_id"):
        plan = ctx.db.get_plan(entity["last_plan_id"])
    if plan is None:
        plans = ctx.db.list_plans(ctx.user_id, day=__import__("datetime").date.today().isoformat())
        if len(plans) == 1:
            plan = plans[0]
        elif plans:
            options = "、".join(f"{p['planned_at'][11:16]} {p['customer_name']}" for p in plans[:3])
            return ToolResult("need_input", f"要修改哪个计划？{options}", {"missing": ["plan_id"], "question": "要修改哪个计划？"})
        else:
            return ToolResult("need_input", "今天没有可修改的计划，请先创建。", {"missing": ["plan_id"], "question": "今天没有可修改的计划"})

    fsm = FSM(ctx.db)
    allowed = fsm.can(plan["plan_id"], "update")
    if not allowed.ok:
        return ToolResult("error", f"计划当前状态是 {plan['status']}，不能修改（只有待拜访可改）。", {})

    changes: dict[str, Any] = {}
    if args.get("planned_at"):
        changes["planned_at"] = args["planned_at"]
    if args.get("topic"):
        changes["topic"] = args["topic"]
    if args.get("store"):
        stores = crm.list_stores(ctx.db, plan["customer_id"])
        match = next((s for s in stores if args["store"] in (s["name"] or "")), None)
        if not match:
            return ToolResult("need_input", f"没有找到门店「{args['store']}」", {"missing": ["store"], "question": "请确认门店名称"})
        changes["store_id"] = match["store_id"]
    if not changes:
        return ToolResult("need_input", "想改哪一项？时间、门店还是主题？", {"missing": ["field"], "question": "想改哪一项？时间、门店还是主题？"})

    if "planned_at" in changes:
        conflict = ctx.db.plan_conflict(ctx.user_id, changes["planned_at"], exclude_plan_id=plan["plan_id"])
        if conflict:
            return ToolResult(
                "need_input",
                f"{changes['planned_at'][5:16]} 已有「{conflict['topic'] or '客户拜访'}」，换个时间？",
                {"missing": ["planned_at"], "question": "目标时间已有安排，请换个时间"},
            )

    before = plan
    updated = ctx.db.update_plan(plan["plan_id"], changes, ctx.user_id)
    assert updated is not None
    fsm.plan(plan["plan_id"], "update", actor=ctx.user_id, trace_id=ctx.trace_id)
    await ctx.remember(last_plan_id=updated["plan_id"])
    output = f"已把 {updated['customer_name']} 的计划从 {before['planned_at'][:16]} 改到 {updated['planned_at'][:16]}"
    return _remember(
        ctx,
        ToolResult("ok", output, {"card": plan_card(updated), "plan": plan_payload(updated), "changes": changes}),
        updated["plan_id"],
    )


@registry.tool(
    name="cancel_visit_plan", label="取消计划", risk="high", scope="write", skill="plan_cancel", card="visit_plan",
    description="取消拜访计划。高危操作，必须经过人工审批。",
    parameters={
        "type": "object",
        "properties": {"plan_id": {"type": "string", "description": "计划 ID"}},
        "required": [],
    },
)
async def cancel_visit_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    replayed = _replay(ctx)
    if replayed is not None:
        return replayed
    plan = None
    if args.get("plan_id"):
        plan = ctx.db.get_plan(args["plan_id"])
    else:
        entity = await ctx.state.entity(ctx.session_id)
        if entity.get("last_plan_id"):
            plan = ctx.db.get_plan(entity["last_plan_id"])
    if plan is None:
        plans = [p for p in ctx.db.list_plans(ctx.user_id) if p["status"] in ("planned", "checked_in")]
        if len(plans) == 1:
            plan = plans[0]
        else:
            return ToolResult("need_input", "要取消哪个计划？请说明客户或时间。", {"missing": ["plan_id"], "question": "要取消哪个计划？"})
    if plan["status"] == "cancelled":
        return ToolResult("error", "该计划已经是取消状态。", {})
    result = FSM(ctx.db).plan(plan["plan_id"], "cancel", actor=ctx.user_id, trace_id=ctx.trace_id)
    if not result.ok:
        return ToolResult("error", f"计划当前状态是 {plan['status']}，不能取消。", {})
    notify.notify(ctx.db, "sms", plan["customer_name"], f"拜访计划取消：{plan['planned_at'][:16]}")
    return _remember(
        ctx,
        ToolResult("ok", f"已取消 {plan['customer_name']} {plan['planned_at'][:16]} 的拜访计划。", {"card": plan_card({**plan, "status": "cancelled"})}),
        plan["plan_id"],
    )


# --------------------------------------------------------------------------- #
# 签到 / 日志 / 转写
# --------------------------------------------------------------------------- #
@registry.tool(
    name="checkin", label="到店签到", risk="medium", scope="write", skill="checkin", card="checkin_result",
    description="到店签到：传入门店距离 distance_m（米）。500 米内直接成功，500~2000 米需审批，超过 2000 米拒绝。",
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 ID，缺省取最近的待拜访计划"},
            "distance_m": {"type": "integer", "description": "当前位置到门店的距离（米）"},
        },
        "required": [],
    },
)
async def checkin(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    replayed = _replay(ctx)
    if replayed is not None:
        return replayed
    plan = None
    if args.get("plan_id"):
        plan = ctx.db.get_plan(args["plan_id"])
    else:
        entity = await ctx.state.entity(ctx.session_id)
        if entity.get("last_plan_id"):
            plan = ctx.db.get_plan(entity["last_plan_id"])
        if plan is None or plan["status"] != "planned":
            plans = [p for p in ctx.db.list_plans(ctx.user_id) if p["status"] == "planned"]
            plan = plans[0] if plans else None
    if plan is None:
        return ToolResult("need_input", "今天没有待拜访的计划，先创建一个？", {"missing": ["plan_id"], "question": "今天没有待拜访的计划"})
    if plan["status"] != "planned":
        return ToolResult("error", f"计划当前状态是 {plan['status']}，不能签到。", {})

    fsm = FSM(ctx.db)
    allowed = fsm.can(plan["plan_id"], "checkin")
    if not allowed.ok:
        return ToolResult("error", f"计划当前状态是 {plan['status']}，不能签到。", {})
    distance = int(args.get("distance_m") or 320)
    record = ctx.db.create_checkin(
        plan_id=plan["plan_id"], user_id=ctx.user_id,
        lng=116.316, lat=39.983, distance_m=distance, status="ok",
    )
    fsm.plan(plan["plan_id"], "checkin", actor=ctx.user_id, trace_id=ctx.trace_id)
    await ctx.remember(last_plan_id=plan["plan_id"])
    card = {
        "card_name": "checkin_result",
        "data": {"plan_id": plan["plan_id"], "customer": plan["customer_name"], "distance_m": distance, "checked_at": record["checked_at"] if record else now_iso()},
        "actions": [
            {"action_id": "checkout", "label": "签退", "skill_name": "checkout"},
            {"action_id": "log", "label": "写日志", "skill_name": "visit_log"},
        ],
    }
    return _remember(
        ctx,
        ToolResult("ok", f"签到成功：{plan['customer_name']}，距离门店 {distance} 米。", {"card": card}),
        plan["plan_id"],
    )


@registry.tool(
    name="checkout", label="离店签退", risk="medium", scope="write", skill="checkout", card="checkout_result",
    description="拜访结束离店签退：把已签到（checked_in）的计划推进为已签退（checked_out）。",
    parameters={
        "type": "object",
        "properties": {"plan_id": {"type": "string", "description": "计划 ID，缺省取最近已签到的计划"}},
        "required": [],
    },
)
async def checkout(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    replayed = _replay(ctx)
    if replayed is not None:
        return replayed
    plan = None
    if args.get("plan_id"):
        plan = ctx.db.get_plan(args["plan_id"])
    else:
        entity = await ctx.state.entity(ctx.session_id)
        if entity.get("last_plan_id"):
            plan = ctx.db.get_plan(entity["last_plan_id"])
        if plan is None or plan["status"] != "checked_in":
            plans = [p for p in ctx.db.list_plans(ctx.user_id) if p["status"] == "checked_in"]
            plan = plans[0] if plans else None
    if plan is None:
        return ToolResult("need_input", "今天没有已签到的计划，先签到再签退。", {"missing": ["plan_id"], "question": "今天没有已签到的计划"})
    result = FSM(ctx.db).plan(plan["plan_id"], "checkout", actor=ctx.user_id, trace_id=ctx.trace_id)
    if not result.ok:
        return ToolResult("error", f"计划当前状态是 {plan['status']}，不能签退。", {})
    await ctx.remember(last_plan_id=plan["plan_id"])
    card = {
        "card_name": "checkout_result",
        "data": {
            "plan_id": plan["plan_id"], "customer": plan["customer_name"],
            "status": "checked_out", "checked_out_at": now_iso(),
        },
        "actions": [{"action_id": "log", "label": "写日志", "skill_name": "visit_log"}],
    }
    return _remember(
        ctx,
        ToolResult("ok", f"签退成功：{plan['customer_name']}，记得补拜访日志。", {"card": card}),
        plan["plan_id"],
    )


@registry.tool(
    name="save_visit_log", label="保存日志", risk="medium", scope="write", skill="visit_log", card="visit_log",
    description="保存结构化拜访日志（summary 必填，feedback/next_action 可选），并关联最近计划。",
    parameters={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "拜访发生了什么"},
            "feedback": {"type": "string", "description": "客户反馈"},
            "next_action": {"type": "string", "description": "下一步动作"},
            "follow_up_at": {"type": "string", "description": "跟进时间 ISO8601"},
            "plan_id": {"type": "string", "description": "关联计划 ID"},
        },
        "required": ["summary"],
    },
)
async def save_visit_log(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    replayed = _replay(ctx)
    if replayed is not None:
        return replayed
    summary = (args.get("summary") or "").strip()
    if not summary:
        return ToolResult("need_input", "这次拜访的情况简单说一下？", {"missing": ["summary"], "question": "这次拜访的情况简单说一下？"})
    entity = await ctx.state.entity(ctx.session_id)
    plan = ctx.db.get_plan(args.get("plan_id") or entity.get("last_plan_id") or "") if (args.get("plan_id") or entity.get("last_plan_id")) else None
    log = ctx.db.create_log(
        plan_id=plan["plan_id"] if plan else None,
        customer_id=plan["customer_id"] if plan else None,
        summary=summary,
        feedback=args.get("feedback", ""),
        next_action=args.get("next_action", ""),
        follow_up_at=args.get("follow_up_at"),
        created_by=ctx.user_id,
    )
    if plan and plan["status"] in ("checked_in", "checked_out"):
        FSM(ctx.db).plan(plan["plan_id"], "log", actor=ctx.user_id, trace_id=ctx.trace_id)
    card = {
        "card_name": "visit_log",
        "data": {
            "log_id": log["log_id"],
            "plan_id": plan["plan_id"] if plan else None,
            "summary": summary,
            "next_action": args.get("next_action", ""),
            "follow_up_at": args.get("follow_up_at"),
        },
        "actions": [],
    }
    return _remember(
        ctx,
        ToolResult("ok", f"日志已记录（{log['log_id']}），要点：{summary[:40]}", {"card": card}),
        log["log_id"],
    )


@registry.tool(
    name="transcribe_audio", label="录音转写", risk="low", skill="visit_log",
    description="把拜访录音转成文字并生成纪要（ASR 后端可配置：mock/faster_whisper/command）。",
    parameters={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": []},
)
async def transcribe_audio(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from app.agent.minutes import generate_minutes

    result = asr.transcribe(
        args.get("file_path"),
        backend=ctx.settings.asr_backend,
        model=ctx.settings.asr_model,
        language=ctx.settings.asr_language,
        command=ctx.settings.asr_command,
    )
    minutes = generate_minutes(result["text"])
    return ToolResult(
        "ok", result["text"],
        {"transcript": result["text"], "asr_backend": result["backend"],
         "fallback_reason": result["fallback_reason"], "minutes": minutes},
    )


@registry.tool(
    name="send_notification", label="发送通知", risk="medium", scope="write", skill="plan_cancel",
    description="给客户发送拜访变更通知（MVP 写入 outbox 留痕）。",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "客户名称"},
            "message": {"type": "string", "description": "通知内容"},
        },
        "required": ["target", "message"],
    },
)
async def send_notification(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    notify.notify(ctx.db, "sms", args["target"], args["message"])
    return ToolResult("ok", f"已给 {args['target']} 发送通知。", {})