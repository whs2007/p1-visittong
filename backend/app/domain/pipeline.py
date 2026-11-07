"""数据管道模拟：Hive -> Kafka -> consumer 的事件箱（落 SQLite，语义与线上一致）。

线上是 hive2kafka 全量/增量 + consumer 异步 embedding；个人项目用 ingest_events
表模拟 topic，`drain()` 等价 consumer：取 pending 事件 -> 处理 -> 标记 processed，
重复事件幂等（doc_id upsert），可被定时任务/接口触发。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.domain.db import Database
from app.utils import new_id, now_iso


class EventPipeline:
    def __init__(self, db: Database) -> None:
        self.db = db

    def emit(self, topic: str, payload: dict[str, Any]) -> str:
        event_id = new_id("evt")
        self.db._exec(  # noqa: SLF001
            "INSERT INTO ingest_events (event_id,topic,payload,status,created_at)"
            " VALUES (?,?,?,?,?)",
            (event_id, topic, json.dumps(payload, ensure_ascii=False), "pending", now_iso()),
        )
        return event_id

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db._rows(  # noqa: SLF001
            "SELECT * FROM ingest_events WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,)
        )

    def mark_processed(self, event_id: str) -> None:
        self.db._exec(  # noqa: SLF001
            "UPDATE ingest_events SET status='processed', processed_at=? WHERE event_id=?",
            (now_iso(), event_id),
        )

    def drain(self, handler: Callable[[dict[str, Any]], Any], limit: int = 100) -> int:
        processed = 0
        for event in self.pending(limit):
            payload = json.loads(event.get("payload") or "{}")
            handler({"topic": event["topic"], "payload": payload, "event_id": event["event_id"]})
            self.mark_processed(event["event_id"])
            processed += 1
        return processed

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db._rows(  # noqa: SLF001
            "SELECT * FROM ingest_events ORDER BY created_at DESC LIMIT ?", (limit,)
        )