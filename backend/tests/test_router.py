"""Phase 3：总控 Skill 的 LLM 路由 + 规则 fallback。

覆盖：
- hybrid：规则高置信度（>=0.9）直通，不调用 LLM；
- 规则不确定时使用 LLM.classify 的 skill/confidence；
- LLM 返回 None/低置信度/异常时回退规则（chat）；
- router_mode=rules 完全走规则；
- 接口集成：路由事件 source=llm，且后续仍能创建计划。
"""
from __future__ import annotations

import asyncio
import json

import app.skills.tools  # noqa: F401
import pytest
from app.config import Settings
from app.domain.db import Database
from app.llm.base import BaseLLM, LLMChunk
from app.llm.mock import MockLLM
from app.main import create_app
from app.orchestrator.router import route_text_hybrid
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


def make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "router.db"))
    db.init()
    db.seed_demo()
    return db


class StubLLM(MockLLM):
    def __init__(self, result=None, fail=False):
        self.result = result
        self.fail = fail
        self.calls = 0

    async def classify(self, text, labels):
        self.calls += 1
        if self.fail:
            raise RuntimeError("router down")
        return self.result


def test_hybrid_uses_llm_when_rule_is_uncertain(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    llm = MockLLM()
    result = asyncio.run(
        route_text_hybrid(db, "下周三上午去字节跳动聊方案", llm, settings)
    )
    assert result.skill == "plan_create"
    assert result.source == "llm"
    assert result.slots.get("customer") == "字节跳动"
    assert result.slots.get("planned_at")


def test_high_confidence_rule_skips_llm(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    llm = StubLLM(result={"skill": "chat", "confidence": 0.99})
    result = asyncio.run(route_text_hybrid(db, "取消这个计划", llm, settings))
    assert result.skill == "plan_cancel"
    assert result.source == "text"
    assert llm.calls == 0


def test_llm_none_falls_back_to_rules(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    llm = StubLLM(result=None)
    result = asyncio.run(route_text_hybrid(db, "下周三上午去字节跳动聊方案", llm, settings))
    assert result.skill == "chat"
    assert result.source == "text"


def test_llm_exception_falls_back_to_rules(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    llm = StubLLM(fail=True)
    result = asyncio.run(route_text_hybrid(db, "你好", llm, settings))
    assert result.skill == "chat"


def test_rules_mode_never_calls_llm(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path, router_mode="rules")
    llm = StubLLM(result={"skill": "plan_create", "confidence": 0.9})
    result = asyncio.run(route_text_hybrid(db, "下周三上午去字节跳动聊方案", llm, settings))
    assert result.skill == "chat"
    assert llm.calls == 0


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


def parse_sse(response):
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line[5:].strip())))
    return events


def test_chat_route_event_uses_llm_and_still_creates_plan(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "下周三上午去字节跳动聊方案"}
    ) as response:
        events = parse_sse(response)
    routes = [data for name, data in events if name == "route"]
    assert routes and routes[0]["skill"] == "plan_create" and routes[0]["source"] == "llm"
    # 补门店后仍能落计划
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": sid, "message": "北京中关村"}
    ) as response:
        events = parse_sse(response)
    cards = [data for name, data in events if name == "card" and data.get("card_name") == "visit_plan"]
    assert cards