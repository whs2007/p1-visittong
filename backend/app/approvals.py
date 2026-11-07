"""人工审批（Human-in-the-loop）：服务端阻塞等待，一次性 Token，超时/拒绝/断连全覆盖。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.trace.store import TraceStore

log = logging.getLogger(__name__)


class ApprovalManager:
    def __init__(self, traces: TraceStore, timeout_s: int = 120) -> None:
        self.traces = traces
        self.timeout_s = timeout_s
        self._waiters: dict[str, asyncio.Event] = {}
        self._results: dict[str, str] = {}
        self._meta: dict[str, dict[str, str]] = {}

    async def request(
        self,
        *,
        trace_id: str,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        risk: str,
        reason: str,
        requester: str = "",
    ) -> dict[str, Any]:
        row = await self.traces.create_approval(
            trace_id=trace_id, session_id=session_id, tool=tool,
            args=args, risk=risk, reason=reason, timeout_s=self.timeout_s,
            requester=requester,
        )
        approval_id = row["approval_id"]
        self._waiters[approval_id] = asyncio.Event()
        self._meta[approval_id] = {"trace_id": trace_id, "session_id": session_id}
        return {**row, "approval_id": approval_id}

    async def wait(self, approval_id: str) -> str:
        """返回 approved / rejected / timeout / cancelled。"""
        event = self._waiters.get(approval_id)
        remaining = float(self.timeout_s)
        try:
            row = await self.traces.get_approval(approval_id)
            if row and row.get("expires_at"):
                deadline = datetime.fromisoformat(row["expires_at"])
                remaining = max(0.0, (deadline - datetime.now().astimezone()).total_seconds())
            if event is not None and not event.is_set():
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    await self.traces.expire_approval(approval_id)
                    self._results[approval_id] = "timeout"
            return self._results.get(approval_id, "timeout")
        finally:
            self._waiters.pop(approval_id, None)
            self._results.pop(approval_id, None)
            self._meta.pop(approval_id, None)

    async def decide(self, approval_id: str, decision: str, reason: str = "", approver: str = "") -> str:
        """返回 ok / not_found / already_decided。"""
        row = await self.traces.get_approval(approval_id)
        if not row:
            return "not_found"
        if row["status"] != "pending":
            return "already_decided"
        ok = await self.traces.decide_approval(approval_id, decision, reason, approver)
        if not ok:
            return "already_decided"
        self._results[approval_id] = decision
        event = self._waiters.get(approval_id)
        if event:
            event.set()
        return "ok"

    async def cancel_by_trace(self, trace_id: str) -> None:
        """客户端断连等场景：把所有等待者唤醒为 cancelled，避免协程泄漏。"""
        await self.traces.cancel_pending_by_trace(trace_id)
        for approval_id, meta in list(self._meta.items()):
            if meta["trace_id"] == trace_id:
                self._results[approval_id] = "cancelled"
                event = self._waiters.get(approval_id)
                if event:
                    event.set()
                log.info("approval %s cancelled (trace %s)", approval_id, trace_id)