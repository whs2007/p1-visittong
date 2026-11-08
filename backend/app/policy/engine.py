"""策略引擎：工具调用前的判定（拜访通版本）。

顺序（短路）：
  0) 身份或角色缺失            -> deny（fail-closed，v0.7 修正）
  1) 工具不存在/已禁用         -> deny
  2) 工具归属与当前 Skill 不符  -> deny
  3) 参数 Schema 校验失败      -> deny（slot_filling 工具跳过 required）
  4) 签到距离超限             -> deny(>2km) / approval(500m~2km)
  5) 命中危险模式             -> approval
  6) 工具风险 high            -> approval
  7) 其余                     -> allow

**必经且取最严**：`ToolPipeline` 无条件调用本引擎，扩展 hook 的判定只能与本
引擎的判定取「更严者」（deny > approval > allow），不能把 allow 强加给引擎的
deny。v0.6 及之前 `pipeline.py` 里 hook 返回非 None 就整段跳过策略判定，
等于「一个扩展点可以关掉核心安全策略」——审计把这条列为 A3，本次修掉。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.policy.rules import match_dangerous, validate_args
from app.skills.registry import ToolRegistry

#: 严格程度排序：数值越大越严。合并判定时取最大值，绝不取最小值。
SEVERITY = {"allow": 0, "approval": 1, "deny": 2}


@dataclass(frozen=True)
class Decision:
    action: str          # allow | deny | approval
    risk: str            # low | medium | high
    reason: str = ""
    rule: str = ""

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.action, 2)


def strictest(*decisions: Any) -> Any:
    """取最严判定；全部为 None 时按 fail-closed 返回 deny。

    参数可以是 `Decision`，也可以是 `HookDecision`（agent.hooks）—— 两者结构
    相同（action/risk/reason/rule），按 action 的严重度比较，避免为合并再造
    一个中间类型。
    """
    items = [d for d in decisions if d is not None]
    if not items:
        return Decision("deny", "high", "没有任何判定结果，按 fail-closed 拒绝", "no_decision")
    return max(items, key=lambda d: SEVERITY.get(getattr(d, "action", "deny"), 2))


class PolicyEngine:
    def __init__(self, registry: ToolRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def evaluate(
        self,
        *,
        user_id: str,
        name: str,
        args: dict[str, Any],
        role: str = "",
        active_skill: str = "",
    ) -> Decision:
        # 0) fail-closed：身份查不到 / 角色为空，直接拒绝。
        #    旧实现 `if role and tool.roles and role not in tool.roles` 在 role 为空时
        #    整条角色限制被跳过 —— 只要上游拿不到用户角色（DB 抖动、脏数据、
        #    未来某个新入口忘记取 role），高危工具就会对任何人开放。
        if not user_id or not role:
            return Decision(
                "deny", "high",
                "无法确定调用者身份或角色，拒绝执行",
                "identity_unverified",
            )

        tool = self.registry.get(name)
        if tool is None or not tool.enabled:
            return Decision("deny", "low", f"工具不存在或已禁用：{name}", "tool_unavailable")

        # 工具与技能归属：schema 裁剪只降低幻觉概率，不构成边界；这里再校验一次，
        # 与 AgentSession.guard 形成两层（装配边界 / 策略边界），任一层不通过都拒绝。
        if active_skill and tool.skill and tool.skill != active_skill:
            return Decision(
                "deny", "low",
                f"工具 {tool.name} 归属技能 {tool.skill}，当前技能是 {active_skill}",
                "skill_mismatch",
            )

        # 工具级角色权限：ToolSpec.roles 缺省允许销售/主管/管理员，可按工具收紧
        if tool.roles and role not in tool.roles:
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
            return Decision("approval", "high", f"命中文本风控规则 {rule}：{snippet}", f"dangerous:{rule}")

        if tool.risk == "high":
            return Decision("approval", "high", f"高危操作需要人工审批：{tool.label}", "high_risk_tool")

        return Decision("allow", tool.risk, "", "allowed")
