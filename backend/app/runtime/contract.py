"""ExecutionContract：一次工具调用的不可变执行凭证。

它是本项目所有「治理」叙事的**连接点**：

- 装配期给出能力快照（AgentSession.capability_fingerprint）；
- 执行期把这份快照写进凭证，凭证再随调用进入 guard / PolicyEngine / 审批 / 幂等账本；
- 检查点（checkpoint）绑定的是同一个指纹。

于是「能力版本一致性」不再是给 resume 单独打的补丁，而是整条执行链的一致性主键：
Turn 开始时快照一次，reload 只影响新 Turn，在途 Turn 的凭证不会中途换权限。

`frozen=True` 是刻意的：凭证一旦签发就不能被后续代码改写（包括 hook、审批回调），
否则「我校验的就是我执行的」这句话就不成立了。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.utils import now_iso


@dataclass(frozen=True)
class ExecutionContract:
    turn_id: str
    step: int
    call_id: str
    agent_name: str
    capability_fingerprint: str
    active_skill: str
    tool_name: str
    user_id: str
    role: str
    issued_at: str

    # ------------------------------------------------------------------ #
    @property
    def idem_key(self) -> str:
        """幂等主键：一次 Turn 里同一个 (step, call_id) 只应产生一次副作用。"""
        return f"{self.turn_id}:{self.step}:{self.call_id}"

    def with_tool(self, tool_name: str) -> "ExecutionContract":
        return replace(self, tool_name=tool_name)

    def describe(self) -> dict[str, Any]:
        """可落库 / 可回放的扁平结构（Trace 与治理台账共用）。"""
        return {
            "turn_id": self.turn_id,
            "step": self.step,
            "call_id": self.call_id,
            "agent": self.agent_name,
            "capability_fingerprint": self.capability_fingerprint,
            "active_skill": self.active_skill,
            "tool": self.tool_name,
            "user_id": self.user_id,
            "role": self.role,
            "issued_at": self.issued_at,
        }


def issue(
    *,
    turn_id: str,
    step: int,
    call_id: str,
    agent_name: str,
    capability_fingerprint: str,
    active_skill: str,
    tool_name: str,
    user_id: str,
    role: str,
) -> ExecutionContract:
    return ExecutionContract(
        turn_id=turn_id,
        step=step,
        call_id=call_id,
        agent_name=agent_name,
        capability_fingerprint=capability_fingerprint,
        active_skill=active_skill,
        tool_name=tool_name,
        user_id=user_id,
        role=role,
        issued_at=now_iso(),
    )
