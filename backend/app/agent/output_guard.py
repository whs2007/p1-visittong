"""工具输出治理：脱敏 → 裁剪 → 引用化。

为什么需要这一层（真实系统里的三个事故来源）：

1. **超长输出挤爆上下文**：一次 `todo_list` 或知识检索会把几百行原始 JSON 塞进
   messages，下一轮要么触发压缩（丢细节），要么直接把窗口顶满。裁剪不是「省
   token」，是「别让一次工具调用把整轮对话顶掉」。
2. **PII 顺着工具结果进模型、进日志、进 Trace**：客户手机号、联系人邮箱、
   身份证号出现在工具返回里，`log.info` 和 `trace_events` 都会原样落地。
   脱敏必须发生在**写出之前**，而不是事后扫日志。
3. **裁掉了就查不回来**：只截断不留引用，出问题只能靠猜「当时模型到底看到了
   什么」。所以超长内容落 `tool_outputs` 表，上下文里留 `ref`，可按 ref 回查
   **脱敏后的全文**并用 sha256 校验「回查到的就是我注入的那段」。

刻意不做的事：不存原始明文。落库的是脱敏后全文——PII 最小化留存比「方便排查」
更重要，排查要看的是业务字段，不是某个人的手机号。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# 顺序有意义：身份证 18 位必须排在银行卡 16~19 位之前，否则会先被当卡号吃掉。
_PII_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("phone", re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    ("id_card", re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)"), r"\1********\2"),
    ("bank_card", re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)"), r"\1 **** **** \2"),
    ("email", re.compile(r"([\w.+-]{1,2})[\w.+-]*@([\w-]+\.[\w.-]+)"), r"\1***@\2"),
)

_MARKER_RESERVE = 96


@dataclass(frozen=True)
class Governed:
    """治理结果。`text` 是唯一允许进入上下文 / Trace / 前端的版本。"""

    text: str
    ref: str | None
    original_chars: int
    kept_chars: int
    redactions: int
    truncated: bool

    @property
    def savings_pct(self) -> float:
        if self.original_chars <= 0:
            return 0.0
        return round((1 - self.kept_chars / self.original_chars) * 100, 1)


def redact_pii(text: str) -> tuple[str, int]:
    """把手机号 / 身份证 / 银行卡 / 邮箱替换成可读掩码，返回 (文本, 命中数)。

    保留首尾是为了「人还能对上是谁」——全打码等于把可用信息一起删掉，
    排查时又要去别的系统捞一遍。
    """
    hits = 0
    for _name, pattern, replacement in _PII_RULES:
        text, count = pattern.subn(replacement, text)
        hits += count
    return text, hits


def _clip(text: str, limit: int, head_ratio: float, ref: str | None) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = (
        f"\n…[输出已裁剪：原始 {len(text)} 字符，仅保留首尾；"
        f"全文引用 {'ref=' + ref if ref else '(未保存)'}]\n"
    )
    budget = max(64, limit - len(marker))
    head_len = max(32, int(budget * head_ratio))
    tail_len = max(0, budget - head_len)
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len else ""
    return f"{head}{marker}{tail}", True


class OutputGovernor:
    """无状态治理器：给定输出返回 Governed，超长时把去敏全文落库。"""

    def __init__(self, db, *, max_chars: int = 1600, head_ratio: float = 0.7, redact: bool = True) -> None:
        self.db = db
        self.max_chars = max(200, int(max_chars))
        self.head_ratio = min(0.9, max(0.1, float(head_ratio)))
        self.redact = redact

    def govern(
        self,
        output: str,
        *,
        tool: str = "",
        trace_id: str = "",
        session_id: str = "",
        user_id: str = "",
    ) -> Governed:
        raw = output or ""
        original = len(raw)
        text = raw
        hits = 0
        if self.redact:
            text, hits = redact_pii(text)

        needs_ref = len(text) > self.max_chars
        ref = self._store(text, tool=tool, trace_id=trace_id, session_id=session_id, user_id=user_id) if needs_ref else None
        clipped, truncated = _clip(text, self.max_chars, self.head_ratio, ref)
        clipped, extra = (redact_pii(clipped) if self.redact else (clipped, 0))
        hits += extra
        return Governed(
            text=clipped, ref=ref, original_chars=original, kept_chars=len(clipped),
            redactions=hits, truncated=truncated,
        )

    # ------------------------------------------------------------------ #
    def _store(self, text: str, *, tool: str, trace_id: str, session_id: str, user_id: str) -> str:
        """落去敏全文；同一 (trace, tool, 内容) 只存一份，重复治理不会膨胀。

        `sha256` 是**内容本身**的摘要（回查时可校验「读到的就是注入的那段」）；
        `ref` 再把 trace/tool 拌进去，避免不同会话的同名输出共用一条引用。
        """
        digest = hashlib.sha256(text.encode()).hexdigest()
        ref = f"TO-{hashlib.sha256(f'{trace_id}:{tool}:{digest}'.encode()).hexdigest()[:12]}"
        try:
            self.db._exec(  # noqa: SLF001
                "INSERT OR IGNORE INTO tool_outputs"
                " (ref,trace_id,session_id,user_id,tool,content,sha256,chars,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ref, trace_id, session_id, user_id, tool, text, digest, len(text), _now()),
            )
        except Exception:  # noqa: BLE001  存不下不能反过来把这一轮搞挂
            return ref
        return ref

    def fetch(self, ref: str) -> dict | None:
        rows = self.db._rows(  # noqa: SLF001
            "SELECT ref,trace_id,session_id,user_id,tool,content,sha256,chars,created_at"
            " FROM tool_outputs WHERE ref=?", (ref,),
        )
        return rows[0] if rows else None


def _now() -> str:
    from app.utils import now_iso

    return now_iso()
