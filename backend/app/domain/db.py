"""业务数据访问层（SQLite；DDL 兼容 MySQL，见 docs/04）。

MVP 采用"每请求新连接 + 短事务"，单机演示足够；生产替换 MySQL 只改 connect()。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.auth import hash_password
from app.utils import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id     TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  role        TEXT NOT NULL,
  password_hash TEXT,
  region      TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  alias       TEXT,
  brand       TEXT,
  owner_id    TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_owner ON customers(owner_id);

CREATE TABLE IF NOT EXISTS stores (
  store_id    TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  name        TEXT NOT NULL,
  address     TEXT,
  lng         REAL,
  lat         REAL,
  region      TEXT
);

CREATE TABLE IF NOT EXISTS visit_plans (
  plan_id      TEXT PRIMARY KEY,
  customer_id  TEXT NOT NULL,
  store_id     TEXT,
  owner_id     TEXT NOT NULL,
  planned_at   TEXT NOT NULL,
  duration_min INTEGER DEFAULT 60,
  topic        TEXT,
  status       TEXT NOT NULL,
  version      INTEGER DEFAULT 1,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_owner_time ON visit_plans(owner_id, planned_at);

CREATE TABLE IF NOT EXISTS plan_change_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    TEXT NOT NULL,
  field      TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  changed_by TEXT,
  changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkins (
  checkin_id TEXT PRIMARY KEY,
  plan_id    TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  lng        REAL,
  lat        REAL,
  distance_m INTEGER,
  status     TEXT NOT NULL,
  checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visit_logs (
  log_id       TEXT PRIMARY KEY,
  plan_id      TEXT,
  customer_id  TEXT,
  summary      TEXT,
  feedback     TEXT,
  next_action  TEXT,
  follow_up_at TEXT,
  transcript   TEXT,
  created_by   TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
  todo_id  TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  title    TEXT NOT NULL,
  due_at   TEXT,
  status   TEXT NOT NULL,
  plan_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_owner ON todos(owner_id, due_at);

CREATE TABLE IF NOT EXISTS outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  channel    TEXT NOT NULL,
  target     TEXT,
  content    TEXT,
  status     TEXT DEFAULT 'sent',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_entries (
  entry_id   TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  parent_id  TEXT,
  role       TEXT NOT NULL,
  content    TEXT,
  tool_calls TEXT,
  card       TEXT,
  tool_call_id TEXT,
  name       TEXT,
  entry_type TEXT DEFAULT 'message',
  is_leaf    INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_session ON session_entries(session_id, created_at);

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
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);

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
  approver        TEXT,
  decision_reason TEXT,
  created_at      TEXT NOT NULL,
  decided_at      TEXT,
  expires_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS state_transitions (
  transition_id TEXT PRIMARY KEY,
  entity_type   TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  from_state    TEXT NOT NULL,
  to_state      TEXT NOT NULL,
  event         TEXT NOT NULL,
  actor         TEXT,
  trace_id      TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_entity ON state_transitions(entity_type, entity_id, created_at);

-- 长期记忆：Pi 没有内置的内存记忆（它的「记忆」是 AGENTS.md 分层文件 + 会话树），
-- 这里是本项目在其之上的扩展：可撤销、带来源、可审计的动态记忆。
CREATE TABLE IF NOT EXISTS memories (
  memory_id    TEXT PRIMARY KEY,
  scope        TEXT NOT NULL,               -- project | agent | user
  owner_id     TEXT,                        -- agent 名 / user_id；project 作用域为 NULL
  kind         TEXT NOT NULL DEFAULT 'fact',-- fact | preference | decision
  content      TEXT NOT NULL,
  tags         TEXT,
  confidence   REAL DEFAULT 0.7,
  status       TEXT NOT NULL DEFAULT 'active',  -- active | revoked
  source_session_id TEXT,                    -- 这条记忆从哪次对话来
  source_entry_id   TEXT,
  created_by   TEXT,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT,
  revoked_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, owner_id, status);

-- Durable Execution：任务运行、步骤检查点、工具执行去重（崩溃后可 resume）
CREATE TABLE IF NOT EXISTS task_runs (
  turn_id      TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  trace_id     TEXT,
  status       TEXT NOT NULL,
  step         INTEGER DEFAULT 0,
  skill        TEXT,
  user_input   TEXT,
  stop_reason  TEXT,
  error        TEXT,
  resume_count INTEGER DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_session ON task_runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status);

CREATE TABLE IF NOT EXISTS turn_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  turn_id       TEXT NOT NULL,
  step          INTEGER NOT NULL,
  skill         TEXT,
  messages      TEXT,
  slots         TEXT,
  tool_calls    INTEGER DEFAULT 0,
  tokens_in     INTEGER DEFAULT 0,
  tokens_out    INTEGER DEFAULT 0,
  agent_name    TEXT,
  capability_fingerprint TEXT,
  capability_json TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_turn ON turn_checkpoints(turn_id, step);

CREATE TABLE IF NOT EXISTS tool_executions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id     TEXT NOT NULL,
  step        INTEGER NOT NULL,
  call_id     TEXT NOT NULL,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL,
  output      TEXT,
  data        TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE(turn_id, step, call_id)
);

-- 操作审计（工具/审批/手工接口），与 Trace 分离，可独立导出
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor       TEXT,
  action      TEXT NOT NULL,
  target_type TEXT,
  target_id   TEXT,
  before      TEXT,
  after       TEXT,
  trace_id    TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

-- 请求级幂等（Idempotency-Key），防重复提交/重复执行
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key        TEXT PRIMARY KEY,
  user_id    TEXT,
  session_id TEXT,
  trace_id   TEXT,
  created_at TEXT NOT NULL
);

-- 知识库（客户/门店/规则）：本地向量索引；生产替换 Milvus（接口见 adapters/knowledge.py）
CREATE TABLE IF NOT EXISTS knowledge_docs (
  doc_id     TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  ref_id     TEXT,
  title      TEXT NOT NULL,
  text       TEXT NOT NULL,
  vector     TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind ON knowledge_docs(kind);

-- 数据管道模拟（Hive -> Kafka -> consumer 的事件箱）
CREATE TABLE IF NOT EXISTS ingest_events (
  event_id    TEXT PRIMARY KEY,
  topic       TEXT NOT NULL,
  payload     TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_status ON ingest_events(status, created_at);
"""

PLAN_FIELDS = ("planned_at", "store_id", "topic", "duration_min")


def today_str() -> str:
    return date.today().isoformat()


def today_at(hour: int, minute: int = 0) -> str:
    return datetime.combine(date.today(), time(hour, minute)).astimezone().isoformat(timespec="seconds")


class Database:
    """业务库访问层：默认 SQLite；`backend="mysql"` 时切到 MySQL（需 pymysql）。

    两种方言共用同一套 SQL（`?` 占位符、INSERT OR IGNORE、ON CONFLICT），
    由 `_sql()` 自动翻译。MySQL 路径需真实实例验证，本地默认仍用 SQLite。
    """

    def __init__(
        self,
        path: str,
        demo_password: str = "123456",
        *,
        backend: str = "sqlite",
        dsn: str = "",
    ) -> None:
        self.dialect = backend if backend in ("sqlite", "mysql") else "sqlite"
        self.dsn = dsn
        self.path = str(Path(path)) if self.dialect == "sqlite" else ""
        self.demo_password = demo_password

    # ------------------------------------------------------------------ #
    # SQL 方言翻译
    # ------------------------------------------------------------------ #
    @staticmethod
    def translate_sql(sql: str) -> str:
        """SQLite 方言 -> MySQL：占位符 / INSERT OR IGNORE / ON CONFLICT。"""
        if "?" not in sql:
            translated = sql
        else:
            translated = sql.replace("?", "%s")
        translated = translated.replace("INSERT OR IGNORE", "INSERT IGNORE")
        if "ON CONFLICT(" in translated:
            head, _, tail = translated.partition("ON CONFLICT(")
            _, _, updates = tail.partition("DO UPDATE SET ")
            if updates:
                pairs = []
                for item in updates.split(","):
                    name, _, value = item.partition("=")
                    value = value.strip().replace("excluded.", "VALUES(") + ")"
                    if value.endswith("))"):
                        value = value[:-1]
                    pairs.append(f"{name.strip()}={value}")
                translated = head + "ON DUPLICATE KEY UPDATE " + ", ".join(pairs)
        return translated

    def _sql(self, sql: str) -> str:
        return self.translate_sql(sql) if self.dialect == "mysql" else sql

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def init(self) -> None:
        if self.dialect == "mysql":
            conn = self._conn()
            try:
                with conn.cursor() as cursor:
                    for statement in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
                        cursor.execute(self._mysql_schema(statement))
                conn.commit()
            finally:
                conn.close()
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            # 轻量迁移：v0.5 之前建的库没有能力快照列。MySQL 分支只做方言
            # 翻译、无真实实例，旧库迁移仅覆盖 SQLite（见 docs/08 红线）。
            cols = {row[1] for row in conn.execute("PRAGMA table_info(turn_checkpoints)").fetchall()}
            for column, ddl in (
                ("agent_name", "TEXT"),
                ("capability_fingerprint", "TEXT"),
                ("capability_json", "TEXT"),
            ):
                if column not in cols:
                    conn.execute(f"ALTER TABLE turn_checkpoints ADD COLUMN {column} {ddl}")

    @staticmethod
    def _mysql_schema(statement: str) -> str:
        return (
            statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "INT AUTO_INCREMENT PRIMARY KEY")
            .replace("AUTOINCREMENT", "AUTO_INCREMENT")
        )

    def _conn(self):
        if self.dialect == "mysql":
            try:
                import pymysql  # type: ignore
                from pymysql.cursors import DictCursor
            except ImportError as exc:  # pragma: no cover - 由部署环境决定
                raise RuntimeError("mysql 后端需要安装 pymysql：pip install pymysql") from exc
            from urllib.parse import urlparse

            parsed = urlparse(self.dsn)
            return pymysql.connect(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 3306,
                user=parsed.username or "root",
                password=parsed.password or "",
                database=(parsed.path or "/storymaker").lstrip("/"),
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=False,
            )
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        return {k: row[k] for k in row.keys()}

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if self.dialect == "mysql":
            conn = self._conn()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(self._sql(sql), params)
                    rows = cursor.fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
        with self._conn() as conn:
            return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params).fetchall()]

    def _exec(self, sql: str, params: tuple = ()) -> int:
        if self.dialect == "mysql":
            conn = self._conn()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(self._sql(sql), params)
                    count = cursor.rowcount
                conn.commit()
                return int(count)
            finally:
                conn.close()
        with self._conn() as conn:
            return conn.execute(sql, params).rowcount

    # ------------------------------------------------------------------ #
    # 种子数据
    # ------------------------------------------------------------------ #
    def seed_demo(self) -> None:
        with self._conn() as conn:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
                return
            created = now_iso()
            conn.executemany(
                "INSERT INTO users (user_id,name,role,region,created_at) VALUES (?,?,?,?,?)",
                [("sales-001", "张伟", "sales", "北京", created),
                 ("manager-001", "李敏", "manager", "北京", created)],
            )
            for uid in ("sales-001", "manager-001"):
                conn.execute(
                    "UPDATE users SET password_hash=? WHERE user_id=?",
                    (hash_password(self.demo_password), uid),
                )
            customers = [
                ("c_byte", "字节跳动", "字节", "互联网", "sales-001", created),
                ("c_ali", "阿里巴巴", "阿里", "互联网", "sales-001", created),
                ("c_yunqi_a", "云启科技", "云启", "SaaS", "sales-001", created),
                ("c_yunqi_b", "云启科技", "云启", "硬件", "sales-001", created),
            ]
            conn.executemany(
                "INSERT INTO customers (customer_id,name,alias,brand,owner_id,created_at) VALUES (?,?,?,?,?,?)",
                customers,
            )
            stores = [
                ("st_byte_zgc", "c_byte", "中关村店", "北京市海淀区中关村大街 1 号", 116.316, 39.983, "北京"),
                ("st_byte_wj", "c_byte", "望京店", "北京市朝阳区望京 SOHO", 116.481, 39.996, "北京"),
                ("st_ali_xixi", "c_ali", "西溪园区", "杭州市余杭区文一西路 969 号", 120.026, 30.279, "杭州"),
                ("st_yunqi_a", "c_yunqi_a", "上地店", "北京市海淀区上地信息路 2 号", 116.306, 40.041, "北京"),
                ("st_yunqi_b", "c_yunqi_b", "酒仙桥店", "北京市朝阳区酒仙桥路 10 号", 116.494, 39.973, "北京"),
            ]
            conn.executemany(
                "INSERT INTO stores (store_id,customer_id,name,address,lng,lat,region) VALUES (?,?,?,?,?,?,?)",
                stores,
            )
            plans = [
                ("p_demo_1", "c_byte", "st_byte_zgc", "sales-001", today_at(10, 0), 60, "产品方案沟通", "planned"),
                ("p_demo_2", "c_ali", "st_ali_xixi", "sales-001", today_at(14, 0), 60, "季度复盘", "planned"),
                ("p_demo_3", "c_yunqi_a", "st_yunqi_a", "sales-001", today_at(16, 30), 45, "续约谈判", "planned"),
            ]
            conn.executemany(
                "INSERT INTO visit_plans (plan_id,customer_id,store_id,owner_id,planned_at,duration_min,topic,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(*p, created, created) for p in plans],
            )
            todos = [
                ("todo_1", "sales-001", "准备字节跳动的产品方案", today_at(10, 0), "open", "p_demo_1"),
                ("todo_2", "sales-001", "整理阿里季度复盘数据", today_at(14, 0), "open", "p_demo_2"),
                ("todo_3", "sales-001", "云启科技续约合同确认", today_at(16, 30), "open", "p_demo_3"),
                ("todo_4", "sales-001", "回访上周未成交客户", today_at(18, 0), "open", None),
            ]
            conn.executemany(
                "INSERT INTO todos (todo_id,owner_id,title,due_at,status,plan_id) VALUES (?,?,?,?,?,?)", todos
            )

    # ------------------------------------------------------------------ #
    # 用户 / 客户 / 门店
    # ------------------------------------------------------------------ #
    def ensure_auth_columns(self) -> None:
        """轻量迁移：给旧库补 password_hash 并回填 demo 账号密码（仅 SQLite）。"""
        if self.dialect != "sqlite":
            return
        with self._conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "password_hash" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            for uid in ("sales-001", "manager-001"):
                row = conn.execute(
                    "SELECT password_hash FROM users WHERE user_id=?", (uid,)
                ).fetchone()
                if row is not None and not row[0]:
                    conn.execute(
                        "UPDATE users SET password_hash=? WHERE user_id=?",
                        (hash_password(self.demo_password), uid),
                    )

    def update_password(self, user_id: str, password_hash_value: str) -> None:
        self._exec(
            "UPDATE users SET password_hash=? WHERE user_id=?",
            (password_hash_value, user_id),
        )

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._row(self._conn().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

    def search_customers(self, name: str) -> list[dict[str, Any]]:
        keyword = f"%{name.strip()}%"
        return self._rows(
            "SELECT * FROM customers WHERE name LIKE ? OR alias LIKE ? OR brand LIKE ?",
            (keyword, keyword, keyword),
        )

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self._row(self._conn().execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone())

    def list_stores(self, customer_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM stores WHERE customer_id=?", (customer_id,))

    def default_store(self, customer_id: str) -> dict[str, Any] | None:
        rows = self.list_stores(customer_id)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # 计划
    # ------------------------------------------------------------------ #
    PLAN_SELECT = """
        SELECT p.*, c.name AS customer_name, c.alias AS customer_alias,
               s.name AS store_name, s.address AS store_address, s.lng, s.lat
        FROM visit_plans p
        LEFT JOIN customers c ON c.customer_id = p.customer_id
        LEFT JOIN stores s ON s.store_id = p.store_id
    """

    def list_plans(
        self, owner_id: str, status: str | None = None, day: str | None = None
    ) -> list[dict[str, Any]]:
        sql = self.PLAN_SELECT + " WHERE p.owner_id=?"
        params: list[Any] = [owner_id]
        if status:
            sql += " AND p.status=?"
            params.append(status)
        if day:
            sql += " AND p.planned_at LIKE ?"
            params.append(f"{day}%")
        sql += " ORDER BY p.planned_at"
        return self._rows(sql, tuple(params))

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        return self._row(self._conn().execute(self.PLAN_SELECT + " WHERE p.plan_id=?", (plan_id,)).fetchone())

    def recent_plans(self, owner_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._rows(
            self.PLAN_SELECT + " WHERE p.owner_id=? ORDER BY p.created_at DESC LIMIT ?", (owner_id, limit)
        )

    def create_plan(
        self,
        *,
        customer_id: str,
        store_id: str | None,
        owner_id: str,
        planned_at: str,
        topic: str = "客户拜访",
        duration_min: int = 60,
    ) -> dict[str, Any]:
        plan_id = new_id("p")
        created = now_iso()
        self._exec(
            "INSERT INTO visit_plans (plan_id,customer_id,store_id,owner_id,planned_at,duration_min,topic,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (plan_id, customer_id, store_id, owner_id, planned_at, duration_min, topic, "planned", created, created),
        )
        return self.get_plan(plan_id)  # type: ignore[return-value]

    def update_plan(self, plan_id: str, changes: dict[str, Any], changed_by: str) -> dict[str, Any] | None:
        before = self.get_plan(plan_id)
        if not before:
            return None
        sets, params = [], []
        for field in PLAN_FIELDS:
            if field in changes and changes[field] not in (None, ""):
                sets.append(f"{field}=?")
                params.append(changes[field])
                if str(before.get(field)) != str(changes[field]):
                    self._exec(
                        "INSERT INTO plan_change_log (plan_id,field,old_value,new_value,changed_by,changed_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (plan_id, field, str(before.get(field)), str(changes[field]), changed_by, now_iso()),
                    )
        if not sets:
            return before
        sets.append("version=version+1")
        sets.append("updated_at=?")
        params.extend([now_iso(), plan_id])
        self._exec(f"UPDATE visit_plans SET {', '.join(sets)} WHERE plan_id=?", tuple(params))
        return self.get_plan(plan_id)

    def set_plan_status(self, plan_id: str, status: str) -> None:
        self._exec(
            "UPDATE visit_plans SET status=?, updated_at=? WHERE plan_id=?",
            (status, now_iso(), plan_id),
        )

    # ------------------------------------------------------------------ #
    # 状态迁移（FSM 审计）
    # ------------------------------------------------------------------ #
    def add_state_transition(
        self,
        *,
        transition_id: str,
        entity_type: str,
        entity_id: str,
        from_state: str,
        to_state: str,
        event: str,
        actor: str = "",
        trace_id: str | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO state_transitions (transition_id,entity_type,entity_id,from_state,to_state,event,actor,trace_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (transition_id, entity_type, entity_id, from_state, to_state, event, actor, trace_id, now_iso()),
        )

    def list_state_transitions(
        self, entity_type: str, entity_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM state_transitions WHERE entity_type=? AND entity_id=?"
            " ORDER BY created_at DESC LIMIT ?",
            (entity_type, entity_id, limit),
        )

    # ------------------------------------------------------------------ #
    # 操作审计 / 请求幂等
    # ------------------------------------------------------------------ #
    def add_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        import json as _json

        self._exec(
            "INSERT INTO audit_log (actor,action,target_type,target_id,before,after,trace_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                actor, action, target_type, target_id,
                _json.dumps(before or {}, ensure_ascii=False),
                _json.dumps(after or {}, ensure_ascii=False),
                trace_id, now_iso(),
            ),
        )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    def reserve_idempotency(self, key: str, user_id: str, session_id: str) -> bool:
        """预留幂等键：首次返回 True，重复返回 False。"""
        cursor = self._exec(
            "INSERT OR IGNORE INTO idempotency_keys (key,user_id,session_id,trace_id,created_at)"
            " VALUES (?,?,?,?,?)",
            (key, user_id, session_id, None, now_iso()),
        )
        return cursor > 0

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        return self._row(
            self._conn().execute("SELECT * FROM idempotency_keys WHERE key=?", (key,)).fetchone()
        )

    def plan_conflict(self, owner_id: str, planned_at: str, exclude_plan_id: str | None = None) -> dict[str, Any] | None:
        day = planned_at[:10]
        # 返回**完整计划行**（与 get_plan/list_plans 同构）：调用方要拿它给用户
        # 回话、拼卡片、甚至在崩溃恢复时直接认领这条已存在的计划。之前只
        # SELECT 三列，`customer_name` 之类字段在认领分支里会 KeyError —— 副作用
        # 明明已经成功，恢复却报 error（chaos_resume 打到 65ms 偏移时暴露的）。
        rows = self._rows(
            self.PLAN_SELECT
            + " WHERE p.owner_id=? AND p.planned_at LIKE ? AND p.status IN ('planned','checked_in')",
            (owner_id, f"{day}%"),
        )
        target = planned_at[:16]
        for row in rows:
            if row["plan_id"] == exclude_plan_id:
                continue
            if row["planned_at"][:16] == target:
                return row
        return None

    # ------------------------------------------------------------------ #
    # 待办
    # ------------------------------------------------------------------ #
    def list_todos(self, owner_id: str, day: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT t.*, p.planned_at, p.status AS plan_status, c.name AS customer_name"
            " FROM todos t LEFT JOIN visit_plans p ON p.plan_id = t.plan_id"
            " LEFT JOIN customers c ON c.customer_id = p.customer_id WHERE t.owner_id=?"
        )
        params: list[Any] = [owner_id]
        if day:
            sql += " AND t.due_at LIKE ?"
            params.append(f"{day}%")
        sql += " ORDER BY t.due_at"
        return self._rows(sql, tuple(params))

    # ------------------------------------------------------------------ #
    # 签到 / 日志 / 通知
    # ------------------------------------------------------------------ #
    def create_checkin(
        self, *, plan_id: str, user_id: str, lng: float, lat: float, distance_m: int, status: str
    ) -> dict[str, Any]:
        checkin_id = new_id("chk")
        self._exec(
            "INSERT INTO checkins (checkin_id,plan_id,user_id,lng,lat,distance_m,status,checked_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (checkin_id, plan_id, user_id, lng, lat, distance_m, status, now_iso()),
        )
        return self._row(self._conn().execute("SELECT * FROM checkins WHERE checkin_id=?", (checkin_id,)).fetchone())

    def list_checkins(self, plan_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM checkins WHERE plan_id=? ORDER BY checked_at", (plan_id,))

    def create_log(
        self,
        *,
        plan_id: str | None,
        customer_id: str | None,
        summary: str,
        feedback: str = "",
        next_action: str = "",
        follow_up_at: str | None = None,
        transcript: str | None = None,
        created_by: str = "sales-001",
    ) -> dict[str, Any]:
        log_id = new_id("log")
        self._exec(
            "INSERT INTO visit_logs (log_id,plan_id,customer_id,summary,feedback,next_action,follow_up_at,"
            "transcript,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (log_id, plan_id, customer_id, summary, feedback, next_action, follow_up_at, transcript, created_by, now_iso()),
        )
        return self._row(self._conn().execute("SELECT * FROM visit_logs WHERE log_id=?", (log_id,)).fetchone())

    def add_outbox(self, channel: str, target: str, content: str) -> None:
        self._exec(
            "INSERT INTO outbox (channel,target,content,status,created_at) VALUES (?,?,?,?,?)",
            (channel, target, content, "sent", now_iso()),
        )


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def add_minutes(iso: str, minutes: int) -> str:
    return (parse_iso(iso) + timedelta(minutes=minutes)).isoformat(timespec="seconds")