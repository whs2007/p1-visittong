"""会话、对话流（SSE）、卡片动作、审批（鉴权版）。"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_ctx, get_current_user
from app.api.sse import SSE_HEADERS, sse
from app.auth import ensure_can_decide, ensure_owner_or_manager
from app.context import AppContext
from app.errors import ApiError, conflict, not_found

router = APIRouter(prefix="/api", tags=["chat"])


class CreateSessionBody(BaseModel):
    title: str = "新会话"


class ChatBody(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=2000)


class CardActionBody(BaseModel):
    session_id: str
    card_name: str
    action_id: str
    skill_name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalBody(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = ""


@router.post("/sessions")
async def create_session(
    body: CreateSessionBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ctx.state.create(user["user_id"], body.title)


@router.get("/sessions")
async def list_sessions(
    limit: int = 20,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    sessions = await ctx.state.list_sessions(user["user_id"], limit)
    for item in sessions:
        try:
            item["message_count"] = ctx.tree.count(item["session_id"])
        except Exception:  # noqa: BLE001 - 统计失败不影响列表
            item["message_count"] = item.get("message_count", 0)
    return {"sessions": sessions}


async def _require_session(ctx: AppContext, session_id: str, user: dict[str, Any]) -> dict[str, Any]:
    meta = await ctx.state.meta(session_id)
    if not meta:
        raise not_found("SESSION_NOT_FOUND", "会话不存在或已过期")
    ensure_owner_or_manager(user, meta.get("user_id", ""))
    return meta


@router.get("/sessions/{session_id}/messages")
async def session_messages(
    session_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _require_session(ctx, session_id, user)
    entries = []
    for item in ctx.tree.recent_entries(session_id, ctx.settings.context_limit):
        if item["role"] == "system":
            continue
        entries.append(
            {
                "entry_id": item["entry_id"],
                "parent_id": item["parent_id"],
                "role": item["role"],
                "content": item["content"],
                "card": item.get("card"),
                "ts": item["created_at"],
            }
        )
    return {"session_id": session_id, "leaf_id": ctx.tree.leaf(session_id), "messages": entries}


@router.get("/sessions/{session_id}/state")
async def session_state(
    session_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """会话 FSM 当前状态 + 最近迁移（配合 /messages 做端到端状态可视化）。"""
    meta = await _require_session(ctx, session_id, user)
    return {
        "session_id": session_id,
        "fsm_state": meta.get("fsm_state") or "idle",
        "status": meta.get("status"),
        "transitions": ctx.db.list_state_transitions("session", session_id),
    }


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """导出会话条目为 JSONL（对齐 Pi 的会话文件格式，便于回放/排障）。"""
    await _require_session(ctx, session_id, user)
    return PlainTextResponse(
        ctx.tree.export_jsonl(session_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.jsonl"'},
    )


async def _prepare(ctx: AppContext, session_id: str, user: dict[str, Any]) -> None:
    await _require_session(ctx, session_id, user)
    if not await ctx.state.try_lock(session_id):
        raise conflict("SESSION_BUSY", "同一会话有正在执行的 Turn，请稍后再试")
    await ctx.state.update(session_id, status="running")


def _stream(ctx: AppContext, **kwargs: Any) -> StreamingResponse:
    async def gen():
        try:
            async for item in ctx.orchestrator.handle(**kwargs):
                yield sse(item["event"], item["data"])
        except Exception as exc:  # noqa: BLE001
            yield sse("error", {"message": str(exc), "code": "INTERNAL_ERROR"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat/stream")
async def chat_stream(
    body: ChatBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    # 请求级幂等：同一 Idempotency-Key 只允许触发一次 Turn（防重复提交/重复写库）
    if idempotency_key:
        if not ctx.db.reserve_idempotency(idempotency_key, user["user_id"], body.session_id):
            raise conflict("DUPLICATE_REQUEST", "重复请求：该 Idempotency-Key 已处理")
    await _prepare(ctx, body.session_id, user)
    return _stream(ctx, session_id=body.session_id, user_id=user["user_id"], message=body.message)


@router.post("/cards/action")
async def card_action(
    body: CardActionBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _prepare(ctx, body.session_id, user)
    card = {
        "card_name": body.card_name,
        "action_id": body.action_id,
        "skill_name": body.skill_name,
        "payload": body.payload,
    }
    return _stream(ctx, session_id=body.session_id, user_id=user["user_id"], card=card)


@router.get("/tasks/{turn_id}")
async def task_detail(
    turn_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Durable Execution：查询任务状态与最近检查点。"""
    task = ctx.tasks.get(turn_id)
    if not task:
        raise not_found("TASK_NOT_FOUND", "任务不存在")
    ensure_owner_or_manager(user, task["user_id"])
    checkpoint = ctx.tasks.latest_checkpoint(turn_id)
    return {
        "task": task,
        "checkpoint": None if not checkpoint else {
            "step": checkpoint["step"], "skill": checkpoint["skill"],
            "created_at": checkpoint["created_at"],
        },
        "tool_executions": ctx.tasks.tool_count(turn_id),
        "resumable": task["status"] == "interrupted" and checkpoint is not None,
    }


@router.post("/tasks/{turn_id}/resume")
async def resume_task(
    turn_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """从最后检查点续跑被中断的 Turn（SSE 流，与原对话同一 session）。"""
    task = ctx.tasks.get(turn_id)
    if not task:
        raise not_found("TASK_NOT_FOUND", "任务不存在")
    ensure_owner_or_manager(user, task["user_id"])
    if task["status"] != "interrupted":
        raise conflict("TASK_NOT_INTERRUPTED", f"任务状态是 {task['status']}，只有 interrupted 可恢复")
    checkpoint = ctx.tasks.latest_checkpoint(turn_id)
    if not checkpoint:
        raise conflict("TASK_NO_CHECKPOINT", "没有可用检查点，无法恢复")
    if not await ctx.state.try_lock(task["session_id"]):
        raise conflict("SESSION_BUSY", "同一会话有正在执行的 Turn，请稍后再试")
    await ctx.state.update(task["session_id"], status="running")
    return _stream(
        ctx, session_id=task["session_id"], user_id=user["user_id"],
        message=f"[resume] {task.get('user_input') or ''}", turn_id=turn_id,
        resume={**checkpoint, "resume_count": task.get("resume_count") or 0},
    )


@router.get("/approvals")
async def list_approvals(
    status: str = "pending",
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以查看审批列表")
    if status == "pending":
        return {"approvals": await ctx.traces.pending_approvals()}
    return {"approvals": []}


@router.post("/approvals/{approval_id}")
async def decide_approval(
    approval_id: str,
    body: ApprovalBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    approval = await ctx.traces.get_approval(approval_id)
    if not approval:
        raise not_found("APPROVAL_NOT_FOUND", "审批单不存在")
    ensure_can_decide(user, approval)
    result = await ctx.approvals.decide(
        approval_id, body.decision, body.reason, approver=user["user_id"]
    )
    if result == "not_found":
        raise not_found("APPROVAL_NOT_FOUND", "审批单不存在")
    if result == "already_decided":
        raise conflict("APPROVAL_ALREADY_DECIDED", "该审批单已处理，请刷新")
    ctx.db.add_audit(
        actor=user["user_id"], action=f"approval.{body.decision}",
        target_type="approval", target_id=approval_id,
        after={"decision": body.decision, "reason": body.reason or ""},
    )
    return {"approval_id": approval_id, "decision": body.decision}