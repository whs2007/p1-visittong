"""Agent Runtime 装配层（对齐 Pi 的 createAgentSession / DefaultResourceLoader）。

把「能力集」从模块级全局常量变成装配期产物：
    ResourceLoader -> AgentSpec -> AgentSession -> Orchestrator
"""
from app.runtime.loader import ResourceLoader, default_session
from app.runtime.session import AgentSession
from app.runtime.spec import AgentSpec

__all__ = ["AgentSpec", "AgentSession", "ResourceLoader", "default_session"]
