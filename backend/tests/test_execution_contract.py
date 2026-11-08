"""执行契约（ExecutionContract）与能力边界。

对应审计 §4.2 第 3 点与 G1：
- guard 不再是「配置里没写白名单就恒真」，未知工具一律拒绝；
- guard 校验**工具归属当前技能**，堵住「在某技能里调别的技能的工具」；
- 策略引擎必经，扩展 hook 只能加严不能放宽；
- role 缺失 fail-closed；
- Turn 级快照：reload 不影响在途 Turn。
"""
from __future__ import annotations

import app.skills.tools  # noqa: F401  注册内置工具
import pytest
from app.agent.events import EventFactory
from app.agent.hooks import BaseToolHook, HookDecision, ToolCall
from app.agent.pipeline import ToolPipeline
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.domain.tasks import TaskStore
from app.orchestrator.loop import _TURN_SESSION, Orchestrator
from app.policy.engine import PolicyEngine
from app.runtime import ResourceLoader, issue_contract
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree
from app.skills.registry import ToolSpec, registry
from app.trace.store import TraceStore


def make_settings(tmp_path) -> Settings:
    return Settings(
        sqlite_path=str(tmp_path / "contract.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=1,
    )


@pytest.fixture()
def loader() -> ResourceLoader:
    return ResourceLoader(registry)


# --------------------------------------------------------------------------- #
# 能力边界
# --------------------------------------------------------------------------- #
def test_unknown_tool_is_denied_even_without_allowlist(loader):
    """默认 visit 配置没有 allowlist，但**未注册的工具**必须被拒绝。

    旧实现 `spec.tools is None -> True`，模型幻觉出的工具名能一路穿到执行期。
    """
    session = loader.create_session("visit")
    ok, rule, _ = session.guard_tool("totally_fake_tool", "chat")
    assert ok is False
    assert rule == "tool_not_registered"


def test_cross_skill_tool_is_denied(loader):
    """在 visit_log 技能里调签到工具：Agent 级白名单放行，但归属不符，必须拒绝。"""
    session = loader.create_session("visit")
    ok, rule, reason = session.guard_tool("checkin", "visit_log")
    assert ok is False
    assert rule == "cross_skill_denied"
    assert "归属技能 checkin" in reason


def test_ops_readonly_cannot_use_write_tools(loader):
    session = loader.create_session("ops_readonly")
    ok, rule, _ = session.guard_tool("cancel_visit_plan", "plan_cancel")
    assert ok is False
    assert rule in ("skill_not_authorized", "not_in_agent_capability")


def test_disabled_tool_is_denied(loader):
    session = loader.create_session("visit")
    registry.set_enabled("checkin", False)
    try:
        ok, rule, _ = session.guard_tool("checkin", "checkin")
        assert ok is False
        assert rule == "tool_disabled"
    finally:
        registry.set_enabled("checkin", True)


def test_contract_is_immutable():
    contract = issue_contract(
        turn_id="t1", step=2, call_id="c1", agent_name="visit",
        capability_fingerprint="f" * 64, active_skill="checkin",
        tool_name="checkin", user_id="sales-001", role="sales",
    )
    with pytest.raises(Exception):
        contract.tool_name = "cancel_visit_plan"  # type: ignore[misc]
    assert contract.idem_key == "t1:2:c1"


# --------------------------------------------------------------------------- #
# 策略引擎：fail-closed + 必经
# --------------------------------------------------------------------------- #
def test_policy_denies_when_role_missing(tmp_path):
    settings = make_settings(tmp_path)
    policy = PolicyEngine(registry, settings)
    decision = policy.evaluate(user_id="sales-001", name="query_todos", args={})
    assert decision.action == "deny"
    assert decision.rule == "identity_unverified"


def test_policy_denies_skill_mismatch(tmp_path):
    settings = make_settings(tmp_path)
    policy = PolicyEngine(registry, settings)
    decision = policy.evaluate(
        user_id="sales-001", role="sales", name="checkin",
        args={"distance_m": 100}, active_skill="visit_log",
    )
    assert decision.action == "deny"
    assert decision.rule == "skill_mismatch"


class _AllowAll(BaseToolHook):
    """一个「什么都放行」的扩展点：用来验证 hook 不能替策略做决定。"""

    name = "allow_all"

    async def before_tool_call(self, call: ToolCall):  # noqa: D401
        return HookDecision(action="allow", risk="low", reason="hook allows", rule="hook_allow")


async def test_hook_cannot_override_policy_deny(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.sqlite_path, settings.demo_password)
    db.init()
    db.seed_demo()
    tree = SessionTree(db)
    state = SessionState(settings)
    traces = TraceStore(settings.sqlite_path)
    traces.init()

    # 临时注册一个「仅主管可用」的工具，挂在 todo_query 技能下：
    # guard 放行（归属匹配、已注册），能不能调交给策略判定。
    async def handler(args, ctx):
        return None

    registry._tools["manager_only_probe"] = ToolSpec(  # noqa: SLF001
        name="manager_only_probe", label="仅主管", description="probe",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler, risk="low", scope="read", roles=("manager",),
        skill="todo_query",
    )
    try:
        pipeline = ToolPipeline(
            settings=settings, registry=registry, policy=PolicyEngine(registry, settings),
            approvals=ApprovalManager(traces, timeout_s=1), traces=traces,
            db=db, tree=tree, state=state, hooks=None,
        )
        pipeline.hooks.add(_AllowAll())
        trace_id = await traces.start_trace("s1", "sales-001", "hook 越权")
        call = ToolCall(
            call_id="c1", name="manager_only_probe", args={},
            spec=registry.get("manager_only_probe"),
        )
        emit = EventFactory()
        async for _event in pipeline.run(
            call, user_id="sales-001", session_id="s1", trace_id=trace_id,
            skill="todo_query", emit=emit,
        ):
            pass
        assert call.result is not None
        assert call.result.status == "denied", "hook 的 allow 不得覆盖策略的 deny"
        assert call.result.decision == "deny"
    finally:
        registry._tools.pop("manager_only_probe", None)


# --------------------------------------------------------------------------- #
# Turn 级快照：reload 不影响在途 Turn
# --------------------------------------------------------------------------- #
async def test_turn_snapshot_survives_reload(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.sqlite_path, settings.demo_password)
    db.init()
    db.seed_demo()
    tree = SessionTree(db)
    state = SessionState(settings)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    tasks = TaskStore(db)
    loader = ResourceLoader(registry)
    visit = loader.create_session("visit")
    ops = loader.create_session("ops_readonly")

    orchestrator = Orchestrator(
        settings=settings, db=db, tree=tree, state=state, registry=registry,
        policy=PolicyEngine(registry, settings), llm=None,  # type: ignore[arg-type]
        traces=traces, approvals=ApprovalManager(traces, 1), tasks=tasks,
        session=visit,
    )
    # Turn 开始时快照的是 visit
    token = _TURN_SESSION.set(visit)
    try:
        assert orchestrator._guard_tool("cancel_visit_plan", "plan_cancel") is None
        # 模拟 reload：全局装配换成只读 Agent
        orchestrator.set_session(ops)
        # 在途 Turn 仍按快照放行（能力不变），新 Turn 才会收紧
        assert orchestrator._guard_tool("cancel_visit_plan", "plan_cancel") is None
    finally:
        _TURN_SESSION.reset(token)
    # 快照结束后回到新装配：同样的调用被拒
    denied = orchestrator._guard_tool("cancel_visit_plan", "plan_cancel")
    assert denied is not None and "CAPABILITY_DENIED" in denied
