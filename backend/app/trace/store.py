"""Trace 链路存储（SQLite）：一次 Turn 一条 trace，节点写入 trace_events。

表结构见 docs/03-开发文档.md §5.2。
所有 SQL 通过 asyncio.to_thread 执行，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from app.utils import avg_int, new_id, now_iso, percentile

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  trace_id    TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  user_id     TEXT,
  input       TEXT NOT NULL,
  skill       TEXT,
  status      TEXT NOT NULL,
  latency_ms  INTEGER DEFAULT 0,
  tool_calls  INTEGER DEFAULT 0,
  tokens_in   INTEGER DEFAULT 0,
  tokens_out  INTEGER DEFAULT 0,
  error       TEXT,
  created_at  TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at DESC);

CREATE TABLE IF NOT EXISTS trace_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id   TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  type       TEXT NOT NULL,
  name       TEXT,
  status     TEXT DEFAULT 'ok',
  latency_ms INTEGER DEFAULT 0,
  payload    TEXT,
  ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_trace ON trace_events(trace_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id     TEXT PRIMARY KEY,
  trace_id        TEXT,
  session_id      TEXT,
  tool            TEXT NOT NULL,
  args            TEXT,
  risk            TEXT,
  reason          TEXT,
  status          TEXT NOT NULL,
  requester       TEXT,
  approver        TEXT,
  decision_reason TEXT,
  created_at      TEXT NOT NULL,
  decided_at      TEXT,
  expires_at      TEXT,
  decision_nonce  TEXT,
  nonce_used_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


class TraceStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # Trace 生命周期
    # ------------------------------------------------------------------ #
    async def start_trace(self, session_id: str, user_id: str, user_input: str) -> str:
        trace_id = new_id("t")
        created = now_iso()

        def _write() -> None:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO traces (trace_id, session_id, user_id, input, status, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (trace_id, session_id, user_id, user_input, "running", created),
                )

        async with self._write_lock:
            await self._run(_write)
        return trace_id

    async def set_trace_skill(self, trace_id: str, skill: str) -> None:
        def _write() -> None:
            with self._conn() as conn:
                conn.execute("UPDATE traces SET skill=? WHERE trace_id=?", (skill, trace_id))

        async with self._write_lock:
            await self._run(_write)

    async def add_event(
        self,
        trace_id: str,
        event_type: str,
        name: str = "",
        status: str = "ok",
        latency_ms: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ts = now_iso()

        def _write() -> None:
            with self._conn() as conn:
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM trace_events WHERE trace_id = ?",
                    (trace_id,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO trace_events (trace_id, seq, type, name, status, latency_ms, payload, ts)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        trace_id, seq, event_type, name, status, latency_ms,
                        json.dumps(payload or {}, ensure_ascii=False), ts,
                    ),
                )

        async with self._write_lock:
            await self._run(_write)

    async def finish_trace(
        self,
        trace_id: str,
        status: str,
        *,
        latency_ms: int = 0,
        tool_calls: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> None:
        finished = now_iso()

        def _write() -> None:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE traces SET status=?, latency_ms=?, tool_calls=?, tokens_in=?,"
                    " tokens_out=?, cost_usd=?, error=?, finished_at=? WHERE trace_id=?",
                    (status, latency_ms, tool_calls, tokens_in, tokens_out, round(float(cost_usd or 0), 6),
                     error, finished, trace_id),
                )

        async with self._write_lock:
            await self._run(_write)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        def _read():
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
                return _row_to_dict(row) if row else None

        return await self._run(_read)

    async def get_events(self, trace_id: str) -> list[dict[str, Any]]:
        def _read():
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT seq, type, name, status, latency_ms, payload, ts FROM trace_events"
                    " WHERE trace_id=? ORDER BY seq",
                    (trace_id,),
                ).fetchall()
                out = []
                for row in rows:
                    item = _row_to_dict(row)
                    try:
                        item["payload"] = json.loads(item["payload"] or "{}")
                    except json.JSONDecodeError:
                        item["payload"] = {}
                    out.append(item)
                return out

        return await self._run(_read)

    async def recent_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        def _read():
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM traces ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
                ).fetchall()
                return [_row_to_dict(r) for r in rows]

        return await self._run(_read)

    async def list_traces(
        self, *, status: str | None = None, skill: str | None = None,
        user_id: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """可筛选的 Trace 列表（状态/技能/用户），供排障与审计检索。"""

        def _read():
            sql = "SELECT * FROM traces WHERE 1=1"
            params: list[Any] = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if skill:
                sql += " AND skill=?"
                params.append(skill)
            if user_id:
                sql += " AND user_id=?"
                params.append(user_id)
            sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
            params.append(max(1, min(limit, 200)))
            with self._conn() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [_row_to_dict(r) for r in rows]

        return await self._run(_read)

    async def count_traces(self) -> int:
        def _read():
            with self._conn() as conn:
                return conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]

        return await self._run(_read)

    # ------------------------------------------------------------------ #
    # 看板聚合
    # ------------------------------------------------------------------ #
    async def dashboard(
        self, limit: int = 20, *, user_id: str | None = None, window_hours: int | None = None
    ) -> dict[str, Any]:
        """看板聚合。

        v0.7 修正两处口径问题（审计 §3）：
        1. **分子分母同源**：旧实现成功率用全表 `traces`、工具统计却只取最近
           1000 条事件，两者不是同一个样本集；现在工具/技能统计都限定在同一批
           trace_id 内。
        2. **可按归属与时间窗过滤**：销售只看自己的数据，主管可看全部；
           `window_hours` 让「最近 24h」这类口径可复现，而不是隐式全表。
        """
        cutoff = None
        if window_hours:
            cutoff = (datetime.now().astimezone() - timedelta(hours=window_hours)).isoformat(
                timespec="seconds"
            )

        def _read():
            where = " WHERE 1=1"
            params: list[Any] = []
            if user_id:
                where += " AND user_id=?"
                params.append(user_id)
            if cutoff:
                where += " AND created_at>=?"
                params.append(cutoff)
            with self._conn() as conn:
                status_rows = [
                    dict(r) for r in conn.execute(
                        "SELECT trace_id, status, latency_ms, skill, tool_calls, created_at"
                        f" FROM traces{where}", tuple(params),
                    ).fetchall()
                ]
                ids = [r["trace_id"] for r in status_rows]
                event_rows: list[dict[str, Any]] = []
                # SQLite 变量上限 999，按 500 分批查
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    event_rows.extend(
                        dict(r) for r in conn.execute(
                            "SELECT payload, status, latency_ms, type FROM trace_events"
                            f" WHERE type='tool_result' AND trace_id IN ({marks})",
                            tuple(chunk),
                        ).fetchall()
                    )
            return status_rows, event_rows

        status_rows, event_rows = await self._run(_read)
        traces = await self.list_traces(user_id=user_id, limit=limit)

        finished = [r for r in status_rows if r["status"] in ("success", "failed")]
        success = sum(1 for r in finished if r["status"] == "success")
        failed = sum(1 for r in finished if r["status"] == "failed")
        running = sum(1 for r in status_rows if r["status"] == "running")
        latencies = [int(r["latency_ms"] or 0) for r in finished]
        total = len(finished)

        tool_stats: dict[str, dict[str, Any]] = {}
        for row in event_rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                continue
            name = payload.get("name") or "unknown"
            stats = tool_stats.setdefault(
                name,
                {"calls": 0, "failed": 0, "avg_ms": 0, "skill": payload.get("skill") or "system", "label": payload.get("label") or name},
            )
            stats["calls"] += 1
            stats["failed"] += 1 if row["status"] in ("error", "rejected", "denied", "timeout") else 0
            stats["avg_ms"] += int(row["latency_ms"] or 0)
            stats["label"] = payload.get("label") or stats["label"]
        for stats in tool_stats.values():
            stats["avg_ms"] = int(round(stats["avg_ms"] / stats["calls"])) if stats["calls"] else 0

        # Skill 统计：按 trace.skill 聚合（每次 Turn 只命中一个子 Skill）
        skill_stats: dict[str, dict[str, Any]] = {}
        for row in status_rows:
            skill = row.get("skill")
            if not skill:
                continue
            stats = skill_stats.setdefault(skill, {"calls": 0, "failed": 0, "avg_ms": 0})
            stats["calls"] += 1
            stats["failed"] += 1 if row["status"] == "failed" else 0
            stats["avg_ms"] += int(row["latency_ms"] or 0)
        for stats in skill_stats.values():
            stats["avg_ms"] = int(round(stats["avg_ms"] / stats["calls"])) if stats["calls"] else 0

        skill_counts = {
            "skill": sum(s["calls"] for s in skill_stats.values()),
            "tool": sum(s["calls"] for s in tool_stats.values()),
        }

        return {
            "summary": {
                "success_rate": int(round(success / total * 100)) if total else 0,
                "success": success,
                "failed": failed,
                "total": total,
                "running": running,
                "scope": "all" if not user_id else "self",
                "window_hours": window_hours,
            },
            "latency": {
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
                "avg": avg_int(latencies),
            },
            "calls": skill_counts,
            "tool_stats": tool_stats,
            "skill_stats": skill_stats,
            "recent": traces,
        }

    # ------------------------------------------------------------------ #
    # 审批单
    # ------------------------------------------------------------------ #
    async def create_approval(
        self,
        *,
        trace_id: str,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        risk: str,
        reason: str,
        timeout_s: int,
        requester: str = "",
    ) -> dict[str, Any]:
        approval_id = new_id("ap")
        created = now_iso()
        expires = (datetime.now().astimezone() + timedelta(seconds=timeout_s)).isoformat(timespec="seconds")
        # 一次性决策令牌：与审批单绑定、只在一次决策中有效。
        # 用 token_urlsafe 而不是自增/时间戳——可预测的令牌等于没有令牌。
        nonce = secrets.token_urlsafe(18)

        def _write():
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO approvals (approval_id, trace_id, session_id, tool, args, risk, reason,"
                    " status, requester, created_at, expires_at, decision_nonce)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        approval_id, trace_id, session_id, tool,
                        json.dumps(args, ensure_ascii=False), risk, reason,
                        "pending", requester, created, expires, nonce,
                    ),
                )

        async with self._write_lock:
            await self._run(_write)
        return {
            "approval_id": approval_id, "created_at": created, "expires_at": expires,
            "decision_nonce": nonce,
        }

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        def _read():
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
                if not row:
                    return None
                item = _row_to_dict(row)
                try:
                    item["args"] = json.loads(item["args"] or "{}")
                except json.JSONDecodeError:
                    item["args"] = {}
                return item

        return await self._run(_read)

    async def pending_approvals(self) -> list[dict[str, Any]]:
        def _read():
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at DESC"
                ).fetchall()
                out = []
                for row in rows:
                    item = _row_to_dict(row)
                    try:
                        item["args"] = json.loads(item["args"] or "{}")
                    except json.JSONDecodeError:
                        item["args"] = {}
                    out.append(item)
                return out

        return await self._run(_read)

    async def decide_approval(
        self, approval_id: str, decision: str, reason: str = "", approver: str = "", nonce: str | None = None,
    ) -> str:
        """按（状态, 一次性令牌）双条件 CAS 决策。

        返回 `ok / not_found / already_decided / token_invalid / token_missing`。
        为什么不是 bool：门禁和排查都需要区分「这张单已被处理」和「令牌不对」——
        前者是重复提交，后者可能是攻击或前端拿了过期的单子，处理方式完全不同。
        """
        decided = now_iso()

        def _write() -> str:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT status, decision_nonce, nonce_used_at FROM approvals WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    return "not_found"
                if row["status"] != "pending":
                    return "already_decided"
                if nonce is None or nonce == "":
                    return "token_missing"
                if not nonce or not secrets.compare_digest(str(row["decision_nonce"] or ""), nonce):
                    return "token_invalid"
                cur = conn.execute(
                    "UPDATE approvals SET status=?, approver=?, decision_reason=?, decided_at=?,"
                    " nonce_used_at=? WHERE approval_id=? AND status='pending'"
                    " AND decision_nonce=? AND nonce_used_at IS NULL",
                    (decision, approver or None, reason or None, decided, decided, approval_id, nonce),
                )
                return "ok" if cur.rowcount == 1 else "already_decided"

        async with self._write_lock:
            return await self._run(_write)

    async def expire_approval(self, approval_id: str) -> bool:
        def _write() -> bool:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE approvals SET status='timeout', decided_at=? WHERE approval_id=? AND status='pending'",
                    (now_iso(), approval_id),
                )
                return cur.rowcount == 1

        async with self._write_lock:
            return await self._run(_write)

    def ensure_schema_columns(self) -> None:
        """轻量迁移：给旧库补 requester（审批发起人）与一次性决策令牌两列。"""
        with self._conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
            if "requester" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN requester TEXT")
            if "decision_nonce" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN decision_nonce TEXT")
            if "nonce_used_at" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN nonce_used_at TEXT")

    async def reconcile_stale(self, *, include_traces: bool = True) -> dict[str, int]:
        """服务启动时收敛上次崩溃遗留的中间态。

        注意：**不再把所有 pending 审批一律 cancel**。审批等待态已经持久化
        （`task_runs.pending_approval_id` + `approvals` 表），重启后用户仍可
        审批，resume 能接回；如果启动就把单子全 cancel，等于自断恢复路径。
        这里只把**已过期**的单子标 timeout，其余保留 pending。

        `include_traces=False` 用于多 worker：把 running trace 标 failed 同样只能
        由一个进程做，否则会误伤其他 worker 正在跑的 Turn。
        """
        now = now_iso()

        def _write() -> tuple[int, int]:
            with self._conn() as conn:
                traces = 0
                if include_traces:
                    traces = conn.execute(
                        "UPDATE traces SET status='failed', error=COALESCE(error,'server_restart'), finished_at=?"
                        " WHERE status='running'",
                        (now,),
                    ).rowcount
                approvals = conn.execute(
                    "UPDATE approvals SET status='timeout', decided_at=?"
                    " WHERE status='pending' AND expires_at IS NOT NULL AND expires_at < ?",
                    (now, now),
                ).rowcount
                return traces, approvals

        async with self._write_lock:
            traces, approvals = await self._run(_write)
        return {"traces_failed": traces, "approvals_expired": approvals}

    async def cancel_pending_by_trace(self, trace_id: str) -> None:
        def _write() -> None:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE approvals SET status='cancelled', decided_at=? WHERE trace_id=? AND status='pending'",
                    (now_iso(), trace_id),
                )

        async with self._write_lock:
            await self._run(_write)