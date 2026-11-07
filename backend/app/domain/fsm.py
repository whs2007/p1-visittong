"""业务状态机（FSM）：拜访计划与会话。

计划：new → planned → checked_in → checked_out → done
      planned / checked_in / checked_out → cancelled（高危取消需先走人工审批）
      planned --update--> planned（改约，字段变更另记 plan_change_log）

会话：idle → routing → executing → done；异常 → failed

所有迁移都写入 `state_transitions`（谁、何时、从哪到哪、事件、关联 Trace），
可通过 `GET /api/plans/{id}` 的 transitions 字段查看，避免"状态只存在代码里"。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.db import Database
from app.sessions.store import SessionState
from app.utils import new_id

PLAN_STATES = ("new", "planned", "checked_in", "checked_out", "done", "cancelled")
PLAN_TRANSITIONS: dict[tuple[str, str], str] = {
    ("planned", "checkin"): "checked_in",
    ("checked_in", "checkout"): "checked_out",
    ("checked_out", "log"): "done",
    ("checked_in", "log"): "done",
    ("planned", "update"): "planned",
    ("planned", "cancel"): "cancelled",
    ("checked_in", "cancel"): "cancelled",
    ("checked_out", "cancel"): "cancelled",
}

SESSION_STATES = ("idle", "routing", "executing", "done", "failed")
SESSION_TRANSITIONS: dict[tuple[str, str], str] = {
    ("idle", "start"): "routing",
    ("done", "start"): "routing",
    ("failed", "start"): "routing",
    ("routing", "route"): "executing",
    ("routing", "finish"): "done",
    ("executing", "finish"): "done",
    ("routing", "fail"): "failed",
    ("executing", "fail"): "failed",
    # 断点续跑：崩溃时可能停在 routing/executing，resume 从原状态继续
    ("routing", "resume"): "routing",
    ("executing", "resume"): "executing",
    ("done", "resume"): "routing",
}


@dataclass(frozen=True)
class Transition:
    ok: bool
    entity_type: str
    entity_id: str
    event: str
    from_state: str
    to_state: str
    reason: str = ""


class FSM:
    """计划状态机：校验迁移、落库、写审计。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def state(self, plan_id: str) -> str | None:
        plan = self.db.get_plan(plan_id)
        return plan["status"] if plan else None

    def can(self, plan_id: str, event: str) -> Transition:
        plan = self.db.get_plan(plan_id)
        if plan is None:
            return Transition(False, "plan", plan_id, event, "", "", "计划不存在")
        current = plan["status"]
        target = PLAN_TRANSITIONS.get((current, event))
        if target is None:
            return Transition(
                False, "plan", plan_id, event, current, "", f"当前状态 {current} 不允许事件 {event}"
            )
        return Transition(True, "plan", plan_id, event, current, target)

    def plan(
        self, plan_id: str, event: str, *, actor: str = "", trace_id: str | None = None
    ) -> Transition:
        check = self.can(plan_id, event)
        if not check.ok:
            return check
        if check.to_state != check.from_state:
            self.db.set_plan_status(plan_id, check.to_state)
        self.record(
            entity_type="plan",
            entity_id=plan_id,
            from_state=check.from_state,
            to_state=check.to_state,
            event=event,
            actor=actor,
            trace_id=trace_id,
        )
        return check

    def record(
        self,
        *,
        entity_type: str,
        entity_id: str,
        from_state: str,
        to_state: str,
        event: str,
        actor: str = "",
        trace_id: str | None = None,
    ) -> None:
        self.db.add_state_transition(
            transition_id=new_id("st"),
            entity_type=entity_type,
            entity_id=entity_id,
            from_state=from_state,
            to_state=to_state,
            event=event,
            actor=actor,
            trace_id=trace_id,
        )

    def create(self, plan_id: str, *, actor: str = "", trace_id: str | None = None) -> Transition:
        """创建计划的初始迁移：new → planned（计划行由 db.create_plan 写入）。"""
        self.record(
            entity_type="plan",
            entity_id=plan_id,
            from_state="new",
            to_state="planned",
            event="create",
            actor=actor,
            trace_id=trace_id,
        )
        return Transition(True, "plan", plan_id, "create", "new", "planned")


class SessionFSM:
    """会话状态机：状态存 SessionState meta（Redis/内存），迁移写 state_transitions。"""

    def __init__(self, db: Database, state: SessionState) -> None:
        self.db = db
        self.state = state

    async def current(self, session_id: str) -> str:
        meta = await self.state.meta(session_id)
        return (meta or {}).get("fsm_state") or "idle"

    async def transition(
        self, session_id: str, event: str, *, actor: str = "", trace_id: str | None = None
    ) -> Transition:
        current = await self.current(session_id)
        target = SESSION_TRANSITIONS.get((current, event))
        if target is None:
            return Transition(
                False, "session", session_id, event, current, "", f"当前状态 {current} 不允许事件 {event}"
            )
        await self.state.update(session_id, fsm_state=target)
        self.db.add_state_transition(
            transition_id=new_id("st"),
            entity_type="session",
            entity_id=session_id,
            from_state=current,
            to_state=target,
            event=event,
            actor=actor,
            trace_id=trace_id,
        )
        return Transition(True, "session", session_id, event, current, target)