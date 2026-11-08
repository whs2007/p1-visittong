"""效果级幂等（exactly-once 的落地方式）。

为什么不是「把副作用和账本塞进同一个事务」：
本项目的副作用分布在多个组件里 —— 业务表走 `Database` 的连接、Trace 走
`TraceStore` 的连接、未来还有 CRM / 短信这类**根本没有事务**的外部系统。
把所有东西塞进一个 SQLite 事务，只能覆盖「纯本地写」，一旦接真实外部
服务立刻失效，属于假的安全感。

这里采用工业界更通用、也更能扛追问的三段式：

1. **声明（claim）**：执行前先提交一条 `state=running` 的幂等记录。崩溃后
   留下的 `running` 就是「这次副作用状态未知」的显式信号，而不是静默重放。
2. **执行 + 记账（finish）**：把完整 ToolResult 写回同一行，`state=done`。
3. **回放（replay）**：同 key 再次进来时直接返回**当初那个结果**，不重跑 handler。

再叠加业务侧的自然唯一性（计划时间冲突、FSM 状态机不允许重复签到/重复取消），
形成「账本幂等 + 业务幂等」双保险 —— 即使崩溃窗口里 `running` 没来得及变
`done`，重复执行也会被 FSM 或冲突检测挡住，而不是产生第二条副作用。

**如实声明边界**：`save_visit_log` 这类天然可重复的写操作，在崩溃窗口内
仍可能留下两条日志；这是有意的取舍（宁可多一条日志，也不丢用户输入），
评测集里有对应用例覆盖，不是「假装没有」。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.utils import now_iso


class IdempotencyStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ #
    def begin(
        self, *, key: str, effect_type: str, turn_id: str, step: int, call_id: str
    ) -> dict[str, Any]:
        """声明一次效果。返回该 key 的当前记录（新建或已存在）。"""
        now = now_iso()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT OR IGNORE INTO idempotency_effects"
                " (idem_key, effect_type, state, turn_id, step, call_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (key, effect_type, "running", turn_id, step, call_id, now, now),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                "SELECT * FROM idempotency_effects WHERE idem_key=?", (key,)
            ).fetchone()
            conn.execute("COMMIT")
            record = {k: row[k] for k in row.keys()} if row else {}
            record["created"] = created
            return record
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def finish(
        self, *, key: str, resource_id: str, result: dict[str, Any]
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE idempotency_effects SET state='done', resource_id=?, result_json=?, updated_at=?"
                " WHERE idem_key=?",
                (resource_id, json.dumps(result, ensure_ascii=False), now_iso(), key),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM idempotency_effects WHERE idem_key=?", (key,)
            ).fetchone()
            return {k: row[k] for k in row.keys()} if row else None
        finally:
            conn.close()

    def unresolved(self, turn_id: str) -> list[dict[str, Any]]:
        """本 Turn 里「已声明但结果未知」的效果（崩溃对账用）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM idempotency_effects WHERE turn_id=? AND state='running'",
                (turn_id,),
            ).fetchall()
            return [{k: r[k] for k in r.keys()} for r in rows]
        finally:
            conn.close()

    def abandon(self, key: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE idempotency_effects SET state='abandoned', updated_at=? WHERE idem_key=? AND state='running'",
                (now_iso(), key),
            )
            conn.commit()
        finally:
            conn.close()


class IdemScope:
    """绑定到一次工具调用的幂等作用域，交给 handler 使用。

    handler 只需两行：进入时 `if (hit := ctx.idem.replay()): return hit`，
    结束时 `ctx.idem.remember(result, resource_id=...)`。
    没传 idem（单测/直调）时全部退化为 no-op，不改变原有行为。
    """

    def __init__(
        self, store: IdempotencyStore, *, key: str, effect_type: str, prior: dict[str, Any]
    ) -> None:
        self.store = store
        self.key = key
        self.effect_type = effect_type
        self.prior = prior
        #: 本地「已记账」标记：`prior` 是进入时的快照，handler 执行完它不会自动变；
        #: 用这个标记避免 pipeline 在 handler 记账后又用空 resource_id 覆盖一次。
        self._finished = str(prior.get("state") or "") == "done"

    @property
    def state(self) -> str:
        return "done" if self._finished else str(self.prior.get("state") or "running")

    @property
    def is_replay(self) -> bool:
        return self.state == "done"

    def replay(self):
        """返回当初的结果对象（延迟导入，避免 domain -> skills 的循环依赖）。"""
        if not self.is_replay:
            return None
        from app.skills.registry import ToolResult

        try:
            payload = json.loads(self.prior.get("result_json") or "{}")
        except ValueError:
            return None
        if not isinstance(payload, dict) or "status" not in payload:
            return None
        return ToolResult(
            payload.get("status") or "ok",
            payload.get("output") or "",
            payload.get("data") or {},
        )

    def remember(self, result: Any, resource_id: str = "") -> None:
        self.store.finish(
            key=self.key,
            resource_id=resource_id,
            result={
                "status": getattr(result, "status", "ok"),
                "output": getattr(result, "output", ""),
                "data": getattr(result, "data", None) or {},
            },
        )
        self._finished = True
