"""编排核心：Turn 状态机 + ReAct 循环 + SSE 事件 + Trace。

对齐 Pi agent 的分层（见 docs/07）：
  - 事件：EventFactory 统一包络（seq/ts/elapsed_ms），SSE 与 Trace 同源；
  - 工具：ToolPipeline 负责「凭证 -> 策略 -> 钩子 -> 审批 -> 执行 -> 记账 -> 观测」；
  - 上下文：prompt.build_system_prompt（技能索引 + 当前技能）+ 超预算压缩；
  - 收敛：显式记录 stop_reason（no_tool_calls / max_steps）。

v0.7 的三处关键变化：
  1. **Turn 级能力快照**：`handle()` 开始时把 `AgentSession` 快照进 ContextVar，
     整个 Turn 只读这份快照。`/api/agents/reload` 只影响新 Turn，在途 Turn 不会
     中途换权限（旧实现全程读 `self.session`，reload 会造成权限提升）。
  2. **执行凭证**：每次工具调用签发 `ExecutionContract`，把能力指纹带进
     guard / 策略 / 审批 / 幂等账本，与 checkpoint 绑定的指纹同源。
  3. **审批断点可接回**：任务行记录 `pending_approval_*`，resume 时先查审批单终态，
     再决定继续 / 拒绝 / 继续等待，而不是盲目重跑 ReAct。

一次 handle() 对应一个 Turn：路由 → 槽位 → 工具循环（含审批）→ 回复 → done。
"""
from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator

from app.agent.compaction import compact_if_needed
from app.agent.events import EventFactory
from app.agent.hooks import ToolCall
from app.agent.output_guard import OutputGovernor
from app.agent.pipeline import ToolPipeline
from app.approvals import ApprovalManager
from app.config import PROJECT_DIR, Settings
from app.domain.db import Database
from app.domain.fsm import SessionFSM
from app.domain.idempotency import IdempotencyStore
from app.domain.tasks import TaskStore
from app.llm.base import BaseLLM
from app.memory.layers import load_layers, render_layers
from app.memory.recall import pinned, recall, render_recall
from app.memory.store import MemoryStore
from app.orchestrator.router import RouteResult, build_slots, route_card, route_text_hybrid
from app.policy.engine import PolicyEngine
from app.runtime import AgentSession, default_session, issue_contract
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree
from app.skills.registry import ToolRegistry, ToolResult
from app.trace.store import TraceStore
from app.utils import new_id

log = logging.getLogger(__name__)

#: 当前 Turn 的能力快照。contextvar 而非实例属性：同一进程并发处理多个 Turn 时
#: 互不干扰（每个 asyncio task 有独立的 context）。
_TURN_SESSION: ContextVar[AgentSession | None] = ContextVar("turn_session", default=None)


def _tool_message(result: ToolResult) -> str:
    return json.dumps(
        {"status": result.status, "output": result.output, "data": result.data or {}},
        ensure_ascii=False,
    )


def _flag(value: Any) -> bool:
    """会话 meta 在 Redis 后端里全部被 str() 化，`"False"` 是真值串。

    直接 `if meta.get(x)` 会把「上一轮没追问」读成「追问过」，所以统一过一遍。
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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
        idem: IdempotencyStore | None = None,
        memories: MemoryStore | None = None,
        governor: OutputGovernor | None = None,
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
        self.idem = idem
        self.memories = memories
        self.session_fsm = SessionFSM(db, state)
        self.pipeline = ToolPipeline(
            settings=settings, registry=registry, policy=policy,
            approvals=approvals, traces=traces, db=db, tree=tree, state=state,
            knowledge=knowledge, tool_guard=self._guard_tool,
            tasks=tasks, idem=idem, governor=governor,
        )

    # ------------------------------------------------------------------ #
    # 能力边界（执行期硬校验）
    # ------------------------------------------------------------------ #
    def _session(self) -> AgentSession:
        """Turn 快照优先；不在 Turn 内（如管理接口）回落到当前装配。"""
        return _TURN_SESSION.get() or self.session

    def _guard_tool(self, tool_name: str, active_skill: str) -> str | None:
        """执行期能力边界：模型吐出装配白名单外的工具名也不会被执行。

        v0.7 起三条硬约束（见 `AgentSession.guard_tool`）：技能已授权、工具归属
        当前技能、工具在授权集内且未停用。旧实现只查「Agent 级白名单」，
        默认 `visit` 配置下 `allows_tool()` 恒真，等于 guard 空转 —— 审计点名项。
        """
        ok, rule, reason = self._session().guard_tool(tool_name, active_skill)
        if ok:
            return None
        return f"CAPABILITY_DENIED[{rule}]: {reason}"

    def replay_decision(self, tool_name: str, skill: str, has_saved: bool) -> tuple[str, str | None]:
        """幂等回放是否允许消费旧结果。

        生效场景：legacy 检查点被兼容放行、同一个 turn_id 被重放、或 Turn 执行
        中途发生 Agent 热更新。跨能力集的正常 resume 已由 check_resume_compat
        提前拒绝，走不到这里。
        """
        if not has_saved:
            return ("execute", None)
        reason = self._guard_tool(tool_name, skill)
        return ("deny", reason) if reason else ("replay", None)

    def _checkpoint_capability(self, session: AgentSession | None = None) -> dict[str, Any]:
        """每次写检查点都带上本次装配的能力快照。"""
        session = session or self._session()
        return {
            "agent_name": session.name,
            "capability_fingerprint": session.capability_fingerprint(),
            "capability": session.capability(),
        }

    def check_resume_compat(
        self, checkpoint: dict[str, Any], session: AgentSession | None = None
    ) -> dict[str, Any]:
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
        session = session or self.session
        recorded = checkpoint.get("capability_fingerprint")
        current = session.capability_fingerprint()
        base = {
            "recorded_agent": checkpoint.get("agent_name"),
            "recorded_fingerprint": recorded,
            "current_agent": session.name,
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
                f"当前装配为 {session.name}（{current[:12]}…）。"
                "能力集已变更，拒绝跨能力集续跑；请恢复原配置后重试，或以当前 Agent 重新发起。"
            ),
            "recorded_capability": checkpoint.get("capability") or {},
            "current_capability": session.capability(),
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
        session: AgentSession,
        session_id: str,
        skill: str,
        slots: dict[str, Any],
        trace_id: str,
        user_id: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        info = await compact_if_needed(
            tree=self.tree, session_id=session_id, settings=self.settings,
            summarizer=self._summarizer(),
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
        memory_block = await self._memory_block(
            session=session, session_id=session_id, user_id=user_id, trace_id=trace_id,
        )
        system = session.system_prompt(
            active_skill=skill, slots=slots, entity=entity, memory_block=memory_block,
        )
        messages = [{"role": "system", "content": system}] + self.tree.messages(
            session_id, self.settings.context_limit
        )
        return messages, info

    def _summarizer(self):
        """压缩摘要器：真模型优先，失败回退确定性摘要（离线/无 Key 时行为不变）。"""
        if not self.settings.llm_summary:
            return None
        from app.agent.summarize import build_summarizer

        return build_summarizer(self.llm, self.settings)

    async def _memory_block(
        self, *, session: AgentSession, session_id: str, user_id: str, trace_id: str
    ) -> str:
        """记忆注入：静态层（AGENTS.md 分层）+ 动态层（按当前输入召回）。

        两层都带来源：静态层标文件路径，动态层标 memory_id 与会话 id。没有来源
        的记忆等于模型幻觉，事后无法区分「它记住了」和「它编了一个」。
        """
        if not self.settings.enable_memory:
            return ""
        parts: list[str] = []

        layers = load_layers(
            PROJECT_DIR, session.name,
            memory_dir=self.settings.memory_dir,
            max_chars=self.settings.memory_layer_max_chars,
        )
        static = render_layers(layers)
        if static:
            parts.append(static)

        if self.memories is not None and user_id:
            query = self._last_user_text(session_id)
            # 两路注入：偏好常驻（不依赖措辞命中）+ 事实召回（按当前输入打分）
            always = pinned(self.memories, user_id=user_id, agent_name=session.name, limit=3)
            items = recall(
                self.memories, query=query, user_id=user_id, agent_name=session.name,
                k=self.settings.memory_recall_k, min_score=self.settings.memory_min_score,
            )
            if always:
                parts.append(render_recall(always, title="记忆（动态层，偏好常驻）"))
            if items:
                parts.append(render_recall(items))
            if always or items:
                await self.traces.add_event(
                    trace_id, "memory_recall", name=f"pinned:{len(always)}+recalled:{len(items)}",
                    payload={
                        "pinned": [item["memory_id"] for item in always],
                        "ids": [item["memory_id"] for item in items],
                        "scores": [item["score"] for item in items],
                        "matched": [item["matched"] for item in items],
                    },
                )
        return "\n\n".join(parts)

    def _last_user_text(self, session_id: str) -> str:
        for item in reversed(self.tree.path(session_id, self.settings.context_limit)):
            if item.get("role") == "user":
                return str(item.get("content") or "")
        return ""

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
    # 主入口
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
        # 本轮是否以「追问用户补槽位」结束。只有这种情形下，下一句才有资格被
        # 当成槽位补充、沿用上一轮的子 Skill（见 2.1）。
        awaiting_slots = False
        step_start = 0
        last_step = 0

        # Turn 级快照：整个 Turn 只读这一份能力视图
        turn_session = self.session
        token_ctx = _TURN_SESSION.set(turn_session)
        capability_fp = turn_session.capability_fingerprint()
        user_row = self.db.get_user(user_id) or {}
        role = str(user_row.get("role") or "")

        try:
            yield emit("start", session_id=session_id, trace_id=trace_id, user_id=user_id)
            await self.traces.add_event(trace_id, "turn_start", name="任务开始", payload={"text": user_text})

            if resume:
                compat = self.check_resume_compat(resume, turn_session)
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

                # ---- 审批断点接回 ------------------------------------------------ #
                task_row = self.tasks.get(turn_id) or {}
                pending_id = task_row.get("pending_approval_id")
                if pending_id:
                    row = await self.traces.get_approval(pending_id) or {}
                    status = row.get("status") or "cancelled"
                    await self.traces.add_event(
                        trace_id, "approval_resume", name=status,
                        payload={"approval_id": pending_id, "status": status},
                    )
                    if status == "pending":
                        # 单子还没人处理：不重跑 ReAct，直接告诉调用方继续等
                        self.tasks.update(turn_id, status="awaiting_approval")
                        yield emit("status", phase="approval", text="仍在等待主管审批")
                        yield emit(
                            "error", message="审批仍在等待中，请主管处理后再次 resume",
                            code="APPROVAL_STILL_PENDING",
                        )
                        finished = True
                        return
                    pending_step = int(task_row.get("pending_step") or 0)
                    pending_call = task_row.get("pending_call_id") or f"resume_{new_id('c')}"
                    pending_tool = task_row.get("pending_tool") or row.get("tool") or ""
                    pending_args = row.get("args") or {}
                    spec = self.registry.get(pending_tool)
                    tool_call = ToolCall(
                        call_id=pending_call, name=pending_tool, args=pending_args, spec=spec,
                        label=spec.label if spec else pending_tool,
                        risk=spec.risk if spec else "high",
                    )
                    contract = issue_contract(
                        turn_id=turn_id, step=pending_step, call_id=pending_call,
                        agent_name=turn_session.name, capability_fingerprint=capability_fp,
                        active_skill=skill, tool_name=pending_tool,
                        user_id=user_id, role=role,
                    )
                    if status == "approved":
                        async for event in self.pipeline.run(
                            tool_call, user_id=user_id, session_id=session_id,
                            trace_id=trace_id, skill=skill, emit=emit,
                            contract=contract, preapproved=True,
                        ):
                            yield event
                        outcome = tool_call.result
                        result = (
                            ToolResult(outcome.status, outcome.output, outcome.data)
                            if outcome else ToolResult("error", "审批通过但执行结果缺失")
                        )
                    else:
                        result = ToolResult(
                            "rejected" if status == "rejected" else "timeout",
                            "REJECTED_BY_HUMAN" if status == "rejected" else f"APPROVAL_{status.upper()}",
                        )
                        yield emit(
                            "tool_result", call_id=pending_call, name=pending_tool,
                            status=result.status, output=result.output, latency_ms=0,
                        )
                    tool_calls_count += 1
                    self.tasks.clear_awaiting(turn_id)
                    self.tree.append(
                        session_id=session_id, parent_id=None, role="tool",
                        content=_tool_message(result), tool_call_id=pending_call,
                        name=pending_tool, card=(result.data or {}).get("card"),
                    )
                    step_start = pending_step + 1
                    last_step = step_start
                    messages, info = await self._prepare_context(
                        session=turn_session, session_id=session_id, skill=skill,
                        slots=slots, trace_id=trace_id, user_id=user_id,
                    )
                    if info:
                        yield emit("compaction", **info)
                    self.tasks.save_checkpoint(
                        turn_id=turn_id, step=step_start, skill=skill, messages=messages,
                        slots=slots, tool_calls=tool_calls_count,
                        tokens_in=tokens_in, tokens_out=tokens_out,
                        **self._checkpoint_capability(turn_session),
                    )
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
                        allowed=turn_session.skill_names(),
                    )
                else:
                    route = await route_text_hybrid(
                        self.db, user_text, self.llm, self.settings,
                        labels=list(turn_session.skill_names()),
                    )
                    route.slots.update(build_slots(self.db, user_text))
                routed_skill = route.skill
                skill = turn_session.resolve_route(routed_skill)
                if skill != routed_skill:
                    await self.traces.add_event(
                        trace_id, "capability_denied", name=routed_skill,
                        payload={"agent": turn_session.name, "requested": routed_skill, "fallback": skill},
                    )

                # 2.1) 槽位补充轮：上一轮问了问题（need_input）时，这一句优先当成回答
                meta = await self.state.meta(session_id) or {}
                last_skill = meta.get("last_skill")
                # 判据只有一条：**上一轮是否以追问结束**（awaiting_slots）。
                # 旧实现是「技能=chat 且历史槽位非空」，两头都错：
                #  - 槽位写入后永不清空 → 同一会话里「你好，你是谁」被拽回上一轮的
                #    plan_cancel，真的发起取消审批（评测 route-chat-fallback 打出来的）；
                #  - 反过来，追问时槽位恰好是空的 → 用户答了也接不上，任务静默丢失。
                # 已知边界：追问之后用户如果只是闲聊，会被当成回答（最坏是重复追问
                # 一遍）。纯文本上「闲聊」与「补信息」不可区分，这一层要靠意图分类
                # 收口；在规则/mock 链路上，重复追问比丢任务更可接受。
                if (
                    not card
                    and skill == "chat"
                    and _flag(meta.get("awaiting_slots"))
                    and last_skill in turn_session.skills
                ):
                    route = RouteResult(last_skill, 0.6, "context", route.slots, "pending_slots")
                    skill = route.skill
                await self.state.update(session_id, last_skill=skill)
                await self._fsm(session_id=session_id, event="route", user_id=user_id, trace_id=trace_id)
                yield emit("route", skill=skill, confidence=route.confidence, source=route.source, label=turn_session.label_of(skill))
                await self.traces.add_event(
                    trace_id, "route", name=route.source,
                    payload={"skill": skill, "confidence": route.confidence, "source": route.source},
                )
                if skill in turn_session.skills:
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
                    session=turn_session, session_id=session_id, skill=skill,
                    slots=slots, trace_id=trace_id, user_id=user_id,
                )
                if info:
                    yield emit("compaction", **info)
                # 初始检查点：进程在此后任一时刻崩溃都能从上下文恢复
                self.tasks.save_checkpoint(
                    turn_id=turn_id, step=0, skill=skill, messages=messages, slots=slots,
                    **self._checkpoint_capability(turn_session),
                )

            tool_names = [t.name for t in turn_session.tools_for(skill)] if skill in turn_session.skills else []
            tools = self.registry.openai_tools(tool_names)

            # 5) ReAct 循环（Pi：while(true) + 显式收敛条件）
            final_text = ""
            for step in range(step_start, self.settings.max_loop):
                last_step = step
                await self.traces.add_event(trace_id, "llm_call", name=self.llm.name, payload={"step": step})
                text_parts: list[str] = []
                raw_calls: list[dict[str, str]] = []
                async for chunk in self.llm.stream(messages, tools):
                    if chunk.type == "text_delta":
                        text_parts.append(chunk.text)
                        yield emit("delta", text=chunk.text)
                    elif chunk.type == "usage":
                        tokens_in += chunk.usage.get("prompt_tokens", 0)
                        tokens_out += chunk.usage.get("completion_tokens", 0)
                    elif chunk.type == "tool_calls":
                        raw_calls = chunk.tool_calls

                if not raw_calls:
                    final_text = "".join(text_parts).strip() or "我没太理解，可以换个说法吗？"
                    stop_reason = "no_tool_calls"
                    break

                # assistant(tool_calls) + 每个 tool 结果都必须成对写入
                assistant_entry = self.tree.append(
                    session_id=session_id, parent_id=None, role="assistant",
                    content="".join(text_parts), tool_calls=raw_calls,
                )
                for call in raw_calls:
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
                    if saved and saved.get("status") not in ("running", "unknown"):
                        result = ToolResult(
                            saved["status"], saved["output"] or "",
                            json.loads(saved.get("data") or "{}"),
                        )
                        yield emit(
                            "tool_result", call_id=call["id"], name=name,
                            status=result.status, output=result.output, latency_ms=0, replay=True,
                        )
                        if result.status == "need_input":
                            awaiting_slots = True
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
                    if saved and saved.get("status") in ("running", "unknown"):
                        # 上次执行到一半进程就没了：副作用状态未知。
                        await self.traces.add_event(
                            trace_id, "tool_attempt_orphaned", name=name,
                            payload={"call_id": call["id"], "status": saved.get("status"),
                                     "action": "re-execute_with_idempotency"},
                        )

                    contract = issue_contract(
                        turn_id=turn_id, step=step, call_id=call["id"],
                        agent_name=turn_session.name, capability_fingerprint=capability_fp,
                        active_skill=skill, tool_name=name, user_id=user_id, role=role,
                    )
                    async for event in self.pipeline.run(
                        tool_call, user_id=user_id, session_id=session_id,
                        trace_id=trace_id, skill=skill, emit=emit, contract=contract,
                    ):
                        yield event

                    outcome = tool_call.result
                    if outcome is None:  # 理论上不会发生
                        continue
                    result = ToolResult(outcome.status, outcome.output, outcome.data)
                    if result.status == "need_input":
                        awaiting_slots = True
                    # 幂等账本（tool_executions）已由 ToolPipeline 在 claim/complete
                    # 中写入，且**先声明后执行**；这里不再二次写，避免两处真相。
                    self.tree.append(
                        session_id=session_id, parent_id=assistant_entry["entry_id"], role="tool",
                        content=_tool_message(result), tool_call_id=call["id"], name=name,
                        card=(outcome.data or {}).get("card"),
                    )

                # 用最新树内容重建上下文（必要时压缩），进入下一轮
                messages, info = await self._prepare_context(
                    session=turn_session, session_id=session_id, skill=skill,
                    slots=slots, trace_id=trace_id, user_id=user_id,
                )
                if info:
                    yield emit("compaction", **info)
                self.tasks.save_checkpoint(
                    turn_id=turn_id, step=step + 1, skill=skill,
                    messages=messages, slots=slots,
                    tool_calls=tool_calls_count, tokens_in=tokens_in, tokens_out=tokens_out,
                    **self._checkpoint_capability(turn_session),
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
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
            )
            await self.traces.add_event(
                trace_id, "turn_end", name="任务完成",
                payload={"latency_ms": latency_ms, "stop_reason": stop_reason},
            )
            await self.state.update(session_id, status="idle", awaiting_slots=awaiting_slots)
            await self._fsm(session_id=session_id, event="finish", user_id=user_id, trace_id=trace_id)
            self.tasks.finish(turn_id, "done", stop_reason=stop_reason, step=last_step)
            # 结构化日志：一条一行 JSON，字段固定，便于采集与告警
            log.info(
                "turn_done",
                extra={
                    "event": "turn_done", "trace_id": trace_id, "turn_id": turn_id,
                    "session_id": session_id, "user_id": user_id, "skill": skill,
                    "stop_reason": stop_reason, "latency_ms": latency_ms,
                    "tool_calls": tool_calls_count, "tokens_in": tokens_in,
                    "tokens_out": tokens_out, "agent": turn_session.name,
                    "capability_fingerprint": capability_fp[:16],
                },
            )
            yield emit(
                "done", trace_id=trace_id, latency_ms=latency_ms, skill=skill,
                tool_calls=tool_calls_count, tokens={"in": tokens_in, "out": tokens_out},
                stop_reason=stop_reason, cost_usd=round(cost_usd, 6),
            )
            finished = True
        except Exception as exc:  # noqa: BLE001
            # 异常原文可能包含 SQL 片段、文件路径、密钥等内部细节，只留在服务端日志，
            # 给客户端一个可引用的错误号（trace_id）即可定位。
            error_id = new_id("err")
            log.exception(
                "turn_failed err=%s trace=%s session=%s user=%s", error_id, trace_id, session_id, user_id,
            )
            await self._fsm(session_id=session_id, event="fail", user_id=user_id, trace_id=trace_id)
            self.tasks.finish(turn_id, "failed", error=f"{type(exc).__name__}:{error_id}")
            await self.traces.add_event(
                trace_id, "error", name="error", status="error",
                payload={"error_id": error_id, "type": type(exc).__name__},
            )
            await self.traces.finish_trace(
                trace_id, "failed", error=f"{type(exc).__name__}:{error_id}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            yield emit(
                "error", message=f"服务内部错误，请把 trace_id={trace_id} 反馈给管理员",
                code="INTERNAL_ERROR",
            )
            finished = True
        finally:
            _TURN_SESSION.reset(token_ctx)
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
