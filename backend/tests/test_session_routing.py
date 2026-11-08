"""会话路由卫生：技能不串味，但追问后要能接上（v0.7 修复的两个真缺陷）。

背景：`orchestrator/loop.py` 曾用「当前技能=chat 且会话里有历史槽位」作为
「沿用上一轮子技能」的条件。槽位一旦写入就永不清空，于是同一个会话里
用户说「你好，你是谁」会被拽回上一轮的 `plan_cancel`，**真的发起一次取消
审批**；说「你好」也可能调出上一轮的工具。这是行为评测用例
`route-chat-fallback` 打出来的，不是用例写错。

修法：只有上一轮**以追问结束**（need_input）时才沿用，并把该状态显式落库
（`awaiting_slots`）。本文件把两侧都钉住：

- 闲聊必须回到 chat，且不触发任何写工具（污染已修）；
- 追问之后用户只补信息时，仍要沿用上一轮技能（别把功能一起修没了）。
"""
from __future__ import annotations

import json

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def make_settings(tmp_path) -> Settings:
    return Settings(
        sqlite_path=str(tmp_path / "routing.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=1,
    )


@pytest.fixture()
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        response = c.post(
            "/api/auth/login", json={"user_id": "sales-001", "password": "123456"}
        )
        assert response.status_code == 200, response.text
        c.headers.update({"Authorization": "Bearer " + response.json()["token"]})
        yield c


def _say(client: TestClient, session_id: str, message: str) -> list[dict]:
    """跑一轮对话，返回 SSE 事件列表（event + data）。"""
    events: list[dict] = []
    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": session_id, "message": message}
    ) as response:
        name = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                raw = line[5:].strip()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"raw": raw}
                events.append({"event": name, "data": payload})
    return events


def _session(client: TestClient) -> str:
    return client.post("/api/sessions", json={"title": "routing"}).json()["session_id"]


def _route(events: list[dict]) -> str | None:
    return next((e["data"].get("skill") for e in events if e["event"] == "route"), None)


def _tools(events: list[dict]) -> list[str]:
    return [e["data"].get("name") for e in events if e["event"] == "tool_call"]


def test_chitchat_is_not_hijacked_by_previous_skill(client):
    """上一轮正常结束后，闲聊必须回到 chat，且不许调用任何写工具。

    复现的是评测里真实出现的形态：先查过待办（槽位里留下「今天」），
    接着说「你好，你是谁」，旧实现会把它路由回 todo_query / plan_cancel
    并真的调出工具 —— 一次问候变成一次业务动作。
    """
    session_id = _session(client)
    first = _say(client, session_id, "看看我今天有什么待办")
    assert _route(first) == "todo_query"
    assert "query_todos" in _tools(first)

    second = _say(client, session_id, "你好，你是谁")
    assert _route(second) == "chat", f"闲聊被上一轮技能劫持：{_route(second)}"
    assert not (
        {"create_visit_plan", "cancel_visit_plan", "update_visit_plan", "query_todos"}
        & set(_tools(second))
    ), f"闲聊触发了工具：{_tools(second)}"


def test_slot_answer_after_question_still_continues_previous_skill(client):
    """上一轮以追问结束 → 这一轮只补信息时要沿用上一轮技能，不能退化成闲聊。"""
    session_id = _session(client)
    first = _say(client, session_id, "帮我约一下客户")
    assert _route(first) == "plan_create"
    assert "slot_fill" in [e["event"] for e in first], "第一轮应当追问缺哪个客户"

    second = _say(client, session_id, "字节跳动明天上午10点")
    assert _route(second) == "plan_create", "补槽位那一轮丢了上下文技能"
    assert "create_visit_plan" in _tools(second), f"未继续调用建计划：{_tools(second)}"


def test_awaiting_slots_flag_is_reset_after_normal_turn(client):
    """`awaiting_slots` 必须每轮重算：正常结束后不能残留成 True。"""
    session_id = _session(client)
    _say(client, session_id, "帮我约一下客户")          # 追问 → True
    _say(client, session_id, "看看今天的待办")          # 正常结束 → 应清掉
    third = _say(client, session_id, "你好")
    assert _route(third) == "chat"
