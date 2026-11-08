"""知识检索与数据管道：语义搜索、同步、状态、事件箱。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

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


# --------------------------------------------------------------------------- #
# 增量同步链路（v0.9）：事件 → consumer → 版本守卫 → 重试 → DLQ
# --------------------------------------------------------------------------- #
def _sync(ctx: AppContext):
    if ctx.knowledge_sync is None:
        raise ApiError(503, "KNOWLEDGE_SYNC_DISABLED", "同步 consumer 未装配")
    return ctx.knowledge_sync


def _ensure_manager(user: dict[str, Any], action: str) -> None:
    if user.get("role") not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", f"只有主管可以{action}")


class KnowledgeEventBody(BaseModel):
    doc_id: str
    op: str = "upsert"          # upsert | delete
    version: int = 1
    kind: str = "customer"
    ref_id: str = ""
    title: str = ""
    text: str = ""


@router.post("/knowledge/events")
async def emit_event(
    body: KnowledgeEventBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """投递一条变更事件（等价于上游 binlog 打到 Kafka）。"""
    _ensure_manager(user, "投递知识变更事件")
    sync = _sync(ctx)
    if body.op not in ("upsert", "delete"):
        raise ApiError(400, "INVALID_OP", "op 只支持 upsert / delete")
    event_id = sync.emit(
        doc_id=body.doc_id, kind=body.kind, ref_id=body.ref_id,
        title=body.title, text=body.text, version=body.version, op=body.op,
    )
    return {"event_id": event_id, "doc_id": body.doc_id, "version": body.version, "op": body.op}


@router.post("/knowledge/sync/drain")
async def drain_events(
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """跑一次 consumer：返回 processed / stale / dead 的分解，而不是一个总数。"""
    _ensure_manager(user, "消费知识变更事件")
    sync = _sync(ctx)
    stats = sync.drain(limit=max(1, min(limit, 500)))
    ctx.db.add_audit(
        actor=user["user_id"], action="knowledge.drain", target_type="pipeline",
        target_id="-", after=stats,
    )
    return {"stats": stats, "state": sync.stats()}


@router.get("/knowledge/sync/status")
async def sync_status(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    _ensure_manager(user, "查看同步状态")
    sync = _sync(ctx)
    return {
        "backend": ctx.knowledge.backend,
        "state": sync.stats(),
        "recent_events": ctx.events.recent(10),
    }


@router.get("/knowledge/dlq")
async def list_dlq(
    limit: int = 50,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    _ensure_manager(user, "查看死信队列")
    return {"dead_letters": _sync(ctx).list_dlq(limit=max(1, min(limit, 200)))}


@router.post("/knowledge/dlq/{dlq_id}/replay")
async def replay_dlq(
    dlq_id: int,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """死信重放：重新入队 → 再走一遍完整链路（不做「直接写库」的捷径）。"""
    _ensure_manager(user, "重放死信")
    sync = _sync(ctx)
    result = sync.replay(dlq_id)
    if result["result"] == "not_found":
        raise ApiError(404, "DLQ_NOT_FOUND", "死信不存在")
    if result["result"] == "requeued":
        result["stats"] = sync.drain()
    ctx.db.add_audit(
        actor=user["user_id"], action="knowledge.dlq.replay", target_type="dlq",
        target_id=str(dlq_id), after={"result": result.get("result")},
    )
    return result