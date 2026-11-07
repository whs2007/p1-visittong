"""会话热状态：meta / 槽位 / 最近实体 / 分布式锁。

优先 Redis；连接失败自动降级为进程内实现（演示零依赖）。会话消息本身存在 SessionTree。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from app.config import Settings
from app.utils import new_id, now_iso


def _new_meta(user_id: str, title: str) -> dict[str, Any]:
    return {
        "session_id": f"s_{datetime.now():%Y%m%d_%H%M%S}_{new_id('x').split('_')[1][:4]}",
        "user_id": user_id,
        "title": title,
        "status": "idle",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "message_count": 0,
    }


class SessionState:
    backend = "memory"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ttl = settings.session_ttl_s
        self._meta: dict[str, dict[str, Any]] = {}
        self._slots: dict[str, dict[str, str]] = {}
        self._entity: dict[str, dict[str, str]] = {}
        self._locks: set[str] = set()
        self._guard = asyncio.Lock()
        self._redis: Any = None
        self.backend_reason: str = ""

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    async def init(self) -> None:
        try:
            import redis.asyncio as redis

            client = redis.from_url(
                self.settings.redis_url, encoding="utf-8", decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
                # 兼容 Redis 5（不支持 RESP3 的 HELLO 命令）
                protocol=2,
            )
            await client.ping()
            self._redis = client
            self.backend = "redis"
            self.backend_reason = ""
        except Exception as exc:  # noqa: BLE001
            self.backend = "memory"
            self.backend_reason = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ #
    # 会话元信息
    # ------------------------------------------------------------------ #
    async def create(self, user_id: str, title: str = "新会话") -> dict[str, Any]:
        meta = _new_meta(user_id, title)
        if self._redis:
            key = f"sess:{meta['session_id']}:meta"
            await self._redis.hset(key, mapping=meta)
            await self._redis.expire(key, self.ttl)
            await self._redis.zadd("session:index", {meta["session_id"]: time.time()})
        else:
            self._meta[meta["session_id"]] = meta
        return meta

    async def meta(self, session_id: str) -> dict[str, Any] | None:
        if self._redis:
            data = await self._redis.hgetall(f"sess:{session_id}:meta")
            return data or None
        return self._meta.get(session_id)

    async def update(self, session_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        if self._redis:
            key = f"sess:{session_id}:meta"
            await self._redis.hset(key, mapping={k: str(v) for k, v in fields.items()})
            await self._redis.expire(key, self.ttl)
            await self._redis.zadd("session:index", {session_id: time.time()})
        elif session_id in self._meta:
            self._meta[session_id].update(fields)

    async def list_sessions(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if self._redis:
            ids = await self._redis.zrevrange("session:index", 0, max(limit - 1, 0))
            out = []
            for sid in ids:
                data = await self.meta(sid)
                if data and data.get("user_id") == user_id:
                    out.append(data)
            return out
        rows = [m for m in self._meta.values() if m.get("user_id") == user_id]
        return sorted(rows, key=lambda m: m.get("updated_at", ""), reverse=True)[:limit]

    # ------------------------------------------------------------------ #
    # 槽位 / 实体
    # ------------------------------------------------------------------ #
    async def slots(self, session_id: str) -> dict[str, str]:
        if self._redis:
            return await self._redis.hgetall(f"sess:{session_id}:slots")
        return dict(self._slots.get(session_id, {}))

    async def set_slots(self, session_id: str, slots: dict[str, str]) -> None:
        if not slots:
            return
        if self._redis:
            key = f"sess:{session_id}:slots"
            await self._redis.hset(key, mapping=slots)
            await self._redis.expire(key, self.ttl)
        else:
            self._slots.setdefault(session_id, {}).update(slots)

    async def clear_slots(self, session_id: str, keys: list[str] | None = None) -> None:
        if self._redis:
            key = f"sess:{session_id}:slots"
            if keys is None:
                await self._redis.delete(key)
            else:
                await self._redis.hdel(key, *keys)
        elif session_id in self._slots:
            if keys is None:
                self._slots[session_id].clear()
            else:
                for key in keys:
                    self._slots[session_id].pop(key, None)

    async def entity(self, session_id: str) -> dict[str, str]:
        if self._redis:
            return await self._redis.hgetall(f"sess:{session_id}:entity")
        return dict(self._entity.get(session_id, {}))

    async def set_entity(self, session_id: str, entity: dict[str, str]) -> None:
        if not entity:
            return
        if self._redis:
            key = f"sess:{session_id}:entity"
            await self._redis.hset(key, mapping=entity)
            await self._redis.expire(key, self.ttl)
        else:
            self._entity.setdefault(session_id, {}).update(entity)

    # ------------------------------------------------------------------ #
    # 并发锁
    # ------------------------------------------------------------------ #
    async def try_lock(self, session_id: str) -> bool:
        if self._redis:
            return bool(await self._redis.set(f"sess:{session_id}:lock", "1", nx=True, ex=60))
        async with self._guard:
            if session_id in self._locks:
                return False
            self._locks.add(session_id)
            return True

    async def unlock(self, session_id: str) -> None:
        if self._redis:
            await self._redis.delete(f"sess:{session_id}:lock")
            return
        async with self._guard:
            self._locks.discard(session_id)