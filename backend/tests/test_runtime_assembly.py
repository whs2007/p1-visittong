"""Runtime 显式装配层：能力隔离、渐进披露、热更新、fail fast。

证明同一套 Orchestrator / ToolRegistry 可以装配出能力集不同的 Agent，
且未授权的技能与工具在装配期 + 路由期双重被裁掉。
"""
from __future__ import annotations

import asyncio
import json

import app.skills.tools  # noqa: F401  确保内置工具已注册到全局 registry
import pytest
from app.agent.events import EventFactory
from app.agent.hooks import ToolCall
from app.agent.pipeline import ToolPipeline
from app.api import routes_agents, routes_chat
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.domain.tasks import TaskStore
from app.errors import ApiError
from app.orchestrator.loop import Orchestrator
from app.orchestrator.router import route_card, route_text_hybrid
from app.policy.engine import PolicyEngine
from app.runtime import AgentSession, AgentSpec, ResourceLoader, default_session
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree, ensure_columns
from app.skills.registry import ToolResult, registry
from app.skills.visit_skills import SKILLS
from app.trace.store import TraceStore


def _config(path, *, name="custom", skills=("todo_query",), tools=("query_todos",)):
    payload = {
        "name": name,
        "label": "测试 Agent",
        "description": "装配层单测用",
        "skills": list(skills),
        "tools": list(tools) if tools is not None else None,
    }
    (path / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def test_default_session_matches_legacy_capabilities():
    """兜底装配必须与改造前的全局 SKILLS 完全等价（零回归）。"""
    session = default_session(registry)
    assert session.name == "visit"
    assert set(session.skills) == set(SKILLS)
    assert "cancel_visit_plan" in session.exposed_tool_names()
    assert session.route_labels() == [*SKILLS, "chat"]


def test_same_runtime_assembles_two_capability_sets():
    """核心：一套 Runtime，两套能力集，写工具只出现在全能力 Agent 里。"""
    loader = ResourceLoader(registry, load_files=False)
    visit = loader.create_session("visit")
    ops = loader.create_session("ops_readonly")

    assert visit.allows_tool("cancel_visit_plan")
    assert not ops.allows_tool("cancel_visit_plan")
    assert not any(t.scope == "write" for t in ops.tools_for("todo_query"))
    assert set(ops.exposed_tool_names()) == {"query_todos", "query_plans"}
    # 同一只读技能下，两套 Agent 拿到的 schema 一致（装配只裁剪边界）
    assert visit.openai_tools("todo_query") == ops.openai_tools("todo_query")


def test_capability_boundary_downgrades_unauthorized_skill():
    loader = ResourceLoader(registry, load_files=False)
    ops = loader.create_session("ops_readonly")
    assert ops.resolve_route("plan_cancel") == "chat"
    assert ops.resolve_route("todo_query") == "todo_query"
    assert ops.resolve_route("chat") == "chat"
    assert ops.tools_for("plan_cancel") == []
    assert "plan_cancel" not in ops.system_prompt(active_skill="todo_query")
    assert "[当前子 Skill: todo_query]" in ops.system_prompt(active_skill="todo_query")


def test_router_respects_scoped_labels():
    """路由层也走装配后的白名单：卡片与 LLM 分类都不能激活越权技能。"""
    result = route_card(None, "visit_plan", "cancel", "plan_cancel", {}, allowed=("todo_query",))
    assert result.skill == "chat"

    class _LLM:
        received: list[str] | None = None

        async def classify(self, text, labels):
            type(self).received = list(labels)
            return {"skill": "plan_cancel", "confidence": 0.99}

    class _Settings:
        router_mode = "llm"
        router_confidence_min = 0.6

    routed = asyncio.run(
        route_text_hybrid(None, "随便说点什么", _LLM(), _Settings(), labels=["todo_query"])
    )
    assert routed.skill == "chat"
    # 候选标签必须真实传给分类器，且已按当前 Agent 的能力集裁剪
    assert _LLM.received == ["todo_query", "chat"]


def test_progressive_disclosure_index_has_no_body():
    loader = ResourceLoader(registry, load_files=False)
    for item in loader.catalog():
        assert set(item) == {"name", "label", "description", "path"}
        assert item["path"].startswith("agent://")

    session = loader.create_session("visit")
    for item in session.skills_index():
        assert "prompt" not in item
        assert item["path"].startswith("skill://")
    assert "取消" in session.load_skill_body("plan_cancel")
    assert session.load_skill_body("not_a_skill") is None


def test_explicit_tool_allowlist_cuts_schema(tmp_path):
    _config(tmp_path, name="tiny", tools=("query_todos",))
    session = ResourceLoader(registry, agents_dir=tmp_path).create_session("tiny")
    schema = session.openai_tools("todo_query")
    assert [item["function"]["name"] for item in schema] == ["query_todos"]


def test_reload_picks_up_config_changes(tmp_path):
    _config(tmp_path, name="visit", skills=("todo_query",), tools=None)
    loader = ResourceLoader(registry, agents_dir=tmp_path)
    assert list(loader.create_session("visit").skills) == ["todo_query"]

    _config(tmp_path, name="visit", skills=("todo_query", "checkin"), tools=None)
    names = loader.reload()
    assert "visit" in names
    assert list(loader.create_session("visit").skills) == ["todo_query", "checkin"]


def test_invalid_config_fails_fast(tmp_path):
    (tmp_path / "bad_skill.json").write_text(
        json.dumps({"name": "bad_skill", "skills": ["unknown_skill"]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="未知 Skill"):
        ResourceLoader(registry, agents_dir=tmp_path)


def test_orphan_tool_is_rejected(tmp_path):
    """工具白名单不能把未授权 Skill 的工具偷偷带进来。"""
    _config(tmp_path, name="bad_tool", tools=("cancel_visit_plan",))
    with pytest.raises(ValueError, match="未被任何已授权 Skill 实际注册"):
        ResourceLoader(registry, agents_dir=tmp_path)


def test_validation_uses_registry_ownership_not_static_declaration(tmp_path):
    """校验集合必须与运行时授权集合同源，否则 fail-fast 保证的是错的东西。

    plan_update 的 SkillSpec.tools 静态声明了 query_plans，但注册表里
    query_plans 归属 todo_query。按静态声明校验会放过「只授权 plan_update
    却把 query_plans 写进白名单」的配置，而运行时 registry.for_skill 根本
    不会暴露它 —— 这是 v0.5 初版的真实不一致。
    """
    _config(tmp_path, name="static_liar", skills=("plan_update",), tools=("query_plans",))
    with pytest.raises(ValueError, match="实际注册"):
        ResourceLoader(registry, agents_dir=tmp_path)

    _config(tmp_path, name="static_liar", skills=("todo_query", "plan_update"), tools=("query_plans",))
    loader = ResourceLoader(registry, agents_dir=tmp_path)
    assert "query_plans" in loader.create_session("static_liar").capability()["tools"]


def test_agent_spec_validation():
    with pytest.raises(ValueError, match="name"):
        AgentSpec.from_dict({"name": "bad name", "skills": ["todo_query"]})
    with pytest.raises(ValueError, match="skills"):
        AgentSpec.from_dict({"name": "x", "skills": []})
    with pytest.raises(ValueError, match="重复"):
        AgentSpec.from_dict({"name": "x", "skills": ["todo_query", "todo_query"]})


# --------------------------------------------------------------------------- #
# 执行期能力边界：schema 裁剪之外的最后一道硬校验
# --------------------------------------------------------------------------- #
def _pipeline(tmp_path, *, guard):
    settings = Settings(
        sqlite_path=str(tmp_path / "guard.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock", seed_demo=True, approval_timeout_s=2,
    )
    db = Database(settings.sqlite_path)
    db.init()
    ensure_columns(db)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    pipeline = ToolPipeline(
        settings=settings, registry=registry, policy=PolicyEngine(registry, settings),
        approvals=ApprovalManager(traces, timeout_s=2), traces=traces,
        db=db, tree=SessionTree(db), state=SessionState(settings), tool_guard=guard,
    )
    return pipeline, traces, db


def test_execution_guard_blocks_unauthorized_tool(tmp_path, monkeypatch):
    """核心：模型吐出白名单外的高危工具名，执行层必须挡住且留痕。

    schema 裁剪只决定「模型看得见什么」；这里模拟幻觉 / 提示词注入导致模型
    直接调用 cancel_visit_plan，验证 handler 一次都没被执行。
    """
    ops = ResourceLoader(registry, load_files=False).create_session("ops_readonly")
    pipeline, traces, db = _pipeline(
        tmp_path,
        guard=lambda name, skill: None if ops.allows_tool(name) else "CAPABILITY_DENIED: test",
    )
    spec = registry.get("cancel_visit_plan")
    invoked: list[dict] = []

    async def _fake(args, ctx):
        invoked.append(args)
        return ToolResult("ok", "不该被执行")

    monkeypatch.setattr(spec, "handler", _fake)

    async def _run():
        trace_id = await traces.start_trace("s1", "sales-001", "越权调用")
        call = ToolCall(
            call_id="c1", name="cancel_visit_plan",
            args={"plan_id": "P-1"}, spec=spec,
        )
        emit = EventFactory()
        events = [
            event
            async for event in pipeline.run(
                call, user_id="sales-001", session_id="s1", trace_id=trace_id,
                skill="todo_query", emit=emit,
            )
        ]
        return trace_id, call, events

    trace_id, call, events = asyncio.run(_run())

    assert invoked == [], "越权工具的 handler 绝不能被执行"
    assert call.result is not None and call.result.status == "denied"
    assert call.result.decision == "deny"
    assert "CAPABILITY_DENIED" in call.result.output
    assert [e["event"] for e in events] == ["tool_call", "tool_result"]

    trace_events = asyncio.run(traces.get_events(trace_id))
    assert any(e["type"] == "capability_denied" for e in trace_events)
    assert any(e["type"] == "tool_result" and e["status"] == "denied" for e in trace_events)
    # 越权尝试必须进操作审计，而不是静默丢弃
    assert any(a["action"] == "tool.cancel_visit_plan.denied" for a in db.list_audit())


def test_guard_follows_hot_reloaded_session(tmp_path):
    """guard 绑定的是 Orchestrator 实例，热更新后无需重建 Pipeline 即生效。"""
    loader = ResourceLoader(registry, load_files=False)
    orch = object.__new__(Orchestrator)   # 只测边界判定，避开完整依赖构造
    orch.session = loader.create_session("ops_readonly")
    assert orch._guard_tool("cancel_visit_plan", "todo_query") is not None
    assert "CAPABILITY_DENIED" in orch._guard_tool("cancel_visit_plan", "todo_query")
    assert orch._guard_tool("query_todos", "todo_query") is None

    orch.set_session(loader.create_session("visit"))
    assert orch._guard_tool("cancel_visit_plan", "plan_cancel") is None


def test_preamble_and_tool_list_follow_agent_spec(tmp_path):
    """配置里的 preamble 与白名单必须真的进入提示词，不能是装饰字段。"""
    _config(tmp_path, name="scoped", tools=("query_todos",))
    payload = json.loads((tmp_path / "scoped.json").read_text(encoding="utf-8"))
    payload["preamble"] = "你是只读巡检助手，禁止任何写操作。"
    (tmp_path / "scoped.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    session = ResourceLoader(registry, agents_dir=tmp_path).create_session("scoped")

    text = session.system_prompt(active_skill="todo_query")
    assert "你是只读巡检助手" in text
    assert "你是「拜访通」销售助手" not in text
    assert "可用工具：query_todos" in text
    assert "可用工具：query_todos, query_plans" not in text
    # 技能正文确实经「按需读取」路径注入，且兼容标记仍然保留
    assert "当前能力是【待办查询】" in text
    assert "[当前子 Skill: todo_query]" in text
    assert session.describe()["preamble_overridden"] is True


def test_agents_api_exposes_assembly_and_guards_reload():
    """装配结果必须能被观测；热更新是主管权限，不能谁都能调。"""
    loader = ResourceLoader(registry, load_files=False)

    class _Orch:
        def __init__(self, session):
            self.session = session

        def set_session(self, session):
            self.session = session

    class _Ctx:
        def __init__(self):
            self.resource_loader = loader
            self.orchestrator = _Orch(loader.create_session("ops_readonly"))

    ctx, manager = _Ctx(), {"role": "manager"}
    listed = asyncio.run(routes_agents.list_agents(ctx, manager))
    assert listed["active"]["name"] == "ops_readonly"
    assert listed["active"]["tools"] == ["query_todos", "query_plans"]
    assert set(listed["agents"][0]) == {"name", "label", "description", "path"}

    scoped = asyncio.run(routes_agents.agent_skills("ops_readonly", ctx, manager))
    assert [item["name"] for item in scoped["skill_index"]] == ["todo_query"]

    reloaded = asyncio.run(routes_agents.reload_agents(ctx, manager))
    assert reloaded["active"]["name"] == "ops_readonly"
    with pytest.raises(ApiError):
        asyncio.run(routes_agents.reload_agents(ctx, {"role": "sales"}))


# --------------------------------------------------------------------------- #
# v0.5.1 能力边界：内容指纹、跨能力集续跑、回放判定
# --------------------------------------------------------------------------- #

def _orch_with(agent_name: str = "ops_readonly") -> Orchestrator:
    """只测能力判定，用 object.__new__ 避开完整依赖构造。"""
    orch = object.__new__(Orchestrator)
    orch.session = ResourceLoader(registry, load_files=False).create_session(agent_name)
    return orch


def test_capability_fingerprint_is_content_based_and_ignores_runtime_toggle():
    loader = ResourceLoader(registry, load_files=False)
    visit = loader.create_session("visit")
    ops = loader.create_session("ops_readonly")
    assert visit.capability_fingerprint() != ops.capability_fingerprint()
    assert set(ops.capability()["tools"]) == {"query_todos", "query_plans"}
    assert "cancel_visit_plan" in visit.capability()["tools"]

    # 内容哈希：改 preamble 就变，不依赖手填版本号
    kwargs = {"name": "custom", "label": "", "skills": ("todo_query",), "tools": ("query_todos",)}
    a = AgentSession(
        spec=AgentSpec(**kwargs, preamble="只读"), loader=loader, registry=registry,
        skills={"todo_query": SKILLS["todo_query"]},
    )
    b = AgentSession(
        spec=AgentSpec(**kwargs, preamble="另一段提示词"), loader=loader, registry=registry,
        skills={"todo_query": SKILLS["todo_query"]},
    )
    assert a.capability_fingerprint() != b.capability_fingerprint()

    # 运行期启停不是权限边界变化：PolicyEngine 每次调用会硬拦 tool_unavailable
    before = visit.capability_fingerprint()
    registry.set_enabled("query_todos", False)
    try:
        assert visit.capability_fingerprint() == before
        assert "query_todos" not in visit.exposed_tool_names()
    finally:
        registry.set_enabled("query_todos", True)


def test_session_skills_are_actually_immutable():
    session = ResourceLoader(registry, load_files=False).create_session("ops_readonly")
    with pytest.raises(TypeError):
        session.skills["plan_cancel"] = SKILLS["plan_cancel"]


def test_checkpoint_persists_capability_snapshot(tmp_path):
    db = Database(str(tmp_path / "cap.db"))
    db.init()
    tasks = TaskStore(db)
    session = ResourceLoader(registry, load_files=False).create_session("ops_readonly")
    tasks.create(turn_id="t1", session_id="s1", user_id="sales-001",
                 trace_id="tr1", user_input="查待办")
    tasks.save_checkpoint(
        turn_id="t1", step=0, skill="todo_query", messages=[], slots={},
        agent_name=session.name,
        capability_fingerprint=session.capability_fingerprint(),
        capability=session.capability(),
    )
    cp = tasks.latest_checkpoint("t1")
    assert cp["agent_name"] == "ops_readonly"
    assert cp["capability_fingerprint"] == session.capability_fingerprint()
    assert cp["capability"]["tools"] == ["query_plans", "query_todos"]


def test_resume_compat_rejects_changed_capability_with_diagnostics():
    orch = _orch_with("ops_readonly")
    visit = ResourceLoader(registry, load_files=False).create_session("visit")
    cp = {
        "agent_name": "visit",
        "capability_fingerprint": visit.capability_fingerprint(),
        "capability": visit.capability(),
    }
    compat = orch.check_resume_compat(cp)
    assert compat["status"] == "incompatible"
    assert "拒绝跨能力集续跑" in compat["reason"]
    assert compat["recorded_capability"]["tools"] != compat["current_capability"]["tools"]
    assert orch.check_resume_compat({
        **cp, "capability_fingerprint": orch.session.capability_fingerprint(),
    })["status"] == "ok"
    assert orch.check_resume_compat({})["status"] == "legacy"


def test_resume_api_returns_explicit_409_and_task_detail_explains_why():
    orch = _orch_with("ops_readonly")
    visit = ResourceLoader(registry, load_files=False).create_session("visit")
    cp = {
        "step": 1, "skill": "todo_query", "messages": [], "slots": {},
        "created_at": "2026-11-07T00:00:00+08:00",
        "agent_name": "visit",
        "capability_fingerprint": visit.capability_fingerprint(),
        "capability": visit.capability(),
    }

    class _Tasks:
        def get(self, _tid):
            return {"user_id": "sales-001", "status": "interrupted",
                    "session_id": "s1", "resume_count": 0}

        def latest_checkpoint(self, _tid):
            return cp

        def tool_count(self, _tid):
            return 0

    class _Ctx:
        tasks = _Tasks()
        orchestrator = orch

    user = {"role": "manager", "user_id": "manager-001"}
    with pytest.raises(ApiError) as exc:
        asyncio.run(routes_chat.resume_task("t1", _Ctx(), user))
    assert exc.value.code == "AGENT_CAPABILITY_CHANGED"

    detail = asyncio.run(routes_chat.task_detail("t1", _Ctx(), user))
    assert detail["resume_compat"]["status"] == "incompatible"
    assert detail["resumable"] is False
    assert detail["checkpoint"]["capability_fingerprint"] == visit.capability_fingerprint()


def test_replay_decision_blocks_stale_result_when_capability_tightened():
    """回放不执行 handler，但能力收紧后不该把旧结果喂给模型。"""
    orch = _orch_with("ops_readonly")
    assert orch.replay_decision("query_todos", "todo_query", False) == ("execute", None)
    assert orch.replay_decision("query_todos", "todo_query", True)[0] == "replay"
    action, reason = orch.replay_decision("cancel_visit_plan", "plan_cancel", True)
    assert action == "deny"
    assert reason and "CAPABILITY_DENIED" in reason
