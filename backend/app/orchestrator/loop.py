"""编排核心：Turn 状态机 + ReAct 循环 + SSE 事件 + Trace。

对齐 Pi agent 的分层（见 docs/07-Pi-Agent架构对照与优化.md）：
  - 事件：EventFactory 统一包络（seq/ts/elapsed_ms），SSE 与 Trace 同源；
  - 工具：ToolPipeline 负责「策略 -> 扩展钩子 -> 审批 -> 执行 -> 观测」；
  - 上下文：prompt.build_system_prompt（技能索引 + 当前技能）+ 超预算压缩；
  - 收敛：显式记录 stop_reason（no_tool_calls / max_steps），对应 Pi 的
    finishTurn 返回 {action:"end"} 的思想；max_loop 只是防失控兜底。

一次 handle() 对应一个 Turn：路由 → 槽位 → 工具循环（含审批）→ 回复 → done。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

from app.agent.compaction import compact_if_needed
from app.agent.events import EventFactory
from app.agent.hooks import ToolCall
from app.agent.pipeline import ToolPipeline
from app.agent.prompt import build_system_prompt
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.domain.fsm import SessionFSM
from app.domain.tasks import TaskStore
from app.llm.base import BaseLLM
from app.orchestrator.router import RouteResult, build_slots, route_card, route_text_hybrid
from app.policy.engine import PolicyEngine
from app.runtime import AgentSession, default_session
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree
from app.skills.registry import ToolRegistry, ToolResult
from app.skills.visit_skills import SKILLS
from app.trace.store import TraceStore

log = logging.getLogger(__name__)


def _tool_message(result: ToolResult) -> str:
    return json.dumps(
        {"status": result.status, "output": result.output, "data": result.data or {}},
        ensure_ascii=False,
    )


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        tree: SessionTree,
        state: SessionState,
        registry: ToolRegistry,
        policy: PolicyEngine,
        llm: BaseLLM,
        traces: TraceStore,
        approvals: ApprovalManager,
        tasks: TaskStore,
        knowledge: Any = None,
        session: AgentSession | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.tree = tree
        self.state = state
        self.registry = registry
        self.policy = policy
        self.llm = llm
        self.traces = traces
        self.approvals = approvals
        self.tasks = tasks
        self.session = session or default_session(registry)
        self.session_fsm = SessionFSM(db, state)
        self.pipeline = ToolPipeline(
            settings=settings, registry=registry, policy=policy,
            approvals=approvals, traces=traces, db=db, tree=tree, state=state,
            knowledge=knowledge, tool_guard=self._guard_tool,
        )

    def _guard_tool(self, tool_name: str, active_skill: str) -> str | None:
        """执行期能力边界：模型吐出装配白名单外的工具名也不会被执行。

        绑定方法在调用时才读 self.session，因此 /api/agents/reload 热更新后
        无需重建 ToolPipeline 即生效。这里只裁 Agent 级白名单；工具与当前技能
        的归属、角色权限、风险等级仍分别由工具 schema 与 PolicyEngine 负责。
        """
        if not self.session.allows_tool(tool_name):
            return (
                f"CAPABILITY_DENIED: 工具 {tool_name} 不在 Agent "
                f"{self.session.name} 的能力白名单内"
            )
        if not self.session.allows_skill(active_skill):
            return f"CAPABILITY_DENIED: 技能 {active_skill} 未授权给 Agent {self.session.name}"
        return None

    def replay_decision(self, tool_name: str, skill: str, has_saved: bool) -> tuple[str, str | None]:
        """幂等回放是否允许消费旧结果。

        抽成可单测的判定，是为了让「回放也过能力边界」不是一句口头声明。
        生效场景（写清楚避免过度宣称）：legacy 检查点被兼容放行、同一个
        turn_id 被重放、或 Turn 执行中途发生 Agent 热更新。跨能力集的正常
        resume 已由 check_resume_compat 提前拒绝，走不到这里。
        """
        if not has_saved:
            return ("execute", None)
        reason = self._guard_tool(tool_name, skill)
        return ("deny", reason) if reason else ("replay", None)

    def _checkpoint_capability(self) -> dict[str, Any]:
        """每次写检查点都带上本次装配的能力快照。"""
        return {
            "agent_name": self.session.name,
            "capability_fingerprint": self.session.capability_fingerprint(),
            "capability": self.session.capability(),
        }

    def check_resume_compat(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """检查检查点绑定的能力集与当前装配是否一致。

        三种结果：
        - `ok`：一致，正常续跑；
        - `legacy`：v0.5 之前的检查点没有指纹，兼容放行但写 trace 留痕；
        - `incompatible`：能力集已变，**拒绝续跑**。

        为什么拒绝而不是「用新能力继续」：checkpoint.messages 里含旧系统提示词
        与旧工具结果，用新权限跑旧上下文等于静默权限提升；也不该自动把旧
        检查点升级到新能力。正确做法是显式失败，让人决定：恢复原配置后
        resume，或以当前 Agent 重新发起任务。
        """
        recorded = checkpoint.get("capability_fingerprint")
        current = self.session.capability_fingerprint()
        base = {
            "recorded_agent": checkpoint.get("agent_name"),
            "recorded_fingerprint": recorded,
            "current_agent": self.session.name,
            "current_fingerprint": current,
        }
        if not recorded:
            return {**base, "status": "legacy", "reason": "检查点无能力指纹（v0.5 之前），按兼容模式续跑并留痕"}
        if recorded == current:
            return {**base, "status": "ok", "reason": "能力集一致"}
        return {
            **base,
            "status": "incompatible",
            "reason": (
                f"检查点由 Agent {checkpoint.get('agent_name')}（{str(recorded)[:12]}…）创建，"
                f"当前装配为 {self.session.name}（{current[:12]}…）。"
                "能力集已变更，拒绝跨能力集续跑；请恢复原配置后重试，或以当前 Agent 重新发起。"
            ),
            "recorded_capability": checkpoint.get("capability") or {},
            "current_capability": self.session.capability(),
        }

    def set_session(self, session: AgentSession) -> None:
        """热更新能力视图（ResourceLoader.reload 之后由装配层调用）。"""
        self.session = session

    # ------------------------------------------------------------------ #
    # 上下文：压缩 + 提示词组装（Pi：transformContext + sections）
    # ------------------------------------------------------------------ #
    async def _prepare_context(
        self,
        *,
        session_id: str,
        skill: str,
        slots: dict[str, Any],
        trace_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        info = await compact_if_needed(
            tree=self.tree, session_id=session_id, settings=self.settings,
        )
        if info:
            await self.traces.add_event(
                trace_id, "compaction", name="context_compacted",
                payload={
                    "summary_id": info["summary_id"],
                    "first_kept_entry_id": info["first_kept_entry_id"],
                    "folded": info["folded"], "kept": info["kept"],
                    "tokens_before": info["tokens_before"], "tokens_after": info["tokens_after"],
                },
            )
        entity = await self.state.entity(session_id)
        system = self.session.system_prompt(active_skill=skill, slots=slots, entity=entity)
        messages = [{"role": "system", "content": system}] + self.tree.messages(
            session_id, self.settings.context_limit
        )
        return messages, info

    async def _fsm(self, *, session_id: str, event: str, user_id: str, trace_id: str) -> None:
        """会话 FSM 迁移：写 meta + state_transitions，并在 Trace 里留一条事件。"""
        result = await self.session_fsm.transition(
            session_id, event, actor=user_id, trace_id=trace_id,
        )
        if result.ok:
            await self.traces.add_event(
                trace_id, "state_transition", name=f"session:{result.from_state}->{result.to_state}",
                payload={"entity": "session", "from": result.from_state, "to": result.to_state, "event": event},
            )
        else:
            log.warning("session fsm rejected: %s", result.reason)

    # ------------------------------------------------------------------ #
    # 快捷键 / 主入口
    # ------------------------------------------------------------------ #
    async def handle(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str | None = None,
        card: dict[str, Any] | None = None,
        turn_id: str | None = None,
        resume: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        started = time.monotonic()
        emit = EventFactory()
        user_text = message or (f"[卡片] {card.get('card_name')}:{card.get('action_id')}" if card else "")
        trace_id = await self.traces.start_trace(session_id, user_id, user_text)
        turn_id = turn_id or trace_id
        finished = False
        tool_calls_count = 0
        tokens_in = tokens_out = 0
        skill = "chat"
        stop_reason = "no_tool_calls"
        step_start = 0
        last_step = 0

        try:
            yield emit("start", session_id=session_id, trace_id=trace_id, user_id=user_id)
            await self.traces.add_event(trace_id, "turn_start", name="任务开始", payload={"text": user_text})

            if resume:
                compat = self.check_resume_compat(resume)
                if compat["status"] == "incompatible":
                    # 防御性分支：HTTP 入口已提前返回 409；这里保证任何直接调用者
                    # 也不会静默用新权限跑旧上下文。任务保持 interrupted，
                    # 恢复原装配后可再次 resume，不需要重放历史。
                    await self.traces.add_event(
                        trace_id, "capability_mismatch", name="resume_rejected",
                        status="error", payload=compat,
                    )
                    self.tasks.update(turn_id, status="interrupted", error="AGENT_CAPABILITY_CHANGED")
                    yield emit("error", message=compat["reason"], code="AGENT_CAPABILITY_CHANGED")
                    finished = True
                    return
                if compat["status"] == "legacy":
                    await self.traces.add_event(
                        trace_id, "capability_legacy_checkpoint", name="legacy_checkpoint",
                        payload=compat,
                    )
                # 断点续跑：不重新路由、不重写用户消息，直接从未完成的 ReAct 上下文继续
                self.tasks.update(
                    turn_id, status="running", trace_id=trace_id,
                    resume_count=int(resume.get("resume_count") or 0) + 1,
                )
                skill = resume.get("skill") or "chat"
                slots = resume.get("slots") or {}
                messages = resume.get("messages") or []
                step_start = int(resume.get("step") or 0)
                tool_calls_count = int(resume.get("tool_calls") or 0)
                tokens_in = int(resume.get("tokens_in") or 0)
                tokens_out = int(resume.get("tokens_out") or 0)
                await self._fsm(session_id=session_id, event="resume", user_id=user_id, trace_id=trace_id)
                await self.traces.add_event(
                    trace_id, "resume", name="checkpoint_resume",
                    payload={"turn_id": turn_id, "from_step": step_start, "skill": skill},
                )
                yield emit("status", phase="resume", text=f"从第 {step_start} 步检查点恢复")
            else:
                await self._fsm(session_id=session_id, event="start", user_id=user_id, trace_id=trace_id)
                self.tasks.create(
                    turn_id=turn_id, session_id=session_id, user_id=user_id,
                    trace_id=trace_id, user_input=user_text,
                )

                # 1) 落用户消息
                self.tree.append(session_id=session_id, parent_id=None, role="user", content=user_text)

                # 2) 路由
                yield emit("status", phase="route", text="正在识别意图")
                if card:
                    route = route_card(
                        self.db, card.get("card_name", ""), card.get("action_id", ""),
                        card.get("skill_name", ""), card.get("payload") or {},
                        allowed=self.session.skill_names(),
                    )
                else:
                    route = await route_text_hybrid(
                        self.db, user_text, self.llm, self.settings,
                        labels=list(self.session.skill_names()),
                    )
                    route.slots.update(build_slots(self.db, user_text))
                routed_skill = route.skill
                skill = self.session.resolve_route(routed_skill)
                if skill != routed_skill:
                    await self.traces.add_event(
                        trace_id, "capability_denied", name=routed_skill,
                        payload={"agent": self.session.name, "requested": routed_skill, "fallback": skill},
                    )

                # 2.1) 槽位补充轮：用户只补信息（没命中任何 Skill）时沿用上轮子 Skill
                pending_slots = await self.state.slots(session_id)
                meta = await self.state.meta(session_id) or {}
                last_skill = meta.get("last_skill")
                if not card and skill == "chat" and last_skill in self.session.skills and pending_slots:
                    route = RouteResult(last_skill, 0.6, "context", route.slots, "pending_slots")
                    skill = route.skill
                await self.state.update(session_id, last_skill=skill)
                await self._fsm(session_id=session_id, event="route", user_id=user_id, trace_id=trace_id)
                yield emit("route", skill=skill, confidence=route.confidence, source=route.source, label=self.session.label_of(skill))
                await self.traces.add_event(
                    trace_id, "route", name=route.source,
                    payload={"skill": skill, "confidence": route.confidence, "source": route.source},
                )
                if skill in self.session.skills:
                    await self.traces.set_trace_skill(trace_id, skill)
                    await self.traces.add_event(trace_id, "skill_select", name=skill, payload={"skill": skill})

                # 3) 槽位合并（会话槽位 + 本轮抽取 + 卡片 payload）
                slots = await self.state.slots(session_id)
                slots.update({k: v for k, v in route.slots.items() if v not in (None, "")})
                await self.state.set_slots(session_id, slots)
                if route.slots.get("plan_id"):
                    await self.state.set_entity(session_id, {"last_plan_id": str(route.slots["plan_id"])})
                if route.slots.get("customer_id"):
                    await self.state.set_entity(session_id, {"last_customer_id": str(route.slots["customer_id"])})

                # 4) 组装上下文（必要时先压缩）
                messages, info = await self._prepare_context(
                    session_id=session_id, skill=skill, slots=slots, trace_id=trace_id,
                )
                if info:
                    yield emit("compaction", **info)
                # 初始检查点：进程在此后任一时刻崩溃都能从上下文恢复
                self.tasks.save_checkpoint(
                    turn_id=turn_id, step=0, skill=skill, messages=messages, slots=slots,
                    **self._checkpoint_capability(),
                )

            tool_names = [t.name for t in self.session.tools_for(skill)] if skill in self.session.skills else []
            tools = self.registry.openai_tools(tool_names)

            # 5) ReAct 循环（Pi：while(true) + 显式收敛条件）
            final_text = ""
            for step in range(step_start, self.settings.max_loop):
                last_step = step
                await self.traces.add_event(trace_id, "llm_call", name=self.llm.name, payload={"step": step})
                calls: list[dict[str, str]] = []
                text_parts: list[str] = []
                async for chunk in self.llm.stream(messages, tools):
                    if chunk.type == "text_delta":
                        text_parts.append(chunk.text)
                        yield emit("delta", text=chunk.text)
                    elif chunk.type == "usage":
                        tokens_in += chunk.usage.get("prompt_tokens", 0)
                        tokens_out += chunk.usage.get("completion_tokens", 0)
                    elif chunk.type == "tool_calls":
                        calls = chunk.tool_calls

                if not calls:
                    final_text = "".join(text_parts).strip() or "我没太理解，可以换个说法吗？"
                    stop_reason = "no_tool_calls"
                    break

                # assistant(tool_calls) + 每个 tool 结果都必须成对写入
                assistant_entry = self.tree.append(
                    session_id=session_id, parent_id=None, role="assistant",
                    content="".join(text_parts), tool_calls=calls,
                )
                for call in calls:
                    tool_calls_count += 1
                    name = call.get("name", "")
                    try:
                        args = json.loads(call.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("arguments 必须是对象")
                    except (json.JSONDecodeError, ValueError):
                        result = ToolResult("error", "参数解析失败：arguments 不是合法 JSON")
                        yield emit("tool_call", call_id=call["id"], name=name, label=name, args={}, risk="low")
                        yield emit("tool_result", call_id=call["id"], name=name, status="error", output=result.output, latency_ms=0)
                        await self.traces.add_event(
                            trace_id, "tool_result", name=name, status="error", latency_ms=0,
                            payload={"name": name, "args": {}, "output": result.output},
                        )
                        self.tree.append(
                            session_id=session_id, parent_id=assistant_entry["entry_id"], role="tool",
                            content=_tool_message(result), tool_call_id=call["id"], name=name,
                        )
                        continue

                    spec = self.registry.get(name)
                    tool_call = ToolCall(
                        call_id=call["id"], name=name, args=args, spec=spec,
                        label=spec.label if spec else name,
                        risk=spec.risk if spec else "low",
                    )
                    # 幂等：同一 (turn_id, step, call_id) 已执行过则回放结果，不重复产生副作用
                    saved = self.tasks.saved_tool(turn_id, step, call["id"])
                    # 回放也要过能力边界。回放本身不执行 handler，但会把旧结果
                    # 喂给模型；能力集收紧后，让模型以为越权工具曾执行成功是
                    # 错误行为。此处显式 deny + 留痕，且**不改写** tool_executions
                    # 里的历史记录（那是已发生的事实，篡改会破坏幂等账本）。
                    replay_action, replay_guard = self.replay_decision(name, skill, saved is not None)
                    if replay_action == "deny":
                        result = ToolResult("denied", replay_guard)
                        yield emit(
                            "tool_result", call_id=call["id"], name=name,
                            status="denied", output=result.output, latency_ms=0,
                        )
                        await self.traces.add_event(
                            trace_id, "capability_denied", name=name,
                            payload={"call_id": call["id"], "skill": skill,
                                     "reason": replay_guard, "replay_blocked": True},
                        )
                        self.db.add_audit(
                            actor=user_id, action=f"tool.{name}.denied",
                            target_type="tool", target_id=call["id"],
                            before={"args": args},
                            after={"status": "denied", "decision": "capability_guard",
                                   "replay_blocked": True},
                        )
                        self.tree.append(
                            session_id=session_id, parent_id=assistant_entry["entry_id"], role="tool",
                            content=_tool_message(result), tool_call_id=call["id"], name=name,
                        )
                        continue
                    if saved:
                        result = ToolResult(
                            saved["status"], saved["output"] or "", json.loads(saved.get("data") or "{}"),
                        )
                        yield emit(
                            "tool_result", call_id=call["id"], name=name,
                            status=result.status, output=result.output, latency_ms=0, replay=True,
                        )
                        await self.traces.add_event(
                            trace_id, "tool_replay", name=name,
                            payload={"call_id": call["id"], "turn_id": turn_id, "step": step},
                        )
                        self.tree.append(
                            session_id=session_id, parent_id=assistant_entry["entry_id"], role="tool",
                            content=_tool_message(result), tool_call_id=call["id"], name=name,
                            card=(result.data or {}).get("card"),
                        )
                        continue
                    async for event in self.pipeline.run(
                        tool_call, user_id=user_id, session_id=session_id,
                        trace_id=trace_id, skill=skill, emit=emit,
                    ):
                        yield event

                    outcome = tool_call.result
                    if outcome is None:  # 理论上不会发生
                        continue
                    result = ToolResult(outcome.status, outcome.output, outcome.data)
                    self.tasks.save_tool(
                        turn_id=turn_id, step=step, call_id=call["id"], name=name,
                        status=result.status, output=result.output, data=outcome.data,
                    )
                    self.tree.append(
                        session_id=session_id, parent_id=assistant_entry["entry_id"], role="tool",
                        content=_tool_message(result), tool_call_id=call["id"], name=name,
                        card=(outcome.data or {}).get("card"),
                    )

                # 用最新树内容重建上下文（必要时压缩），进入下一轮
                messages, info = await self._prepare_context(
                    session_id=session_id, skill=skill, slots=slots, trace_id=trace_id,
                )
                if info:
                    yield emit("compaction", **info)
                self.tasks.save_checkpoint(
                    turn_id=turn_id, step=step + 1, skill=skill,
                    messages=messages, slots=slots,
                    tool_calls=tool_calls_count, tokens_in=tokens_in, tokens_out=tokens_out,
                    **self._checkpoint_capability(),
                )
            else:
                stop_reason = "max_steps"
                final_text = f"已达到最大循环次数（{self.settings.max_loop}），请把任务拆小一点再试。"

            # 6) 收尾
            if not final_text:
                final_text = "已完成。"
            if not any(m.get("role") == "assistant" and m.get("content") == final_text for m in messages[-2:]):
                self.tree.append(session_id=session_id, parent_id=None, role="assistant", content=final_text)
            yield emit("status", phase="final", text="生成回复")
            latency_ms = int((time.monotonic() - started) * 1000)
            cost_usd = (
                tokens_in / 1_000_000 * self.settings.price_in_per_mtok
                + tokens_out / 1_000_000 * self.settings.price_out_per_mtok
            )
            await self.traces.finish_trace(
                trace_id, "success", latency_ms=latency_ms, tool_calls=tool_calls_count,
                tokens_in=tokens_in, tokens_out=tokens_out,
            )
            await self.traces.add_event(
                trace_id, "turn_end", name="任务完成",
                payload={"latency_ms": latency_ms, "stop_reason": stop_reason},
            )
            await self.state.update(session_id, status="idle")
            await self._fsm(session_id=session_id, event="finish", user_id=user_id, trace_id=trace_id)
            self.tasks.finish(turn_id, "done", stop_reason=stop_reason, step=last_step)
            log.info(
                "turn done trace=%s session=%s user=%s skill=%s stop=%s latency_ms=%s tools=%s tokens_in=%s tokens_out=%s",
                trace_id, session_id, user_id, skill, stop_reason, latency_ms,
                tool_calls_count, tokens_in, tokens_out,
            )
            yield emit(
                "done", trace_id=trace_id, latency_ms=latency_ms, skill=skill,
                tool_calls=tool_calls_count, tokens={"in": tokens_in, "out": tokens_out},
                stop_reason=stop_reason, cost_usd=round(cost_usd, 6),
            )
            finished = True
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed trace=%s session=%s user=%s skill=%s", trace_id, session_id, user_id, skill)
            await self._fsm(session_id=session_id, event="fail", user_id=user_id, trace_id=trace_id)
            self.tasks.finish(turn_id, "failed", error=str(exc))
            await self.traces.add_event(trace_id, "error", name="error", status="error", payload={"message": str(exc)})
            await self.traces.finish_trace(trace_id, "failed", error=str(exc), latency_ms=int((time.monotonic() - started) * 1000))
            yield emit("error", message=str(exc), code="INTERNAL_ERROR")
            finished = True
        finally:
            if not finished:
                log.warning("turn aborted trace=%s session=%s user=%s (client disconnected)", trace_id, session_id, user_id)
                await self._fsm(session_id=session_id, event="fail", user_id=user_id, trace_id=trace_id)
                self.tasks.finish(turn_id, "interrupted", error="client_disconnected")
                await self.approvals.cancel_by_trace(trace_id)
                await self.traces.finish_trace(
                    trace_id, "failed", error="client_disconnected",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            await self.state.update(session_id, status="idle")
            await self.state.unlock(session_id)
