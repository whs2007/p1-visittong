"""登录 / 修改密码：PBKDF2 校验 + HMAC Token 签发 + 登录失败锁定。"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_ctx, get_current_user
from app.auth import create_token, hash_password, verify_password
from app.context import AppContext
from app.errors import ApiError

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordBody(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


def _throttle_key(request: Request, user_id: str) -> str:
    host = request.client.host if request.client else "-"
    return f"{user_id}:{host}"


@router.post("/login")
async def login(body: LoginBody, request: Request, ctx: AppContext = Depends(get_ctx)):
    key = _throttle_key(request, body.user_id)
    now = time.time()
    record = ctx.login_failures.get(key, {"count": 0.0, "until": 0.0})
    if float(record["until"]) > now:
        wait = int(float(record["until"]) - now) + 1
        raise ApiError(429, "TOO_MANY_ATTEMPTS", f"登录尝试过多，请 {wait} 秒后再试")

    user = ctx.db.get_user(body.user_id)
    if not user or not verify_password(body.password, user.get("password_hash")):
        record["count"] = float(record["count"]) + 1
        if record["count"] >= ctx.settings.login_max_attempts:
            record["count"] = 0.0
            record["until"] = now + ctx.settings.login_lock_seconds
        ctx.login_failures[key] = record
        # 不区分“用户不存在/密码错”，避免枚举
        raise ApiError(401, "LOGIN_FAILED", "用户名或密码错误")

    ctx.login_failures.pop(key, None)
    token, exp = create_token(user, ctx.settings.auth_secret, ctx.settings.auth_token_ttl_h)
    return {
        "token": token,
        "expires_at": exp,
        "user": {"user_id": user["user_id"], "name": user.get("name"), "role": user.get("role")},
    }


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    row = ctx.db.get_user(user["user_id"])
    if not row or not verify_password(body.old_password, row.get("password_hash")):
        raise ApiError(400, "INVALID_OLD_PASSWORD", "原密码不正确")
    if body.old_password == body.new_password:
        raise ApiError(400, "PASSWORD_UNCHANGED", "新密码不能与原密码相同")
    ctx.db.update_password(user["user_id"], hash_password(body.new_password))
    # 说明：已签发的 Token 在过期前仍有效（无状态 Token）；生产可缩短 TTL 或引入黑名单
    return {"ok": True, "user_id": user["user_id"]}


@router.get("/demo-accounts")
async def demo_accounts(ctx: AppContext = Depends(get_ctx)):
    """demo 模式下给出演示账号提示；严格模式不暴露。"""
    if ctx.settings.auth_mode != "demo":
        return {"accounts": []}
    return {
        "accounts": [
            {"user_id": "sales-001", "label": "销售 · 张伟", "password": ctx.settings.demo_password},
            {"user_id": "manager-001", "label": "主管 · 李敏", "password": ctx.settings.demo_password},
        ]
    }