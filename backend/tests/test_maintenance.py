"""运维子命令（`python -m app.maintenance`）必须真的能跑。

这条用例的存在理由很具体：`main.py` 的注释里写着「用
`python -m app.maintenance reconcile` 单独对账」，而这个模块当时并不存在 ——
文档让人执行一条不存在的命令，比不给方案更糟。现在把它钉成可执行的检查：
起一个子进程，指向临时库，跑 status 与 reconcile，断言退出码 0 且输出可解析。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _run(command: str, db_path: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "SQLITE_PATH": str(db_path),
        "AUTH_MODE": "demo",
        "AUTH_ALLOW_INSECURE_SECRET": "1",
        "LLM_MODE": "mock",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "RECONCILE_ON_START": "0",
    })
    proc = subprocess.run(
        [sys.executable, "-m", "app.maintenance", command],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_maintenance_status_and_reconcile(tmp_path):
    db_path = tmp_path / "maint.db"
    status = _run("status", db_path)
    assert status["schema_version"] >= 1, "迁移没有在子进程里应用到"

    reconciled = _run("reconcile", db_path)
    assert reconciled["turns_interrupted"] == 0
    assert reconciled["tool_attempts_unknown"] == 0
    assert "traces_failed" in reconciled and "approvals_expired" in reconciled
