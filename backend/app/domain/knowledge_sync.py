"""知识同步 consumer：版本校验 / 幂等 Upsert / 重试 / 死信队列。

简历上的链路是 `Hive → Kafka → Embedding → Milvus`。本项目的等价物是
`SQLite 事件箱（模拟 Kafka） → consumer 同步 embedding（hash-gram）→ knowledge_docs`，
但**链路语义一条不减**，否则「异步同步」四个字经不住问：

| 线上问题 | 这里的落点 |
|---|---|
| 事件重复投递（at-least-once） | `ON CONFLICT(doc_id)` + 版本守卫，重复写同一版本不改行 |
| 乱序到达（v3 先到、v2 后到） | `WHERE excluded.source_version > 当前版本`，v2 被 SQL 拒掉 |
| 删除后旧事件回流 | 软删墓碑 + 版本守卫：删除也是版本，迟到事件写不回来 |
| consumer 处理失败 | 有界重试 → 进 `knowledge_dlq`，**不静默丢** |
| 死信之后怎么恢复 | DLQ 可重放，成功置 `replayed`，原始 attempts 留档 |

为什么版本守卫必须写在 SQL 里：`if version > stored: write` 在单进程单线程下没
问题，两个 consumer 并发时都会读到旧版本、都通过判断，最后写入顺序决定结果——
这就是「本地跑得好、上线丢数据」的经典形态。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.db import Database
from app.domain.pipeline import EventPipeline
from app.utils import now_iso

log = logging.getLogger(__name__)

TOPIC = "hive2kafka.knowledge_sync"
SUPPORTED_OPS = ("upsert", "delete")


class KnowledgeSync:
    def __init__(
        self, db: Database, knowledge: Any, events: EventPipeline, *, max_retries: int = 2,
    ) -> None:
        self.db = db
        self.knowledge = knowledge
        self.events = events
        self.max_retries = max(0, int(max_retries))

    # ------------------------------------------------------------------ #
    # 生产者
    # ------------------------------------------------------------------ #
    def emit(
        self, *, doc_id: str, kind: str = "customer", ref_id: str = "", title: str = "",
        text: str = "", version: int = 1, op: str = "upsert",
    ) -> str:
        """发一条变更事件。version 来自上游 binlog 位点，必须单调递增。"""
        payload = {
            "doc_id": doc_id, "kind": kind, "ref_id": ref_id, "title": title,
            "text": text, "version": int(version), "op": op,
        }
        return self.events.emit(TOPIC, payload)

    # ------------------------------------------------------------------ #
    # 消费者
    # ------------------------------------------------------------------ #
    def drain(self, limit: int = 100) -> dict[str, int]:
        stats = {"scanned": 0, "processed": 0, "stale": 0, "dead": 0, "retried": 0}
        for event in self.events.pending(limit):
            stats["scanned"] += 1
            try:
                payload = json.loads(event.get("payload") or "{}")
            except json.JSONDecodeError:
                self._to_dlq(event, {}, "invalid_payload", 0)
                self.events.mark_status(event["event_id"], "dead")
                stats["dead"] += 1
                continue
            outcome, attempts = self._process(event, payload)
            stats["retried"] += max(0, attempts - 1)
            if outcome == "processed":
                self.events.mark_processed(event["event_id"])
                stats["processed"] += 1
            elif outcome == "stale":
                # 陈旧事件也要标终态，否则每轮 drain 都会重新扫到它
                self.events.mark_processed(event["event_id"])
                stats["stale"] += 1
            else:
                self.events.mark_status(event["event_id"], "dead")
                stats["dead"] += 1
        return stats

    def _process(self, event: dict[str, Any], payload: dict[str, Any]) -> tuple[str, int]:
        """返回 (outcome, 实际尝试次数)。尝试次数 = 首发 1 次 + 重试次数。"""
        doc_id = str(payload.get("doc_id") or "")
        version = int(payload.get("version") or 0)
        op = str(payload.get("op") or "upsert")
        if not doc_id:
            self._to_dlq(event, payload, "missing_doc_id", 0)
            return "dead", 1

        stored = self.knowledge.version_of(doc_id)      # -1 表示库里没有
        if stored >= 0 and version <= stored:
            return "stale", 1

        used = 0
        while True:
            used += 1
            try:
                if op == "upsert":
                    self.knowledge.upsert(
                        doc_id=doc_id, kind=str(payload.get("kind") or "customer"),
                        ref_id=str(payload.get("ref_id") or ""),
                        title=str(payload.get("title") or doc_id),
                        text=str(payload.get("text") or ""), version=version,
                    )
                elif op == "delete":
                    self.knowledge.soft_delete(doc_id=doc_id, version=version)
                else:
                    raise ValueError(f"unsupported_op:{op}")
                return "processed", used
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge sync failed doc=%s op=%s attempt=%s err=%s", doc_id, op, used, exc)
                if used > self.max_retries:
                    self._to_dlq(event, payload, f"{type(exc).__name__}: {exc}", used)
                    return "dead", used

    def _to_dlq(self, event: dict[str, Any], payload: dict[str, Any], reason: str, attempts: int) -> None:
        self.db._exec(  # noqa: SLF001
            "INSERT INTO knowledge_dlq (event_id,doc_id,op,reason,attempts,payload,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                event.get("event_id"), str(payload.get("doc_id") or ""),
                str(payload.get("op") or ""), reason[:400], attempts,
                json.dumps(payload, ensure_ascii=False), "dead", now_iso(),
            ),
        )
        log.error("knowledge event to DLQ: doc=%s reason=%s", payload.get("doc_id"), reason)

    # ------------------------------------------------------------------ #
    # 运维视图 / 死信重放
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        def _count(sql: str, params: tuple = ()) -> int:
            rows = self.db._rows(sql, params)  # noqa: SLF001
            return int(rows[0]["n"]) if rows else 0

        total = _count("SELECT COUNT(*) AS n FROM knowledge_docs")
        active = _count("SELECT COUNT(*) AS n FROM knowledge_docs WHERE deleted_at IS NULL")
        return {
            "topic": TOPIC,
            "max_retries": self.max_retries,
            "events": {
                "pending": _count("SELECT COUNT(*) AS n FROM ingest_events WHERE status='pending'"),
                "processed": _count("SELECT COUNT(*) AS n FROM ingest_events WHERE status='processed'"),
                "dead": _count("SELECT COUNT(*) AS n FROM ingest_events WHERE status='dead'"),
            },
            "dlq": {
                "dead": _count("SELECT COUNT(*) AS n FROM knowledge_dlq WHERE status='dead'"),
                "replayed": _count("SELECT COUNT(*) AS n FROM knowledge_dlq WHERE status='replayed'"),
            },
            "docs": {"active": active, "deleted": total - active, "total": total},
        }

    def list_dlq(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db._rows(  # noqa: SLF001
            "SELECT id,event_id,doc_id,op,reason,attempts,status,created_at,replayed_at"
            " FROM knowledge_dlq WHERE status='dead' ORDER BY id DESC LIMIT ?", (limit,),
        )

    def replay(self, dlq_id: int) -> dict[str, Any]:
        """把死信重新投递。重放走同一条链路，不做「直接写库」的捷径——
        捷径等于绕过版本守卫，重放就成了新的数据污染源。"""
        rows = self.db._rows(  # noqa: SLF001
            "SELECT * FROM knowledge_dlq WHERE id=?", (dlq_id,),
        )
        if not rows:
            return {"result": "not_found"}
        row = rows[0]
        if row["status"] != "dead":
            return {"result": "already_replayed", "status": row["status"]}
        payload = json.loads(row.get("payload") or "{}")
        event_id = self.events.emit(TOPIC, payload)
        self.db._exec(  # noqa: SLF001
            "UPDATE knowledge_dlq SET status='replayed', replayed_at=?, event_id=? WHERE id=? AND status='dead'",
            (now_iso(), event_id, dlq_id),
        )
        return {"result": "requeued", "event_id": event_id}
