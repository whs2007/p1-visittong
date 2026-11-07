"""工具执行流水线：策略 -> 扩展钩子 -> 审批 -> 执行 -> 观测。

Pi 把「工具拦截/审批」放在 extension 的 tool_call hook 里；P1 是业务系统，
把等价能力内建为可测试的流水线：

  1. emit tool_call
  2. hooks.before_tool_call（扩展可 block / 降级）
  3. PolicyEngine（白名单 / Schema / 危险模式 / 风险等级）
  4. 高危 -> ApprovalManager 阻塞等待（一次性 Token，超时/拒绝/断连）
  5. 执行（超时 + 异常兜底）
  6. hooks.after_tool_result + Trace 落库
  7. emit card / slot_fill / tool_result

并行说明（Pi 默认 parallel）：P1 的工具会写 SQLite 且有"待人工审批"的阻塞，
因此保持顺序执行；ToolSpec 上保留扩展位，后续可对只读工具开启并发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable

from app.agent.events import ev
from app.agent.hooks import HookChain, HookDecision, ToolCall, ToolOutcome
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree
from app.skills.registry import ToolContext, ToolRegistry, ToolResult
from app.trace.store import TraceStore

log = logging.getLogger(__name__)


class ToolPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ToolRegistry,
        policy: PolicyEngine,
        approvals: ApprovalManager,
        traces: TraceStore,
        db: Database,
        tree: SessionTree,
        state: SessionState,
        knowledge: Any = None,
        hooks: HookChain | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.traces = traces
        self.db = db
        self.tree = tree
        self.state = state
        self.knowledge = knowledge
        self.hooks = hooks or HookChain()

    async def run(
        self,
        call: ToolCall,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        skill: str,
        emit: Callable[..., dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        yield emit(
            "tool_call", call_id=call.call_id, name=call.name,
            label=call.label, args=call.args, risk=call.risk,
        )

        started = time.monotonic()
        # 1) 扩展钩子优先（对齐 Pi tool_call hook 的 block 语义）
        decision: HookDecision | None = await self.hooks.before_tool_call(call)
        if decision is None:
            user = self.db.get_user(user_id) or {}
            policy = self.policy.evaluate(
                user_id=user_id, role=str(user.get("role") or ""), name=call.name, args=call.args,
            )
            decision = HookDecision(
                action=policy.action, reason=policy.reason, risk=policy.risk, rule=policy.rule
            )

        approval_id: str | None = None
        approved: bool | None = None
        if decision.action == "deny":
            result = ToolResult("denied", decision.reason)
            latency = 0
        elif decision.action == "approval":
            info = await self.approvals.request(
                trace_id=trace_id, session_id=session_id, tool=call.name,
                args=call.args, risk=decision.risk, reason=decision.reason,
                requester=user_id,
            )
            approval_id = info["approval_id"]
            await self.traces.add_event(
                trace_id, "approval_required", name=call.name,
                payload={"approval_id": approval_id, "reason": decision.reason, "args": call.args},
            )
            yield emit(
                "approval_required", approval_id=approval_id, call_id=call.call_id,
                tool=call.name, label=call.label, args=call.args, risk=decision.risk,
                reason=decision.reason, expires_in=self.settings.approval_timeout_s,
            )
            verdict = await self.approvals.wait(approval_id)
            await self.traces.add_event(
                trace_id, "approval_result", name=verdict,
                payload={"approval_id": approval_id, "decision": verdict},
            )
            yield emit("approval_result", approval_id=approval_id, decision=verdict)
            latency = int((time.monotonic() - started) * 1000)
            if verdict == "approved":
                approved = True
                result = await self._execute(call, user_id, session_id, trace_id)
            else:
                approved = False
                result = ToolResult(
                    "rejected" if verdict == "rejected" else "timeout",
                    "REJECTED_BY_HUMAN" if verdict == "rejected" else "APPROVAL_TIMEOUT",
                )
        else:
            result = await self._execute(call, user_id, session_id, trace_id)
            latency = int((time.monotonic() - started) * 1000)

        outcome = ToolOutcome(
            status=result.status, output=result.output, data=result.data,
            latency_ms=latency, risk=decision.risk, decision=decision.action,
            approval_id=approval_id, approved=approved,
        )
        # 2) 扩展钩子后置（审计 / 脱敏 / 改写）
        outcome = await self.hooks.after_tool_result(call, outcome)
        call.result = outcome

        # 操作审计：写工具 + 被拦截/被拒绝/超时的调用都留痕（与 Trace 分离，可导出）
        spec = call.spec
        if spec is not None and (
            spec.scope == "write" or outcome.status in ("denied", "rejected", "timeout")
        ):
            self.db.add_audit(
                actor=user_id,
                action=f"tool.{spec.name}.{outcome.status}",
                target_type="tool",
                target_id=call.call_id,
                before={"args": call.args},
                after={
                    "status": outcome.status, "output": (outcome.output or "")[:300],
                    "decision": outcome.decision, "approved": approved,
                },
                trace_id=trace_id,
            )

        await self.traces.add_event(
            trace_id, "tool_result", name=call.name, status=outcome.status, latency_ms=latency,
            payload={
                "name": call.name, "label": call.label,
                "skill": call.spec.skill if call.spec else None,
                "args": call.args, "output": outcome.output[:500],
                "decision": outcome.decision, "rule": decision.rule,
                "approval_id": approval_id, "approved": approved,
            },
        )
        if outcome.data and outcome.data.get("card"):
            yield emit("card", **outcome.data["card"])
        if outcome.status == "need_input":
            yield emit(
                "slot_fill", skill=skill,
                missing=(outcome.data or {}).get("missing", []),
                question=outcome.output,
            )
        yield emit(
            "tool_result", call_id=call.call_id, name=call.name, label=call.label,
            status=outcome.status, output=outcome.output, latency_ms=latency,
            risk=outcome.risk, decision=outcome.decision,
        )

    async def _execute(
        self, call: ToolCall, user_id: str, session_id: str, trace_id: str
    ) -> ToolResult:
        if call.spec is None:
            return ToolResult("error", f"工具不存在：{call.name}")
        ctx = ToolContext(
            settings=self.settings, session_id=session_id, trace_id=trace_id,
            user_id=user_id, db=self.db, tree=self.tree, state=self.state,
            knowledge=self.knowledge,
        )
        try:
            return await asyncio.wait_for(
                call.spec.handler(call.args, ctx), timeout=self.settings.tool_timeout_s
            )
        except asyncio.TimeoutError:
            return ToolResult("error", f"工具执行超时（>{self.settings.tool_timeout_s}s）")
        except Exception as exc:  # noqa: BLE001
            log.exception("tool failed: %s", call.name)
            return ToolResult("error", f"工具执行异常：{exc}")