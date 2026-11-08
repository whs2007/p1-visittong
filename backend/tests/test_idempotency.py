"""幂等账本与崩溃对账（G0）。

两件事必须分开验证：
1. 账本本身：claim → complete → 重放，`running` 残留能被启动对账收敛为 `unknown`；
2. 效果本身：同一个 ExecutionContract 重复执行，业务表里只有一条计划。
"""
from __future__ import annotations

import app.skills.tools  # noqa: F401  注册内置工具
import pytest
from app.agent.events import EventFactory
from app.agent.hooks import ToolCall
from app.agent.pipeline import ToolPipeline
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.db import Database
from app.domain.idempotency import IdempotencyStore
from app.domain.tasks import TaskStore
from app.policy.engine import PolicyEngine
from app.runtime import issue_contract
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree
from app.skills.registry import registry
from app.trace.store import TraceStore


def make_settings(tmp_path) -> Settings:
    return Settings(
        sqlite_path=str(tmp_path / "idem.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=1,
    )


@pytest.fixture()
def env(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.sqlite_path, settings.demo_password)
    db.init()
    db.seed_demo()
    tree = SessionTree(db)
    state = SessionState(settings)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    tasks = TaskStore(db)
    idem = IdempotencyStore(settings.sqlite_path)
    # 迁移表是治理/幂等依赖的结构，手工应用一次（生产由 main.lifespan 调）
    from app.domain.migrations import apply as apply_migrations

    apply_migrations(settings.sqlite_path)
    pipeline = ToolPipeline(
        settings=settings, registry=registry, policy=PolicyEngine(registry, settings),
        approvals=ApprovalManager(traces, timeout_s=1), traces=traces,
        db=db, tree=tree, state=state, tasks=tasks, idem=idem,
    )
    return {
        "settings": settings, "db": db, "tree": tree, "state": state,
        "traces": traces, "tasks": tasks, "idem": idem, "pipeline": pipeline,
    }


def _customer(db: Database) -> dict[str, str]:
    """挑一个「只有一家门店」的客户，避免 handler 因为多门店追问。"""
    rows = db._rows(  # noqa: SLF001
        "SELECT c.customer_id, c.name, s.name AS store FROM customers c"
        " JOIN stores s ON s.customer_id = c.customer_id"
        " WHERE c.customer_id IN ("
        "   SELECT customer_id FROM stores GROUP BY customer_id HAVING COUNT(*) = 1)"
        " LIMIT 1"
    )
    assert rows, "种子数据缺少单门店客户"
    return {
        "customer_id": str(rows[0]["customer_id"]),
        "customer": str(rows[0]["name"]),
        "store": str(rows[0]["store"]),
    }


def _contract(turn_id: str, call_id: str = "c1"):
    return issue_contract(
        turn_id=turn_id, step=0, call_id=call_id, agent_name="visit",
        capability_fingerprint="f" * 64, active_skill="plan_create",
        tool_name="create_visit_plan", user_id="sales-001", role="sales",
    )


async def _run(pipeline: ToolPipeline, traces: TraceStore, db: Database, contract) -> ToolCall:
    spec = registry.get("create_visit_plan")
    call = ToolCall(
        call_id=contract.call_id, name="create_visit_plan",
        args={
            **_customer(db),
            "planned_at": "2026-12-25T10:00:00+08:00",
            "topic": "幂等验证",
        },
        spec=spec, label="创建计划", risk="medium",
    )
    trace_id = await traces.start_trace("s1", "sales-001", "幂等验证")
    emit = EventFactory()
    async for _event in pipeline.run(
        call, user_id="sales-001", session_id="s1", trace_id=trace_id,
        skill="plan_create", emit=emit, contract=contract,
    ):
        pass
    return call


# --------------------------------------------------------------------------- #
# 账本
# --------------------------------------------------------------------------- #
def test_claim_is_idempotent_and_terminal(env):
    tasks: TaskStore = env["tasks"]
    first = tasks.claim_tool(turn_id="t1", step=0, call_id="c1", name="create_visit_plan")
    assert first is not None and first["status"] == "running"
    second = tasks.claim_tool(turn_id="t1", step=0, call_id="c1", name="create_visit_plan")
    assert second is not None and second["status"] == "running"
    assert tasks.tool_count("t1") == 1, "重复声明不应产生第二条账本记录"

    tasks.complete_tool(
        turn_id="t1", step=0, call_id="c1", status="ok", output="done", data={"a": 1},
    )
    done = tasks.saved_tool("t1", 0, "c1")
    assert done is not None and done["status"] == "ok"


def test_reconcile_marks_orphan_attempts_unknown(env):
    tasks: TaskStore = env["tasks"]
    tasks.claim_tool(turn_id="t2", step=0, call_id="c9", name="checkin")
    report = tasks.reconcile_tool_attempts()
    assert report["count"] >= 1
    row = tasks.saved_tool("t2", 0, "c9")
    assert row is not None and row["status"] == "unknown"
    # 重试时重新置为 running，并累计 attempt
    again = tasks.claim_tool(turn_id="t2", step=0, call_id="c9", name="checkin")
    assert again is not None and again["status"] == "running"
    assert int(again.get("attempt") or 1) >= 2


def test_idempotency_store_roundtrip(env):
    idem: IdempotencyStore = env["idem"]
    record = idem.begin(
        key="k1", effect_type="plan", turn_id="t3", step=0, call_id="c1",
    )
    assert record["state"] == "running" and record["created"] is True
    idem.finish(key="k1", resource_id="p1", result={"status": "ok", "output": "hi", "data": {}})
    hit = idem.get("k1")
    assert hit is not None and hit["state"] == "done" and hit["resource_id"] == "p1"
    # 同 key 再声明：不覆盖已有结果
    again = idem.begin(key="k1", effect_type="plan", turn_id="t3", step=0, call_id="c1")
    assert again["created"] is False and again["state"] == "done"


# --------------------------------------------------------------------------- #
# 效果：同一契约重复执行只产生一条计划
# --------------------------------------------------------------------------- #
async def test_same_contract_does_not_duplicate_plan(env):
    pipeline, traces, db = env["pipeline"], env["traces"], env["db"]
    contract = _contract("turn-dup")
    baseline = len(db.list_plans("sales-001"))
    await _run(pipeline, traces, db, contract)
    assert len(db.list_plans("sales-001")) == baseline + 1

    # 崩溃后重放：同一 (turn, step, call_id)
    await _run(pipeline, traces, db, contract)
    assert len(db.list_plans("sales-001")) == baseline + 1, "重放不得产生第二条计划"

    row = env["tasks"].saved_tool("turn-dup", 0, "c1")
    assert row is not None and row["status"] == "ok"


async def test_effect_replay_returns_same_output(env):
    pipeline, traces, db = env["pipeline"], env["traces"], env["db"]
    contract = _contract("turn-replay")
    first = await _run(pipeline, traces, db, contract)
    second = await _run(pipeline, traces, db, contract)
    assert first.result is not None and second.result is not None
    assert first.result.status == second.result.status == "ok"
    assert first.result.output == second.result.output
    # 幂等账本应指向同一个资源
    effect = env["idem"].get(contract.idem_key)
    assert effect is not None and effect["state"] == "done" and effect["resource_id"]
