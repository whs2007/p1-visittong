"""人工审批（Human-in-the-loop）：DB 为准 + 本地事件加速 + 轮询兜底。

v0.6 及之前：等待态是进程内 `asyncio.Event`，`decide()` 必须在同一个进程里
调用才能唤醒等待者。于是 ——
  * 多 worker 下，审批请求落到 A、审批决定落到 B，A 永远等不到；
  * 进程被杀后重启，`reconcile_stale` 把单子标 cancelled，等待协程已不存在，
    这个 Turn 的审批断点接不回来。

v0.7：审批单的**唯一事实源是 `approvals` 表**（`status` 有 CAS 更新），
进程内 Event 降级为「同进程即时唤醒」的加速器，每 `poll_interval_s` 再读一次
库兜底。这样跨进程、跨重启都能接回；`ApprovalManager` 变成无状态协调器。

边界如实声明：单条审批的**决策**必须由 `decide()` 走 CAS 写入（防并发双批），
这一点没变；变的只是「等待者如何得知结果」。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.trace.store import TraceStore

log = logging.getLogger(__name__)

TERMINAL = {"approved", "rejected", "timeout", "cancelled", "expired"}


class ApprovalManager:
    def __init__(
        self, traces: TraceStore, timeout_s: int = 120, poll_interval_s: float = 0.2
    ) -> None:
        self.traces = traces
        self.timeout_s = timeout_s
        self.poll_interval_s = max(0.05, poll_interval_s)
        self._waiters: dict[str, asyncio.Event] = {}
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
        """返回 approved / rejected / timeout / cancelled（以 DB 终态为准）。"""
        event = self._waiters.get(approval_id)
        deadline: datetime | None = None
        try:
            row = await self.traces.get_approval(approval_id)
            if row and row.get("expires_at"):
                try:
                    deadline = datetime.fromisoformat(row["expires_at"])
                except ValueError:
                    deadline = None
            if row and row["status"] in TERMINAL:
                return row["status"]

            while True:
                # 1) 读库：跨进程 / 跨重启的唯一事实源
                current = await self.traces.get_approval(approval_id)
                if current and current["status"] in TERMINAL:
                    return current["status"]

                remaining = None
                if deadline is not None:
                    remaining = (deadline - datetime.now().astimezone()).total_seconds()
                    if remaining <= 0:
                        await self.traces.expire_approval(approval_id)
                        return "timeout"

                # 2) 本地事件只用于降低延迟；超时后回到轮询
                timeout = self.poll_interval_s
                if remaining is not None:
                    timeout = min(timeout, max(remaining, 0.01))
                if event is not None:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=timeout)
                        event.clear()
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(timeout)
        finally:
            self._waiters.pop(approval_id, None)
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
        event = self._waiters.get(approval_id)
        if event:
            event.set()
        return "ok"

    async def cancel_by_trace(self, trace_id: str) -> None:
        """客户端断连等场景：把所有等待者标 cancelled，避免协程泄漏。"""
        await self.traces.cancel_pending_by_trace(trace_id)
        for approval_id, meta in list(self._meta.items()):
            if meta["trace_id"] == trace_id:
                event = self._waiters.get(approval_id)
                if event:
                    event.set()
                log.info("approval %s cancelled (trace %s)", approval_id, trace_id)
