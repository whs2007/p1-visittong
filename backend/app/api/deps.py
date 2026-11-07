"""FastAPI 依赖：应用上下文、鉴权用户。"""
from __future__ import annotations

from fastapi import Request

from app.auth import get_current_user  # noqa: F401  对外统一从这里导出
from app.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx