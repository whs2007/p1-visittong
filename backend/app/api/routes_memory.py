"""记忆接口：写入、查看、撤销，以及「记忆到底从哪来」的溯源查询。

写记忆是要被审计的动作，不是随手一次 POST：
- `project` / `agent` 作用域只有主管能写（那是所有会话的上下文）；
- `user` 作用域只能写自己（主管也不能替别人「记错事」）；
- 内容要走投毒门禁（元指令、伪造角色行、特殊 token、超长）。

`GET /api/memory/layers` 是这套设计的「可举证」部分：它返回**每一层文件、每个
小节、谁覆盖了谁**，面试时可以直接打开页面证明「模型看到的那句约定来自哪个文件」。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_ctx, get_current_user
from app.auth import ensure_manager, is_manager
from app.config import PROJECT_DIR
from app.context import AppContext
from app.errors import ApiError, bad_request, not_found
from app.memory.guard import MemoryRejected, check_content, check_scope
from app.memory.layers import load_layers, render_layers
from app.memory.store import MemoryStore

router = APIRouter(prefix="/api", tags=["memory"])


class MemoryCreateBody(BaseModel):
    content: str
    scope: str = "user"
    kind: str = "fact"
    owner_id: str | None = None      # agent 作用域填 Agent 名；user 作用域忽略（强制自己）
    tags: list[str] | None = None
    source_session_id: str | None = None
    source_entry_id: str | None = None


def _store(ctx: AppContext) -> MemoryStore:
    store = getattr(ctx, "memories", None)
    if store is None:
        raise ApiError(503, "MEMORY_DISABLED", "记忆子系统未装配")
    return store


def _record(ctx: AppContext, *, user: dict[str, Any], action: str, target_id: str, after: dict[str, Any]) -> None:
    governance = getattr(ctx, "governance", None)
    if governance is not None:
        governance.record(
            actor=user.get("user_id", ""), actor_role=user.get("role", ""),
            action=action, target_type="memory", target_id=target_id,
            before={}, after=after,
        )


@router.post("/memories")
async def create_memory(
    body: MemoryCreateBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    store = _store(ctx)
    actor = str(user.get("user_id") or "")
    role = str(user.get("role") or "")
    owner_id = body.owner_id or (ctx.session.name if body.scope == "agent" else actor)

    try:
        check_scope(
            scope=body.scope, owner_id=owner_id, actor=actor, actor_role=role,
            is_manager=is_manager(user),
        )
        content = check_content(body.content, max_chars=ctx.settings.memory_entry_max_chars)
    except MemoryRejected as exc:
        _record(
            ctx, user=user, action="memory.reject", target_id="-",
            after={"reason": exc.reason, "scope": body.scope, "chars": len(body.content or "")},
        )
        raise bad_request(
            "MEMORY_REJECTED",
            f"[{exc.reason}] {exc.detail or exc.reason}",
        ) from exc

    item = store.add(
        scope=body.scope, owner_id=owner_id, content=content, kind=body.kind,
        tags=body.tags, source_session_id=body.source_session_id,
        source_entry_id=body.source_entry_id, created_by=actor,
    )
    _record(
        ctx, user=user, action="memory.create", target_id=item["memory_id"],
        after={
            "scope": item["scope"], "owner_id": owner_id, "kind": item["kind"],
            "chars": len(content), "source_session_id": body.source_session_id,
        },
    )
    return {"memory": item}


@router.get("/memories")
async def list_memories(
    scope: str | None = None,
    include_revoked: bool = False,
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    store = _store(ctx)
    if is_manager(user):
        items = store.list(scope=scope, include_revoked=include_revoked, limit=limit)
    else:
        # 销售只看「对自己有效」的记忆：项目级 + 当前 Agent + 自己
        items = store.candidates(user_id=str(user.get("user_id") or ""), agent_name=ctx.session.name)
        if scope:
            items = [item for item in items if item["scope"] == scope]
    return {
        "memories": items,
        "count": len(items),
        "scope": "all" if is_manager(user) else "self",
    }


@router.post("/memories/{memory_id}/revoke")
async def revoke_memory(
    memory_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    store = _store(ctx)
    item = store.get(memory_id)
    if item is None:
        raise not_found("MEMORY_NOT_FOUND", "记忆不存在")
    actor = str(user.get("user_id") or "")
    # user 作用域：本人即可撤销；project / agent 作用域：仅主管
    if item["scope"] == "user" and item["owner_id"] != actor and not is_manager(user):
        raise not_found("MEMORY_NOT_FOUND", "记忆不存在")     # 不泄露他人记忆的存在性
    if item["scope"] in ("project", "agent"):
        ensure_manager(user, action="撤销项目级记忆", hint="项目级/Agent 级记忆影响所有人")
    if not store.revoke(memory_id, actor=actor):
        raise bad_request("MEMORY_ALREADY_REVOKED", "记忆已是撤销状态")
    _record(
        ctx, user=user, action="memory.revoke", target_id=memory_id,
        after={"scope": item["scope"], "owner_id": item["owner_id"]},
    )
    return {"memory_id": memory_id, "status": "revoked"}


@router.get("/memory/layers")
async def memory_layers(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """静态记忆层溯源：每条约定来自哪个文件、被谁覆盖。"""
    layers = load_layers(
        PROJECT_DIR, ctx.session.name,
        memory_dir=ctx.settings.memory_dir,
        max_chars=ctx.settings.memory_layer_max_chars,
    )
    return {
        "agent": ctx.session.name,
        "enabled": ctx.settings.enable_memory,
        "layers": [
            {
                "path": layer.path,
                "scope": layer.scope,
                "sections": [{"title": title, "chars": len(body)} for title, body in layer.sections],
            }
            for layer in layers
        ],
        "rendered": render_layers(layers),
    }
