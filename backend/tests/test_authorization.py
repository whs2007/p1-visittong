"""授权边界与治理台账（审计 §4.2 第 4/5 点、G3）。

覆盖：
- 销售不能读他人 Trace（404，不泄露存在性）；主管可以；
- `/traces` 列表按归属过滤；看板按归属收窄口径；
- 工具启停写治理台账，销售无权操作；
- 治理台账只有主管可读。
"""
from __future__ import annotations

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "authz.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=2,
    )
    base.update(overrides)
    return Settings(**base)


def _login(app, user_id: str) -> TestClient:
    client = TestClient(app)
    client.__enter__()
    response = client.post("/api/auth/login", json={"user_id": user_id, "password": "123456"})
    assert response.status_code == 200, response.text
    client.headers.update({"Authorization": "Bearer " + response.json()["token"]})
    return client


def _run_turn(client: TestClient) -> str:
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    with client.stream(
        "POST", "/api/chat/stream",
        json={"session_id": session_id, "message": "看看今天的待办"},
    ) as response:
        for _line in response.iter_lines():
            pass
    return session_id


@pytest.fixture()
def app(tmp_path):
    return create_app(make_settings(tmp_path))


def test_sales_cannot_read_others_trace(app):
    manager = _login(app, "manager-001")
    sales = _login(app, "sales-001")

    _run_turn(manager)
    manager_trace = manager.get("/api/traces").json()["traces"][0]["trace_id"]

    # 销售读主管的 Trace：404（而不是 403 —— 不泄露「这条存在」）
    response = sales.get(f"/api/traces/{manager_trace}")
    assert response.status_code == 404
    assert response.json()["code"] == "TRACE_NOT_FOUND"

    # 主管自己读得到
    assert manager.get(f"/api/traces/{manager_trace}").status_code == 200


def test_trace_list_is_scoped_by_owner(app):
    manager = _login(app, "manager-001")
    sales = _login(app, "sales-001")
    _run_turn(manager)
    _run_turn(sales)

    listed = sales.get("/api/traces?limit=100").json()["traces"]
    assert listed, "销售应能看到自己的 Trace"
    assert all(item["user_id"] == "sales-001" for item in listed)

    all_traces = manager.get("/api/traces?limit=100").json()["traces"]
    assert any(item["user_id"] == "manager-001" for item in all_traces)


def test_dashboard_scope_is_marked(app):
    sales = _login(app, "sales-001")
    own = sales.get("/api/dashboard/stats").json()
    assert own["summary"]["scope"] == "self"

    manager = _login(app, "manager-001")
    everything = manager.get("/api/dashboard/stats").json()
    assert everything["summary"]["scope"] == "all"


def test_tool_toggle_writes_governance_ledger(app):
    manager = _login(app, "manager-001")
    sales = _login(app, "sales-001")

    # 销售无权启停工具
    assert sales.patch("/api/tools/checkin", json={"enabled": False}).status_code == 403

    assert manager.patch("/api/tools/checkin", json={"enabled": False}).status_code == 200
    try:
        events = manager.get("/api/governance/events?action=tool.disable").json()["events"]
        assert events and events[0]["target_id"] == "checkin"
        assert events[0]["before"] == {"enabled": True}
        assert events[0]["after"] == {"enabled": False}
    finally:
        manager.patch("/api/tools/checkin", json={"enabled": True})


def test_governance_ledger_is_manager_only(app):
    sales = _login(app, "sales-001")
    response = sales.get("/api/governance/events")
    assert response.status_code == 403
    assert response.json()["code"] == "MANAGER_ONLY"


def test_manual_override_requires_manager_and_is_recorded(app):
    manager = _login(app, "manager-001")
    sales = _login(app, "sales-001")

    customer = app.state.ctx.db._rows(  # noqa: SLF001
        "SELECT customer_id FROM customers LIMIT 1"
    )[0]["customer_id"]
    plan = sales.post(
        "/api/plans",
        json={"customer_id": customer, "planned_at": "2026-12-18T10:00:00+08:00"},
    ).json()["plan"]

    # 销售自助取消被拒（必须走对话 + 审批）
    assert sales.post(f"/api/plans/{plan['plan_id']}/cancel").status_code == 403

    assert manager.post(f"/api/plans/{plan['plan_id']}/cancel").status_code == 200
    ledger = manager.get("/api/governance/manual-overrides").json()
    assert ledger["count"] >= 1
    assert any(item["target_id"] == plan["plan_id"] for item in ledger["items"])
    # 同一次豁免只算一条：v0.7 起 governance_events 与 audit_log 都会写，
    # 合并时若不去重，豁免数量会被系统性放大一倍（冒烟场景 s5 曾因此失败）。
    same = [row for row in ledger["items"] if row["target_id"] == plan["plan_id"]]
    assert len(same) == 1, f"同一次豁免被重复计数：{same}"
    assert same[0]["source"] == "governance_events", "结构化台账应优先于 audit_log"
    assert ledger["count"] == len(ledger["items"])


def test_logout_revokes_token(app):
    client = _login(app, "sales-001")
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    # 同一个 token 再请求：已撤销
    assert client.get("/api/me").status_code == 401
