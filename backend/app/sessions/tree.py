"""会话树：entries(parent_id) + leaf 指针，支持历史节点切回与分支续写。

对齐 Pi 的会话模型（docs/session-format.md）：
  - 条目基类 {type, id, parentId, timestamp}，只追加，不删改历史；
  - 分支 = 在任意节点下新增子节点，旧节点保留；
  - compaction / branch_summary 是独立条目类型，而不是删除旧消息；
  - 对外可导出 JSONL（Pi 的会话文件格式），便于调试与回放。

Redis 只存热状态（meta/slots/entity/leaf 缓存），SQLite 的 entries 树是唯一事实来源。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.domain.db import Database
from app.utils import new_id, now_iso


def _openai_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把内部存储的 tool_calls 还原成 OpenAI 兼容请求格式。

    内部存的是扁平结构 `{id, name, arguments}`（好读、好审计），但请求体要求
    `{id, type:"function", function:{name, arguments}}`。少了 `function` 这层包装，
    DashScope 会直接 500（严格 pydantic 校验），OpenAI 也可能报 400 —— 这个差异
    只有接真模型时才会暴露，mock 永远发现不了。
    """
    out: list[dict[str, Any]] = []
    for call in calls or []:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") or call.get("name") or ""
        args = function.get("arguments") if function else call.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False)
        out.append(
            {
                "id": call.get("id") or new_id("call"),
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    return out


class SessionTree:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def append(
        self,
        *,
        session_id: str,
        parent_id: str | None,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        card: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        entry_type: str = "message",
    ) -> dict[str, Any]:
        entry_id = new_id("e")
        created = now_iso()
        with self.db._conn() as conn:  # noqa: SLF001 - 同包内复用连接
            if parent_id is None:
                row = conn.execute(
                    "SELECT entry_id FROM session_entries WHERE session_id=? AND is_leaf=1", (session_id,)
                ).fetchone()
                parent_id = row[0] if row else None
            conn.execute("UPDATE session_entries SET is_leaf=0 WHERE session_id=? AND is_leaf=1", (session_id,))
            conn.execute(
                "INSERT INTO session_entries (entry_id,session_id,parent_id,role,content,tool_calls,card,"
                "is_leaf,tool_call_id,name,created_at,entry_type) VALUES (?,?,?,?,?,?,?,1,?,?,?,?)",
                (
                    entry_id, session_id, parent_id, role, content,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    json.dumps(card, ensure_ascii=False) if card else None,
                    tool_call_id, name, created, entry_type,
                ),
            )
        return {
            "entry_id": entry_id, "parent_id": parent_id, "role": role,
            "content": content, "created_at": created, "entry_type": entry_type,
        }

    def set_leaf(self, session_id: str, entry_id: str) -> bool:
        with self.db._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM session_entries WHERE entry_id=? AND session_id=?", (entry_id, session_id)
            ).fetchone()
            if not exists:
                return False
            conn.execute("UPDATE session_entries SET is_leaf=0 WHERE session_id=?", (session_id,))
            conn.execute("UPDATE session_entries SET is_leaf=1 WHERE entry_id=?", (entry_id,))
            return True

    def delete_session(self, session_id: str) -> None:
        with self.db._conn() as conn:
            conn.execute("DELETE FROM session_entries WHERE session_id=?", (session_id,))

    def compact(
        self,
        session_id: str,
        *,
        summary_text: str,
        first_kept_entry_id: str,
    ) -> dict[str, Any] | None:
        """把 first_kept_entry_id 之前的条目折叠成一条 compaction 摘要。

        语义对齐 Pi：保留 firstKeptEntryId 起的最近历史；summary 条目成为
        保留窗口的新父节点，旧条目仍在库里（可回溯/导出），只是不再进上下文。
        """
        path = self.path(session_id, limit=100_000)
        ids = [item["entry_id"] for item in path]
        if first_kept_entry_id not in ids:
            return None
        start = ids.index(first_kept_entry_id)
        if start <= 0:
            return None
        summary_id = new_id("e")
        created = now_iso()
        with self.db._conn() as conn:
            conn.execute(
                "INSERT INTO session_entries (entry_id,session_id,parent_id,role,content,is_leaf,"
                "created_at,entry_type) VALUES (?,?,?,?,?,0,?,?)",
                (summary_id, session_id, None, "summary", summary_text, created, "compaction"),
            )
            conn.execute(
                "UPDATE session_entries SET parent_id=? WHERE entry_id=?",
                (summary_id, first_kept_entry_id),
            )
        return {
            "summary_id": summary_id,
            "first_kept_entry_id": first_kept_entry_id,
            "folded": start,
            "kept": len(path) - start,
        }

    def switch_leaf(
        self,
        session_id: str,
        entry_id: str,
        *,
        summary_text: str | None = None,
    ) -> dict[str, Any] | None:
        """切换到历史节点（分叉点）；可附带被放弃分支的摘要（Pi branch_summary）。"""
        if not self.set_leaf(session_id, entry_id):
            return None
        summary_id = None
        if summary_text:
            entry = self.append(
                session_id=session_id, parent_id=entry_id, role="summary",
                content=summary_text, entry_type="branch_summary",
            )
            summary_id = entry["entry_id"]
        return {
            "leaf_id": summary_id or entry_id,
            "branch_from": entry_id,
            "branch_summary_id": summary_id,
        }

    def fork(
        self,
        session_id: str,
        from_entry_id: str,
        *,
        summary_text: str | None = None,
    ) -> dict[str, Any] | None:
        """从任意历史条目分叉出新分支（Pi：分支只新增子节点，不删旧节点）。

        与 `switch_leaf` 的差别只是语义命名：这里明确「旧的后续分支被放弃，
        要给它留一条 branch_summary」，否则切回去以后模型会以为那一段没发生过。
        """
        # 必须在切换**之前**取被放弃的分支：切换后 leaf 已指向新分支，回溯出来的
        # 是新路径而不是旧路径（这个顺序错误会让「被放弃了什么」永远是空列表）。
        abandoned = [item["entry_id"] for item in self.abandoned_path(session_id, from_entry_id)]
        info = self.switch_leaf(session_id, from_entry_id, summary_text=summary_text)
        if info is None:
            return None
        return {**info, "abandoned": abandoned}

    def abandoned_path(self, session_id: str, from_entry_id: str) -> list[dict[str, Any]]:
        """当前分支上位于 `from_entry_id` 之后的条目（用于生成被放弃分支的摘要）。"""
        items = self.path(session_id, limit=100_000)
        ids = [item["entry_id"] for item in items]
        if from_entry_id not in ids:
            return []
        return items[ids.index(from_entry_id) + 1:]

    def context_edit(
        self,
        session_id: str,
        *,
        note: str,
        exclude_entry_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """上下文修订（Pi 的 context_edit）：只改「模型看到什么」，不改历史。

        为什么不能直接删条目：会话文件是可举证的事实记录，删了就说不清「当时
        到底说了什么」。Pi 的做法是追加一条 context_edit，让后续上下文组装时
        跳过指定条目；这里保持一致，并在 `messages()` 里额外保护 tool 配对。
        """
        if not self.leaf(session_id):
            return None
        payload = {
            "note": (note or "").strip()[:500],
            "exclude": [eid for eid in (exclude_entry_ids or []) if eid],
        }
        entry = self.append(
            session_id=session_id, parent_id=None, role="context_edit",
            content=json.dumps(payload, ensure_ascii=False), entry_type="context_edit",
        )
        return {
            "entry_id": entry["entry_id"],
            "excluded": payload["exclude"],
            "note": payload["note"],
        }

    def import_jsonl(self, session_id: str, text: str) -> dict[str, Any]:
        """从导出的 JSONL 恢复会话（Pi 的会话文件可以换个环境继续用）。

        旧 id 全部重映射，避免与目标会话的既有条目冲突；parent 关系按映射重建，
        根条目保持 parent 为空；最后一条成为 leaf。
        """
        import_rows: list[dict[str, Any]] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {len(import_rows) + 1} 行不是合法 JSON") from exc
            if not isinstance(row, dict) or not row.get("role"):
                raise ValueError(f"第 {len(import_rows) + 1} 行缺少 role")
            import_rows.append(row)
        if not import_rows:
            raise ValueError("没有可导入的条目")

        mapping: dict[str, str] = {}
        last_id: str | None = None
        with self.db._conn() as conn:  # noqa: SLF001
            for row in import_rows:
                old_id = str(row.get("entry_id") or row.get("id") or "")
                new_id_value = new_id("e")
                if old_id:
                    mapping[old_id] = new_id_value
                parent_old = row.get("parent_id")
                parent_new = mapping.get(str(parent_old)) if parent_old else None
                conn.execute(
                    "INSERT INTO session_entries (entry_id,session_id,parent_id,role,content,tool_calls,"
                    "card,is_leaf,tool_call_id,name,created_at,entry_type) "
                    "VALUES (?,?,?,?,?,?,?,0,?,?,?,?)",
                    (
                        new_id_value, session_id, parent_new,
                        str(row.get("role")), row.get("content"),
                        row.get("tool_calls") if isinstance(row.get("tool_calls"), str) else None,
                        row.get("card") if isinstance(row.get("card"), str) else None,
                        row.get("tool_call_id"), row.get("name"),
                        str(row.get("created_at") or now_iso()),
                        str(row.get("entry_type") or "message"),
                    ),
                )
                last_id = new_id_value
            conn.execute("UPDATE session_entries SET is_leaf=0 WHERE session_id=?", (session_id,))
            if last_id:
                conn.execute("UPDATE session_entries SET is_leaf=1 WHERE entry_id=?", (last_id,))
        return {"imported": len(import_rows), "leaf_id": last_id}

    def export_jsonl(self, session_id: str) -> str:
        """导出会话条目为 JSONL（Pi 的会话文件格式，便于回放/排障）。"""
        rows = self.db._rows(
            "SELECT * FROM session_entries WHERE session_id=? ORDER BY created_at, rowid",
            (session_id,),
        )
        return "\n".join(json.dumps(dict(row), ensure_ascii=False) for row in rows)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def count(self, session_id: str) -> int:
        """会话消息数（user + assistant 条目），用于侧栏展示。"""
        rows = self.db._rows(
            "SELECT COUNT(*) AS n FROM session_entries"
            " WHERE session_id=? AND role IN ('user','assistant')",
            (session_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    def leaf(self, session_id: str) -> str | None:
        rows = self.db._rows(
            "SELECT entry_id FROM session_entries WHERE session_id=? AND is_leaf=1 LIMIT 1", (session_id,)
        )
        return rows[0]["entry_id"] if rows else None

    def path(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """从 leaf 回溯到根，返回时间正序（旧 → 新）。"""
        leaf = self.leaf(session_id)
        if not leaf:
            return []
        items: list[dict[str, Any]] = []
        cursor: str | None = leaf
        while cursor and len(items) < limit:
            row = self.db._rows("SELECT * FROM session_entries WHERE entry_id=?", (cursor,))
            if not row:
                break
            items.append(row[0])
            cursor = row[0]["parent_id"]
        items.reverse()
        return items

    def messages(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """转成 OpenAI 消息格式（供 LLM 使用），并应用 context_edit 修订。

        修订规则（对齐 Pi 的 context_edit 语义）：被 exclude 的条目**不删**，
        只是不进模型上下文；修订说明本身以 system 消息插在最前面。
        额外保护 tool 配对——assistant 的 tool_calls 被排除时，它产生的
        tool 结果必须一起排除，否则会构造出「孤儿 tool 消息」，多数 provider 直接 400。
        """
        path = self.path(session_id, limit)
        excluded: set[str] = set()
        notes: list[str] = []
        for item in path:
            if item.get("entry_type") != "context_edit":
                continue
            try:
                payload = json.loads(item["content"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            excluded.update(payload.get("exclude") or [])
            if payload.get("note"):
                notes.append(str(payload["note"]))

        if excluded:
            orphan_calls: set[str] = set()
            for item in path:
                if item["role"] == "assistant" and item.get("tool_calls") and item["entry_id"] in excluded:
                    try:
                        orphan_calls.update(
                            call.get("id", "") for call in json.loads(item["tool_calls"])
                        )
                    except (json.JSONDecodeError, TypeError):
                        continue
            for item in path:
                if item["role"] == "tool" and item.get("tool_call_id") in orphan_calls:
                    excluded.add(item["entry_id"])

        out: list[dict[str, Any]] = []
        if notes:
            out.append({"role": "system", "content": "[上下文修订] " + "；".join(notes)})
        for item in path:
            if item["entry_id"] in excluded or item.get("entry_type") == "context_edit":
                continue
            role = item["role"]
            base = {"role": role, "content": item["content"] or ""}
            if role == "assistant" and item["tool_calls"]:
                try:
                    base["tool_calls"] = _openai_tool_calls(json.loads(item["tool_calls"]))
                except json.JSONDecodeError:
                    pass
            if role == "tool":
                base["tool_call_id"] = item["tool_call_id"]
                base["name"] = item["name"]
            if role == "summary":
                base = {"role": "system", "content": f"[历史摘要] {item['content'] or ''}"}
            out.append(base)
        return out

    def recent_entries(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        items = self.path(session_id, limit)
        for item in items:
            for key in ("tool_calls", "card"):
                if item.get(key):
                    try:
                        item[key] = json.loads(item[key])
                    except json.JSONDecodeError:
                        item[key] = None
        return items

    def ensure_leaf(self, session_id: str) -> str | None:
        """没有 leaf 但有历史（例如迁移数据）时，修复为最后一条。"""
        if self.leaf(session_id):
            return self.leaf(session_id)
        rows = self.db._rows(
            "SELECT entry_id FROM session_entries WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        if rows:
            self.set_leaf(session_id, rows[0]["entry_id"])
            return rows[0]["entry_id"]
        return None


def ensure_columns(db: Database) -> None:
    """轻量迁移：补 tool_call_id/name/entry_type 列（仅 SQLite 旧库）。"""
    if getattr(db, "dialect", "sqlite") != "sqlite":
        return
    with sqlite3.connect(db.path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_entries)").fetchall()}
        if "tool_call_id" not in cols:
            conn.execute("ALTER TABLE session_entries ADD COLUMN tool_call_id TEXT")
        if "name" not in cols:
            conn.execute("ALTER TABLE session_entries ADD COLUMN name TEXT")
        if "entry_type" not in cols:
            conn.execute("ALTER TABLE session_entries ADD COLUMN entry_type TEXT DEFAULT 'message'")