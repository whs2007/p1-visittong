"""Runtime 装配查询：把「当前 Agent 能做什么」变成可观测的接口。

对齐 Pi 的 ResourceLoader / AgentSession 语义：能力集是装配期产物，
可以通过接口读取（渐进披露）与热更新。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_ctx, get_current_user
from app.auth import ensure_manager
from app.context import AppContext
from app.errors import ApiError
from app.runtime import ResourceLoader

router = APIRouter(prefix="/api", tags=["runtime"])


def _loader(ctx: AppContext) -> ResourceLoader:
    if ctx.resource_loader is None:
        raise ApiError(503, "RUNTIME_NOT_ASSEMBLED", "Runtime 未显式装配")
    return ctx.resource_loader


@router.get("/agents")
async def list_agents(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """可用 Agent 索引（只含 name/description/path，不展开技能正文）。"""
    loader = _loader(ctx)
    return {
        "active": ctx.orchestrator.session.describe(),
        "agents": loader.catalog(),
    }


@router.get("/agents/{name}/skills")
async def agent_skills(
    name: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """按需展开某个 Agent 的技能索引（渐进式披露）。"""
    loader = _loader(ctx)
    try:
        session = loader.create_session(name)
    except KeyError as exc:
        raise ApiError(404, "AGENT_NOT_FOUND", str(exc)) from exc
    return {"agent": session.describe(), "skill_index": session.skills_index()}


@router.post("/agents/reload")
async def reload_agents(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """热更新配置并重建当前 Agent 的能力视图（主管/管理员）。"""
    ensure_manager(user, action="热更新 Agent 能力装配", hint="")
    loader = _loader(ctx)
    names = loader.reload()
    active = ctx.orchestrator.session.name
    session = loader.create_session(active if active in names else "visit")
    ctx.orchestrator.set_session(session)
    return {"agents": names, "active": session.describe()}
