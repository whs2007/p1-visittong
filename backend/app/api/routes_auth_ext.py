"""登出 / 强制下线：Token 可撤销的最小闭环。

为什么单独一个文件而不是改 `routes_auth.py`：登录流程与本模块没有共享状态，
拆开可以避免动已验证过的登录逻辑；两个 router 都挂在 /api/auth 下，路径不冲突。

语义：
- `POST /api/auth/logout`：撤销**当前** token（按 jti 写入 revoked_tokens）。
- `POST /api/auth/revoke-all`：把用户 token 版本号 +1，该用户此前签发的所有
  token 一起失效（改密、设备丢失、怀疑泄露时用）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_ctx, get_current_user
from app.auth import revoke_token
from app.context import AppContext
from app.errors import ApiError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/logout")
async def logout(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    jti = getattr(request.state, "token_jti", None)
    if not jti:
        # demo 模式（X-User-Id）没有 token 可撤
        return {"ok": True, "revoked": False, "mode": "demo"}
    revoke_token(
        ctx.db, jti=str(jti), user_id=user["user_id"],
        expires_at=str(getattr(request.state, "token_exp", "")),
    )
    if getattr(ctx, "governance", None) is not None:
        ctx.governance.record(
            action="auth.logout", actor=user["user_id"],
            actor_role=str(user.get("role") or ""),
            target_type="token", target_id=str(jti),
            reason="user logout",
        )
    return {"ok": True, "revoked": True}


@router.post("/revoke-all")
async def revoke_all(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """把 token_epoch +1：该用户所有已签发 token 立即失效。"""
    try:
        ctx.db._exec(  # noqa: SLF001
            "UPDATE users SET token_epoch = COALESCE(token_epoch, 0) + 1 WHERE user_id=?",
            (user["user_id"],),
        )
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "TOKEN_EPOCH_UNAVAILABLE", f"当前库不支持 token 版本化：{exc}") from exc
    if getattr(ctx, "governance", None) is not None:
        ctx.governance.record(
            action="auth.revoke_all", actor=user["user_id"],
            actor_role=str(user.get("role") or ""),
            target_type="user", target_id=user["user_id"],
            reason="revoke all sessions",
        )
    return {"ok": True}
