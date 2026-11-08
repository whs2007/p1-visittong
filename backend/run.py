"""启动入口：python backend/run.py（默认 127.0.0.1:8020）。

多 worker 说明（重要）：
审批等待态已经持久化（DB 为准 + 轮询兜底），因此可以开多 worker；但**崩溃对账
只能由一个进程做**（否则会误伤其他 worker 正在跑的任务），所以除首个 worker 外
都应设置 `RECONCILE_ON_START=0`：

    WEB_CONCURRENCY=4 RECONCILE_ON_START=0 python run.py

副作用幂等不依赖进程内状态（账本在 SQLite），所以多 worker 下的重复执行
仍会被 `tool_executions` / `idempotency_effects` 拦住。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    workers = int(os.getenv("WEB_CONCURRENCY", "1"))
    if workers > 1:
        # 多 worker 必须用 import string 启动（uvicorn 要求）
        uvicorn.run(
            "app.main:app", host=settings.host, port=settings.port,
            log_level="info", workers=workers,
        )
    else:
        uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")
