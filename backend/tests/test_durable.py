"""Phase 2：Durable Execution 测试。

覆盖：
- 每个 Turn 落 task_runs + turn_checkpoints + tool_executions；
- 任务被标记 interrupted 后，POST /tasks/{id}/resume 从检查点继续并收敛为 done；
- resume 不重复执行已完成的工具（签到次数仍为 1）；
- 启动收敛：running -> interrupted；工具执行去重唯一约束。
"""
from __future__ import annotations

import json
import sqlite3

import app.skills.tools  # noqa: F401  注册工具
import pytest
from app.config import Settings
from app.domain.db import Database
from app.domain.tasks import TaskStore
from app.main import create_app
from fastapi.testclient import TestClient


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "test.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=2,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/auth/login", json={"user_id": "sales-001", "password": "123456"}
        )
        assert login.status_code == 200, login.text
        test_client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield test_client


def parse_sse(response) -> list[tuple[str | None, dict]]:
    events: list[tuple[str | None, dict]] = []
    name = None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line[5:].strip())))
    return events


def chat(client: TestClient, session_id: str, message: str):
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": session_id, "message": message}
    ) as response:
        assert response.status_code == 200, response.text
        return parse_sse(response)


def new_session(client: TestClient) -> str:
    return client.post("/api/sessions", json={}).json()["session_id"]


def trace_of(events) -> str:
    return next(data["trace_id"] for name, data in events if name == "start")


def create_plan_via_chat(client: TestClient, session_id: str) -> str:
    for message in ("帮我约下周三上午拜访字节跳动", "北京中关村"):
        events = chat(client, session_id, message)
        for name, data in events:
            if name == "card" and data.get("card_name") == "visit_plan":
                return data["data"]["plan_id"]
    raise AssertionError("未能创建计划")


def test_turn_writes_task_checkpoint_and_tool_execution(client) -> None:
    sid = new_session(client)
    events = chat(client, sid, "查一下我今天的拜访待办")
    turn_id = trace_of(events)
    detail = client.get(f"/api/tasks/{turn_id}").json()
    assert detail["task"]["status"] == "done"
    assert detail["task"]["stop_reason"] == "no_tool_calls"
    assert detail["checkpoint"]["step"] >= 1
    assert detail["tool_executions"] >= 1
    assert detail["resumable"] is False


def test_resume_from_checkpoint_does_not_duplicate_tool(client, tmp_path) -> None:
    sid = new_session(client)
    plan_id = create_plan_via_chat(client, sid)
    events = chat(client, sid, "我到了，签到")
    turn_id = trace_of(events)
    assert client.get(f"/api/plans/{plan_id}").json()["plan"]["status"] == "checked_in"

    # 模拟进程崩溃：任务停在 running/后续未完成（把 done 改成 interrupted）
    con = sqlite3.connect(str(tmp_path / "test.db"))
    con.execute("UPDATE task_runs SET status='interrupted', finished_at=NULL WHERE turn_id=?", (turn_id,))
    con.commit()
    con.close()

    with client.stream("POST", f"/api/tasks/{turn_id}/resume") as response:
        assert response.status_code == 200, response.text
        events = parse_sse(response)
    names = [name for name, _ in events]
    assert "done" in names, events
    assert any(name == "status" and data.get("phase") == "resume" for name, data in events)

    detail = client.get(f"/api/tasks/{turn_id}").json()
    assert detail["task"]["status"] == "done"
    assert detail["task"]["resume_count"] == 1
    # 工具未重复执行：签到记录仍只有 1 条，计划状态未被破坏
    plan = client.get(f"/api/plans/{plan_id}").json()
    assert len(plan["checkins"]) == 1
    assert plan["plan"]["status"] == "checked_in"


def test_resume_requires_interrupted_status(client) -> None:
    sid = new_session(client)
    events = chat(client, sid, "查一下我今天的拜访待办")
    turn_id = trace_of(events)
    res = client.post(f"/api/tasks/{turn_id}/resume")
    assert res.status_code == 409
    assert res.json()["code"] == "TASK_NOT_INTERRUPTED"


def test_task_store_reconcile_and_idempotent_tool(tmp_path) -> None:
    db = Database(str(tmp_path / "tasks.db"))
    db.init()
    store = TaskStore(db)
    store.create(turn_id="t1", session_id="s1", user_id="u1", trace_id="t1", user_input="hi")
    assert store.reconcile_interrupted() == 1
    assert store.get("t1")["status"] == "interrupted"
    store.save_tool(turn_id="t1", step=0, call_id="c1", name="checkin", status="ok", output="ok", data={"a": 1})
    store.save_tool(turn_id="t1", step=0, call_id="c1", name="checkin", status="ok", output="ok", data={"a": 1})
    assert store.tool_count("t1") == 1
    saved = store.saved_tool("t1", 0, "c1")
    assert saved and saved["name"] == "checkin"