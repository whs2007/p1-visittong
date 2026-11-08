"""结构化日志：一行一个 JSON，字段固定，便于采集与告警。

v0.6 及之前只有一条 `turn done ... latency_ms=...` 的 printf 日志，问题：
- 字段靠正则从文本里抠，改个文案就断；
- 没有 request_id / trace_id 贯穿，出了事只能按时间猜；
- 异常堆栈与业务日志混在一起，日志平台无法按 level 聚合。

这里做三件事：
1. `JsonFormatter` 把标准字段（ts/level/logger/msg）+ 业务字段（extra）打平；
2. `request_id` 用 contextvar 贯穿一次 HTTP 请求；
3. `setup()` 在应用启动时替换 root handler（保留 uvicorn 的 access log 不动）。
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

#: 允许从 LogRecord 打平到 JSON 顶层的业务字段白名单（避免把 args/exc_info 打进去）
_EXTRA_FIELDS = (
    "event", "request_id", "trace_id", "turn_id", "session_id", "user_id",
    "skill", "stop_reason", "latency_ms", "tool_calls", "tokens_in", "tokens_out",
    "agent", "capability_fingerprint", "status_code", "path", "method",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if not payload.get("request_id"):
            rid = request_id_var.get()
            if rid:
                payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
