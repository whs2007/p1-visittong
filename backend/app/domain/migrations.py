"""版本化 schema 迁移。

为什么不能继续用散落的 `PRAGMA table_info + ALTER TABLE`（v0.6 及之前）：

- 没有版本号，无法回答「这个库升到哪一版了」，也无法审计「谁在什么时候改的表」；
- 多个进程同时启动会并发执行 ALTER，后到者拿到 duplicate column 错误；
- 不可回滚、不可重复执行，测试库和线上库容易悄悄漂移。

本模块把「结构变更」收敛成有序迁移：每条迁移在 `BEGIN IMMEDIATE` 内执行，
成功后写 `schema_migrations`。重复执行是幂等的（版本已存在则跳过），并发启动
靠 SQLite 的写锁串行化，第二个进程看到版本已存在即跳过。

设计边界（如实声明）：这是**前向迁移**，没有 down 脚本。SQLite 的 ALTER 能力
有限，回滚只能靠备份恢复；生产迁移应当「先备份再迁移」，`Makefile` 里的
`make backup` 就是干这个的。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.utils import now_iso

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 迁移清单：只追加，不修改已发布的条目
# --------------------------------------------------------------------------- #
_0001 = """
-- 治理台账：谁在什么时候改了什么，为什么改。与 audit_log 的区别是--
-- audit_log 面向业务动作（工具调用/审批/手工改单），这里是**配置与权限**
-- 类动作（工具启停、Agent 热更新、审批决策、人工豁免），字段是结构化的，
-- 不再靠 JSON 里约定 channel=manual_override 来事后 grep。
CREATE TABLE IF NOT EXISTS governance_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  actor      TEXT,                    -- 操作者 user_id（系统动作写 system）
  actor_role TEXT,
  action     TEXT NOT NULL,           -- tool.disable / agent.reload / approval.approve ...
  target_type TEXT,                   -- tool / agent / plan / approval / trace
  target_id  TEXT,
  before_json TEXT,
  after_json  TEXT,
  reason     TEXT,
  trace_id   TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gov_created ON governance_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gov_action ON governance_events(action);

-- 效果级幂等账本：一次工具调用的**副作用**记在这里，而不是只记「我执行过」。
-- state: running（已声明，结果未知）/ done（有可回放结果）/ abandoned
CREATE TABLE IF NOT EXISTS idempotency_effects (
  idem_key    TEXT PRIMARY KEY,       -- turn_id:step:call_id
  effect_type TEXT NOT NULL,          -- plan / checkin / cancel / log / notify
  state       TEXT NOT NULL,
  resource_id TEXT,
  result_json TEXT,                   -- 回放用的完整 ToolResult
  turn_id     TEXT,
  step        INTEGER,
  call_id     TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_turn ON idempotency_effects(turn_id);

-- Token 撤销：登出后按 jti 立即失效，不必等到 exp
CREATE TABLE IF NOT EXISTS revoked_tokens (
  jti        TEXT PRIMARY KEY,
  user_id    TEXT,
  expires_at TEXT,
  revoked_at TEXT NOT NULL
);

-- 登录失败计数（进程内 dict 重启即清零、且只增不减）
CREATE TABLE IF NOT EXISTS login_attempts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT,
  client_ip  TEXT,
  ok         INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempt ON login_attempts(client_ip, created_at DESC);
"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """幂等加列：表不存在或列已存在都视为成功，返回是否真的改了结构。"""
    if not _table_exists(conn, table):
        return False
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def _exec_script(conn: sqlite3.Connection, script: str) -> None:
    """逐条执行 SQL。

    不用 `executescript`：它会先隐式 COMMIT 当前事务，导致迁移不再原子
    （而且随后的 ROLLBACK 会报 "no transaction is active"）。
    """
    lines = []
    for raw in script.splitlines():
        if raw.strip().startswith("--"):
            continue
        lines.append(raw)
    for statement in "\n".join(lines).split(";"):
        if statement.strip():
            conn.execute(statement)


def _migrate_0001_governance_and_durability(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _0001)
    # 审批等待态持久化：Turn 挂起等审批时，把「等的是哪张单、第几步、哪个 call」
    # 写进任务行。重启后 resume 能据此接回，而不是盲目重跑 ReAct。
    _add_column(conn, "task_runs", "pending_approval_id", "TEXT")
    _add_column(conn, "task_runs", "pending_step", "INTEGER")
    _add_column(conn, "task_runs", "pending_call_id", "TEXT")
    _add_column(conn, "task_runs", "pending_tool", "TEXT")
    # token 版本号：改密/强制下线时 +1，旧 token 全部失效（比逐条撤销更彻底）
    _add_column(conn, "users", "token_epoch", "INTEGER NOT NULL DEFAULT 0")
    # 工具执行尝试次数：对账时能区分「首次执行」与「重试后成功」
    _add_column(conn, "tool_executions", "attempt", "INTEGER DEFAULT 1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_status ON task_runs(status)"
    )


def _migrate_0002_memories(conn: sqlite3.Connection) -> None:
    """长期记忆表（v0.8）。

    与 Pi 的关系要说清：Pi 的「记忆」是**文件层**——AGENTS.md 分层注入 + 会话树
    持久化，没有可撤销的动态记忆库。本表是本项目在其之上的扩展，因此额外强调
    「来源可追溯 + 可撤销 + 写入门禁」，否则记忆就会变成不可审计的隐形状态。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
          memory_id    TEXT PRIMARY KEY,
          scope        TEXT NOT NULL,
          owner_id     TEXT,
          kind         TEXT NOT NULL DEFAULT 'fact',
          content      TEXT NOT NULL,
          tags         TEXT,
          confidence   REAL DEFAULT 0.7,
          status       TEXT NOT NULL DEFAULT 'active',
          source_session_id TEXT,
          source_entry_id   TEXT,
          created_by   TEXT,
          created_at   TEXT NOT NULL,
          revoked_at   TEXT,
          revoked_by   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, owner_id, status)"
    )


_0003 = """
CREATE TABLE IF NOT EXISTS tool_outputs (
  ref         TEXT PRIMARY KEY,
  trace_id    TEXT,
  session_id  TEXT,
  user_id     TEXT,
  tool        TEXT,
  content     TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  chars       INTEGER DEFAULT 0,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_outputs_trace ON tool_outputs(trace_id);
CREATE TABLE IF NOT EXISTS knowledge_dlq (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT,
  doc_id       TEXT,
  op           TEXT,
  reason       TEXT,
  attempts     INTEGER DEFAULT 0,
  payload      TEXT,
  status       TEXT NOT NULL DEFAULT 'dead',
  created_at   TEXT NOT NULL,
  replayed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_dlq_status ON knowledge_dlq(status, created_at);
"""


def _migrate_0003_outputs_nonce_and_sync(conn: sqlite3.Connection) -> None:
    """v0.9：工具输出治理 / 审批一次性令牌 / 知识同步版本三件事的表结构。

    三件事放在一条迁移里，是因为它们同属「简历上写了但代码里没有」的那批缺口，
    一起补、一起验证、一起发布；拆成三条会让「升到哪一版」失去语义。
    """
    _exec_script(conn, _0003)
    # 审批决策一次性令牌：nonce 随审批单下发，决策时必须带、且只能用一次。
    _add_column(conn, "approvals", "decision_nonce", "TEXT")
    _add_column(conn, "approvals", "nonce_used_at", "TEXT")
    # 知识同步事件版本：consumer 侧用它丢弃乱序到达的旧事件。
    _add_column(conn, "knowledge_docs", "source_version", "INTEGER DEFAULT 0")
    # 软删墓碑：删除也带版本，否则迟到的旧事件会把已删数据写回来。
    _add_column(conn, "knowledge_docs", "deleted_at", "TEXT")
    # Trace 落成本，供按 skill / 按天聚合（单价可配，未配置时只报 token）。
    _add_column(conn, "traces", "cost_usd", "REAL DEFAULT 0")


MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "governance_and_durability", _migrate_0001_governance_and_durability),
    (2, "memories", _migrate_0002_memories),
    (3, "outputs_nonce_and_sync", _migrate_0003_outputs_nonce_and_sync),
]


def apply(sqlite_path: str | Path) -> list[int]:
    """执行未应用的迁移，返回本次实际应用的版本号列表。

    在 `BEGIN IMMEDIATE` 中执行：并发启动的两个进程会串行化，后到者读到
    版本已写入即跳过，因此不会出现「同时 ALTER 同一列」。
    """
    path = str(sqlite_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    applied: list[int] = []
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version, name, fn in MIGRATIONS:
            if conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone():
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 拿到写锁后重新确认（另一个进程可能刚刚提交）
                if conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
                ).fetchone():
                    conn.execute("ROLLBACK")
                    continue
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
                    (version, name, now_iso()),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            applied.append(version)
            log.info("migration applied: %s %s", version, name)
    finally:
        conn.close()
    return applied


def current_version(sqlite_path: str | Path) -> int:
    path = str(sqlite_path)
    if not Path(path).exists():
        return 0
    conn = sqlite3.connect(path, timeout=5)
    try:
        if not _table_exists(conn, "schema_migrations"):
            return 0
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
