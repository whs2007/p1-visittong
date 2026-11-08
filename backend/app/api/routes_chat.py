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
from app.errors import ApiError, bad_request, conflict, not_found

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
    nonce: str | None = None      # 一次性决策令牌（`GET /api/approvals` 下发，用后即废）


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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """卡片动作（点击「改约 / 签到 / 取消」等按钮）。

    卡片动作**可能是写操作**，所以与 `/chat/stream` 一样支持 `Idempotency-Key`：
    前端双击、网络重试、移动端弱网重发都不应该产生第二个 Turn。
    """
    if idempotency_key:
        if not ctx.db.reserve_idempotency(idempotency_key, user["user_id"], body.session_id):
            raise conflict("DUPLICATE_REQUEST", "重复请求：该 Idempotency-Key 已处理")
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
            "agent_name": checkpoint.get("agent_name"),
            "capability_fingerprint": checkpoint.get("capability_fingerprint"),
            "capability": checkpoint.get("capability") or {},
        },
        # 把「为什么不能续跑」变成可查询的数据，而不只是 409 里的一句提示
        "resume_compat": ctx.orchestrator.check_resume_compat(checkpoint or {}),
        "tool_executions": ctx.tasks.tool_count(turn_id),
        # 审批断点：重启后被中断的任务仍然挂着「在等哪张单」，可据此接回
        "pending_approval": (
            None if not task.get("pending_approval_id") else {
                "approval_id": task["pending_approval_id"],
                "step": task.get("pending_step"),
                "call_id": task.get("pending_call_id"),
                "tool": task.get("pending_tool"),
            }
        ),
        "resumable": (
            task["status"] in ("interrupted", "awaiting_approval")
            and checkpoint is not None
            and ctx.orchestrator.check_resume_compat(checkpoint)["status"] != "incompatible"
        ),
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
    if task["status"] not in ("interrupted", "awaiting_approval"):
        raise conflict("TASK_NOT_INTERRUPTED", f"任务状态是 {task['status']}，只有 interrupted/awaiting_approval 可恢复")
    checkpoint = ctx.tasks.latest_checkpoint(turn_id)
    if not checkpoint:
        raise conflict("TASK_NO_CHECKPOINT", "没有可用检查点，无法恢复")
    compat = ctx.orchestrator.check_resume_compat(checkpoint)
    if compat["status"] == "incompatible":
        # 放在 try_lock 之前：拒绝时不占用会话锁，任务仍为 interrupted，
        # 恢复原装配后可原样重试，无需重建任务。
        raise conflict(
            "AGENT_CAPABILITY_CHANGED",
            compat["reason"] + f" 详见 GET /api/tasks/{turn_id} 的 resume_compat。",
        )
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
        approval_id, body.decision, body.reason, approver=user["user_id"], nonce=body.nonce,
    )
    if result == "not_found":
        raise not_found("APPROVAL_NOT_FOUND", "审批单不存在")
    if result == "token_missing":
        raise bad_request("APPROVAL_TOKEN_REQUIRED", "缺少一次性决策令牌（decision_nonce）")
    if result == "token_invalid":
        raise ApiError(403, "APPROVAL_TOKEN_INVALID", "决策令牌与审批单不匹配")
    if result == "already_decided":
        # 同一张单的第二次提交：可能是重复点击，也可能是重放攻击。
        # 两种都只能拒——但不能说「令牌已用过」而泄露「这张单刚才被谁批了」。
        raise conflict("APPROVAL_ALREADY_DECIDED", "该审批单已处理或令牌已失效，请刷新")
    ctx.db.add_audit(
        actor=user["user_id"], action=f"approval.{body.decision}",
        target_type="approval", target_id=approval_id,
        after={"decision": body.decision, "reason": body.reason or ""},
    )
    if getattr(ctx, "governance", None) is not None:
        ctx.governance.record(
            action=f"approval.{body.decision}",
            actor=user["user_id"], actor_role=str(user.get("role") or ""),
            target_type="approval", target_id=approval_id,
            before={"status": "pending", "tool": approval.get("tool"),
                    "requester": approval.get("requester")},
            after={"status": body.decision},
            reason=body.reason or "",
            trace_id=str(approval.get("trace_id") or ""),
        )
    return {"approval_id": approval_id, "decision": body.decision}

# --------------------------------------------------------------------------- #
# 会话管理（对齐 Pi 的会话树：分支 / 上下文修订 / 会话文件恢复）
# --------------------------------------------------------------------------- #
class ForkBody(BaseModel):
    from_entry_id: str | None = None    # 缺省 = 当前 leaf（等价于「从最新一条分叉」）
    note: str = ""


class ContextEditBody(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    exclude_entry_ids: list[str] = Field(default_factory=list)


class ImportBody(BaseModel):
    jsonl: str = Field(min_length=1)


@router.post("/sessions/{session_id}/fork")
async def fork_session(
    session_id: str,
    body: ForkBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """从历史条目分叉；被放弃的分支自动留一条 branch_summary。

    为什么要摘要：切换分支后，模型看到的是新分支的上下文。如果不留摘要，
    「刚才在旧分支上确认过的事」会彻底消失，用户会觉得助手失忆——这正是 Pi
    用 branch_summary 解决的问题。
    """
    await _require_session(ctx, session_id, user)
    from_entry = body.from_entry_id or ctx.tree.leaf(session_id)
    if not from_entry:
        raise not_found("SESSION_EMPTY", "会话没有可分支的历史条目")

    abandoned = ctx.tree.abandoned_path(session_id, from_entry)
    summary = ""
    if abandoned:
        from app.agent.compaction import digest_entries

        summary = digest_entries(abandoned)
        if body.note:
            summary = f"{summary}\n\n## 用户说明\n- {body.note}"

    info = ctx.tree.fork(session_id, from_entry, summary_text=summary or None)
    if info is None:
        raise not_found("ENTRY_NOT_FOUND", "分叉点不在该会话中")
    ctx.db.add_audit(
        actor=user["user_id"], action="session.fork", target_type="session", target_id=session_id,
        after={"branch_from": from_entry, "abandoned": len(abandoned), "note": body.note},
    )
    return {
        "session_id": session_id,
        "leaf_id": info["leaf_id"],
        "branch_from": from_entry,
        "branch_summary_id": info.get("branch_summary_id"),
        "abandoned_entries": len(abandoned),
    }


@router.post("/sessions/{session_id}/context-edit")
async def edit_context(
    session_id: str,
    body: ContextEditBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """上下文修订：把某些条目「移出模型上下文」，但保留在会话文件里。

    典型场景：用户说「刚才那句价格是错的，别当真」。直接删条目会破坏可追溯性；
    标记排除则既能纠正上下文，又能事后回答「它当时确实这么说过」。
    """
    await _require_session(ctx, session_id, user)
    info = ctx.tree.context_edit(
        session_id, note=body.note, exclude_entry_ids=body.exclude_entry_ids,
    )
    if info is None:
        raise not_found("SESSION_EMPTY", "会话没有可修订的上下文")
    ctx.db.add_audit(
        actor=user["user_id"], action="session.context_edit",
        target_type="session", target_id=session_id,
        after={"entry_id": info["entry_id"], "excluded": len(info["excluded"]), "note": info["note"]},
    )
    return {"session_id": session_id, **info}


@router.post("/sessions/{session_id}/import")
async def import_session(
    session_id: str,
    body: ImportBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """把导出的 JSONL 恢复到当前会话（Pi 的会话文件可跨环境继续）。"""
    await _require_session(ctx, session_id, user)
    try:
        info = ctx.tree.import_jsonl(session_id, body.jsonl)
    except ValueError as exc:
        raise ApiError(400, "SESSION_IMPORT_INVALID", str(exc)) from exc
    ctx.db.add_audit(
        actor=user["user_id"], action="session.import", target_type="session",
        target_id=session_id, after={"imported": info["imported"]},
    )
    return {"session_id": session_id, **info}
