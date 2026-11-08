"""工具执行流水线：执行凭证 -> 能力边界 -> 策略 -> 审批 -> 执行 -> 记账 -> 观测。

Pi 把「工具拦截/审批」放在 extension 的 tool_call hook 里；P1 是业务系统，
把等价能力内建为可测试的流水线：

  0. ExecutionContract 签发 + capability_guard（装配期白名单的执行期硬校验）
  1. emit tool_call
  2. hooks.before_tool_call（扩展只能**加严**，不能放宽）
  3. PolicyEngine（身份/角色 / 技能归属 / Schema / 距离 / 风险等级）—— **必经**
  4. 高危 -> ApprovalManager 阻塞等待（持久化，可跨进程/跨重启）
  5. claim（先声明，崩溃可对账）-> 执行（超时 + 异常兜底）-> complete
  6. hooks.after_tool_result + Trace 落库
  7. emit card / slot_fill / tool_result

安全语义（v0.7 修正的两处）：
- **hook 不能短路策略**：旧实现 `if decision is None: policy.evaluate(...)`，
  只要任何一个扩展返回 allow，PolicyEngine 整段被跳过（角色、风险、距离全部
  失效）。现在两者都算，取更严者（deny > approval > allow）。
- **幂等与声明顺序**：先 `claim_tool` 提交一条 running，再执行，再 `complete_tool`。
  崩溃时 running 残留 = 状态未知，启动对账会标 unknown，而不是当作没执行过。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable

from app.agent.hooks import HookChain, HookDecision, ToolCall, ToolOutcome
from app.agent.output_guard import OutputGovernor
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.domain.idempotency import IdempotencyStore, IdemScope
from app.domain.tasks import TaskStore
from app.policy.engine import PolicyEngine, strictest
from app.runtime.contract import ExecutionContract
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
        tool_guard: Callable[[str, str], str | None] | None = None,
        tasks: TaskStore | None = None,
        idem: IdempotencyStore | None = None,
        governor: OutputGovernor | None = None,
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
        # (tool_name, active_skill) -> 拒绝原因；None 表示放行
        self.tool_guard = tool_guard
        self.tasks = tasks
        self.idem = idem
        # 输出治理只在一处生效：`_execute` 的返回处。这样 Trace、SSE、上下文、
        # 幂等账本看到的是**同一份**文本，不会出现「日志里是全的、模型看到是截的」。
        self.governor = governor or OutputGovernor(
            db,
            max_chars=settings.tool_output_max_chars,
            max_tokens=settings.tool_output_max_tokens,
            head_ratio=settings.tool_output_head_ratio,
            redact=settings.pii_redact,
        )

    async def run(
        self,
        call: ToolCall,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        skill: str,
        emit: Callable[..., dict[str, Any]],
        contract: ExecutionContract | None = None,
        preapproved: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        turn_id = contract.turn_id if contract else trace_id
        step = contract.step if contract else 0
        yield emit(
            "tool_call", call_id=call.call_id, name=call.name,
            label=call.label, args=call.args, risk=call.risk,
        )

        started = time.monotonic()
        # 0) 能力边界：装配期白名单在执行期硬校验，越权调用连策略/审批都不进入
        guard_reason = self.tool_guard(call.name, skill) if self.tool_guard is not None else None
        if guard_reason is not None:
            await self.traces.add_event(
                trace_id, "capability_denied", name=call.name,
                payload={"skill": skill, "reason": guard_reason, "args": call.args,
                         "contract": contract.describe() if contract else None},
            )
            decision: HookDecision = HookDecision(
                action="deny", reason=guard_reason, risk="high", rule="capability_guard"
            )
        else:
            user = self.db.get_user(user_id) or {}
            policy_decision = self.policy.evaluate(
                user_id=user_id,
                role=str(user.get("role") or ""),
                name=call.name,
                args=call.args,
                active_skill=skill,
            )
            hook_decision = await self.hooks.before_tool_call(call)
            # 顺序有意义：同为 deny 时保留**扩展点给出的更具体理由**（max 取首个最大值），
            # 但绝不因为 hook 说 allow 就跳过策略判定。
            merged = strictest(
                hook_decision,
                HookDecision(
                    action=policy_decision.action, reason=policy_decision.reason,
                    risk=policy_decision.risk, rule=policy_decision.rule,
                ),
            )
            decision = merged

        approval_id: str | None = None
        approved: bool | None = None
        if decision.action == "deny":
            result = ToolResult("denied", decision.reason)
            latency = 0
        elif decision.action == "approval" and not preapproved:
            info = await self.approvals.request(
                trace_id=trace_id, session_id=session_id, tool=call.name,
                args=call.args, risk=decision.risk, reason=decision.reason,
                requester=user_id,
            )
            approval_id = info["approval_id"]
            # 等待态持久化：把「在等哪张单、第几步、哪个 call」写进任务行，
            # 重启后 resume 能据此接回，而不是重跑整个 ReAct。
            if self.tasks is not None:
                self.tasks.mark_awaiting(
                    turn_id=turn_id, approval_id=approval_id, step=step,
                    call_id=call.call_id, tool=call.name,
                )
            await self.traces.add_event(
                trace_id, "approval_required", name=call.name,
                payload={"approval_id": approval_id, "reason": decision.reason,
                         "args": call.args, "step": step, "call_id": call.call_id},
            )
            yield emit(
                "approval_required", approval_id=approval_id, call_id=call.call_id,
                tool=call.name, label=call.label, args=call.args, risk=decision.risk,
                reason=decision.reason, expires_in=self.settings.approval_timeout_s,
            )
            verdict = await self.approvals.wait(approval_id)
            if self.tasks is not None:
                self.tasks.clear_awaiting(turn_id)
            await self.traces.add_event(
                trace_id, "approval_result", name=verdict,
                payload={"approval_id": approval_id, "decision": verdict},
            )
            yield emit("approval_result", approval_id=approval_id, decision=verdict)
            latency = int((time.monotonic() - started) * 1000)
            if verdict == "approved":
                approved = True
                result = await self._execute(call, user_id, session_id, trace_id, contract)
            else:
                approved = False
                result = ToolResult(
                    "rejected" if verdict == "rejected" else "timeout",
                    "REJECTED_BY_HUMAN" if verdict == "rejected" else "APPROVAL_TIMEOUT",
                )
        else:
            if decision.action == "approval":
                # 已由人工审批过（resume 接回），凭证里保留审批通过的语义
                approved = True
            result = await self._execute(call, user_id, session_id, trace_id, contract)
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
                    "rule": decision.rule,
                    "contract": contract.describe() if contract else None,
                },
                trace_id=trace_id,
            )

        await self.traces.add_event(
            trace_id, "tool_result", name=call.name, status=outcome.status, latency_ms=latency,
            payload={
                "name": call.name, "label": call.label,
                "skill": call.spec.skill if call.spec else None,
                "args": call.args, "output": outcome.output[:500],
                "output_ref": (outcome.data or {}).get("_output_ref"),
                "pii_redacted": (outcome.data or {}).get("_pii_redacted", 0),
                "decision": outcome.decision, "rule": decision.rule,
                "approval_id": approval_id, "approved": approved,
                "contract": contract.describe() if contract else None,
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
            output_ref=(outcome.data or {}).get("_output_ref"),
            pii_redacted=(outcome.data or {}).get("_pii_redacted", 0),
        )

    async def _execute(
        self, call: ToolCall, user_id: str, session_id: str, trace_id: str,
        contract: ExecutionContract | None = None,
    ) -> ToolResult:
        if call.spec is None:
            return ToolResult("error", f"工具不存在：{call.name}")

        # 先声明后执行：副作用状态未知时，账本里要留得下痕迹。
        # 账本覆盖**所有**工具（含只读）——它是「这条调用发生过/结果是什么」的
        # 事实来源，也是回放依据；副作用幂等只对写工具做（读没有副作用）。
        write_tool = call.spec.scope == "write"
        claim: dict[str, Any] | None = None
        if self.tasks is not None and contract is not None:
            claim = self.tasks.claim_tool(
                turn_id=contract.turn_id, step=contract.step,
                call_id=contract.call_id, name=call.name,
            )
            if claim and claim.get("status") not in (None, "", "running"):
                # 已有终态：直接回放，绝不重复产生副作用
                try:
                    import json as _json

                    return ToolResult(
                        claim["status"], claim.get("output") or "",
                        _json.loads(claim.get("data") or "{}"),
                    )
                except ValueError:
                    return ToolResult(claim["status"], claim.get("output") or "", {})

        scope: IdemScope | None = None
        if self.idem is not None and contract is not None and write_tool:
            record = self.idem.begin(
                key=contract.idem_key, effect_type=call.name,
                turn_id=contract.turn_id, step=contract.step, call_id=contract.call_id,
            )
            scope = IdemScope(
                self.idem, key=contract.idem_key, effect_type=call.name, prior=record
            )
            replayed = scope.replay()
            if replayed is not None:
                await self.traces.add_event(
                    trace_id, "tool_effect_replay", name=call.name,
                    payload={"idem_key": contract.idem_key, "effect_type": call.name},
                )
                if self.tasks is not None:
                    self.tasks.complete_tool(
                        turn_id=contract.turn_id, step=contract.step,
                        call_id=contract.call_id, status=replayed.status,
                        output=replayed.output, data=replayed.data,
                    )
                return replayed

        ctx = ToolContext(
            settings=self.settings, session_id=session_id, trace_id=trace_id,
            user_id=user_id, db=self.db, tree=self.tree, state=self.state,
            knowledge=self.knowledge, idem=scope,
            contract=contract.describe() if contract else None,
        )
        try:
            result = await asyncio.wait_for(
                call.spec.handler(call.args, ctx), timeout=self.settings.tool_timeout_s
            )
        except asyncio.TimeoutError:
            result = ToolResult("error", f"工具执行超时（>{self.settings.tool_timeout_s}s）")
        except Exception as exc:  # noqa: BLE001
            log.exception("tool failed: %s", call.name)
            result = ToolResult("error", f"工具执行异常：{exc}")

        # 输出治理：脱敏 → 裁剪 → 引用化。放在 complete_tool 之前，是为了让
        # 幂等账本里存的也是治理后文本——崩溃重放时模型看到的东西必须一致，
        # 否则「重放」等于换了一份输入在跑。
        result = self._govern(result, call=call, user_id=user_id, session_id=session_id, trace_id=trace_id)

        if self.tasks is not None and contract is not None:
            self.tasks.complete_tool(
                turn_id=contract.turn_id, step=contract.step, call_id=contract.call_id,
                status=result.status, output=result.output, data=result.data,
            )
        # handler 没自己 remember（例如前置校验就返 need_input）时补记，
        # 避免幂等账本长期停在 running 制造假「悬空执行」。
        if scope is not None and scope.state != "done":
            scope.remember(result)
        return result

    def _govern(
        self, result: ToolResult, *, call: ToolCall, user_id: str, session_id: str, trace_id: str,
    ) -> ToolResult:
        """对工具输出做脱敏 / 裁剪 / 引用化，返回新的 ToolResult。

        只改 `output`（进模型的那段），`data` 里的结构化字段（card、missing、
        plan_id 等）原样保留：它们是业务契约，截断就等于改契约。
        """
        governed = self.governor.govern(
            result.output, tool=call.name, trace_id=trace_id,
            session_id=session_id, user_id=user_id,
        )
        if not governed.truncated and not governed.redactions:
            return result
        data = dict(result.data or {})
        if governed.ref:
            data["_output_ref"] = governed.ref
            data["_output_truncated"] = True
        if governed.redactions:
            data["_pii_redacted"] = governed.redactions
        return ToolResult(result.status, governed.text, data)
