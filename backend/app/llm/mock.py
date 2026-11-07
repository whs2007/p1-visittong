"""Mock LLM：无 API Key 也能演示 5 条脚本（待办/创建/修改/取消/签到/日志）。

编排层通过 system 消息注入 `[当前子 Skill: xxx] [槽位: {...}]`，Mock 据此决定调哪个工具。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

from app.llm.base import BaseLLM, LLMChunk

SKILL_RE = re.compile(r"\[当前子 Skill: (\w+)\]")
SLOT_RE = re.compile(r"\[槽位: (\{.*?\})\]")


def _last_user(messages: list[dict[str, Any]]) -> str:
    return next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")


def _hint(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    text = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
    skill = "chat"
    slots: dict[str, Any] = {}
    m = SKILL_RE.search(text)
    if m:
        skill = m.group(1)
    s = SLOT_RE.search(text)
    if s:
        try:
            slots = json.loads(s.group(1))
        except json.JSONDecodeError:
            slots = {}
    return skill, slots


class MockLLM(BaseLLM):
    name = "mock"

    async def classify(self, text: str, labels: list[str]) -> dict[str, Any] | None:
        """Mock 的"LLM 路由"：用关键词模拟模型分类，供 hybrid 路由链路测试/演示。"""
        await asyncio.sleep(0.05)
        rules = (
            ("plan_cancel", r"取消|不去了|别去了"),
            ("checkout", r"签退|离店|结束拜访"),
            ("reminder", r"提醒|通知|告知"),
            ("checkin", r"签到|到店|到了"),
            ("visit_log", r"日志|记一下|复盘"),
            ("plan_update", r"推迟|调整|换个时间|改到"),
            ("plan_create", r"约|拜访|计划|见客户|排期|聊|方案|见面"),
            ("todo_query", r"待办|安排|行程"),
        )
        import re as _re

        for skill, pattern in rules:
            if skill in labels and _re.search(pattern, text):
                return {"skill": skill, "confidence": 0.8, "reason": f"mock:{skill}"}
        return {"skill": "chat", "confidence": 0.2, "reason": "mock:chat"}

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        await asyncio.sleep(0.2)
        available = {t["function"]["name"] for t in (tools or []) if "function" in t}
        skill, slots = _hint(messages)
        trailing: list[dict[str, Any]] = []
        for message in reversed(messages):
            if message.get("role") == "user":
                break
            if message.get("role") == "tool":
                trailing.append(message)
        trailing.reverse()

        if trailing:
            payload = self._payload(trailing[-1])
            text = self._final(payload)
            async for chunk in self._emit_text(text):
                yield chunk
            return

        call = self._tool_for(skill, slots, _last_user(messages), available)
        if call:
            async for chunk in self._emit_tool(call[0], call[1]):
                yield chunk
        else:
            async for chunk in self._emit_text(self._chat(skill, _last_user(messages))):
                yield chunk

    # ---------------------------------------------------------------- #
    def _tool_for(
        self, skill: str, slots: dict[str, Any], text: str, available: set[str]
    ) -> tuple[str, dict[str, Any]] | None:
        if skill == "todo_query" and "query_todos" in available:
            return "query_todos", {"day": "today"}
        if skill == "plan_create" and "create_visit_plan" in available:
            args = {k: slots[k] for k in ("customer", "planned_at", "store", "topic") if slots.get(k)}
            return "create_visit_plan", args
        if skill == "plan_update" and "update_visit_plan" in available:
            args = {k: slots[k] for k in ("planned_at", "topic", "store", "plan_id") if slots.get(k)}
            return "update_visit_plan", args
        if skill == "plan_cancel" and "cancel_visit_plan" in available:
            return "cancel_visit_plan", {"plan_id": slots.get("plan_id")} if slots.get("plan_id") else {}
        if skill == "checkin" and "checkin" in available:
            return "checkin", {"distance_m": int(slots.get("distance_m") or 320)}
        if skill == "checkout" and "checkout" in available:
            return "checkout", {}
        if skill == "reminder" and "send_notification" in available:
            target = slots.get("target") or slots.get("customer") or "客户"
            return "send_notification", {"target": target, "message": text[:200]}
        if skill == "visit_log" and "save_visit_log" in available:
            return "save_visit_log", {"summary": text[:200]}
        return None

    @staticmethod
    def _payload(message: dict[str, Any]) -> dict[str, Any]:
        try:
            data = json.loads(message.get("content") or "{}")
            return data if isinstance(data, dict) else {"status": "ok", "output": str(data)}
        except json.JSONDecodeError:
            return {"status": "ok", "output": message.get("content") or ""}

    @staticmethod
    def _final(payload: dict[str, Any]) -> str:
        status = payload.get("status") or "ok"
        output = payload.get("output") or ""
        data = payload.get("data") or {}
        if status == "need_input":
            return data.get("question") or output or "还需要补充信息。"
        if status == "rejected":
            return "已取消该操作，计划保持不变。"
        if status == "timeout":
            return "审批等待超时，操作已自动取消。"
        if status == "denied":
            return f"这个操作被安全策略拦截了：{output}"
        return output or "已完成。"

    @staticmethod
    def _chat(skill: str, text: str) -> str:
        return (
            f"收到：「{text[:30]}」。我可以帮你查待办、创建/修改/取消拜访计划、到店签到、整理拜访日志。"
            "你可以直接说：「查一下我今天的拜访待办」。"
        )

    # ---------------------------------------------------------------- #
    async def _emit_text(self, text: str) -> AsyncIterator[LLMChunk]:
        for i in range(0, len(text), 8):
            yield LLMChunk("text_delta", text=text[i : i + 8])
            await asyncio.sleep(0.015)
        yield LLMChunk("usage", usage={"prompt_tokens": 240, "completion_tokens": max(1, len(text) // 2)})
        yield LLMChunk("done")

    async def _emit_tool(self, name: str, args: dict[str, Any]) -> AsyncIterator[LLMChunk]:
        await asyncio.sleep(0.1)
        yield LLMChunk(
            "tool_calls",
            tool_calls=[
                {
                    "id": f"call_{name}_{abs(hash((name, json.dumps(args, sort_keys=True)))) % 10000}",
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            ],
        )
        yield LLMChunk("usage", usage={"prompt_tokens": 200, "completion_tokens": 16})
        yield LLMChunk("done")