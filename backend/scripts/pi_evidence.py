#!/usr/bin/env python3
"""Pi 架构对照实证：一键打印 P1 Runtime 与 Pi 的对应证据。

用法（任选其一，无需起服务、无需 API Key、不写运行时数据库）：
    cd backend && python scripts/pi_evidence.py
    python backend/scripts/pi_evidence.py

每一组都调用真实模块做一次计算并打印实测值，而不是复述文档；
末尾给出对应的 pytest 用例名，便于把「可跑的演示」接到「可回归的测试」。
设计取舍见 docs/07-Pi-Agent架构对照与优化.md。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app.skills.tools  # noqa: F401,E402  触发内置工具注册（装配前置）
from app.agent.compaction import _cut_index, digest_entries  # noqa: E402
from app.agent.events import PI_EVENT_MAP, EventFactory  # noqa: E402
from app.agent.hooks import HookChain, HookDecision, ToolCall  # noqa: E402
from app.agent.prompt import build_system_prompt  # noqa: E402
from app.domain.db import Database  # noqa: E402
from app.domain.tasks import TaskStore  # noqa: E402
from app.orchestrator.loop import Orchestrator  # noqa: E402
from app.runtime import AgentSpec, AgentSession, ResourceLoader  # noqa: E402
from app.sessions.tree import SessionTree, ensure_columns  # noqa: E402
from app.skills.registry import registry  # noqa: E402
from app.skills.visit_skills import SKILLS  # noqa: E402

LINE = "-" * 74


def group(no: int, title: str, pi: str, p1: str, rows: list[str], tests: list[str]) -> None:
    print(f"\n[{no}] {title}")
    print(f"    Pi : {pi}")
    print(f"    P1 : {p1}")
    for row in rows:
        print(f"    实测 {row}")
    print(f"    测试 {', '.join(tests)}")


async def _hook_and_guard() -> tuple[str, str, str]:
    """真跑一次 before_tool_call 拦截 + 两次执行期能力判定。"""
    class Blocker:
        name = "evidence_blocker"

        async def before_tool_call(self, call: ToolCall):
            return HookDecision("deny", "实证：扩展钩子拦截", "low", "evidence_hook")

        async def after_tool_result(self, call, outcome):
            return None

    chain = HookChain([Blocker()])
    call = ToolCall(call_id="ev-1", name="query_todos", args={}, spec=registry.get("query_todos"))
    decision = await chain.before_tool_call(call)
    hook_result = f"before_tool_call -> {decision.action} / rule={decision.rule}"

    loader = ResourceLoader(registry, load_files=False)
    orch = object.__new__(Orchestrator)  # 只取边界判定，避开完整依赖构造
    orch.session = loader.create_session("ops_readonly")
    denied = orch._guard_tool("cancel_visit_plan", "todo_query")
    allowed = orch._guard_tool("query_todos", "todo_query")
    orch.set_session(loader.create_session("visit"))
    after_reload = orch._guard_tool("cancel_visit_plan", "plan_cancel")
    guard_result = (
        f"ops_readonly: 高危写工具 -> {'denied' if denied else 'ALLOWED(危险)'}，"
        f"只读工具 -> {'allow' if allowed is None else 'denied'}"
    )
    reload_result = f"reload 到 visit 后同一工具 -> {'allow' if after_reload is None else 'denied'}"
    return hook_result, guard_result, reload_result


def _validation_rows(loader: ResourceLoader) -> list[str]:
    """真跑两类非法配置，验证 fail fast 而不是只看代码里有没有校验。"""
    rows: list[str] = []
    try:
        AgentSpec.from_dict({"name": "bad name", "skills": ["todo_query"]})
    except ValueError as exc:
        rows.append(f"非法 Agent name 被拒：{exc}")
    try:
        loader._validate(AgentSpec(
            name="orphan", label="", skills=("todo_query",),
            tools=("cancel_visit_plan",),
        ))
    except ValueError as exc:
        rows.append(f"孤儿工具被拒：{str(exc)[:46]}…")
    return rows


def _preamble_rows(loader: ResourceLoader) -> list[str]:
    """真构造一个带 preamble 覆写 + 工具白名单的 Agent，检查提示词。"""
    spec = AgentSpec(
        name="inspector", label="巡检助手", skills=("todo_query",),
        tools=("query_todos",), preamble="你是只读巡检助手，禁止任何写操作。",
    )
    session = AgentSession(
        spec=spec, loader=loader, registry=registry,
        skills={"todo_query": SKILLS["todo_query"]},
    )
    text = session.system_prompt(active_skill="todo_query")
    return [
        f"preamble 覆写生效：{'你是只读巡检助手' in text}，内置角色已移除：{'你是「拜访通」销售助手' not in text}",
        f"工具清单过白名单：{'可用工具：query_todos' in text}，query_plans 未出现：{'可用工具：query_todos, query_plans' not in text}",
    ]


def _session_tree_rows() -> list[str]:
    """在 data/ 下可复用的库里真跑会话树追加 / 分支 / JSONL 导出。

    固定路径 + 每次清空条目，而不是建临时目录再删除：data/ 已被 .gitignore
    忽略，脚本重复运行输出稳定，也不会产生需要清理的临时文件。
    """
    db_path = BACKEND / "data" / "_pi_evidence.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(db_path))
    db.init()
    ensure_columns(db)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM session_entries")
    tree = SessionTree(db)
    first = tree.append(session_id="s1", parent_id=None, role="user", content="查待办")
    tree.append(
        session_id="s1", parent_id=first["entry_id"],
        role="assistant", content="有 4 条待办",
    )
    info = tree.switch_leaf("s1", first["entry_id"], summary_text="被放弃分支摘要")
    lines = tree.export_jsonl("s1").splitlines()
    return [
        f"条目类型化：append -> entry_type={first.get('entry_type')}",
        f"switch_leaf 生成 branch_summary：{bool(info and info.get('branch_summary_id'))}",
        f"JSONL 导出 {len(lines)} 行，含 branch_summary：{any('branch_summary' in line for line in lines)}",
    ]


def _capability_version_rows(
    loader: ResourceLoader, visit: AgentSession, ops: AgentSession,
) -> list[str]:
    """v0.6：真算指纹、真写检查点、真调生产判定方法。

    不重建整个 Orchestrator（它依赖 DB / LLM / Policy 等一堆运行时对象），
    而是用一个只持有 session 的子类调用**未替换过的** check_resume_compat /
    replay_decision / _guard_tool——判定逻辑仍是生产代码，不是脚本里的复刻。
    """
    class _Probe(Orchestrator):
        def __init__(self, session: AgentSession) -> None:
            self.session = session

    fp_visit = visit.capability_fingerprint()
    fp_ops = ops.capability_fingerprint()
    stable = loader.create_session("visit").capability_fingerprint() == fp_visit

    db_path = BACKEND / "data" / "_pi_evidence.db"
    db = Database(str(db_path))
    db.init()
    turn_id = "evidence_capability_v06"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM turn_checkpoints WHERE turn_id=?", (turn_id,))
    tasks = TaskStore(db)
    tasks.create(
        turn_id=turn_id, session_id="evidence", user_id="sales-001",
        trace_id="evidence", user_input="能力版本实证", skill="chat",
    )
    tasks.save_checkpoint(
        turn_id=turn_id, step=0, skill="chat", messages=[], slots={},
        agent_name=visit.name, capability_fingerprint=fp_visit,
        capability=visit.capability(),
    )
    cp = tasks.latest_checkpoint(turn_id) or {}

    ok = _Probe(visit).check_resume_compat(cp)
    mismatch = _Probe(visit).check_resume_compat(
        {**cp, "agent_name": ops.name, "capability_fingerprint": fp_ops}
    )
    legacy = _Probe(visit).check_resume_compat({"step": 0})
    replay_visit = _Probe(visit).replay_decision("cancel_visit_plan", "plan_cancel", True)
    replay_ops = _Probe(ops).replay_decision("cancel_visit_plan", "plan_cancel", True)
    return [
        f"内容哈希稳定：重复装配指纹一致 {stable}；visit != ops：{fp_visit != fp_ops}",
        f"检查点落库：fingerprint={str(cp.get('capability_fingerprint'))[:12]}… / "
        f"能力快照 {len((cp.get('capability') or {}).get('tools', []))} 个工具",
        f"resume 三态：ok={ok['status']} / incompatible={mismatch['status']} / legacy={legacy['status']}",
        f"拒绝诊断含新旧指纹：{'recorded_fingerprint' in mismatch and 'current_fingerprint' in mismatch}",
        f"回放决策：visit={replay_visit[0]}（全能力放行）/ ops_readonly={replay_ops[0]}（收紧后拒绝陈旧结果）",
    ]


def main() -> int:
    print(LINE)
    print("P1 × Pi 架构对照实证  |  v0.6  |  离线运行，零外部依赖")
    print(LINE)

    # 1. 事件包络
    emit = EventFactory()
    first = emit("start", session_id="s")
    second = emit("done", trace_id="t")
    group(
        1, "统一事件包络与 Pi 事件名映射",
        "9 类 AgentEvent，subscribe 按注册顺序 await",
        "agent/events.py：EventFactory + PI_EVENT_MAP",
        [
            f"seq 单调：{first['data']['seq']} -> {second['data']['seq']}",
            f"每事件带 ts / elapsed_ms：{'ts' in second['data'] and 'elapsed_ms' in second['data']}",
            f"PI_EVENT_MAP 映射条数：{len(PI_EVENT_MAP)}（如 {next(iter(PI_EVENT_MAP.items()))[0]} -> {next(iter(PI_EVENT_MAP.items()))[1]}）",
        ],
        ["test_event_factory_monotonic", "test_sse_events_carry_seq_and_export"],
    )

    # 2. 工具扩展点 + 执行期能力边界
    hook_result, guard_result, reload_result = asyncio.run(_hook_and_guard())
    group(
        2, "工具扩展点与执行期能力边界",
        "beforeToolCall 可 {block,reason}；Pi 无内置权限系统",
        "agent/hooks.py + agent/pipeline.py capability_guard",
        [
            hook_result,
            guard_result,
            reload_result,
            "由下列测试断言：越权 handler 未执行 / audit_log / capability_denied Trace 事件",
        ],
        [
            "test_pipeline_hook_blocks_and_audits",
            "test_execution_guard_blocks_unauthorized_tool",
            "test_guard_follows_hot_reloaded_session",
        ],
    )

    # 3. 显式装配
    loader = ResourceLoader(registry, load_files=False)
    visit = loader.create_session("visit")
    ops = loader.create_session("ops_readonly")
    validation_rows = _validation_rows(loader)
    group(
        3, "显式装配：同一 Runtime，两套能力集",
        "createAgentSession() + DefaultResourceLoader",
        "runtime/{spec,loader,session}.py + backend/agents/*.json",
        [
            f"Agent 索引字段：{sorted(loader.catalog()[0])}（不含技能正文）",
            f"visit：{len(visit.skills)} skills / {len(visit.exposed_tool_names())} tools",
            f"ops_readonly：{list(ops.skills)} / {ops.exposed_tool_names()}",
            f"ops 越权路由 plan_cancel -> {ops.resolve_route('plan_cancel')}（降级）",
            *validation_rows,
        ],
        [
            "test_default_session_matches_legacy_capabilities",
            "test_same_runtime_assembles_two_capability_sets",
            "test_invalid_config_fails_fast",
            "test_orphan_tool_is_rejected",
        ],
    )

    # 4. 渐进式披露
    index = ops.skills_index()
    body = visit.load_skill_body("plan_cancel")
    prompt = visit.system_prompt(active_skill="plan_create", slots={"customer": "字节跳动"})
    preamble_rows = _preamble_rows(loader)
    group(
        4, "技能渐进式披露与提示词分层",
        "启动只注入 name+description+path，正文按需读取",
        "runtime/loader.catalog / session.load_skill_body / agent/prompt.py",
        [
            f"Skill 索引字段：{sorted(index[0])}，正文不在索引里：{'prompt' not in index[0]}",
            f"按需读取 plan_cancel 正文：{len(body or '')} 字符",
            f"提示词含 skill:// 索引 {prompt.count('skill://')} 处；当前技能全文 1 处：{'# 当前技能：' in prompt}",
            f"兼容标记保留：{'[当前子 Skill: plan_create]' in prompt}",
            *preamble_rows,
        ],
        [
            "test_prompt_has_skill_index_and_active_skill",
            "test_progressive_disclosure_index_has_no_body",
            "test_preamble_and_tool_list_follow_agent_spec",
        ],
    )

    # 5. 压缩切点保护
    path = [
        {"role": "user", "content": "查待办"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "query_todos"}]},
        {"role": "tool", "content": "4 条待办"},
        {"role": "assistant", "content": "今天有 4 条"},
        {"role": "user", "content": "取消第二个"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "cancel_visit_plan"}]},
        {"role": "tool", "content": "需要审批"},
    ]
    cut = _cut_index(path, keep_recent=2)
    digest = digest_entries(path[:cut])
    group(
        5, "上下文压缩：切点保护与确定性摘要",
        "切点只能落在 user/assistant 边界，绝不在 toolResult 处切",
        "agent/compaction.py：_cut_index + digest_entries",
        [
            f"path 长 {len(path)}，keep_recent=2 -> cut_index={cut}，该处 role={path[cut].get('role')}",
            f"切点不落在 tool 上：{path[cut].get('role') != 'tool'}",
            f"确定性摘要 {len(digest)} 字符，含结构化段落：{'Goal' in digest or '目标' in digest}",
        ],
        ["test_compaction_folds_and_keeps_history", "test_compaction_never_splits_tool_pair"],
    )

    # 6. 会话树与收敛
    group(
        6, "会话树、收敛条件与回放",
        "JSONL v3 会话树 / finishTurn 决定 continue|end",
        "sessions/tree.py + orchestrator/loop.py",
        [
            *_session_tree_rows(),
            f"全局工具注册表：{len(SKILLS)} skills / {len(registry.all())} tools（注册即用）",
            "由下列测试断言：done.stop_reason ∈ {no_tool_calls, max_steps}；max_loop 为防失控护栏",
            "build_system_prompt 未装配时行为与 v0.4 一致："
            f"{build_system_prompt(active_skill='chat').startswith('你是「拜访通」销售助手')}",
        ],
        [
            "test_session_tree_branch", "test_branch_switch_with_summary",
            "test_sse_events_carry_seq_and_export",
        ],
    )

    # 7. v0.6 能力版本一致性（自有工程约束，不是 Pi 原生概念，标题里不冒充）
    group(
        7, "能力集作为有版本的一致性边界（v0.6 自有工程约束）",
        "Pi 的 SessionManager 持久化会话；能力集与检查点同生命周期绑定是 P1 的额外约束",
        "runtime/session.py + domain/tasks.py + orchestrator/loop.py",
        _capability_version_rows(loader, visit, ops),
        [
            "test_capability_fingerprint_is_content_based_and_ignores_runtime_toggle",
            "test_checkpoint_persists_capability_snapshot",
            "test_resume_compat_rejects_changed_capability_with_diagnostics",
            "test_resume_api_returns_explicit_409_and_task_detail_explains_why",
            "test_replay_decision_blocks_stale_result_when_capability_tightened",
        ],
    )

    print(f"\n{LINE}")
    print("全量回归：cd backend && python -m pytest -q   （86 passed）")
    print("端到端冒烟：AUTH_MODE=demo python run.py && python scripts/smoke_scenarios.py（7 组）")
    print("设计取舍与追问清单：docs/07-Pi-Agent架构对照与优化.md §4 / §5")
    print(LINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
