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
    """热更新配置并重建当前 Agent 的能力视图（主管/管理员）。

    两条纪律（v0.7 修正）：
    1. **只影响新 Turn**：在途 Turn 读的是 `handle()` 开始时快照的 AgentSession
       与能力指纹，reload 不会让它中途换权限；新 Turn 才会用新能力集。
    2. **必须留痕**：这是改「Agent 能做什么」的动作，写 governance_events，
       记录前后指纹，事后可举证「某次事故时线上是哪套能力集」。
    """
    ensure_manager(user, action="热更新 Agent 能力装配", hint="")
    loader = _loader(ctx)
    before = ctx.orchestrator.session.describe()
    names = loader.reload()
    active = ctx.orchestrator.session.name
    session = loader.create_session(active if active in names else "visit")
    ctx.orchestrator.set_session(session)
    # 双源修正：ctx.session 与 orchestrator.session 过去会不一致
    ctx.session = session
    after = session.describe()
    if getattr(ctx, "governance", None) is not None:
        ctx.governance.record(
            action="agent.reload",
            actor=user["user_id"], actor_role=str(user.get("role") or ""),
            target_type="agent", target_id=session.name,
            before={
                "capability_fingerprint": before.get("capability_fingerprint"),
                "tools": before.get("tools"),
            },
            after={
                "capability_fingerprint": after.get("capability_fingerprint"),
                "tools": after.get("tools"),
            },
            reason="hot reload agent resources",
        )
    return {"agents": names, "active": after}
