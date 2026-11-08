"""审批一次性令牌：防重放、防错单、防并发双批（v0.9）。

简历上那句「使用一次性 Token 防止重放」必须有对应的实现和用例，否则面试官问
「那第二次提交会怎样」就只能现场编。这里的语义是：

- 令牌由服务端生成（`secrets.token_urlsafe`，不可预测），与审批单绑定；
- 决策时必须带上**这张单的**令牌，且只能用一次（`nonce_used_at` 由同一条
  UPDATE 写入，不靠先查后写）；
- 重放 → 409、令牌不匹配 → 403、没带令牌 → 400，三种失败原因分开报；
- 并发的两个「批准」只有一个成功：CAS 条件是（status=pending ∧ nonce 匹配
  ∧ nonce_used_at IS NULL），SQLite 写锁保证串行。
"""
from __future__ import annotations

import asyncio

import app.skills.tools  # noqa: F401
from app.approvals import ApprovalManager
from app.config import Settings
from app.main import create_app
from app.trace.store import TraceStore
from fastapi.testclient import TestClient


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "approval.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        auth_mode="strict",
        approval_timeout_s=5,
    )
    base.update(overrides)
    return Settings(**base)


def _login(client: TestClient, user: str, password: str) -> dict[str, str]:
    token = client.post("/api/auth/login", json={"user_id": user, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _manager(tmp_path) -> tuple[TraceStore, ApprovalManager]:
    store = TraceStore(str(tmp_path / "approval.db"))
    store.init()
    return store, ApprovalManager(store, timeout_s=5)


async def _new_approval(store: TraceStore, *, tool: str = "cancel_visit_plan") -> dict:
    return await store.create_approval(
        trace_id="t_probe", session_id="s_probe", tool=tool,
        args={"plan_id": "p1"}, risk="high", reason="high_risk_write",
        timeout_s=5, requester="sales-001",
    )


def test_nonce_is_random_and_bound(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    first = asyncio.run(_new_approval(store))
    second = asyncio.run(_new_approval(store))
    assert len(first["decision_nonce"]) >= 20
    assert first["decision_nonce"] != second["decision_nonce"]   # 不是自增/时间戳


def test_wrong_or_missing_nonce_is_rejected(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    row = asyncio.run(_new_approval(store))
    assert asyncio.run(manager.decide(row["approval_id"], "approved")) == "token_missing"
    assert asyncio.run(manager.decide(row["approval_id"], "approved", nonce="wrong-token")) == "token_invalid"
    # 失败尝试不能把单子推进到终态——否则「错令牌」等于「取消了一次审批」
    current = asyncio.run(store.get_approval(row["approval_id"]))
    assert current["status"] == "pending"
    assert asyncio.run(manager.decide(row["approval_id"], "approved", nonce=row["decision_nonce"])) == "ok"
    assert asyncio.run(manager.decide(row["approval_id"], "approved", nonce=row["decision_nonce"])) == "already_decided"


def test_replay_five_times_only_first_succeeds(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    row = asyncio.run(_new_approval(store))
    outcome = [asyncio.run(manager.decide(row["approval_id"], "approved", nonce=row["decision_nonce"])) for _ in range(5)]
    assert outcome[0] == "ok"
    assert outcome[1:] == ["already_decided"] * 4
    decided = asyncio.run(store.get_approval(row["approval_id"]))
    assert decided["status"] == "approved"
    assert decided["nonce_used_at"]           # 令牌生命周期有独立时间戳


async def _race(manager: ApprovalManager, approval_id: str, nonce: str) -> list[str]:
    return await asyncio.gather(*[
        manager.decide(approval_id, "approved", nonce=nonce) for _ in range(6)
    ])


def test_concurrent_approve_yields_exactly_one_winner(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    row = asyncio.run(_new_approval(store))
    results = asyncio.run(_race(manager, row["approval_id"], row["decision_nonce"]))
    assert results.count("ok") == 1


def test_api_rejects_missing_wrong_and_replayed_nonce(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        manager = _login(client, "manager-001", ctx.settings.demo_password)
        sales = _login(client, "sales-001", ctx.settings.demo_password)

        pending = client.get("/api/approvals", headers=manager).json()["approvals"]
        assert pending == []          # 种子数据没有待审单
        row = asyncio.run(_new_approval(ctx.traces))

        listed = client.get("/api/approvals", headers=manager).json()["approvals"]
        assert listed and listed[0]["approval_id"] == row["approval_id"]
        assert listed[0]["decision_nonce"] == row["decision_nonce"]

        url = f"/api/approvals/{row['approval_id']}"
        assert client.post(url, json={"decision": "approved"}, headers=manager).status_code == 400
        assert client.post(url, json={"decision": "approved", "nonce": "bad"}, headers=manager).status_code == 403
        # 销售即使拿到令牌也不能决策（角色校验先于令牌校验）
        assert client.post(url, json={"decision": "approved", "nonce": row["decision_nonce"]},
                           headers=sales).status_code == 403
        ok = client.post(url, json={"decision": "approved", "nonce": row["decision_nonce"]}, headers=manager)
        assert ok.status_code == 200
        replay = client.post(url, json={"decision": "approved", "nonce": row["decision_nonce"]}, headers=manager)
        assert replay.status_code == 409
        assert replay.json()["code"] == "APPROVAL_ALREADY_DECIDED"
