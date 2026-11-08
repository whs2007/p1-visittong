"""应用上下文：组件装配。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.output_guard import OutputGovernor
from app.approvals import ApprovalManager
from app.config import Settings
from app.degradations import DegradationLog
from app.domain.audio import AudioStore
from app.domain.db import Database
from app.domain.governance import GovernanceStore
from app.domain.idempotency import IdempotencyStore
from app.domain.knowledge_sync import KnowledgeSync
from app.domain.pipeline import EventPipeline
from app.domain.tasks import TaskStore
from app.llm.base import BaseLLM
from app.memory.store import MemoryStore
from app.orchestrator.loop import Orchestrator
from app.policy.engine import PolicyEngine
from app.runtime import AgentSession, ResourceLoader
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
    knowledge_sync: KnowledgeSync | None = None
    session: AgentSession | None = None
    resource_loader: ResourceLoader | None = None
    idem: IdempotencyStore | None = None
    governance: GovernanceStore | None = None
    memories: MemoryStore | None = None
    # 降级台账：启动时由各组件状态汇总，供 /api/metrics 与 PROM 暴露
    degradations: DegradationLog = field(default_factory=DegradationLog)
    governor: OutputGovernor | None = None
    login_failures: dict[str, dict[str, float]] = field(default_factory=dict)
    orchestrator: Orchestrator = field(init=False)

    def __post_init__(self) -> None:
        # 输出治理器在装配期建一份，pipeline 与回查接口共用（同一个 db、同一套阈值），
        # 避免「写进去用一套参数、读出来用另一套」这类只有排查时才发现的不一致。
        if self.governor is None:
            self.governor = OutputGovernor(
                self.db,
                max_chars=self.settings.tool_output_max_chars,
                max_tokens=self.settings.tool_output_max_tokens,
                head_ratio=self.settings.tool_output_head_ratio,
                redact=self.settings.pii_redact,
            )
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
            session=self.session,
            memories=self.memories,
            idem=self.idem,
            governor=self.governor,
        )
