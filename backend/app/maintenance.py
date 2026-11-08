"""运维子命令：单独跑一次崩溃对账（不启动 Web 服务）。

存在的原因：多 worker / 多副本部署时，`reconcile_*` 只能由**一个**进程执行，
否则会把其他进程正在跑的任务误判成崩溃（`running` → `interrupted`）。
所以 web 进程统一 `RECONCILE_ON_START=0`，由本命令在发布流程里跑一次。

用法：
    python -m app.maintenance reconcile
    python -m app.maintenance status

退出码 0 = 执行成功（有没有需要对账的对象都算成功）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.domain.db import Database  # noqa: E402
from app.domain.migrations import apply as apply_migrations  # noqa: E402
from app.domain.migrations import current_version  # noqa: E402
from app.domain.tasks import TaskStore  # noqa: E402
from app.trace.store import TraceStore  # noqa: E402


async def _reconcile(settings) -> dict:
    db = Database(
        settings.sqlite_path, settings.demo_password,
        backend=settings.db_backend, dsn=settings.mysql_dsn,
    )
    db.init()
    applied = apply_migrations(settings.sqlite_path)
    tasks = TaskStore(db)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    interrupted = tasks.reconcile_interrupted()
    attempts = tasks.reconcile_tool_attempts()
    stale = await traces.reconcile_stale(include_traces=True)
    return {
        "migrations_applied": applied,
        "schema_version": current_version(settings.sqlite_path),
        "turns_interrupted": interrupted,
        "tool_attempts_unknown": attempts["count"],
        **stale,
    }


def _status(settings) -> dict:
    db = Database(
        settings.sqlite_path, settings.demo_password,
        backend=settings.db_backend, dsn=settings.mysql_dsn,
    )
    # status 也可能在全新库上被调用（发版前先看一眼），所以同样先保证 schema 存在
    db.init()
    apply_migrations(settings.sqlite_path)
    rows = db._rows(  # noqa: SLF001
        "SELECT status, COUNT(*) AS n FROM task_runs GROUP BY status"
    )
    dangling = db._rows(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM tool_executions WHERE status='running'"
    )
    return {
        "schema_version": current_version(settings.sqlite_path),
        "turns_by_status": {r["status"]: r["n"] for r in rows},
        "tool_attempts_running": dangling[0]["n"] if dangling else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.maintenance")
    parser.add_argument("command", choices=["reconcile", "status"])
    args = parser.parse_args()

    settings = get_settings()
    if args.command == "reconcile":
        payload = asyncio.run(_reconcile(settings))
    else:
        payload = _status(settings)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
