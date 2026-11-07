"""OpenAI 兼容流式客户端（DeepSeek / Qwen / vLLM 通用）。

要点：流式响应里 tool_calls 的 arguments 是分片字符串，必须按 index 累积后再解析。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import BaseLLM, LLMChunk, LLMError


class _ToolCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, deltas: list[dict[str, Any]]) -> None:
        for delta in deltas:
            index = int(delta.get("index", 0))
            current = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if delta.get("id"):
                current["id"] = delta["id"]
            function = delta.get("function") or {}
            if function.get("name"):
                current["name"] = function["name"]
            if function.get("arguments"):
                current["arguments"] += function["arguments"]

    @property
    def empty(self) -> bool:
        return not self._calls

    def finalize(self) -> list[dict[str, str]]:
        calls = []
        for index in sorted(self._calls):
            call = self._calls[index]
            calls.append(
                {
                    "id": call["id"] or f"call_{index}",
                    "name": call["name"],
                    "arguments": call["arguments"] or "{}",
                }
            )
        return calls


class OpenAICompatLLM(BaseLLM):
    name = "live"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": True,
            "temperature": self.settings.llm_temperature,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        accumulator = _ToolCallAccumulator()
        timeout = httpx.Timeout(self.settings.llm_timeout_s, connect=10.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "ignore")[:300]
                        raise LLMError(f"LLM HTTP {response.status_code}: {body}")
                    async for raw in response.aiter_lines():
                        if not raw or raw.startswith(":") or not raw.startswith("data:"):
                            continue
                        data = raw[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        usage = chunk.get("usage")
                        if usage:
                            yield LLMChunk(
                                "usage",
                                usage={
                                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                                },
                            )
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield LLMChunk("text_delta", text=content)
                        if delta.get("tool_calls"):
                            accumulator.feed(delta["tool_calls"])
        except httpx.TimeoutException as exc:
            raise LLMError(f"模型调用超时：{exc}", code="LLM_TIMEOUT") from exc
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(f"模型连接失败：{exc}") from exc

        if not accumulator.empty:
            yield LLMChunk("tool_calls", tool_calls=accumulator.finalize())
        yield LLMChunk("done")

    async def classify(self, text: str, labels: list[str]) -> dict[str, Any] | None:
        """总控路由的轻量分类：一次非流式调用，要求模型只回 JSON。

        失败（网络/超时/非 JSON）返回 None，由路由层回退到规则通道。
        """
        system = (
            "你是销售 Agent 的意图路由器。只输出一个 JSON 对象："
            '{"skill": "<label>", "confidence": 0.0~1.0, "slots": {}, "reason": "..."}。'
            f"可选 skill: {', '.join(labels)}。"
            "skill 含义：plan_create 创建拜访计划；plan_update 改约；plan_cancel 取消；"
            "checkin 到店签到；checkout 离店签退；visit_log 写拜访日志；todo_query 查询待办/计划；"
            "reminder 发提醒/通知；chat 闲聊或无法判断。"
        )
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": 200,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code >= 400:
                    return None
                content = (response.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
                start, end = content.find("{"), content.rfind("}")
                if start < 0 or end <= start:
                    return None
                data = json.loads(content[start : end + 1])
                if not isinstance(data, dict) or data.get("skill") not in labels:
                    return None
                return data
        except (httpx.HTTPError, ValueError, KeyError):
            return None