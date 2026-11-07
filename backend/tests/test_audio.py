"""Phase 5：录音链路——ASR 适配、分片聚合、纪要、审计。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.skills.tools  # noqa: F401
from app.adapters import asr
from app.agent.minutes import generate_minutes, render_markdown
from app.config import Settings
from app.main import create_app


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "test.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=2,
        audio_dir=str(tmp_path / "audio"),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        login = test_client.post("/api/auth/login", json={"user_id": "sales-001", "password": "123456"})
        assert login.status_code == 200, login.text
        test_client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield test_client


@pytest.fixture()
def manager_client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        login = test_client.post("/api/auth/login", json={"user_id": "manager-001", "password": "123456"})
        assert login.status_code == 200, login.text
        test_client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield test_client


def test_asr_mock_and_fallback() -> None:
    result = asr.transcribe(None, backend="mock")
    assert result["backend"] == "mock" and result["text"]
    fallback = asr.transcribe(None, backend="faster_whisper")
    # 未安装/无文件都必须回退 mock，且给出原因，不能抛异常
    assert fallback["backend"] == "mock"
    assert fallback["fallback_reason"]
    assert asr.transcribe("x.wav", backend="command")["backend"] == "mock"


def test_minutes_structure() -> None:
    minutes = generate_minutes("客户对方案感兴趣。下周提供试用账号。约下周三演示。")
    assert minutes["points"]
    assert "下周提供试用账号" in minutes["next_action"]
    markdown = render_markdown(minutes)
    assert "## 拜访纪要" in markdown and "**下一步**" in markdown


def test_audio_chunk_aggregation_and_complete(client, manager_client) -> None:
    base = "/api/audio/up_test"
    first = client.post(
        f"{base}/chunk", data={"seq": "1"},
        files={"file": ("b.bin", b"second-part", "application/octet-stream")},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"{base}/chunk", data={"seq": "0"},
        files={"file": ("a.bin", b"first-part", "application/octet-stream")},
    )
    assert second.status_code == 200 and second.json()["chunks"] == 2

    completed = client.post(f"{base}/complete", json={"save_log": True})
    assert completed.status_code == 200, completed.text
    data = completed.json()
    assert data["chunks"] == 2
    assert data["transcript"]
    assert data["minutes"]["points"]
    assert data["log"] and data["log"]["log_id"]

    audit = manager_client.get("/api/audit").json()["audit"]
    assert any(row["action"] == "audio.transcribe" and row["target_id"] == "up_test" for row in audit)


def test_complete_without_chunks_is_404(client) -> None:
    assert client.post("/api/audio/missing/complete").status_code == 404


def test_transcribe_tool_returns_minutes(tmp_path) -> None:
    """通过注册表直接调用工具，验证 ASR + 纪要数据契约。"""
    import asyncio

    from app.skills.registry import ToolContext, registry
    from app.sessions.store import SessionState
    from app.sessions.tree import SessionTree
    from app.domain.db import Database

    settings = make_settings(tmp_path)
    db = Database(settings.sqlite_path)
    db.init()
    tree = SessionTree(db)
    state = SessionState(settings)
    asyncio.run(state.init())
    ctx = ToolContext(
        settings=settings, session_id="s", trace_id="t", user_id="sales-001",
        db=db, tree=tree, state=state,
    )
    result = asyncio.run(registry.get("transcribe_audio").handler({}, ctx))
    assert result.status == "ok"
    assert result.data["transcript"]
    assert result.data["minutes"]["points"]