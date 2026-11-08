"""真崩溃注入：用子进程 kill -9 验证「副作用恰好一次」。

为什么不用 `test_durable.py` 里那种「把 status 改成 interrupted」的模拟：
那不是崩溃，是改字段。真正要回答的问题是 —— **handler 已经产生了副作用、
账本还没写终态时进程死掉**，重启后会不会重复建计划？

做法：
  1. 子进程跑一个真实的 ToolPipeline 调用（create_visit_plan），并在
     `Database.create_plan` 外层包 60ms 延迟，把「可被击杀的窗口」撑开；
  2. 父进程轮询 marker 文件，在窗口内的不同偏移（5/25/45/65/90/120ms）
     `Popen.kill()`（Windows 上是 TerminateProcess，等同 kill -9，不做清理）；
  3. 父进程重启装配（会执行 `reconcile_tool_attempts` 把 running 标 unknown），
     用**同一个 ExecutionContract** 重新执行（这就是 resume 的本质路径）；
  4. 断言：计划恰好 1 条、账本恰好 1 行且为终态、无遗留 running。

覆盖的三种崩溃点：
  - 副作用提交前被杀  → 重跑建 1 条（0+1）
  - 副作用提交后、账本终态前被杀 → 幂等账本 running，重跑被业务唯一性/幂等认领（1+0）
  - 全部提交后被杀     → 直接回放，不执行（1+0）

用法：`python scripts/chaos_resume.py [--iterations 6]`
退出码 0 = 全部用例满足「恰好一次」。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

WORK = BACKEND / "data" / "_chaos"
DB_PATH = WORK / "chaos.db"
MARKER = WORK / "marker"


# --------------------------------------------------------------------------- #
# 环境装配（与 app.main.lifespan 对齐的最小版本）
# --------------------------------------------------------------------------- #
def _settings(db_path: str):
    from app.config import Settings

    return Settings(
        sqlite_path=db_path,
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=5,
    )


async def build(db_path: str):
    """返回 (ctx, orchestrator, session)。不启 HTTP，只装配编排链路。"""
    import app.skills.tools  # noqa: F401  注册内置工具
    from app.approvals import ApprovalManager
    from app.context import AppContext
    from app.domain.audio import AudioStore
    from app.domain.db import Database
    from app.domain.governance import GovernanceStore
    from app.domain.idempotency import IdempotencyStore
    from app.domain.migrations import apply as apply_migrations
    from app.domain.pipeline import EventPipeline
    from app.domain.tasks import TaskStore
    from app.llm import build_llm
    from app.policy.engine import PolicyEngine
    from app.runtime import ResourceLoader
    from app.sessions.store import SessionState
    from app.sessions.tree import SessionTree, ensure_columns
    from app.skills.registry import registry
    from app.trace.store import TraceStore

    settings = _settings(db_path)
    db = Database(settings.sqlite_path, settings.demo_password,
                  backend=settings.db_backend, dsn=settings.mysql_dsn)
    db.init()
    db.seed_demo()
    db.ensure_auth_columns()
    apply_migrations(settings.sqlite_path)
    tree = SessionTree(db)
    ensure_columns(db)
    state = SessionState(settings)
    await state.init()
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    traces.ensure_schema_columns()
    tasks = TaskStore(db)
    tasks.reconcile_interrupted()
    tasks.reconcile_tool_attempts()
    audio = AudioStore(settings)
    await audio.init()
    from app.adapters.knowledge import build_knowledge

    knowledge = build_knowledge(settings, db)
    loader = ResourceLoader(registry)
    session = loader.create_session("visit")
    ctx = AppContext(
        settings=settings, db=db, tree=tree, state=state, registry=registry,
        policy=PolicyEngine(registry, settings), llm=build_llm(settings),
        traces=traces, approvals=ApprovalManager(traces, settings.approval_timeout_s),
        tasks=tasks, audio=audio, knowledge=knowledge, events=EventPipeline(db),
        session=session, resource_loader=loader,
        idem=IdempotencyStore(settings.sqlite_path),
        governance=GovernanceStore(settings.sqlite_path),
    )
    return ctx, ctx.orchestrator, session


def pick_customer(db_path: str) -> dict[str, str]:
    """挑一个**只有一家门店**的客户，并把 customer/store 一起返回。

    为什么要限定单门店：`create_visit_plan` 在多门店且未指定 store 时会返回
    `need_input` 追问，handler 根本走不到 `create_plan`，于是「副作用窗口」
    从未打开、marker 不出现、父进程只能在窗口外击杀 —— 那样测出来的
    exactly-once 是假的（什么都没发生，当然只发生一次）。
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT c.customer_id, c.name, s.name AS store FROM customers c"
            " JOIN stores s ON s.customer_id = c.customer_id"
            " WHERE c.customer_id IN ("
            "   SELECT customer_id FROM stores GROUP BY customer_id HAVING COUNT(*) = 1)"
            " LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError("种子库里没有单门店客户，无法进入 create_plan 窗口")
    return {"customer_id": str(row[0]), "customer": str(row[1]), "store": str(row[2])}


def _tool_args(db_path: str, planned_at: str) -> dict[str, str]:
    """子进程与 resume 必须用**完全一致**的参数，否则比较的就不是同一次调用。"""
    return {**pick_customer(db_path), "planned_at": planned_at, "topic": "崩溃注入"}


# --------------------------------------------------------------------------- #
# 子进程：跑一次工具调用，等待被杀
# --------------------------------------------------------------------------- #
async def _child(db_path: str, turn_id: str, planned_at: str, delay_ms: int) -> None:
    from app.domain.db import Database

    original = Database.create_plan

    def slow_create_plan(self, *args, **kwargs):  # noqa: ANN001
        MARKER.write_text("entered", encoding="utf-8")
        time.sleep(delay_ms / 1000)
        plan = original(self, *args, **kwargs)
        time.sleep(delay_ms / 1000)   # 副作用已提交、账本终态未写：最危险的窗口
        return plan

    Database.create_plan = slow_create_plan  # type: ignore[method-assign]

    ctx, orchestrator, session = await build(db_path)
    from app.agent.hooks import ToolCall
    from app.runtime import issue_contract
    from app.skills.registry import registry

    spec = registry.get("create_visit_plan")
    call = ToolCall(
        call_id="c1", name="create_visit_plan",
        args=_tool_args(db_path, planned_at),
        spec=spec, label=spec.label if spec else "create_visit_plan", risk="medium",
    )
    contract = issue_contract(
        turn_id=turn_id, step=0, call_id="c1", agent_name=session.name,
        capability_fingerprint=session.capability_fingerprint(),
        active_skill="plan_create", tool_name="create_visit_plan",
        user_id="sales-001", role="sales",
    )
    from app.agent.events import EventFactory
    from app.trace.store import TraceStore

    trace_id = await ctx.traces.start_trace("chaos", "sales-001", "崩溃注入")
    emit = EventFactory()
    async for _event in orchestrator.pipeline.run(
        call, user_id="sales-001", session_id="chaos", trace_id=trace_id,
        skill="plan_create", emit=emit, contract=contract,
    ):
        pass
    print(json.dumps({"child": "finished", "status": call.result.status if call.result else None}))
    await asyncio.sleep(30)  # 正常情况下父进程会先杀掉我们


# --------------------------------------------------------------------------- #
# 父进程：编排 + 断言
# --------------------------------------------------------------------------- #
def _count_plans(db_path: str, owner: str, planned_at: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if {"plan_id", "owner_id", "planned_at"} <= cols:
                return int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE owner_id=? AND planned_at=?",
                    (owner, planned_at),
                ).fetchone()[0])
    finally:
        conn.close()
    return 0


def _ledger(db_path: str, turn_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, attempt FROM tool_executions WHERE turn_id=?", (turn_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _effects(db_path: str, turn_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT state, effect_type FROM idempotency_effects WHERE turn_id=?", (turn_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def _resume(db_path: str, turn_id: str, planned_at: str) -> None:
    """用同一个 ExecutionContract 再执行一次（resume 的本质路径）。"""
    from app.agent.events import EventFactory
    from app.agent.hooks import ToolCall
    from app.runtime import issue_contract
    from app.skills.registry import registry

    ctx, orchestrator, session = await build(db_path)
    spec = registry.get("create_visit_plan")
    call = ToolCall(
        call_id="c1", name="create_visit_plan",
        args=_tool_args(db_path, planned_at),
        spec=spec, label="create_visit_plan", risk="medium",
    )
    contract = issue_contract(
        turn_id=turn_id, step=0, call_id="c1", agent_name=session.name,
        capability_fingerprint=session.capability_fingerprint(),
        active_skill="plan_create", tool_name="create_visit_plan",
        user_id="sales-001", role="sales",
    )
    trace_id = await ctx.traces.start_trace("chaos", "sales-001", "resume")
    emit = EventFactory()
    async for _event in orchestrator.pipeline.run(
        call, user_id="sales-001", session_id="chaos", trace_id=trace_id,
        skill="plan_create", emit=emit, contract=contract,
    ):
        pass


def run_case(index: int, kill_offset_ms: int, delay_ms: int) -> dict:
    turn_id = f"chaos-{index}"
    planned_at = f"2026-12-{index + 1:02d}T10:00:00+08:00"
    marker = MARKER
    if marker.exists():
        marker.unlink()

    env = dict(os.environ)
    env["CHAOS_CHILD"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__)), "--child",
         "--db", str(DB_PATH), "--turn", turn_id, "--planned-at", planned_at,
         "--delay-ms", str(delay_ms)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # 等子进程真正进入副作用窗口，再按偏移击杀
    deadline = time.time() + 30
    while time.time() < deadline and not marker.exists():
        if proc.poll() is not None:
            break
        time.sleep(0.003)
    entered = marker.exists()
    time.sleep(kill_offset_ms / 1000)
    alive = proc.poll() is None
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass

    before_plans = _count_plans(str(DB_PATH), "sales-001", planned_at)
    before_ledger = _ledger(str(DB_PATH), turn_id)

    # 重启装配（自动跑 reconcile_tool_attempts）+ 同契约重放
    asyncio.run(_resume(str(DB_PATH), turn_id, planned_at))

    after_plans = _count_plans(str(DB_PATH), "sales-001", planned_at)
    after_ledger = _ledger(str(DB_PATH), turn_id)
    effects = _effects(str(DB_PATH), turn_id)
    ok = (
        after_plans == 1
        and len(after_ledger) == 1
        and after_ledger[0]["status"] == "ok"
        and all(e["state"] == "done" for e in effects)
    )
    return {
        "case": index,
        "kill_offset_ms": kill_offset_ms,
        "entered_window": entered,
        "killed_alive": alive,
        "plans_before_resume": before_plans,
        "ledger_before_resume": [r["status"] for r in before_ledger],
        "plans_after_resume": after_plans,
        "ledger_after_resume": [r["status"] for r in after_ledger],
        "idem_states": [e["state"] for e in effects],
        "exactly_once": ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--turn", default="chaos-0")
    parser.add_argument("--planned-at", default="2026-12-01T10:00:00+08:00")
    parser.add_argument("--delay-ms", type=int, default=60)
    parser.add_argument("--iterations", type=int, default=6)
    args = parser.parse_args()

    if args.child:
        asyncio.run(_child(args.db, args.turn, args.planned_at, args.delay_ms))
        return 0

    WORK.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    asyncio.run(build(str(DB_PATH)))  # 初始化 schema + 种子数据

    offsets = [5, 25, 45, 65, 90, 120][: args.iterations]
    results = [run_case(i, offset, args.delay_ms) for i, offset in enumerate(offsets)]
    for row in results:
        print(json.dumps(row, ensure_ascii=False))
    passed = sum(1 for r in results if r["exactly_once"])
    print(f"\nchaos_resume: {passed}/{len(results)} cases satisfied exactly-once")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
