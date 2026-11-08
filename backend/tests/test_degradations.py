"""降级台账：从「悄悄降级」到「看得见 + 可以选择起不来」。

覆盖：
- 知识后端降级（milvus 不可用 → local）会出现在 /api/metrics 的 degradations；
- Prometheus 暴露 visittong_degraded / visittong_degraded_critical；
- STRICT_DEGRADE=1 且存在 critical 降级时，启动直接失败（fail-closed）。
"""
from __future__ import annotations

import pytest
from app.config import Settings
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


def login(client: TestClient, user_id: str = "manager-001") -> TestClient:
    # 密码从 settings 取，不在测试里硬编码（改了默认值也不该让用例假红）
    response = client.post(
        "/api/auth/login",
        json={"user_id": user_id, "password": client.app.state.settings.demo_password},
    )
    token = response.json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def manager_client(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    client.__enter__()
    yield login(client)
    client.__exit__(None, None, None)


def test_knowledge_degradation_is_reported(tmp_path):
    settings = make_settings(tmp_path, knowledge_backend="milvus", milvus_uri="http://127.0.0.1:19530")
    with TestClient(create_app(settings)) as client:
        login(client)
        metrics = client.get("/api/metrics").json()
        assert metrics["degraded"] is True
        components = {item["component"] for item in metrics["degradations"]}
        assert "knowledge" in components
        entry = next(item for item in metrics["degradations"] if item["component"] == "knowledge")
        # 原因必须能说清「为什么退了」，不能只有一个布尔
        assert entry["reason"]
        assert entry["impact"]


def test_redis_fallback_is_critical_when_configured(manager_client):
    metrics = manager_client.get("/api/metrics").json()
    entry = next(item for item in metrics["degradations"] if item["component"] == "redis")
    assert entry["severity"] == "critical"
    assert "多实例" in entry["impact"]
    assert metrics["degraded_critical"] is True


def test_prometheus_exposes_degraded_gauges(manager_client):
    text = manager_client.get("/api/metrics/prometheus").text
    assert "# TYPE visittong_degraded gauge" in text
    assert "# TYPE visittong_degraded_critical gauge" in text
    assert "visittong_degraded 1" in text


def test_strict_degrade_refuses_to_start(tmp_path):
    settings = make_settings(tmp_path, strict_degrade=True)
    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(create_app(settings)):
            pass
    assert "STRICT_DEGRADE" in str(excinfo.value)
