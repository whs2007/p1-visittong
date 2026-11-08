"""可观测：看板聚合、Trace 明细、治理台账、指标。

v0.7 的授权修正（审计 §4.2 第 4 点）：
`/traces`、`/traces/{id}`、`/dashboard/stats` 过去只校验「是否登录」，任一销售
可以读别人的 Trace（含输入原文、工具参数、审批信息）。现在统一按归属过滤：
- 列表：销售强制 `user_id=自己`，主管可看全部；
- 明细：非本人且非主管返回 **404**（不泄露「这条存在」）；
- 看板：销售只看自己的聚合口径，并在 summary 里标注 `scope`。

治理台账同时从 `governance_events` 查询（结构化），`audit_log` 里旧的
`channel=manual_override` 记录作为历史兼容并入，不再作为唯一来源。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.api.deps import get_ctx, get_current_user
from app.auth import can_view_trace, ensure_manager, is_manager
from app.context import AppContext
from app.errors import not_found
from app.skills.visit_skills import skill_catalog

router = APIRouter(prefix="/api", tags=["obs"])


@router.get("/audit")
async def audit_log(
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """操作审计（工具写操作/审批/手工接口），主管可查。"""
    ensure_manager(user, action="查看操作审计", hint="")
    return {"audit": ctx.db.list_audit(limit)}


@router.get("/governance/events")
async def governance_events(
    action: str | None = None,
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """治理台账：谁改了系统的行为边界（工具启停 / Agent 热更新 / 审批 / 豁免）。"""
    ensure_manager(user, action="查看治理台账", hint="")
    store = getattr(ctx, "governance", None)
    if store is None:
        return {"events": [], "count": 0}
    items = store.list_events(action=action, limit=limit)
    return {"events": items, "count": len(items)}


@router.get("/governance/manual-overrides")
async def manual_overrides(
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """人工降级通道的豁免台账（主管可查）。

    把「审批有例外」从一句口头声明变成可举证的数据：每次豁免都有操作者、
    目标、豁免理由与时间戳，可统计、可回放。没有这个视图，「显式豁免」
    和「偷偷开后门」在证据上无法区分。

    数据源已从「grep audit_log 的 JSON 字段」改为 `governance_events`
    结构化查询（action=plan.cancel.manual），旧记录作为历史兼容并入。
    """
    ensure_manager(user, action="查看治理豁免台账", hint="")
    items: list[dict[str, Any]] = []
    if getattr(ctx, "governance", None) is not None:
        for row in ctx.governance.list_events(action="plan.cancel.manual", limit=limit):
            after = row.get("after") or {}
            items.append({
                "actor": row.get("actor"),
                "action": row.get("action"),
                "target_id": row.get("target_id"),
                "owner_id": after.get("owner_id"),
                "approval": after.get("approval"),
                "waiver_reason": row.get("reason") or after.get("waiver_reason"),
                "created_at": row.get("created_at"),
                "source": "governance_events",
            })
    # 历史兼容：v0.6 之前的豁免只写在 audit_log
    for row in ctx.db.list_audit(min(max(limit, 1), 500)):
        try:
            after = json.loads(row.get("after") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(after, dict) or after.get("channel") != "manual_override":
            continue
        items.append({
            "actor": row.get("actor"),
            "action": row.get("action"),
            "target_id": row.get("target_id"),
            "owner_id": after.get("owner_id"),
            "approval": after.get("approval"),
            "waiver_reason": after.get("waiver_reason"),
            "created_at": row.get("created_at"),
            "source": "audit_log",
        })
    # 去重：v0.7 之后同一次豁免会**同时**写 governance_events 和 audit_log，
    # 直接拼起来会把豁免数量算成两倍（冒烟 s5 就是被这个卡住的）。
    # 以 (action, target_id, created_at) 为自然键合并，governance 版本优先
    # （它带结构化的 reason，比 audit_log 里的 JSON 片段更可信）。
    merged: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for item in items:
        key = (item.get("action"), item.get("target_id"), item.get("created_at"))
        if key in merged and merged[key].get("source") == "governance_events":
            continue
        merged[key] = item
    items = sorted(
        merged.values(), key=lambda x: str(x.get("created_at") or ""), reverse=True
    )[:limit]
    return {
        "policy": {
            "scope": "Agent 自主通道的高危动作 100% 进入审批",
            "exception": "人工表单降级通道（POST /api/plans/{id}/cancel）不进入审批",
            "constraints": [
                "仅 manager/admin",
                "仍走显式 FSM 迁移表，终态不可取消",
                "每次豁免写入 governance_events（含 actor/target/reason）",
                "主管可通过本端点拉取全部豁免记录",
            ],
            "rationale": "审批约束模型自主动作；模型不可用时业务不能锁死，故保留高权限人工通道，降级不降权。",
        },
        "count": len(items),
        "items": items,
    }


@router.get("/dashboard/stats")
async def dashboard(
    window_hours: int | None = None,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """看板：主管看全量，销售只看自己（同一套聚合口径）。"""
    scope_user = None if is_manager(user) else user["user_id"]
    data = await ctx.traces.dashboard(20, user_id=scope_user, window_hours=window_hours)
    skill_meta = {s["name"]: s for s in skill_catalog()}
    skills = []
    for name, meta in skill_meta.items():
        stats = data["skill_stats"].get(name, {})
        skills.append(
            {
                "name": name,
                "label": meta["label"],
                "status": "ENABLED",
                "calls": stats.get("calls", 0),
                "failed": stats.get("failed", 0),
                "avg_ms": stats.get("avg_ms", 0),
            }
        )
    tools = []
    for tool in ctx.registry.catalog():
        stats = data["tool_stats"].get(tool["name"], {})
        tools.append(
            {
                "name": tool["name"],
                "label": tool["label"],
                "status": "ENABLED" if tool["enabled"] else "DISABLED",
                "risk": tool["risk"],
                "calls": stats.get("calls", 0),
                "failed": stats.get("failed", 0),
                "avg_ms": stats.get("avg_ms", 0),
            }
        )
    return {
        **{k: data[k] for k in ("summary", "latency", "calls")},
        "skills": skills,
        "tools": tools,
        "recent": data["recent"],
        "traces": [
            {
                "trace_id": t["trace_id"],
                "input": t["input"],
                "skill": t.get("skill"),
                "status": t["status"],
                "latency_ms": t["latency_ms"],
                "created_at": t["created_at"],
            }
            for t in data["recent"]
        ],
    }


@router.get("/traces")
async def traces(
    status: str | None = None,
    skill: str | None = None,
    limit: int = 50,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Trace 列表：销售只能看到自己的，主管可看全部。"""
    owner = None if is_manager(user) else user["user_id"]
    return {
        "traces": await ctx.traces.list_traces(
            status=status, skill=skill, user_id=owner, limit=limit
        )
    }


def _scalar(ctx: AppContext, sql: str, params: tuple = ()) -> int:
    row = ctx.db._row(ctx.db._conn().execute(sql, params).fetchone())  # noqa: SLF001
    if not row:
        return 0
    return int(next(iter(row.values())))


def _cost_summary(ctx: AppContext) -> dict[str, Any]:
    """Token / 成本归因：按 skill 与按天两个切面。

    单价来自配置（`price_in_per_mtok` / `price_out_per_mtok`），默认 0 表示只报
    token 不报钱——**不编一个假价格**比报一个看起来像真的数字更负责。
    """
    by_skill = ctx.db._rows(  # noqa: SLF001
        "SELECT COALESCE(skill,'unknown') AS skill, COUNT(*) AS turns,"
        " COALESCE(SUM(tokens_in),0) AS tokens_in, COALESCE(SUM(tokens_out),0) AS tokens_out,"
        " COALESCE(SUM(cost_usd),0) AS cost_usd"
        " FROM traces GROUP BY COALESCE(skill,'unknown') ORDER BY tokens_in DESC"
    )
    by_day = ctx.db._rows(  # noqa: SLF001
        "SELECT substr(created_at,1,10) AS day, COUNT(*) AS turns,"
        " COALESCE(SUM(tokens_in),0) AS tokens_in, COALESCE(SUM(tokens_out),0) AS tokens_out,"
        " COALESCE(SUM(cost_usd),0) AS cost_usd"
        " FROM traces GROUP BY day ORDER BY day DESC LIMIT 14"
    )
    totals = {
        "turns": sum(int(r["turns"] or 0) for r in by_skill),
        "tokens_in": sum(int(r["tokens_in"] or 0) for r in by_skill),
        "tokens_out": sum(int(r["tokens_out"] or 0) for r in by_skill),
        "cost_usd": round(sum(float(r["cost_usd"] or 0) for r in by_skill), 6),
    }
    totals["tokens_per_turn"] = (
        round((totals["tokens_in"] + totals["tokens_out"]) / totals["turns"], 1) if totals["turns"] else 0.0
    )
    return {
        "totals": totals,
        "unit_price": {
            "in_per_mtok": ctx.settings.price_in_per_mtok,
            "out_per_mtok": ctx.settings.price_out_per_mtok,
            "currency": "USD",
        },
        "by_skill": by_skill,
        "by_day": by_day,
    }


def _alerts(ctx: AppContext, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """阈值告警：只报「样本量够 + 超阈值」的两类，避免冷启动就一片红。"""
    traces = snapshot["traces"]
    finished = int(traces["total"]) - int(traces["running"])
    alerts: list[dict[str, Any]] = []
    if finished >= ctx.settings.alert_min_samples:
        failure_rate = int(traces["failed"]) / finished
        if failure_rate > ctx.settings.alert_failure_rate:
            alerts.append({
                "name": "turn_failure_rate", "level": "warning",
                "value": round(failure_rate, 4), "threshold": ctx.settings.alert_failure_rate,
                "samples": finished,
                "hint": "Turn 失败率超过阈值：先看 error 事件的 type 分布，再看最近一次改动的工具",
            })
    decided = _scalar(ctx, "SELECT COUNT(*) FROM approvals WHERE status IN ('approved','rejected','timeout')")
    if decided >= ctx.settings.alert_min_samples:
        timeout_rate = _scalar(ctx, "SELECT COUNT(*) FROM approvals WHERE status='timeout'") / decided
        if timeout_rate > ctx.settings.alert_approval_timeout_rate:
            alerts.append({
                "name": "approval_timeout_rate", "level": "warning",
                "value": round(timeout_rate, 4), "threshold": ctx.settings.alert_approval_timeout_rate,
                "samples": decided,
                "hint": "审批超时率偏高：检查审批人是否在线 / 超时时间是否短于实际响应时间",
            })
    # 降级不设样本量门槛：它不是比率，是「能力已经少了一块」的状态事实
    if ctx.degradations.degraded:
        alerts.append({
            "name": "runtime_degraded",
            "level": "critical" if ctx.degradations.critical else "warning",
            "value": len(ctx.degradations.items),
            "threshold": 0,
            "samples": len(ctx.degradations.items),
            "hint": "存在降级组件：" + ctx.degradations.summary()
            + "；critical 表示多实例语义已不成立，不要横向扩容",
        })
    return alerts


def _metrics_snapshot(ctx: AppContext) -> dict[str, Any]:
    snapshot = {
        "traces": {
            "total": _scalar(ctx, "SELECT COUNT(*) FROM traces"),
            "failed": _scalar(ctx, "SELECT COUNT(*) FROM traces WHERE status='failed'"),
            "running": _scalar(ctx, "SELECT COUNT(*) FROM traces WHERE status='running'"),
        },
        "tool_executions": _scalar(ctx, "SELECT COUNT(*) FROM tool_executions"),
        "tool_attempts_unknown": _scalar(
            ctx, "SELECT COUNT(*) FROM tool_executions WHERE status='unknown'"
        ),
        "approvals_pending": _scalar(ctx, "SELECT COUNT(*) FROM approvals WHERE status='pending'"),
        "tasks": {
            "done": _scalar(ctx, "SELECT COUNT(*) FROM task_runs WHERE status='done'"),
            "failed": _scalar(ctx, "SELECT COUNT(*) FROM task_runs WHERE status='failed'"),
            "interrupted": _scalar(ctx, "SELECT COUNT(*) FROM task_runs WHERE status='interrupted'"),
            "awaiting_approval": _scalar(
                ctx, "SELECT COUNT(*) FROM task_runs WHERE status='awaiting_approval'"
            ),
        },
        "audit": _scalar(ctx, "SELECT COUNT(*) FROM audit_log"),
        "governance_events": _scalar(ctx, "SELECT COUNT(*) FROM governance_events"),
        "knowledge_docs": ctx.knowledge.count(),
        "tools": {
            "total": len(ctx.registry.all()),
            "enabled": len([t for t in ctx.registry.all() if t.enabled]),
            "write": len([t for t in ctx.registry.all() if t.scope == "write"]),
        },
        "storage": {"db": ctx.db.dialect, "redis": ctx.state.backend, "audio": ctx.audio.backend},
        "pipeline_events": _scalar(ctx, "SELECT COUNT(*) FROM ingest_events"),
        # 降级台账：启动时汇总一次，这里只做投影（不重新探测，避免指标接口有副作用）
        "degraded": ctx.degradations.degraded,
        "degraded_critical": ctx.degradations.critical,
        "degradations": ctx.degradations.as_dicts(),
        "cost": _cost_summary(ctx),
        "ingest": ctx.knowledge_sync.stats() if ctx.knowledge_sync is not None else {},
    }
    snapshot["alerts"] = _alerts(ctx, snapshot)
    return snapshot


@router.get("/metrics")
async def metrics(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    ensure_manager(user, action="查看指标", hint="")
    return _metrics_snapshot(ctx)


@router.get("/metrics/cost")
async def cost_metrics(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """成本归因（主管）：按 skill / 按天，外加当前活跃告警。"""
    ensure_manager(user, action="查看成本指标", hint="")
    return {**_cost_summary(ctx), "alerts": _alerts(ctx, _metrics_snapshot(ctx))}


# (指标名, 类型, 帮助文本)。counter 语义的用 _total，存量/瞬时值用 gauge，
# 不再出现「名字叫 _total、值是当前条数」这种自相矛盾的暴露方式。
_PROMETHEUS_SPEC: list[tuple[str, str, str]] = [
    ("visittong_traces_total", "gauge", "Total traces stored"),
    ("visittong_traces_failed", "gauge", "Traces in failed state"),
    ("visittong_traces_running", "gauge", "Traces currently running"),
    ("visittong_tool_executions_total", "gauge", "Recorded tool executions (idempotency ledger)"),
    ("visittong_tool_attempts_unknown_total", "gauge", "Tool executions whose effect state is unknown after a crash"),
    ("visittong_approvals_pending", "gauge", "Approvals waiting for a human decision"),
    ("visittong_tasks_done_total", "gauge", "Turns finished successfully"),
    ("visittong_tasks_failed_total", "gauge", "Turns finished with an error"),
    ("visittong_tasks_interrupted_total", "gauge", "Turns interrupted and resumable"),
    ("visittong_tasks_awaiting_approval", "gauge", "Turns suspended waiting for approval"),
    ("visittong_audit_entries_total", "gauge", "Business audit entries"),
    ("visittong_governance_events_total", "gauge", "Governance ledger entries"),
    ("visittong_knowledge_docs", "gauge", "Knowledge documents indexed"),
    ("visittong_tools_total", "gauge", "Registered tools"),
    ("visittong_tools_enabled", "gauge", "Enabled tools"),
    ("visittong_tools_write_scope", "gauge", "Tools with write scope"),
    ("visittong_pipeline_events_total", "gauge", "Ingest pipeline events"),
    ("visittong_llm_tokens_in_total", "gauge", "Prompt tokens accumulated over finished traces"),
    ("visittong_llm_tokens_out_total", "gauge", "Completion tokens accumulated over finished traces"),
    ("visittong_cost_usd", "gauge", "Estimated LLM cost in USD (unit price from config)"),
    ("visittong_knowledge_events_pending", "gauge", "Knowledge sync events waiting for the consumer"),
    ("visittong_knowledge_dlq_dead", "gauge", "Knowledge sync dead letters awaiting replay"),
    # 降级是「能力少了一块」，不是「请求失败了」——不暴露成指标就没人会看
    ("visittong_degraded", "gauge", "1 when any runtime component degraded to a weaker backend"),
    ("visittong_degraded_critical", "gauge", "1 when a critical degradation makes multi-instance semantics invalid"),
]


@router.get("/metrics/prometheus")
async def metrics_prometheus(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Prometheus 文本格式：带 `# HELP` / `# TYPE`，可被标准 scrape 解析。"""
    ensure_manager(user, action="查看指标", hint="")
    data = _metrics_snapshot(ctx)
    values = {
        "visittong_traces_total": data["traces"]["total"],
        "visittong_traces_failed": data["traces"]["failed"],
        "visittong_traces_running": data["traces"]["running"],
        "visittong_tool_executions_total": data["tool_executions"],
        "visittong_tool_attempts_unknown_total": data["tool_attempts_unknown"],
        "visittong_approvals_pending": data["approvals_pending"],
        "visittong_tasks_done_total": data["tasks"]["done"],
        "visittong_tasks_failed_total": data["tasks"]["failed"],
        "visittong_tasks_interrupted_total": data["tasks"]["interrupted"],
        "visittong_tasks_awaiting_approval": data["tasks"]["awaiting_approval"],
        "visittong_audit_entries_total": data["audit"],
        "visittong_governance_events_total": data["governance_events"],
        "visittong_knowledge_docs": data["knowledge_docs"],
        "visittong_tools_total": data["tools"]["total"],
        "visittong_tools_enabled": data["tools"]["enabled"],
        "visittong_tools_write_scope": data["tools"]["write"],
        "visittong_pipeline_events_total": data["pipeline_events"],
        "visittong_llm_tokens_in_total": data["cost"]["totals"]["tokens_in"],
        "visittong_llm_tokens_out_total": data["cost"]["totals"]["tokens_out"],
        "visittong_cost_usd": data["cost"]["totals"]["cost_usd"],
        "visittong_knowledge_events_pending": (data.get("ingest") or {}).get("events", {}).get("pending", 0),
        "visittong_knowledge_dlq_dead": (data.get("ingest") or {}).get("dlq", {}).get("dead", 0),
        "visittong_degraded": 1 if data["degraded"] else 0,
        "visittong_degraded_critical": 1 if data["degraded_critical"] else 0,
    }
    lines: list[str] = []
    for name, kind, help_text in _PROMETHEUS_SPEC:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name} {values.get(name, 0)}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/traces/{trace_id}")
async def trace_detail(
    trace_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    trace = await ctx.traces.get_trace(trace_id)
    if not trace or not can_view_trace(user, trace.get("user_id")):
        # 越权与不存在返回同一个 404：不泄露「这条 Trace 存在」
        raise not_found("TRACE_NOT_FOUND", "Trace 不存在")
    events = await ctx.traces.get_events(trace_id)
    return {"trace": trace, "events": events}


@router.get("/tool-outputs/{ref}")
async def tool_output(
    ref: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """工具输出的「引用化」回查点。

    上下文里只留裁剪后的首尾 + `ref`，要看全文就按 ref 取——取到的是**脱敏后**
    的全文（原始 PII 从未落库）。越权与不存在同样返回 404，不泄露存在性。
    """
    row = ctx.governor.fetch(ref) if ctx.governor is not None else None
    if row is None or (not is_manager(user) and row.get("user_id") != str(user.get("user_id") or "")):
        raise not_found("TOOL_OUTPUT_NOT_FOUND", "引用不存在或已过期")
    row.pop("user_id", None)
    return {"output": row, "sha256_verified": bool(row.get("sha256"))}
