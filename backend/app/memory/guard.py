"""记忆写入门禁：投毒、越权与膨胀。

为什么这一层必须单独存在：记忆是**跨会话持续生效**的上下文。一次性的提示词
注入只污染一轮；被写进记忆的注入会在此后每一轮都被「当成系统自己的知识」
注入，而且看起来完全合法。所以写入路径要比对话路径更严，而不是更松。

三道门：
1. 内容门：拦截「忽略以上指令」这类元指令、伪造角色行、伪造特殊 token；
2. 权限门：`project` / `agent` 作用域只有主管能写，`user` 作用域只能写自己；
3. 规模门：单条长度、控制字符、换行数都有上限，防止用一条记忆挤掉上下文。

边界（如实声明）：这是**启发式**门禁，不是内容审核。它挡的是「明显的元指令
与伪造结构」，挡不住措辞隐蔽的偏好污染——那需要人工审核队列，本项目没做。
"""
from __future__ import annotations

import re
from typing import Final

SCOPES: Final = ("project", "agent", "user")
KINDS: Final = ("fact", "preference", "decision")

# 元指令：试图改变「规则」而不只是陈述「事实」
_POISON_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"忽略(掉)?(以上|之前|上述|前面|先前)(所有)?(的)?(指令|规则|设定|要求)", "meta_instruction"),
    (r"(无视|绕过|跳过|取消)(掉)?(所有)?(的)?(规则|限制|审批|校验|权限)", "meta_instruction"),
    (r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?|prompts?)", "meta_instruction"),
    (r"disregard\s+(all\s+)?(previous|above)", "meta_instruction"),
    (r"^\s*(system|assistant|developer)\s*[:：]", "fake_role_line"),
    (r"<\|[^|]{0,24}\|>", "fake_special_token"),
    (r"\[\s*(system|INST)\s*\]", "fake_special_token"),
    (r"(从现在起|以后)你(就)?是|你现在是(一个)?(新)?(角色|模型)", "role_override"),
    (r"开发者模式|jailbreak|\bDAN\b", "role_override"),
    (r"(把|将)(自己的)?(系统)?(提示词|prompt)(输出|打印|发)给", "prompt_exfiltration"),
)

MAX_LINES = 8


class MemoryRejected(ValueError):
    """记忆写入被拒绝；`reason` 是结构化的拒因码，可直接进台账。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def check_content(content: str, *, max_chars: int = 400) -> str:
    """校验内容并返回规范化文本（去首尾空白、压缩空行）。"""
    text = (content or "").strip()
    if not text:
        raise MemoryRejected("empty_content")
    if len(text) > max_chars:
        raise MemoryRejected("too_long", f"{len(text)} > {max_chars}")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise MemoryRejected("control_chars")
    if text.count("\n") + 1 > MAX_LINES:
        raise MemoryRejected("too_many_lines", f"{text.count(chr(10)) + 1} > {MAX_LINES}")
    for pattern, reason in _POISON_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            raise MemoryRejected(f"poisoned:{reason}")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def check_scope(
    *,
    scope: str,
    owner_id: str | None,
    actor: str,
    actor_role: str,
    is_manager: bool,
) -> None:
    """作用域权限：越权写记忆等于越权改所有人的上下文。"""
    if scope not in SCOPES:
        raise MemoryRejected("unknown_scope", scope)
    if scope in ("project", "agent"):
        if not is_manager:
            raise MemoryRejected("scope_forbidden", f"{scope} 仅主管可写")
        return
    # user 作用域：只能写自己；主管也不例外（避免「替别人记错事」）
    if not owner_id or owner_id != actor:
        raise MemoryRejected("scope_forbidden", "只能写自己的 user 作用域记忆")
    if actor_role not in ("sales", "manager", "admin"):
        raise MemoryRejected("role_unknown", actor_role)
