"""LLM 工厂：mock / live。"""
from __future__ import annotations

from app.config import Settings
from app.llm.base import BaseLLM
from app.llm.client import OpenAICompatLLM
from app.llm.mock import MockLLM


def build_llm(settings: Settings) -> BaseLLM:
    mode = settings.llm_mode
    if mode == "live" or (mode == "auto" and settings.llm_api_key):
        return OpenAICompatLLM(settings)
    return MockLLM()