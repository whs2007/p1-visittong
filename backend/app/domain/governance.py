"""治理台账：配置与权限类动作的 append-only 记录。

与 `audit_log` 的分工（刻意的，不是重复造表）：

| 表 | 回答的问题 | 典型内容 |
|---|---|---|
| `audit_log` | 业务发生了什么 | 建计划、签到、取消、审批决定、工具调用结果 |
| `governance_events` | **谁改了系统的行为边界** | 工具启停、Agent 热更新、人工豁免、审批决策、权限拒绝 |

为什么要分开：审计报告里「工具启停无留痕」是硬伤 —— 一个主管把
`cancel_visit_plan` 停掉，整个高危审批链路就静默失效了，而 `audit_log` 里
什么都没有。配置动作和业务动作的**检索维度不同**（前者按 action+actor，
后者按 target），混在一张表里只能靠 JSON 字符串 grep。

只做追加：没有 update / delete 方法。要「撤销」就再写一条相反动作，
历史不可篡改 —— 这是台账能作为证据的前提。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.utils import now_iso


class GovernanceStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        *,
        action: str,
        actor: str = "system",
        actor_role: str = "",
        target_type: str = "",
        target_id: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str = "",
        trace_id: str = "",
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO governance_events"
                " (actor, actor_role, action, target_type, target_id, before_json, after_json,"
                "  reason, trace_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    actor, actor_role, action, target_type, target_id,
                    json.dumps(before or {}, ensure_ascii=False),
                    json.dumps(after or {}, ensure_ascii=False),
                    reason, trace_id, now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def list_events(
        self, *, action: str | None = None, actor: str | None = None,
        target_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM governance_events WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action=?"
            params.append(action)
        if actor:
            sql += " AND actor=?"
            params.append(actor)
        if target_id:
            sql += " AND target_id=?"
            params.append(target_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        conn = self._conn()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = {k: row[k] for k in row.keys()}
            for key in ("before_json", "after_json"):
                try:
                    item[key.replace("_json", "")] = json.loads(item.get(key) or "{}")
                except ValueError:
                    item[key.replace("_json", "")] = {}
                item.pop(key, None)
            out.append(item)
        return out

    def count(self, *, action: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM governance_events"
        params: tuple[Any, ...] = ()
        if action:
            sql += " WHERE action=?"
            params = (action,)
        conn = self._conn()
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        finally:
            conn.close()
