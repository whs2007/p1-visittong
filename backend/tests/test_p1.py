"""P1 拜访通：单元 + 接口 + 审批 + 会话树测试（Mock LLM，临时 SQLite）。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import app.skills.tools  # noqa: F401  注册工具
import pytest
from app.approvals import ApprovalManager
from app.auth import ensure_can_decide
from app.config import Settings
from app.domain.db import Database
from app.errors import ApiError
from app.main import create_app
from app.orchestrator.router import build_slots, parse_time, route_text
from app.policy.engine import PolicyEngine
from app.sessions.tree import SessionTree, ensure_columns
from app.skills.registry import registry
from app.trace.store import TraceStore
from fastapi.testclient import TestClient


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


@pytest.fixture()
def manager_client(tmp_path):
    app = create_app(make_settings(tmp_path / "manager"))
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/auth/login", json={"user_id": "manager-001", "password": "123456"}
        )
        assert login.status_code == 200, login.text
        test_client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield test_client


def test_auth_required_and_login(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as anon:
        assert anon.get("/api/me").status_code == 401
        assert anon.post("/api/auth/login", json={"user_id": "sales-001", "password": "wrong"}).status_code == 401
        ok = anon.post("/api/auth/login", json={"user_id": "sales-001", "password": "123456"})
        assert ok.status_code == 200 and ok.json()["user"]["role"] == "sales"


def test_change_password(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login", json={"user_id": "sales-001", "password": "123456"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        r = client.post(
            "/api/auth/change-password",
            json={"old_password": "123456", "new_password": "newpass-123"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert client.post(
            "/api/auth/login", json={"user_id": "sales-001", "password": "123456"}
        ).status_code == 401
        assert client.post(
            "/api/auth/login", json={"user_id": "sales-001", "password": "newpass-123"}
        ).status_code == 200


def test_login_throttle(tmp_path):
    app = create_app(make_settings(tmp_path, login_max_attempts=3, login_lock_seconds=60))
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post(
                "/api/auth/login", json={"user_id": "sales-001", "password": "bad"}
            ).status_code == 401
        blocked = client.post(
            "/api/auth/login", json={"user_id": "sales-001", "password": "bad"}
        )
        assert blocked.status_code == 429 and blocked.json()["code"] == "TOO_MANY_ATTEMPTS"


def test_self_approval_and_role_rules():
    sales = {"user_id": "sales-001", "role": "sales"}
    manager = {"user_id": "manager-001", "role": "manager"}
    pending = {"status": "pending", "requester": "sales-001"}
    with pytest.raises(ApiError) as exc:
        ensure_can_decide(sales, pending)
    assert exc.value.code == "FORBIDDEN"
    with pytest.raises(ApiError) as exc:
        ensure_can_decide({**manager, "user_id": "sales-001"}, pending)
    assert exc.value.code == "SELF_APPROVAL_FORBIDDEN"
    ensure_can_decide(manager, pending)


def test_owner_and_role_rules():
    from app.auth import ensure_owner_or_manager

    with pytest.raises(ApiError) as exc:
        ensure_owner_or_manager({"user_id": "sales-001", "role": "sales"}, "sales-002")
    assert exc.value.code == "FORBIDDEN"
    ensure_owner_or_manager({"user_id": "sales-001", "role": "sales"}, "sales-001")
    ensure_owner_or_manager({"user_id": "manager-001", "role": "manager"}, "sales-001")


def test_approvals_manager_only(client, manager_client):
    assert client.get("/api/approvals").status_code == 403
    assert manager_client.get("/api/approvals").status_code == 200


def parse_sse(response) -> list[tuple[str | None, dict]]:
    events: list[tuple[str | None, dict]] = []
    name = None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line[5:].strip())))
    return events


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def test_health(client):
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert data["llm_mode"] == "mock"
    assert data["db"] == "sqlite"


def test_session_todo_chat(client):
    session = client.post("/api/sessions", json={}).json()
    sid = session["session_id"]
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "查一下我今天的拜访待办"}
    ) as response:
        events = parse_sse(response)
    names = [name for name, _ in events]
    cards = [data["card_name"] for name, data in events if name == "card"]
    assert names[0] == "start"
    assert "done" in names
    assert "error" not in names
    assert "todo_list" in cards


def test_plan_create_with_slot_fill(client):
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "帮我约下周三上午拜访字节跳动"}
    ) as response:
        events = parse_sse(response)
    assert any(name == "slot_fill" for name, _ in events)

    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "北京中关村，找张总"}
    ) as response:
        events = parse_sse(response)
    cards = [data["card_name"] for name, data in events if name == "card"]
    assert "visit_plan" in cards
    assert any(name == "done" for name, _ in events)


def test_session_not_found(client):
    response = client.post(
        "/api/chat/stream", json={"session_id": "s_not_exist", "message": "你好"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_dashboard_after_turn(client):
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "现在几点了"}
    ) as response:
        list(response.iter_lines())
    stats = client.get("/api/dashboard/stats").json()
    assert stats["summary"]["total"] >= 1
    assert len(stats["skills"]) == 8
    assert len(stats["tools"]) == 10


# --------------------------------------------------------------------------- #
# 策略引擎
# --------------------------------------------------------------------------- #
def test_policy_decisions(tmp_path):
    settings = make_settings(tmp_path)
    policy = PolicyEngine(registry, settings)

    assert policy.evaluate(user_id="u", role="sales", name="cancel_visit_plan", args={}).action == "approval"
    assert policy.evaluate(user_id="u", role="sales", name="checkin", args={"distance_m": 320}).action == "allow"
    assert policy.evaluate(user_id="u", role="sales", name="checkin", args={"distance_m": 1200}).action == "approval"
    assert policy.evaluate(user_id="u", role="sales", name="checkin", args={"distance_m": 3000}).action == "deny"
    assert policy.evaluate(user_id="u", role="sales", name="query_todos", args={}).action == "allow"
    assert policy.evaluate(user_id="u", role="sales", name="save_visit_log", args={}).action == "deny"
    assert policy.evaluate(user_id="u", role="sales", name="no_such_tool", args={}).action == "deny"


def test_tool_disable_denies(tmp_path):
    settings = make_settings(tmp_path)
    policy = PolicyEngine(registry, settings)
    registry.set_enabled("query_todos", False)
    try:
        assert policy.evaluate(user_id="u", name="query_todos", args={}).action == "deny"
    finally:
        registry.set_enabled("query_todos", True)


# --------------------------------------------------------------------------- #
# 路由 / 时间解析 / 槽位
# --------------------------------------------------------------------------- #
def test_route_text(tmp_path):
    db = Database(str(tmp_path / "route.db"))
    db.init()
    db.seed_demo()
    assert route_text(db, "查一下我今天的拜访待办").skill == "todo_query"
    assert route_text(db, "把下周三的拜访取消掉").skill == "plan_cancel"
    assert route_text(db, "我到店了，签到").skill == "checkin"
    assert route_text(db, "帮我记一下：客户很感兴趣").skill == "visit_log"
    assert route_text(db, "把刚才那个计划改到下午 3 点").skill == "plan_update"
    assert route_text(db, "帮我约下周三拜访字节跳动").skill == "plan_create"


def test_parse_time():
    planned = parse_time("下周三上午")
    assert planned is not None
    dt = datetime.fromisoformat(planned)
    assert dt.hour == 10 and dt.minute == 0

    afternoon = parse_time("下午 3 点")
    assert afternoon is not None
    dt2 = datetime.fromisoformat(afternoon)
    assert dt2.hour == 15


def test_slots_extraction(tmp_path):
    db = Database(str(tmp_path / "slots.db"))
    db.init()
    db.seed_demo()
    slots = build_slots(db, "下周三上午去北京中关村拜访字节跳动")
    assert slots["customer"] == "字节跳动"
    assert "中关村" in slots["store"]
    assert slots["planned_at"].endswith("10:00:00+08:00")


# --------------------------------------------------------------------------- #
# 会话树
# --------------------------------------------------------------------------- #
def test_session_tree_branch(tmp_path):
    db = Database(str(tmp_path / "tree.db"))
    db.init()
    ensure_columns(db)
    tree = SessionTree(db)

    first = tree.append(session_id="s1", parent_id=None, role="user", content="第一个问题")
    second = tree.append(session_id="s1", parent_id=None, role="assistant", content="第一个回答")
    assert tree.leaf("s1") == second["entry_id"]

    # 切回第一个节点再续写 -> 形成分支
    assert tree.set_leaf("s1", first["entry_id"])
    third = tree.append(session_id="s1", parent_id=None, role="user", content="分支问题")
    assert third["parent_id"] == first["entry_id"]
    assert tree.leaf("s1") == third["entry_id"]
    assert [m["content"] for m in tree.messages("s1")] == ["第一个问题", "分支问题"]
    # 旧分支入口仍存在，可切回
    assert tree.set_leaf("s1", second["entry_id"])


# --------------------------------------------------------------------------- #
# 审批
# --------------------------------------------------------------------------- #
async def test_approval_approve_and_idempotent(tmp_path):
    settings = make_settings(tmp_path)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    manager = ApprovalManager(traces, timeout_s=5)

    info = await manager.request(
        trace_id="t_test", session_id="s_test", tool="cancel_visit_plan",
        args={}, risk="high", reason="测试审批",
    )
    waiter = asyncio.create_task(manager.wait(info["approval_id"]))
    await asyncio.sleep(0.05)
    assert await manager.decide(
        info["approval_id"], "approved", approver="manager-001", nonce=info["decision_nonce"]
    ) == "ok"
    assert await waiter == "approved"
    # 重放：同一令牌再来一次，只能得到 already_decided（不改状态、不重复执行）
    assert await manager.decide(info["approval_id"], "approved", nonce=info["decision_nonce"]) == "already_decided"
    row = await traces.get_approval(info["approval_id"])
    assert row["status"] == "approved" and row["approver"] == "manager-001"


async def test_approval_timeout(tmp_path):
    settings = make_settings(tmp_path, approval_timeout_s=1)
    traces = TraceStore(settings.sqlite_path)
    traces.init()
    manager = ApprovalManager(traces, timeout_s=1)

    info = await manager.request(
        trace_id="t_timeout", session_id="s_timeout", tool="cancel_visit_plan",
        args={}, risk="high", reason="超时测试",
    )
    assert await manager.wait(info["approval_id"]) == "timeout"
    row = await traces.get_approval(info["approval_id"])
    assert row["status"] == "timeout"
