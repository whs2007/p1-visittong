"""Durable Execution 存储：任务运行、步骤检查点、工具执行去重。

设计目标（对齐模板的"断点续跑"）：
- 每个 Turn 落一条 task_runs（running → done / failed / interrupted）；
- 每步工具执行后写 turn_checkpoints（模型上下文 + 槽位 + 计数），进程崩溃后可
  从最后检查点继续；
- tool_executions 以 (turn_id, step, call_id) 唯一约束做工具级幂等，resume/重放
  时不会重复产生副作用（重复签到、重复建计划等）；
- 服务重启时把遗留的 running 任务标为 interrupted，供 resume 接口消费。
"""
from __future__ import annotations

import json
from typing import Any

from app.domain.db import Database
from app.utils import new_id, now_iso


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # 任务
    # ------------------------------------------------------------------ #
    def create(
        self, *, turn_id: str, session_id: str, user_id: str, trace_id: str, user_input: str,
        skill: str | None = None,
    ) -> dict[str, Any]:
        now = now_iso()
        self.db._exec(  # noqa: SLF001
            "INSERT OR IGNORE INTO task_runs (turn_id,session_id,user_id,trace_id,status,step,skill,user_input,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (turn_id, session_id, user_id, trace_id, "running", 0, skill, user_input, now, now),
        )
        return self.get(turn_id)  # type: ignore[return-value]

    def get(self, turn_id: str) -> dict[str, Any] | None:
        return self.db._row(  # noqa: SLF001
            self.db._conn().execute("SELECT * FROM task_runs WHERE turn_id=?", (turn_id,)).fetchone()  # noqa: SLF001
        )

    def update(self, turn_id: str, **fields: Any) -> None:
        allowed = {"status", "step", "skill", "stop_reason", "error", "trace_id", "resume_count", "finished_at"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        params.extend([now_iso(), turn_id])
        self.db._exec(  # noqa: SLF001
            f"UPDATE task_runs SET {', '.join(sets)} WHERE turn_id=?", tuple(params)
        )

    def finish(
        self, turn_id: str, status: str, *, stop_reason: str | None = None,
        error: str | None = None, step: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {"status": status, "finished_at": now_iso()}
        if stop_reason is not None:
            fields["stop_reason"] = stop_reason
        if error is not None:
            fields["error"] = error
        if step is not None:
            fields["step"] = step
        self.update(turn_id, **fields)

    def reconcile_interrupted(self) -> int:
        """服务启动时把上次崩溃遗留的 running 任务标为 interrupted（可 resume）。"""
        now = now_iso()
        with self.db._conn() as conn:  # noqa: SLF001
            rows = conn.execute("UPDATE task_runs SET status='interrupted', updated_at=? WHERE status='running'", (now,)).rowcount
        return rows

    # ------------------------------------------------------------------ #
    # 检查点
    # ------------------------------------------------------------------ #
    def save_checkpoint(
        self, *, turn_id: str, step: int, skill: str, messages: list[dict[str, Any]],
        slots: dict[str, Any], tool_calls: int = 0, tokens_in: int = 0, tokens_out: int = 0,
        agent_name: str | None = None,
        capability_fingerprint: str | None = None,
        capability: dict[str, Any] | None = None,
    ) -> str:
        """检查点绑定本次装配的能力集。

        为什么需要：resume 会用**当前** Agent 的能力视图继续执行，而 checkpoint
        里的 messages 是**旧** Agent 的系统提示词与工具结果。若中途 reload 换了
        能力集，静默续跑等于「用新权限执行旧意图」，是权限提升。三者一起存：
        name 便于人读、fingerprint 用于比较、完整 JSON 用于事后举证。
        """
        checkpoint_id = new_id("ck")
        self.db._exec(  # noqa: SLF001
            "INSERT INTO turn_checkpoints (checkpoint_id,turn_id,step,skill,messages,slots,tool_calls,tokens_in,tokens_out,agent_name,capability_fingerprint,capability_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                checkpoint_id, turn_id, step, skill,
                json.dumps(messages, ensure_ascii=False),
                json.dumps(slots, ensure_ascii=False),
                tool_calls, tokens_in, tokens_out,
                agent_name, capability_fingerprint,
                json.dumps(capability or {}, ensure_ascii=False), now_iso(),
            ),
        )
        self.update(turn_id, step=step, skill=skill)
        return checkpoint_id

    def latest_checkpoint(self, turn_id: str) -> dict[str, Any] | None:
        row = self.db._row(  # noqa: SLF001
            self.db._conn()  # noqa: SLF001
            .execute("SELECT * FROM turn_checkpoints WHERE turn_id=? ORDER BY step DESC, created_at DESC LIMIT 1", (turn_id,))
            .fetchone()
        )
        if not row:
            return None
        row["messages"] = json.loads(row.get("messages") or "[]")
        row["slots"] = json.loads(row.get("slots") or "{}")
        try:
            row["capability"] = json.loads(row.get("capability_json") or "{}")
        except (TypeError, ValueError):
            row["capability"] = {}
        return row

    # ------------------------------------------------------------------ #
    # 工具执行去重（幂等）
    # ------------------------------------------------------------------ #
    def saved_tool(self, turn_id: str, step: int, call_id: str) -> dict[str, Any] | None:
        return self.db._row(  # noqa: SLF001
            self.db._conn()  # noqa: SLF001
            .execute("SELECT * FROM tool_executions WHERE turn_id=? AND step=? AND call_id=?", (turn_id, step, call_id))
            .fetchone()
        )

    def save_tool(
        self, *, turn_id: str, step: int, call_id: str, name: str,
        status: str, output: str, data: dict[str, Any] | None,
    ) -> None:
        self.db._exec(  # noqa: SLF001
            "INSERT OR IGNORE INTO tool_executions (turn_id,step,call_id,name,status,output,data,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (turn_id, step, call_id, name, status, output, json.dumps(data or {}, ensure_ascii=False), now_iso()),
        )

    def tool_count(self, turn_id: str) -> int:
        row = self.db._row(  # noqa: SLF001
            self.db._conn().execute("SELECT COUNT(*) AS n FROM tool_executions WHERE turn_id=?", (turn_id,)).fetchone()  # noqa: SLF001
        )
        return int(row["n"]) if row else 0