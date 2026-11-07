"""音频分片聚合：优先 Redis（生产可横向扩展），不可用时降级进程内。

对齐模板链路：录音分片上传 → Redis 聚合 → 完成后 ASR → 异步纪要。
`AudioStore` 只负责"按 upload_id 聚合有序分片"，转写/纪要由上层完成。
"""
from __future__ import annotations

from typing import Any

from app.config import Settings


class AudioStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = "memory"
        self.backend_reason = ""
        self._redis: Any = None
        self._chunks: dict[str, dict[int, bytes]] = {}

    async def init(self) -> None:
        try:
            import redis.asyncio as redis

            client = redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=1, socket_timeout=1, protocol=2,
                decode_responses=False,
            )
            await client.ping()
            self._redis = client
            self.backend = "redis"
        except Exception as exc:  # noqa: BLE001
            self.backend = "memory"
            self.backend_reason = f"{type(exc).__name__}: {exc}"

    async def add_chunk(self, upload_id: str, seq: int, data: bytes) -> int:
        if self._redis:
            key = f"audio:{upload_id}"
            await self._redis.hset(key, str(seq), data)
            await self._redis.expire(key, 3600)
            return int(await self._redis.hlen(key))
        bucket = self._chunks.setdefault(upload_id, {})
        bucket[int(seq)] = data
        return len(bucket)

    async def chunks(self, upload_id: str) -> list[bytes]:
        if self._redis:
            raw = await self._redis.hgetall(f"audio:{upload_id}")
            return [raw[key] for key in sorted(raw, key=lambda k: int(k))]
        bucket = self._chunks.get(upload_id) or {}
        return [bucket[seq] for seq in sorted(bucket)]

    async def count(self, upload_id: str) -> int:
        if self._redis:
            return int(await self._redis.hlen(f"audio:{upload_id}"))
        return len(self._chunks.get(upload_id) or {})

    async def clear(self, upload_id: str) -> None:
        if self._redis:
            await self._redis.delete(f"audio:{upload_id}")
            return
        self._chunks.pop(upload_id, None)