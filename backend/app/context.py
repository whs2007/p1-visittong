"""应用上下文：组件装配。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.adapters.knowledge import build_knowledge
from app.approvals import ApprovalManager
from app.config import Settings
from app.domain.audio import AudioStore
from app.domain.db import Database
from app.domain.pipeline import EventPipeline
from app.domain.tasks import TaskStore
from app.llm.base import BaseLLM
from app.orchestrator.loop import Orchestrator
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree
from app.skills.registry import ToolRegistry
from app.trace.store import TraceStore


@dataclass
class AppContext:
    settings: Settings
    db: Database
    tree: SessionTree
    state: SessionState
    registry: ToolRegistry
    policy: PolicyEngine
    llm: BaseLLM
    traces: TraceStore
    approvals: ApprovalManager
    tasks: TaskStore
    audio: AudioStore
    knowledge: Any
    events: EventPipeline
    login_failures: dict[str, dict[str, float]] = field(default_factory=dict)
    orchestrator: Orchestrator = field(init=False)

    def __post_init__(self) -> None:
        self.orchestrator = Orchestrator(
            settings=self.settings,
            db=self.db,
            tree=self.tree,
            state=self.state,
            registry=self.registry,
            policy=self.policy,
            llm=self.llm,
            traces=self.traces,
            approvals=self.approvals,
            tasks=self.tasks,
            knowledge=self.knowledge,
        )