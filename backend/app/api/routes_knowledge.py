"""知识检索与数据管道：语义搜索、同步、状态、事件箱。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.adapters.knowledge import sync_knowledge
from app.api.deps import get_ctx, get_current_user
from app.context import AppContext
from app.errors import ApiError

router = APIRouter(prefix="/api", tags=["knowledge"])


@router.get("/knowledge/search")
async def search(
    q: str,
    kind: str | None = None,
    top_k: int = 5,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not q.strip():
        raise ApiError(400, "INVALID_QUERY", "q 不能为空")
    top_k = max(1, min(top_k, 20))
    try:
        hits = ctx.knowledge.search(q, kind=kind, top_k=top_k)
    except RuntimeError as exc:
        raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", str(exc)) from exc
    return {
        "backend": ctx.knowledge.backend,
        "backend_reason": getattr(ctx.knowledge, "backend_reason", ""),
        "query": q,
        "results": hits,
    }


def _sync_handler(ctx: AppContext):
    def handler(event: dict[str, Any]) -> None:
        if event["topic"] == "hive2kafka.knowledge_sync":
            sync_knowledge(ctx.db, ctx.knowledge)

    return handler


@router.post("/knowledge/sync")
async def sync(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """触发一次「Hive 全量 -> Kafka -> consumer embedding」链路（主管）。"""
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以触发知识同步")
    if ctx.knowledge.backend != "local":
        raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", getattr(ctx.knowledge, "backend_reason", "Milvus 未接入"))
    event_id = ctx.events.emit("hive2kafka.knowledge_sync", {"mode": "full"})
    processed = ctx.events.drain(_sync_handler(ctx))
    ctx.db.add_audit(
        actor=user["user_id"], action="knowledge.sync", target_type="pipeline",
        target_id=event_id, after={"processed": processed, "backend": ctx.knowledge.backend},
    )
    return {
        "event_id": event_id, "processed": processed,
        "backend": ctx.knowledge.backend, "docs": ctx.knowledge.count(),
    }


@router.get("/knowledge/status")
async def status(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    return {
        "backend": ctx.knowledge.backend,
        "backend_reason": getattr(ctx.knowledge, "backend_reason", ""),
        "docs": ctx.knowledge.count(),
        "events": ctx.events.recent(5),
    }


@router.get("/knowledge/events")
async def events(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以查看管道事件")
    return {"events": ctx.events.recent(50)}