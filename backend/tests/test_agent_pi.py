"""Agent 层测试（Pi 架构对齐）：事件包络 / 提示词 / 压缩 / 分支 / 工具流水线。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app.skills.tools  # noqa: F401  注册工具
from app.agent.compaction import compact_if_needed
from app.agent.events import EventFactory
from app.agent.hooks import BaseToolHook, HookChain, HookDecision, ToolCall
from app.agent.pipeline import ToolPipeline
from app.agent.prompt import build_system_prompt
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.main import create_app
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree, ensure_columns
from app.skills.registry import registry
from app.skills.visit_skills import SKILLS
from app.trace.store import TraceStore


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "test.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=2,
    )
    base.update(overrides)
    return Settings(**base)


def parse_sse(response) -> list[tuple[str | None, dict]]:
    events: list[tuple[str | None, dict]] = []
    name = None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line[5:].strip())))
    return events


def make_tree(tmp_path, name: str = "agent.db") -> tuple[Database, SessionTree]:
    db = Database(str(tmp_path / name))
    db.init()
    ensure_columns(db)
    return db, SessionTree(db)


# --------------------------------------------------------------------------- #
# 事件包络
# --------------------------------------------------------------------------- #
def test_event_factory_monotonic():
    emit = EventFactory()
    first = emit("start", session_id="s")
    second = emit("done", trace_id="t")
    assert first["data"]["seq"] == 1 and second["data"]["seq"] == 2
    assert first["data"]["ts"] and second["data"]["elapsed_ms"] >= 0
    assert second["data"]["trace_id"] == "t"


# --------------------------------------------------------------------------- #
# 提示词：技能索引 + 当前技能 + 兼容标记
# --------------------------------------------------------------------------- #
def test_prompt_has_skill_index_and_active_skill():
    text = build_system_prompt(active_skill="plan_create", slots={"customer": "字节跳动"})
    for name in SKILLS:
        assert name in text
        assert f"skill://{name}" in text
    assert "当前能力是【创建拜访计划】" in text
    assert "当前能力是【待办查询】" not in text
    assert '"customer": "字节跳动"' in text
    # Mock LLM / Trace 解析依赖的兼容标记
    assert "[当前子 Skill: plan_create]" in text
    assert "[槽位: {" in text


# --------------------------------------------------------------------------- #
# 上下文压缩：折叠早期消息，旧条目仍可回溯
# --------------------------------------------------------------------------- #
async def test_compaction_folds_and_keeps_history(tmp_path):
    db, tree = make_tree(tmp_path)
    for i in range(30):
        tree.append(
            session_id="s1", parent_id=None,
            role="user" if i % 2 == 0 else "assistant",
            content=f"第 {i} 条历史消息：" + "很长的上下文内容" * 20,
        )
    settings = make_settings(tmp_path, context_token_budget=10, compaction_keep_recent=6)
    info = await compact_if_needed(tree=tree, session_id="s1", settings=settings)

    assert info is not None
    assert info["folded"] >= 1 and info["tokens_before"] > info["tokens_after"]

    path = tree.path("s1", limit=100)
    assert len(path) <= 7
    assert path[0]["role"] == "summary"
    assert path[0]["entry_type"] == "compaction"
    assert "## 目标" in path[0]["content"]
    # 原始条目一条都没删（只追加 compaction），可导出回放
    rows = db._rows("SELECT entry_id FROM session_entries WHERE session_id='s1'")
    assert len(rows) == 31


async def test_compaction_never_splits_tool_pair(tmp_path):
    db, tree = make_tree(tmp_path, "pair.db")
    base = tree.append(session_id="s2", parent_id=None, role="user", content="用户问题")
    assistant = tree.append(
        session_id="s2", parent_id=base["entry_id"], role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "query_todos", "arguments": "{}"}],
    )
    tree.append(
        session_id="s2", parent_id=assistant["entry_id"], role="tool",
        content='{"status": "ok", "output": "ok"}', tool_call_id="c1", name="query_todos",
    )
    for i in range(20):
        tree.append(session_id="s2", parent_id=None, role="user", content=f"后续 {i} " + "内容" * 50)

    settings = make_settings(tmp_path, context_token_budget=10, compaction_keep_recent=3)
    await compact_if_needed(tree=tree, session_id="s2", settings=settings)
    kept = tree.path("s2", limit=100)
    # 保留窗口不能以 tool 消息开头（否则会丢掉对应的 assistant tool_call）
    assert kept[1]["role"] != "tool"


# --------------------------------------------------------------------------- #
# 分支：切回历史节点 + branch_summary
# --------------------------------------------------------------------------- #
def test_branch_switch_with_summary(tmp_path):
    _, tree = make_tree(tmp_path, "branch.db")
    first = tree.append(session_id="s1", parent_id=None, role="user", content="第一个问题")
    tree.append(session_id="s1", parent_id=None, role="assistant", content="第一个回答")

    info = tree.switch_leaf("s1", first["entry_id"], summary_text="被放弃分支摘要")
    assert info and info["branch_summary_id"]
    third = tree.append(session_id="s1", parent_id=None, role="user", content="分支问题")

    path = tree.path("s1", limit=50)
    assert [item["role"] for item in path] == ["user", "summary", "user"]
    assert path[1]["entry_type"] == "branch_summary"
    assert third["parent_id"] == path[1]["entry_id"]
    # 被放弃的分支仍保留在 JSONL 里（只追加不删除），活跃路径只有 3 条
    lines = tree.export_jsonl("s1").splitlines()
    assert len(lines) == 4
    assert "第一个回答" in tree.export_jsonl("s1")


# --------------------------------------------------------------------------- #
# 工具流水线：策略短路 + 扩展钩子
# --------------------------------------------------------------------------- #
async def _pipeline(tmp_path, hooks: HookChain | None = None) -> tuple[ToolPipeline, TraceStore]:
    settings = make_settings(tmp_path)
    db, tree = make_tree(tmp_path, "pipeline.db")
    state = SessionState(settings)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    pipeline = ToolPipeline(
        settings=settings, registry=registry, policy=PolicyEngine(registry, settings),
        approvals=ApprovalManager(traces, timeout_s=2), traces=traces,
        db=db, tree=tree, state=state, hooks=hooks,
    )
    return pipeline, traces


async def _collect(pipeline, call, trace_id, skill="chat"):
    emit = EventFactory()
    events = [
        event
        async for event in pipeline.run(
            call, user_id="sales-001", session_id="s1", trace_id=trace_id,
            skill=skill, emit=emit,
        )
    ]
    return events


async def test_pipeline_denies_unknown_tool(tmp_path):
    pipeline, traces = await _pipeline(tmp_path)
    trace_id = await traces.start_trace("s1", "sales-001", "测试")
    call = ToolCall(call_id="c1", name="no_such_tool", args={}, spec=None)
    events = await _collect(pipeline, call, trace_id)

    assert [event["event"] for event in events] == ["tool_call", "tool_result"]
    assert call.result is not None and call.result.status == "denied"
    assert call.result.decision == "deny"


async def test_pipeline_hook_blocks_and_audits(tmp_path):
    blocked: dict[str, str] = {}

    class Blocker(BaseToolHook):
        name = "blocker"

        async def before_tool_call(self, call: ToolCall):
            return HookDecision("deny", "扩展钩子拦截", "low", "test_hook")

    class Audit(BaseToolHook):
        name = "audit"

        async def after_tool_result(self, call: ToolCall, outcome):
            blocked["status"] = outcome.status
            blocked["rule"] = "audited"
            return None

    pipeline, traces = await _pipeline(tmp_path, hooks=HookChain([Blocker(), Audit()]))
    trace_id = await traces.start_trace("s1", "sales-001", "测试钩子")
    call = ToolCall(call_id="c2", name="query_todos", args={}, spec=registry.get("query_todos"))
    await _collect(pipeline, call, trace_id, skill="todo_query")

    assert call.result is not None and call.result.status == "denied"
    assert call.result.output == "扩展钩子拦截"
    assert blocked == {"status": "denied", "rule": "audited"}


# --------------------------------------------------------------------------- #
# 端到端：SSE 带 seq，导出 JSONL
# --------------------------------------------------------------------------- #
def test_turn_compaction_emits_event(tmp_path):
    """多轮对话超预算时，Turn 内会触发压缩并推送 compaction 事件。"""
    app = create_app(make_settings(tmp_path, context_token_budget=60, compaction_keep_recent=4))
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "sales-001", "password": "123456"})
        client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        sid = client.post("/api/sessions", json={}).json()["session_id"]
        names: list[str | None] = []
        for text in ("你好", "现在几点了", "今天适合拜访吗", "再聊聊", "查一下今天的拜访待办"):
            with client.stream(
                "POST", "/api/chat/stream",
                json={"session_id": sid, "message": text},
            ) as response:
                names += [name for name, _ in parse_sse(response)]

        assert "compaction" in names
        export = client.get(f"/api/sessions/{sid}/export").text
        assert '"entry_type": "compaction"' in export


def test_sse_events_carry_seq_and_export(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "sales-001", "password": "123456"})
        client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        sid = client.post("/api/sessions", json={}).json()["session_id"]
        with client.stream(
            "POST", "/api/chat/stream",
            json={"session_id": sid, "message": "现在几点了"},
        ) as response:
            events = parse_sse(response)

        names = [name for name, _ in events]
        assert names[0] == "start" and "done" in names
        seqs = [data["seq"] for _, data in events]
        assert seqs == sorted(seqs) and seqs[0] == 1
        assert all("ts" in data for _, data in events)
        done = next(data for name, data in events if name == "done")
        assert done["stop_reason"] in ("no_tool_calls", "max_steps")

        export = client.get(f"/api/sessions/{sid}/export")
        assert export.status_code == 200
        rows = [json.loads(line) for line in export.text.splitlines() if line.strip()]
        assert rows and all("entry_id" in row for row in rows)
        assert any(row["role"] == "user" for row in rows)