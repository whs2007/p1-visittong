"""Phase 4：治理层——工具读写 scope / 角色权限 / 操作审计 / 请求幂等 / 原生表单降级。"""
from __future__ import annotations

import json
import sqlite3

import app.skills.tools  # noqa: F401
import pytest
from app.config import Settings
from app.domain.db import Database
from app.main import create_app
from app.policy.engine import PolicyEngine
from app.skills.registry import ToolRegistry, ToolSpec
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


def _login(app, user_id: str = "sales-001") -> TestClient:
    client = TestClient(app)
    client.__enter__()
    login = client.post("/api/auth/login", json={"user_id": user_id, "password": "123456"})
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


@pytest.fixture()
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    c = _login(app)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture()
def manager_client(tmp_path):
    app = create_app(make_settings(tmp_path))
    c = _login(app, "manager-001")
    yield c
    c.__exit__(None, None, None)


def parse_sse(response):
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line[5:].strip())))
    return events


def chat(client, sid: str, message: str):
    with client.stream("POST", "/api/chat/stream", json={"session_id": sid, "message": message}) as r:
        return parse_sse(r)


def test_catalog_exposes_read_write_scope() -> None:
    from app.skills.registry import registry

    create = next(t for t in registry.all() if t.name == "create_visit_plan")
    assert create.scope == "write"
    query = next(t for t in registry.all() if t.name == "query_todos")
    assert query.scope == "read"
    catalog = {t["name"]: t for t in registry.catalog()}
    assert catalog["checkin"]["scope"] == "write"
    assert catalog["query_plans"]["scope"] == "read"


def test_policy_denies_role_without_permission(tmp_path) -> None:
    reg = ToolRegistry()

    async def handler(args, ctx):  # pragma: no cover 只用于构造 spec
        return None

    reg._tools["manager_only"] = ToolSpec(  # noqa: SLF001
        name="manager_only", label="主管专用", description="", parameters={},
        handler=handler, roles=("manager", "admin"), scope="write",
    )
    policy = PolicyEngine(reg, make_settings(tmp_path))
    denied = policy.evaluate(user_id="sales-001", role="sales", name="manager_only", args={})
    assert denied.action == "deny" and denied.rule == "role_denied"
    allowed = policy.evaluate(user_id="manager-001", role="manager", name="manager_only", args={})
    assert allowed.action == "allow"
    # 未传 role 时保持旧行为（系统内部调用/单测）
    # v0.7 fail-closed：role 缺失不再等于「跳过角色限制」，而是直接拒绝
    assert policy.evaluate(user_id="sales-001", name="manager_only", args={}).action == "deny"


def test_write_tool_is_audited_and_manager_can_read(client, manager_client) -> None:
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    chat(client, sid, "查一下我今天的拜访待办")  # read tool，不写审计
    chat(client, sid, "我到了，签到")          # write tool，写审计
    audit = manager_client.get("/api/audit").json()["audit"]
    actions = [row["action"] for row in audit]
    assert any(a.startswith("tool.checkin.") for a in actions), actions
    assert all(not a.startswith("tool.query_todos.") for a in actions), actions
    assert client.get("/api/audit").status_code == 403


def test_approval_decision_is_audited(client, manager_client, tmp_path) -> None:
    con = sqlite3.connect(str(tmp_path / "test.db"))
    con.execute(
        "INSERT INTO approvals"
        " (approval_id,trace_id,session_id,tool,args,risk,reason,status,requester,created_at,expires_at,decision_nonce)"
        " VALUES ('apr_test','t1','s1','cancel_visit_plan','{}','high','高危','pending','sales-001',"
        "datetime('now'),datetime('now','+5 minutes'),'nonce-for-test')"
    )
    con.commit()
    con.close()
    res = manager_client.post(
        "/api/approvals/apr_test",
        json={"decision": "approved", "reason": "ok", "nonce": "nonce-for-test"},
    )
    assert res.status_code == 200, res.text
    audit = manager_client.get("/api/audit").json()["audit"]
    assert any(row["action"] == "approval.approved" and row["target_id"] == "apr_test" for row in audit)


def test_chat_idempotency_key_blocks_duplicate(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    headers = {"Idempotency-Key": "req-123"}
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "查一下我今天的拜访待办"}, headers=headers
    ) as response:
        assert response.status_code == 200
        list(response.iter_lines())
    duplicate = client.post(
        "/api/chat/stream", json={"session_id": sid, "message": "查一下我今天的拜访待办"}, headers=headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "DUPLICATE_REQUEST"


def test_manual_plan_endpoints_fsm_and_audit(client, manager_client) -> None:
    created = client.post(
        "/api/plans",
        json={"customer_id": "c_byte", "planned_at": "2026-11-06T10:00:00+08:00", "topic": "手工创建"},
    )
    assert created.status_code == 200, created.text
    plan_id = created.json()["plan"]["plan_id"]
    assert created.json()["plan"]["status"] == "planned"

    updated = client.patch(f"/api/plans/{plan_id}", json={"topic": "手工改主题"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["plan"]["topic"] == "手工改主题"

    # 人工降级通道是显式豁免：销售不能自助取消（否则等于绕过业绩监督）
    denied = client.post(f"/api/plans/{plan_id}/cancel")
    assert denied.status_code == 403
    assert denied.json()["code"] == "MANAGER_ONLY"

    cancelled = manager_client.post(f"/api/plans/{plan_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["plan"]["status"] == "cancelled"
    assert cancelled.json()["governance"]["approval"] == "waived"

    # 终态不可再改
    again = client.patch(f"/api/plans/{plan_id}", json={"topic": "x"})
    assert again.status_code == 409
    assert again.json()["code"] == "PLAN_STATE_INVALID"

    audit = manager_client.get("/api/audit").json()["audit"]
    actions = [row["action"] for row in audit]
    assert "plan.create.manual" in actions
    assert "plan.update.manual" in actions
    assert "plan.cancel.manual" in actions

    # 豁免标记真的写进审计，不是只在响应里回显
    assert any(
        row["action"] == "plan.cancel.manual"
        and "manual_override" in (row.get("after") or "")
        and "explicit_human_action_by_manager" in (row.get("after") or "")
        for row in audit
    )

    # 台账本身也是主管专属：销售不该读到「谁被豁免过」
    ledger_denied = client.get("/api/governance/manual-overrides")
    assert ledger_denied.status_code == 403
    assert ledger_denied.json()["code"] == "MANAGER_ONLY"

    # 豁免必须进台账：可举证、可统计，而不是只在文档里声明
    ledger = manager_client.get("/api/governance/manual-overrides")
    assert ledger.status_code == 200
    body = ledger.json()
    assert body["count"] >= 1
    assert any(row["target_id"] == plan_id for row in body["items"])
    assert body["items"][0]["waiver_reason"] == "explicit_human_action_by_manager"