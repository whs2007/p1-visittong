"""拜访业务 REST：待办/计划/用户/健康检查/Skill 目录/工具启停（鉴权版）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_ctx, get_current_user
from app.auth import ensure_owner_or_manager
from app.context import AppContext
from app.domain.fsm import FSM
from app.errors import ApiError, not_found
from app.skills.visit_skills import skill_catalog

router = APIRouter(prefix="/api", tags=["visit"])


@router.get("/health")
async def health(ctx: AppContext = Depends(get_ctx)):
    """探活：进程 + 存储可用性（不暴露敏感信息）。"""
    checks = {"db": "ok", "redis": ctx.state.backend}
    try:
        ctx.db.get_user("sales-001")
    except Exception:  # noqa: BLE001
        checks["db"] = "error"
    return {
        "status": "ok" if checks["db"] == "ok" else "degraded",
        "llm_mode": ctx.llm.name,
        "redis": ctx.state.backend,
        "redis_reason": getattr(ctx.state, "backend_reason", ""),
        "db": "sqlite",
        "auth_mode": ctx.settings.auth_mode,
        "checks": checks,
        "version": ctx.settings.version,
    }


@router.get("/config")
async def config(ctx: AppContext = Depends(get_ctx)):
    return {
        "app": ctx.settings.app_name,
        "llm_mode": ctx.llm.name,
        "model": ctx.settings.llm_model,
        "redis": ctx.state.backend,
        "auth_mode": ctx.settings.auth_mode,
        "version": ctx.settings.version,
    }


@router.get("/me")
async def me(user: dict[str, Any] = Depends(get_current_user)):
    return user


@router.get("/todos")
async def todos(
    date: str = "today",
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    from datetime import date as date_cls

    day = date_cls.today().isoformat() if date in ("today", "", None) else date
    return {"day": day, "todos": ctx.db.list_todos(user["user_id"], day)}


@router.get("/plans")
async def plans(
    status: str | None = None,
    date: str | None = None,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    day = None if date in (None, "", "all") else ("today" if date == "today" else date)
    if day == "today":
        from datetime import date as date_cls

        day = date_cls.today().isoformat()
    return {"plans": ctx.db.list_plans(user["user_id"], status=status, day=day)}


@router.get("/plans/{plan_id}")
async def plan_detail(
    plan_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    plan = ctx.db.get_plan(plan_id)
    if not plan:
        raise not_found("PLAN_NOT_FOUND", "计划不存在")
    ensure_owner_or_manager(user, plan.get("owner_id", ""))
    checkins = ctx.db.list_checkins(plan_id)
    changes = ctx.db._rows(  # noqa: SLF001
        "SELECT * FROM plan_change_log WHERE plan_id=? ORDER BY changed_at DESC", (plan_id,)
    )
    transitions = ctx.db.list_state_transitions("plan", plan_id)
    return {"plan": plan, "checkins": checkins, "changes": changes, "transitions": transitions}


class PlanCreateBody(BaseModel):
    customer_id: str | None = None
    customer: str | None = None
    planned_at: str
    topic: str = "客户拜访"
    store_id: str | None = None


class PlanUpdateBody(BaseModel):
    planned_at: str | None = None
    topic: str | None = None
    store_id: str | None = None


def _resolve_manual_customer(ctx: AppContext, body: PlanCreateBody) -> dict[str, Any]:
    customer = None
    if body.customer_id:
        customer = ctx.db.get_customer(body.customer_id)
    elif body.customer:
        hits = ctx.db.search_customers(body.customer)
        customer = hits[0] if len(hits) == 1 else None
    if customer is None:
        raise ApiError(400, "CUSTOMER_NOT_FOUND", "请提供唯一的 customer_id 或客户全称")
    return customer


@router.post("/plans")
async def create_plan_manual(
    body: PlanCreateBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """手工创建计划（模型不可用时的原生表单降级通道，同样走 FSM + 审计）。"""
    customer = _resolve_manual_customer(ctx, body)
    if not body.planned_at:
        raise ApiError(400, "INVALID_PLAN", "planned_at 必填")
    plan = ctx.db.create_plan(
        customer_id=customer["customer_id"], store_id=body.store_id,
        owner_id=user["user_id"], planned_at=body.planned_at,
        topic=body.topic or "客户拜访",
    )
    FSM(ctx.db).create(plan["plan_id"], actor=user["user_id"])
    ctx.db.add_audit(
        actor=user["user_id"], action="plan.create.manual",
        target_type="plan", target_id=plan["plan_id"],
        after={"planned_at": plan["planned_at"], "topic": plan["topic"]},
    )
    return {"plan": plan}


@router.patch("/plans/{plan_id}")
async def update_plan_manual(
    plan_id: str,
    body: PlanUpdateBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    plan = ctx.db.get_plan(plan_id)
    if not plan:
        raise not_found("PLAN_NOT_FOUND", "计划不存在")
    ensure_owner_or_manager(user, plan.get("owner_id", ""))
    allowed = FSM(ctx.db).can(plan_id, "update")
    if not allowed.ok:
        raise ApiError(409, "PLAN_STATE_INVALID", allowed.reason)
    changes = {k: v for k, v in body.model_dump().items() if v not in (None, "")}
    if not changes:
        raise ApiError(400, "INVALID_PLAN", "没有可修改字段")
    updated = ctx.db.update_plan(plan_id, changes, user["user_id"])
    FSM(ctx.db).plan(plan_id, "update", actor=user["user_id"])
    ctx.db.add_audit(
        actor=user["user_id"], action="plan.update.manual",
        target_type="plan", target_id=plan_id,
        before={k: plan.get(k) for k in changes}, after=changes,
    )
    return {"plan": updated}


@router.post("/plans/{plan_id}/cancel")
async def cancel_plan_manual(
    plan_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    plan = ctx.db.get_plan(plan_id)
    if not plan:
        raise not_found("PLAN_NOT_FOUND", "计划不存在")
    ensure_owner_or_manager(user, plan.get("owner_id", ""))
    result = FSM(ctx.db).plan(plan_id, "cancel", actor=user["user_id"])
    if not result.ok:
        raise ApiError(409, "PLAN_STATE_INVALID", result.reason)
    ctx.db.add_audit(
        actor=user["user_id"], action="plan.cancel.manual",
        target_type="plan", target_id=plan_id,
        before={"status": plan["status"]}, after={"status": "cancelled"},
    )
    return {"plan": ctx.db.get_plan(plan_id)}


@router.get("/skills")
async def skills(ctx: AppContext = Depends(get_ctx), user: dict[str, Any] = Depends(get_current_user)):
    return {"skills": skill_catalog(), "tools": ctx.registry.catalog()}


class ToolToggle(BaseModel):
    enabled: bool


@router.patch("/tools/{name}")
async def toggle_tool(
    name: str,
    body: ToolToggle,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以启停工具")
    spec = ctx.registry.set_enabled(name, body.enabled)
    if not spec:
        raise not_found("TOOL_NOT_FOUND", f"工具不存在：{name}")
    return {"name": name, "enabled": spec.enabled}