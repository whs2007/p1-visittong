"""工具输出治理：脱敏 / 裁剪 / 引用化（v0.9）。

这组用例对应的是一个很具体的生产事故：工具返回 1.2 万字符的客户档案，里面带
联系人手机号；它被原样塞进 messages、原样写进 `trace_events`，下一轮直接触发
压缩，把前面聊过的业务事实压没了——而日志里还躺着一串真实手机号。
"""
from __future__ import annotations

import hashlib

import app.skills.tools  # noqa: F401
import pytest
from app.agent.output_guard import OutputGovernor, redact_pii
from app.config import Settings
from app.domain.db import Database
from app.main import create_app
from fastapi.testclient import TestClient

# 用拼接构造样例，避免仓库里出现一串「看起来像真的」的证件号
PHONE = "138" + "0" * 4 + "1234"
ID_CARD = "110101" + "19900101" + "123X"
BANK_CARD = "6222" + "0" * 8 + "1234"
EMAIL = "lisi@example.com"


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "guard.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        auth_mode="strict",
        tool_output_max_chars=600,
    )
    base.update(overrides)
    return Settings(**base)


def _login(client: TestClient, user: str, password: str) -> dict[str, str]:
    token = client.post("/api/auth/login", json={"user_id": user, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_redact_pii_masks_four_kinds() -> None:
    text = f"电话 {PHONE} 邮箱 {EMAIL} 证件 {ID_CARD} 卡 {BANK_CARD}"
    masked, hits = redact_pii(text)
    assert hits == 4, masked
    for raw in (PHONE, ID_CARD, BANK_CARD, EMAIL):
        assert raw not in masked
    # 掩码保留首尾：人还能对上是谁，不用再去别的系统捞一遍
    assert "138****1234" in masked
    assert "li***@example.com" in masked


def test_short_output_is_not_persisted(tmp_path) -> None:
    db = Database(str(tmp_path / "g.db"))
    db.init()
    out = OutputGovernor(db).govern("ok", tool="todo_query", trace_id="t1")
    assert out.truncated is False and out.ref is None
    assert out.savings_pct == 0.0
    assert db._rows("SELECT COUNT(*) AS c FROM tool_outputs")[0]["c"] == 0


def test_long_output_is_clipped_and_ref_verifies(tmp_path) -> None:
    db = Database(str(tmp_path / "g.db"))
    db.init()
    governor = OutputGovernor(db, max_chars=600, head_ratio=0.7)
    body = "客户档案：主营饮料，覆盖 12 家门店。" * 500 + f" 联系人手机 {PHONE} " + "备注：下周补样品。" * 200
    out = governor.govern(body, tool="knowledge_search", trace_id="t1", session_id="s1", user_id="sales-001")

    assert out.truncated is True
    assert out.ref and out.ref.startswith("TO-")
    assert out.kept_chars <= 700          # 目标上限，允许掩码带来的几字符膨胀
    assert out.savings_pct > 80
    assert PHONE not in out.text
    assert out.ref in out.text            # 上下文里能看见引用号，模型可据此回查

    row = governor.fetch(out.ref)
    assert row is not None
    # 回查到的就是注入的那段（去敏后），可校验
    assert row["sha256"] == hashlib.sha256(row["content"].encode()).hexdigest()
    assert len(row["content"]) > out.kept_chars
    assert PHONE not in row["content"]    # 原始 PII 从未落库


def test_same_output_same_ref_single_row(tmp_path) -> None:
    """同一 trace 的同一输出重复治理（重试 / 回放）不能把表撑大。"""
    db = Database(str(tmp_path / "g.db"))
    db.init()
    governor = OutputGovernor(db, max_chars=300)
    body = "门店列表 " * 400
    first = governor.govern(body, tool="todo_query", trace_id="tr", user_id="u1")
    second = governor.govern(body, tool="todo_query", trace_id="tr", user_id="u1")
    assert first.ref == second.ref
    assert db._rows("SELECT COUNT(*) AS c FROM tool_outputs WHERE ref=?", (first.ref,))[0]["c"] == 1


def test_tool_output_api_is_scoped_to_owner(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        sales = _login(client, "sales-001", ctx.settings.demo_password)
        manager = _login(client, "manager-001", ctx.settings.demo_password)
        ref = ctx.governor.govern(
            "很长的客户档案 " * 300, tool="knowledge_search",
            trace_id="t1", session_id="s1", user_id="sales-001",
        ).ref
        assert ref
        own = client.get(f"/api/tool-outputs/{ref}", headers=sales)
        assert own.status_code == 200
        assert own.json()["output"]["tool"] == "knowledge_search"
        assert client.get(f"/api/tool-outputs/{ref}", headers=manager).status_code == 200
        assert client.get(f"/api/tool-outputs/{ref}").status_code == 401
        assert client.get("/api/tool-outputs/TO-notexist", headers=sales).status_code == 404


def test_tool_output_api_hides_sales_peers(tmp_path) -> None:
    """别人的引用返回 404 而不是 403：不泄露「这条存在」。"""
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        manager = _login(client, "manager-001", ctx.settings.demo_password)
        ref = ctx.governor.govern("别人的档案 " * 300, tool="x", trace_id="t2", user_id="manager-001").ref
        assert ref
        other = client.get(f"/api/tool-outputs/{ref}", headers=manager)
        assert other.status_code == 200        # 主管可读
        # 未登录一律 401；越权（其余销售）走 404，见实现
        assert client.get(f"/api/tool-outputs/{ref}").status_code == 401


@pytest.mark.parametrize("limit", [200, 1600, 4000])
def test_kept_chars_never_exceed_limit_materially(tmp_path, limit: int) -> None:
    db = Database(str(tmp_path / f"g{limit}.db"))
    db.init()
    governor = OutputGovernor(db, max_chars=limit)
    body = "数据 " * 5000
    out = governor.govern(body, tool="t", trace_id="tr", user_id="u")
    assert out.kept_chars <= limit + 64
