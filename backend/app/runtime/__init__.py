"""Agent Runtime 装配层（对齐 Pi 的 createAgentSession / DefaultResourceLoader）。

把「能力集」从模块级全局常量变成装配期产物：
    ResourceLoader -> AgentSpec -> AgentSession -> ExecutionContract -> Orchestrator
"""
from app.runtime.contract import ExecutionContract
from app.runtime.contract import issue as issue_contract
from app.runtime.loader import ResourceLoader, default_session
from app.runtime.session import AgentSession, authorized_tool_names
from app.runtime.spec import AgentSpec

__all__ = [
    "AgentSpec",
    "AgentSession",
    "ExecutionContract",
    "ResourceLoader",
    "authorized_tool_names",
    "default_session",
    "issue_contract",
]
