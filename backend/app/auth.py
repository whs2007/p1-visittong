"""鉴权：PBKDF2 密码校验 + HMAC 签名 Token + 会话归属/审批角色校验。

Token 格式：base64url(payload).base64url(hmac_sha256(secret, payload))
payload：{"sub": user_id, "role": role, "exp": epoch_seconds}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Header, Request

from app.errors import ApiError

PBKDF2_ITERATIONS = 120_000


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    expected = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(expected.hex(), digest_hex)


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_token(user: dict[str, Any], secret: str, ttl_hours: int) -> tuple[str, int]:
    """签发 Token。

    payload 带三类声明：
    - `sub` / `role`：身份与角色（角色仅用于展示，授权判定始终回查 DB）；
    - `exp`：过期时间；
    - `jti` + `tv`：**可撤销性**。`jti` 让「登出」立即失效单个 token（写入
      `revoked_tokens`）；`tv` 是用户级 token 版本号，改密/强制下线时 +1，
      该用户此前签发的所有 token 一起失效 —— 旧实现只有 HMAC + exp，
      签发后无法收回，属于审计里的「无撤销」缺口。
    """
    exp = int(time.time()) + ttl_hours * 3600
    payload = {
        "sub": user["user_id"],
        "role": user["role"],
        "exp": exp,
        "jti": secrets.token_urlsafe(12),
        "tv": int(user.get("token_epoch") or 0),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}", exp


def decode_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    if not payload.get("sub"):
        return None
    return payload


def _is_revoked(db: Any, jti: str, expires_at: str) -> bool:
    """token 是否已被撤销。表不存在（老库未迁移）时按未撤销处理。"""
    if not jti:
        return False
    try:
        conn = db._conn()  # noqa: SLF001
        row = conn.execute(
            "SELECT 1 FROM revoked_tokens WHERE jti=?", (jti,)
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001 - 迁移未执行 / 表不存在
        return False


def revoke_token(db: Any, *, jti: str, user_id: str, expires_at: str) -> None:
    try:
        conn = db._conn()  # noqa: SLF001
        conn.execute(
            "INSERT OR IGNORE INTO revoked_tokens (jti, user_id, expires_at, revoked_at)"
            " VALUES (?,?,?,?)",
            (jti, user_id, expires_at, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# FastAPI 依赖
# --------------------------------------------------------------------------- #
def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "name": user.get("name"),
        "role": user.get("role"),
        "region": user.get("region"),
    }


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict[str, Any]:
    """严格模式只认 Bearer Token；demo 模式额外允许 X-User-Id（仅供本地演示）。"""
    ctx = request.app.state.ctx
    settings = ctx.settings

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError(401, "UNAUTHORIZED", "Authorization 头格式应为 Bearer <token>")
        payload = decode_token(token, settings.auth_secret)
        if not payload:
            raise ApiError(401, "TOKEN_INVALID", "Token 无效或已过期，请重新登录")
        user = ctx.db.get_user(payload["sub"])
        if not user:
            raise ApiError(401, "UNAUTHORIZED", "用户不存在")
        # 1) 单 token 撤销（登出）
        if _is_revoked(ctx.db, str(payload.get("jti") or ""), str(payload.get("exp") or "")):
            raise ApiError(401, "TOKEN_REVOKED", "该 Token 已登出失效，请重新登录")
        # 2) 用户级版本号（改密 / 强制下线）
        current_tv = int(user.get("token_epoch") or 0)
        if int(payload.get("tv") or 0) != current_tv:
            raise ApiError(401, "TOKEN_STALE", "登录状态已失效（密码变更或强制下线），请重新登录")
        request.state.token_jti = payload.get("jti")
        request.state.token_exp = payload.get("exp")
        return _public_user(user)

    if settings.auth_mode == "demo":
        user_id = x_user_id or settings.demo_user
        user = ctx.db.get_user(user_id)
        if not user:
            raise ApiError(401, "UNAUTHORIZED", "用户不存在")
        return _public_user(user)

    raise ApiError(401, "UNAUTHORIZED", "缺少 Bearer Token，请先登录")


# --------------------------------------------------------------------------- #
# 授权
# --------------------------------------------------------------------------- #
def ensure_owner_or_manager(user: dict[str, Any], owner_id: str) -> None:
    if user["role"] not in ("manager", "admin") and user["user_id"] != owner_id:
        raise ApiError(403, "FORBIDDEN", "无权访问该会话")


def ensure_manager(
    user: dict[str, Any], action: str = "执行该操作", *, hint: str | None = None,
) -> None:
    """高权限通道：只允许 manager/admin。

    与 ensure_owner_or_manager 的区别是语义而非强度：后者是「数据归属」校验，
    前者是「治理豁免」校验。人工降级通道绕过了 Agent 的审批链，因此必须把
    权限收窄到主管——降级是可用性的兜底，不是权限的降级。

    v0.6 起，本轮新增的三个高权限端点（人工取消 / 豁免台账 / 能力热更新）
    统一走这个 helper，错误码与文案可预测；`hint` 让非审批类端点不套用
    「销售请通过对话发起」这句不适用提示。v0.4 遗留端点仍有内联角色判断，
    尚未迁移，属已知工程债（见 docs/08）。
    """
    if user["role"] not in ("manager", "admin"):
        suffix = hint if hint is not None else "；销售请通过对话发起，由主管审批"
        raise ApiError(403, "MANAGER_ONLY", f"只有主管可以{action}{suffix}")


def is_manager(user: dict[str, Any]) -> bool:
    return user.get("role") in ("manager", "admin")


def can_view_trace(user: dict[str, Any], owner_id: str | None) -> bool:
    """Trace 的可见性：本人或主管。

    与 `ensure_owner_or_manager` 的区别在**拒绝时的语义**：Trace 明细里含
    输入原文、工具参数、审批信息，越权读取应当返回 404（不泄露「这条存在」），
    而不是 403（等于告诉对方「有这个东西但你不能看」）。所以这里只做布尔
    判定，由路由决定返回什么码。
    """
    return is_manager(user) or (owner_id is not None and user["user_id"] == owner_id)


def ensure_can_decide(user: dict[str, Any], approval: dict[str, Any]) -> None:
    """审批：必须是 manager/admin，且不能批准自己发起的操作。"""
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以审批")
    if approval.get("requester") and approval["requester"] == user["user_id"]:
        raise ApiError(403, "SELF_APPROVAL_FORBIDDEN", "不能审批自己发起的操作，请找其他主管")
    if approval.get("status") != "pending":
        raise ApiError(409, "APPROVAL_ALREADY_DECIDED", "该审批单已处理，请刷新")