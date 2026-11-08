"""Phase 8：可观测增强——指标 JSON/Prometheus、Trace 筛选。"""
from __future__ import annotations

import json

import app.skills.tools  # noqa: F401
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


def _login(app, user_id="sales-001"):
    client = TestClient(app)
    client.__enter__()
    login = client.post("/api/auth/login", json={"user_id": user_id, "password": "123456"})
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


@pytest.fixture()
def client(tmp_path):
    c = _login(create_app(make_settings(tmp_path)))
    yield c
    c.__exit__(None, None, None)


@pytest.fixture()
def manager_client(tmp_path):
    c = _login(create_app(make_settings(tmp_path)), "manager-001")
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


def test_metrics_json_and_prometheus(client, manager_client) -> None:
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    with client.stream("POST", "/api/chat/stream", json={"session_id": sid, "message": "查一下我今天的拜访待办"}) as r:
        parse_sse(r)

    assert client.get("/api/metrics").status_code == 403
    metrics = manager_client.get("/api/metrics").json()
    assert metrics["traces"]["total"] >= 1
    assert metrics["tools"]["write"] >= 6
    assert metrics["knowledge_docs"] >= 1
    assert metrics["storage"]["db"] == "sqlite"
    assert metrics["tools"]["enabled"] <= metrics["tools"]["total"]

    text = manager_client.get("/api/metrics/prometheus").text
    assert "visittong_traces_total" in text
    assert "visittong_tasks_interrupted" in text
    assert "visittong_knowledge_docs" in text
    assert manager_client.get("/api/metrics/prometheus").headers["content-type"].startswith("text/plain")


def test_trace_list_filter(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    with client.stream("POST", "/api/chat/stream", json={"session_id": sid, "message": "查一下我今天的拜访待办"}) as r:
        parse_sse(r)
    traces = client.get("/api/traces", params={"skill": "todo_query"}).json()["traces"]
    assert traces and all(row["skill"] == "todo_query" for row in traces)
    done = client.get("/api/traces", params={"status": "success"}).json()["traces"]
    assert done and all(row["status"] == "success" for row in done)
    assert client.get("/api/traces", params={"status": "failed"}).json()["traces"] == []


def test_audit_search_requires_manager(client, manager_client) -> None:
    assert client.get("/api/audit").status_code == 403
    assert manager_client.get("/api/audit", params={"limit": 10}).status_code == 200

def test_request_id_header_is_echoed_and_generated(client) -> None:
    """`X-Request-Id` 要能「传进来就沿用、没传就生成」——用户报障时才对得齐日志行。"""
    echoed = client.get("/api/health", headers={"X-Request-Id": "abc123"})
    assert echoed.status_code == 200
    assert echoed.headers["X-Request-Id"] == "abc123"
    assert echoed.headers["X-Elapsed-Ms"].isdigit()

    generated = client.get("/api/health")
    rid = generated.headers["X-Request-Id"]
    assert rid and rid != "abc123" and len(rid) >= 8


def test_prometheus_exposition_declares_help_and_type(manager_client) -> None:
    """Prometheus 文本格式要求每个指标都有 HELP/TYPE；缺了采集端只会当成 untyped。"""
    text = manager_client.get("/api/metrics/prometheus").text
    names = {
        line.split()[0].split("{")[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }
    assert names, "指标为空"
    for name in names:
        assert f"# HELP {name} " in text, f"{name} 缺少 HELP"
        assert f"# TYPE {name} " in text, f"{name} 缺少 TYPE"
    assert text.endswith("\n")


def test_cors_is_deny_by_default(client) -> None:
    """未配置 ALLOWED_ORIGINS 时，不让任何跨域来源拿到 CORS 响应头。

    旧默认是 `"*"` 且 `allow_credentials=True`：等于任意站点都能带凭据调接口。
    前端与后端同源，本就不需要 CORS；分离开发时显式配白名单。
    """
    response = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_cors_whitelist_allows_configured_origin(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, allowed_origins="http://127.0.0.1:5173"))
    with TestClient(app) as c:
        response = c.get("/api/health", headers={"Origin": "http://127.0.0.1:5173"})
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
