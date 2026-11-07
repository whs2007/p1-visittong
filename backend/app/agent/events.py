"""统一事件包络：一个 Turn 一条单调递增的事件流。

Pi（agent-core）用事件流把「运行时」和「渲染/持久化」解耦；P1 把同一条流
同时喂给 SSE（前端）和 Trace（SQLite）。每个事件带 seq / ts / elapsed_ms，
前端可据此补帧、去重，Trace 可直接落库。
"""
from __future__ import annotations

import time
from typing import Any

from app.utils import now_iso

# P1 SSE 事件名 -> Pi agent-core 事件名（文档/面试对照用，不影响契约）
PI_EVENT_MAP: dict[str, str] = {
    "start": "agent_start",
    "status": "turn_start/update",
    "route": "turn_start",
    "delta": "message_update",
    "tool_call": "tool_execution_start",
    "tool_result": "tool_execution_end",
    "approval_required": "tool_execution_update(approval)",
    "approval_result": "tool_execution_update(approval)",
    "card": "message_end(custom)",
    "done": "agent_end",
    "error": "agent_end(error)",
    "compaction": "turn_end(compaction)",
}


class EventFactory:
    """一个 Turn 一个工厂：保证 seq 单调、时间基准一致。"""

    def __init__(self) -> None:
        self.seq = 0
        self.started_at = time.monotonic()

    def __call__(self, event: str, **data: Any) -> dict[str, Any]:
        self.seq += 1
        return {
            "event": event,
            "data": {
                **data,
                "seq": self.seq,
                "ts": now_iso(),
                "elapsed_ms": int((time.monotonic() - self.started_at) * 1000),
            },
        }


def ev(event: str, **data: Any) -> dict[str, Any]:
    """兼容旧调用（无 seq）。新代码统一用 EventFactory。"""
    return {"event": event, "data": data}