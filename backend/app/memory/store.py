"""动态长期记忆存储：可追溯、可撤销、只追加状态。

为什么不用「模型自己总结然后写库」：那样写进去的东西既无法归因，也无法撤回，
一旦模型把用户的一句玩笑固化成「偏好」，之后每轮都会生效。这里的原则是
**每条记忆都必须有来源**（session_id + entry_id），并且撤销是状态位而不是
删除——历史要能回答「它曾经被记住过多久」，删了就答不了。
"""
from __future__ import annotations

from typing import Any

from app.domain.db import Database
from app.utils import new_id, now_iso

_FIELDS = (
    "memory_id, scope, owner_id, kind, content, tags, confidence, status,"
    " source_session_id, source_entry_id, created_by, created_at, revoked_at, revoked_by"
)


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def add(
        self,
        *,
        scope: str,
        content: str,
        owner_id: str | None = None,
        kind: str = "fact",
        tags: list[str] | None = None,
        confidence: float = 0.7,
        source_session_id: str | None = None,
        source_entry_id: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        memory_id = new_id("m")
        created = now_iso()
        with self.db._conn() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO memories (memory_id,scope,owner_id,kind,content,tags,confidence,status,"
                "source_session_id,source_entry_id,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?, 'active', ?,?,?,?)",
                (
                    memory_id, scope, owner_id, kind, content,
                    ",".join(tags or []) or None, max(0.0, min(1.0, confidence)),
                    source_session_id, source_entry_id, created_by, created,
                ),
            )
        return {
            "memory_id": memory_id, "scope": scope, "owner_id": owner_id, "kind": kind,
            "content": content, "tags": tags or [], "confidence": confidence,
            "status": "active", "source_session_id": source_session_id,
            "source_entry_id": source_entry_id, "created_by": created_by, "created_at": created,
        }

    def revoke(self, memory_id: str, *, actor: str) -> bool:
        """撤销而不是删除：历史留痕，召回立即失效。"""
        with self.db._conn() as conn:  # noqa: SLF001
            cursor = conn.execute(
                "UPDATE memories SET status='revoked', revoked_at=?, revoked_by=? "
                "WHERE memory_id=? AND status='active'",
                (now_iso(), actor, memory_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get(self, memory_id: str) -> dict[str, Any] | None:
        rows = self.db._rows(  # noqa: SLF001
            f"SELECT {_FIELDS} FROM memories WHERE memory_id=?", (memory_id,)
        )
        return _shape(rows[0]) if rows else None

    def list(
        self,
        *,
        scope: str | None = None,
        owner_id: str | None = None,
        include_revoked: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {_FIELDS} FROM memories WHERE 1=1"
        params: list[Any] = []
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if owner_id:
            sql += " AND owner_id=?"
            params.append(owner_id)
        if not include_revoked:
            sql += " AND status='active'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [_shape(row) for row in self.db._rows(sql, tuple(params))]  # noqa: SLF001

    def candidates(self, *, user_id: str, agent_name: str) -> list[dict[str, Any]]:
        """当前会话可见的候选集：项目级 ∪ 本 Agent ∪ 本人。

        **不做「全体用户共享」**：那是最省事也最危险的默认值——A 用户的偏好
        会被注入到 B 用户的对话里，既是隐私问题也是正确性问题。
        """
        return [_shape(row) for row in self.db._rows(  # noqa: SLF001
            f"SELECT {_FIELDS} FROM memories WHERE status='active' AND ("
            " (scope='project') OR (scope='agent' AND owner_id=?) OR (scope='user' AND owner_id=?)"
            ") ORDER BY created_at DESC LIMIT 200",
            (agent_name, user_id),
        )]

    def count(self) -> dict[str, int]:
        rows = self.db._rows(  # noqa: SLF001
            "SELECT status, COUNT(*) AS n FROM memories GROUP BY status"
        )
        return {str(row["status"]): int(row["n"]) for row in rows}


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = [t for t in str(item.get("tags") or "").split(",") if t]
    try:
        item["confidence"] = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        item["confidence"] = 0.0
    return item
