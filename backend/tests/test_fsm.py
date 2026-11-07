"""Phase 1：业务 FSM（计划/会话）测试。

覆盖：
- 纯状态机：合法/非法迁移、迁移审计落库；
- 计划生命周期：创建 → 签到 → 签退 → 写日志（checked_out → done）；
- 会话 FSM：一次 Turn 后 fsm_state=done，并可从 /state 查到迁移；
- 非法操作：已签到计划不可改约。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.skills.tools  # noqa: F401  注册工具
from app.config import Settings
from app.domain.db import Database
from app.domain.fsm import FSM, PLAN_TRANSITIONS, SessionFSM
from app.main import create_app
from app.sessions.store import SessionState


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "test.db"),
        redis_url="redis://127.0.0.1:1/0",  # 必定失败 -> 内存降级
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


def create_plan_via_chat(client: TestClient, session_id: str) -> str:
    """创建计划（客户可能有多个门店，首轮会追问一次门店）。"""
    for message in ("帮我约下周三上午拜访字节跳动", "北京中关村"):
        events = chat(client, session_id, message)
        for name, data in events:
            if name == "card" and data.get("card_name") == "visit_plan":
                return data["data"]["plan_id"]
    raise AssertionError("未能创建计划")


def test_plan_fsm_transition_table_is_consistent() -> None:
    states = {"new", "planned", "checked_in", "checked_out", "done", "cancelled"}
    for (source, event), target in PLAN_TRANSITIONS.items():
        assert source in states and target in states, (source, event, target)
    assert PLAN_TRANSITIONS[("planned", "checkin")] == "checked_in"
    assert PLAN_TRANSITIONS[("checked_in", "checkout")] == "checked_out"
    assert PLAN_TRANSITIONS[("checked_out", "log")] == "done"
    assert ("done", "cancel") not in PLAN_TRANSITIONS


def test_fsm_pure_transitions_and_audit(tmp_path) -> None:
    db = Database(str(tmp_path / "fsm.db"))
    db.init()
    plan = db.create_plan(
        customer_id="c_byte", store_id=None, owner_id="sales-001",
        planned_at="2026-11-05T10:00:00+08:00", topic="产品方案",
    )
    fsm = FSM(db)
    assert fsm.state(plan["plan_id"]) == "planned"
    assert fsm.plan(plan["plan_id"], "update", actor="sales-001").ok
    result = fsm.plan(plan["plan_id"], "checkin", actor="sales-001")
    assert result.ok and result.from_state == "planned" and result.to_state == "checked_in"
    # 已签到不能再改约/签到
    assert not fsm.can(plan["plan_id"], "update").ok
    assert not fsm.plan(plan["plan_id"], "checkin").ok
    assert fsm.plan(plan["plan_id"], "checkout", actor="sales-001").ok
    assert fsm.state(plan["plan_id"]) == "checked_out"
    assert fsm.plan(plan["plan_id"], "log", actor="sales-001").ok
    assert fsm.state(plan["plan_id"]) == "done"
    rows = db.list_state_transitions("plan", plan["plan_id"])
    assert [row["to_state"] for row in rows] == ["done", "checked_out", "checked_in", "planned"]


def test_plan_lifecycle_checkin_checkout_log(client) -> None:
    sid = new_session(client)
    plan_id = create_plan_via_chat(client, sid)

    # 签到
    chat(client, sid, "我到了，签到")
    detail = client.get(f"/api/plans/{plan_id}").json()
    assert detail["plan"]["status"] == "checked_in"

    # 签退
    chat(client, sid, "结束拜访，签退")
    detail = client.get(f"/api/plans/{plan_id}").json()
    assert detail["plan"]["status"] == "checked_out"

    # 写日志 -> done
    chat(client, sid, "写日志：客户对方案感兴趣，下周给试用账号")
    detail = client.get(f"/api/plans/{plan_id}").json()
    assert detail["plan"]["status"] == "done"
    events = [row["event"] for row in detail["transitions"]]
    assert events[0] == "log"
    assert {"create", "checkin", "checkout", "log"} <= set(events)


def test_checked_in_plan_cannot_be_modified(client) -> None:
    sid = new_session(client)
    plan_id = create_plan_via_chat(client, sid)
    chat(client, sid, "我到了，签到")
    assert client.get(f"/api/plans/{plan_id}").json()["plan"]["status"] == "checked_in"
    # 改约会被 FSM 拒绝，计划状态保持不变
    events = chat(client, sid, "把刚才那个计划改到明天下午三点")
    outputs = [data.get("output", "") for name, data in events if name == "tool_result"]
    assert any("不能修改" in text for text in outputs), outputs
    assert client.get(f"/api/plans/{plan_id}").json()["plan"]["status"] == "checked_in"


def test_session_fsm_state_endpoint(client) -> None:
    sid = new_session(client)
    chat(client, sid, "查一下我今天的拜访待办")
    state = client.get(f"/api/sessions/{sid}/state").json()
    assert state["fsm_state"] == "done"
    events = [row["event"] for row in state["transitions"]]
    assert events[:3] == ["finish", "route", "start"], events
    assert {"start", "route", "finish"} <= set(events)