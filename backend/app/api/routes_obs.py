"""可观测：看板聚合与 Trace 明细。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.api.deps import get_ctx, get_current_user
from app.context import AppContext
from app.errors import ApiError, not_found
from app.skills.visit_skills import skill_catalog

router = APIRouter(prefix="/api", tags=["obs"])


@router.get("/audit")
async def audit_log(
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """操作审计（工具写操作/审批/手工接口），主管可查。"""
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以查看操作审计")
    return {"audit": ctx.db.list_audit(limit)}


@router.get("/dashboard/stats")
async def dashboard(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    data = await ctx.traces.dashboard(20)
    skill_meta = {s["name"]: s for s in skill_catalog()}
    skills = []
    for name, meta in skill_meta.items():
        stats = data["skill_stats"].get(name, {})
        skills.append(
            {
                "name": name,
                "label": meta["label"],
                "status": "PROD",
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
    return {"traces": await ctx.traces.list_traces(status=status, skill=skill, limit=limit)}


def _scalar(ctx: AppContext, sql: str, params: tuple = ()) -> int:
    row = ctx.db._row(ctx.db._conn().execute(sql, params).fetchone())  # noqa: SLF001
    if not row:
        return 0
    return int(next(iter(row.values())))


def _metrics_snapshot(ctx: AppContext) -> dict[str, Any]:
    return {
        "traces": {
            "total": _scalar(ctx, "SELECT COUNT(*) FROM traces"),
            "failed": _scalar(ctx, "SELECT COUNT(*) FROM traces WHERE status='failed'"),
            "running": _scalar(ctx, "SELECT COUNT(*) FROM traces WHERE status='running'"),
        },
        "tool_executions": _scalar(ctx, "SELECT COUNT(*) FROM tool_executions"),
        "approvals_pending": _scalar(ctx, "SELECT COUNT(*) FROM approvals WHERE status='pending'"),
        "tasks": {
            "done": _scalar(ctx, "SELECT COUNT(*) FROM task_runs WHERE status='done'"),
            "failed": _scalar(ctx, "SELECT COUNT(*) FROM task_runs WHERE status='failed'"),
            "interrupted": _scalar(ctx, "SELECT COUNT(*) FROM task_runs WHERE status='interrupted'"),
        },
        "audit": _scalar(ctx, "SELECT COUNT(*) FROM audit_log"),
        "knowledge_docs": ctx.knowledge.count(),
        "tools": {
            "total": len(ctx.registry.all()),
            "enabled": len([t for t in ctx.registry.all() if t.enabled]),
            "write": len([t for t in ctx.registry.all() if t.scope == "write"]),
        },
        "storage": {"db": ctx.db.dialect, "redis": ctx.state.backend, "audio": ctx.audio.backend},
        "pipeline_events": _scalar(ctx, "SELECT COUNT(*) FROM ingest_events"),
    }


@router.get("/metrics")
async def metrics(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以查看指标")
    return _metrics_snapshot(ctx)


@router.get("/metrics/prometheus")
async def metrics_prometheus(
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Prometheus 文本格式（可被 scrape；无需额外依赖）。"""
    if user["role"] not in ("manager", "admin"):
        raise ApiError(403, "FORBIDDEN", "只有主管可以查看指标")
    data = _metrics_snapshot(ctx)
    lines = []
    for name, value in (
        ("visittong_traces_total", data["traces"]["total"]),
        ("visittong_traces_failed", data["traces"]["failed"]),
        ("visittong_traces_running", data["traces"]["running"]),
        ("visittong_tool_executions_total", data["tool_executions"]),
        ("visittong_approvals_pending", data["approvals_pending"]),
        ("visittong_tasks_done", data["tasks"]["done"]),
        ("visittong_tasks_failed", data["tasks"]["failed"]),
        ("visittong_tasks_interrupted", data["tasks"]["interrupted"]),
        ("visittong_audit_entries", data["audit"]),
        ("visittong_knowledge_docs", data["knowledge_docs"]),
        ("visittong_tools_total", data["tools"]["total"]),
        ("visittong_tools_enabled", data["tools"]["enabled"]),
        ("visittong_tools_write_scope", data["tools"]["write"]),
        ("visittong_pipeline_events", data["pipeline_events"]),
    ):
        lines.append(f"{name} {value}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/traces/{trace_id}")
async def trace_detail(
    trace_id: str,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    trace = await ctx.traces.get_trace(trace_id)
    if not trace:
        raise not_found("TRACE_NOT_FOUND", "Trace 不存在")
    events = await ctx.traces.get_events(trace_id)
    return {"trace": trace, "events": events}