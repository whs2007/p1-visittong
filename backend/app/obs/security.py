"""HTTP 层安全与可观测中间件（纯 ASGI 实现）。

**为什么不用 `BaseHTTPMiddleware`**：它会把响应体整体包一层，破坏
`StreamingResponse`（本项目 `/chat/stream` 是 SSE 长连接），实测会导致
请求挂住不返回。纯 ASGI 中间件只在 `http.response.start` 上动手，流式响应
原样透传 —— 这类问题在本地 Jupyter/浏览器里表现为「对话一直转圈」，
很难排查，所以直接写 ASGI 是更稳的选择。

`RequestContextMiddleware`：给每个请求分配 request_id，写回响应头并放进
contextvar，让同一次请求内所有日志自动带同一个 ID。

`LoginThrottleMiddleware`：登录失败限流。原实现放在 `ctx.login_failures`
（进程内 dict）：重启清零、多 worker 各算各的、且只增不减。这里改成
`login_attempts` 表计数 —— 跨进程一致、可审计、按窗口滑动。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.obs.logging import new_request_id, request_id_var


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = (headers.get(b"x-request-id") or b"").decode("latin-1") or new_request_id()
        token = request_id_var.set(rid)
        started = time.monotonic()

        async def send_with_context(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["X-Request-Id"] = rid
                out["X-Elapsed-Ms"] = str(int((time.monotonic() - started) * 1000))
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            request_id_var.reset(token)


class LoginThrottleMiddleware:
    """只拦 `POST /api/auth/login`，按 client_ip + 时间窗统计失败次数。"""

    def __init__(self, app: ASGIApp, *, max_failures: int = 10, window_s: int = 300) -> None:
        self.app = app
        self.max_failures = max_failures
        self.window_s = window_s

    # -- DB 访问（失败不阻断登录：老库未迁移时退化为不限流） ------------------ #
    def _recent_failures(self, scope: Scope, ip: str) -> int:
        db = getattr(scope["app"].state, "ctx", None)
        db = getattr(db, "db", None) if db is not None else None
        if db is None:
            return 0
        since = (datetime.now().astimezone() - timedelta(seconds=self.window_s)).isoformat(
            timespec="seconds"
        )
        try:
            row = db._conn().execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM login_attempts WHERE client_ip=? AND ok=0 AND created_at>=?",
                (ip, since),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def _record(self, scope: Scope, ip: str, ok: bool) -> None:
        ctx = getattr(scope["app"].state, "ctx", None)
        db = getattr(ctx, "db", None) if ctx is not None else None
        if db is None:
            return
        try:
            conn = db._conn()  # noqa: SLF001
            conn.execute(
                "INSERT INTO login_attempts (user_id, client_ip, ok, created_at) VALUES (?,?,?,?)",
                ("", ip, 1 if ok else 0,
                 datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/auth/login" \
                or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else "unknown"
        if self._recent_failures(scope, ip) >= self.max_failures:
            body = (
                '{"detail":"登录失败次数过多，请稍后再试","code":"LOGIN_THROTTLED"}'
            ).encode("utf-8")
            await send({
                "type": "http.response.start", "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(self.window_s).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        status_code: dict[str, int] = {}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_code["value"] = int(message["status"])
            await send(message)

        await self.app(scope, receive, capture)
        self._record(scope, ip, status_code.get("value", 500) < 400)
