"""策略引擎：工具调用前的判定（拜访通版本）。

顺序（短路）：
  1) 工具不存在/已禁用        -> deny
  2) 参数 Schema 校验失败     -> deny（slot_filling 工具跳过 required）
  3) 签到距离超限             -> deny(>2km) / approval(500m~2km)
  4) 命中危险模式             -> approval
  5) 工具风险 high            -> approval
  6) 其余                     -> allow
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.policy.rules import match_dangerous, validate_args
from app.skills.registry import ToolRegistry


@dataclass(frozen=True)
class Decision:
    action: str          # allow | deny | approval
    risk: str            # low | medium | high
    reason: str = ""
    rule: str = ""


class PolicyEngine:
    def __init__(self, registry: ToolRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def evaluate(self, *, user_id: str, name: str, args: dict[str, Any], role: str = "") -> Decision:
        tool = self.registry.get(name)
        if tool is None or not tool.enabled:
            return Decision("deny", "low", f"工具不存在或已禁用：{name}", "tool_unavailable")

        # 工具级角色权限：ToolSpec.roles 缺省允许销售/主管/管理员，可按工具收紧
        if role and tool.roles and role not in tool.roles:
            return Decision(
                "deny", "low",
                f"当前角色（{role}）无权调用工具：{tool.label}",
                "role_denied",
            )

        if not tool.slot_filling:
            error = validate_args(tool.parameters, args)
            if error:
                return Decision("deny", tool.risk, error, "invalid_args")

        if name == "checkin":
            distance = int(args.get("distance_m") or 0)
            if distance > self.settings.distance_approval_m:
                return Decision(
                    "deny", "high",
                    f"距离门店 {distance} 米，超过 {self.settings.distance_approval_m} 米，无法签到",
                    "geo_too_far",
                )
            if distance > self.settings.distance_ok_m:
                return Decision(
                    "approval", "high",
                    f"距离门店 {distance} 米，超出 {self.settings.distance_ok_m} 米，需要主管确认",
                    "geo_out_of_range",
                )

        hit = match_dangerous(args)
        if hit:
            rule, snippet = hit
            return Decision("approval", "high", f"命中危险操作规则 {rule}：{snippet}", f"dangerous:{rule}")

        if tool.risk == "high":
            return Decision("approval", "high", f"高危操作需要人工审批：{tool.label}", "high_risk_tool")

        return Decision("allow", tool.risk, "", "allowed")