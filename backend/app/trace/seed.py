"""演示种子：5 条历史 Trace，让看板/时间线首屏不空。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.trace.store import TraceStore

DEMOS = [
    ("查一下我今天的拜访待办", "todo_query", 4200, 1, 100),
    ("帮我约下周三上午拜访字节跳动", "plan_create", 6100, 2, 160),
    ("把刚才那个计划改到下午 3 点", "plan_update", 3800, 2, 180),
    ("我到店了，签到", "checkin", 2900, 1, 90),
    ("帮我记一下：客户对新产品感兴趣", "visit_log", 2400, 1, 120),
]


async def seed_traces(traces: TraceStore) -> None:
    if await traces.count_traces() > 0:
        return
    base = datetime.now().astimezone() - timedelta(hours=3)
    for index, (text, skill, latency, tools, tokens) in enumerate(DEMOS):
        created = (base + timedelta(minutes=index * 17)).isoformat(timespec="seconds")
        trace_id = await traces.start_trace("s_seed", "sales-001", text)
        await traces.set_trace_skill(trace_id, skill)
        await traces.add_event(trace_id, "route", payload={"skill": skill, "confidence": 0.9})
        await traces.add_event(trace_id, "tool_result", name=skill, payload={"name": skill, "skill": skill, "label": skill})
        await traces.finish_trace(trace_id, "success", latency_ms=latency, tool_calls=tools,
                                  tokens_in=tokens, tokens_out=tokens // 2)
        await asyncio.to_thread(_backdate, traces, trace_id, created)


def _backdate(traces: TraceStore, trace_id: str, created: str) -> None:
    with traces._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE traces SET created_at=? WHERE trace_id=?", (created, trace_id))
        conn.execute("UPDATE trace_events SET ts=? WHERE trace_id=?", (created, trace_id))