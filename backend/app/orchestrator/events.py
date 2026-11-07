"""事件工厂：SSE 与 Trace 共用一套字段约定。"""
from __future__ import annotations

from typing import Any


def ev(event: str, **data: Any) -> dict[str, Any]:
    return {"event": event, "data": data}