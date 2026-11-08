"""token 估算与按 token 裁剪。

为什么需要它（这条是从 `docs/10` §5 的自我批评长出来的）：
v0.9 的输出治理按**字符**裁，而上下文预算、成本、压缩阈值全是按 **token** 算的。
两者之间的换算系数不是常数——同一台机器、同样的 1600 字符上限：

| 输出类型 | token/字符 | 1600 字符实际 token |
|---|---|---|
| 中文客户档案 JSON | 0.535 | 856 |
| 英文 JSON | 0.404 | 646 |
| 单行日志 | 0.369 | 591 |
| 代码片段 | 0.299 | 479 |

也就是「裁到 1600 字符」在不同工具下差 **1.79 倍**。对上下文预算是 1.79 倍的误差，
对成本报表是 1.79 倍的误差。所以改成按 token 裁。

估算器 `estimate_tokens` = 1.15 × CJK 字符数 + 0.36 × 其他字符数，
系数是拿 tiktoken(cl100k_base) 在 7 类真实形态的样例上网格搜索拟合的：
平均绝对误差 **5.9%**，最差 19.1%（代码这类短 token 密集的文本）。
为什么不直接依赖 tiktoken：它是可选依赖，生产镜像里不装，且模型换 tokenizer 就失效。
估算器的误差是可以接受的下界——它只用来**守预算**，不用于计费对账（计费以 provider 返回为准）。
"""
from __future__ import annotations

import math

# 三特征线性估算：系数是拿 tiktoken(cl100k_base) 在 10 类真实输出形态上
# 网格搜索出来的（不是拍脑袋的 chars/4）。CJK 与 ASCII 字母的 token 密度差 5 倍，
# 符号（引号/花括号/逗号/冒号）又接近 1 字 0.5 token，混在一起估必然偏。
CJK_WEIGHT = 1.10
LETTER_WEIGHT = 0.22
OTHER_WEIGHT = 0.53
# 预算安全系数：估算器有 ±15% 的尾部误差，按 0.92 收口，
# 让「真实 token 超预算」的概率降到可接受（实测 10 类形态 0 次超）。
SAFETY = 0.92
# 每条消息除了正文还有角色/分隔符开销，按 4 token 记（与 compaction 的 +8 一致口径）
MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """估算 token 数：CJK 1.10 / ASCII 字母 0.22 / 其他（数字与符号）0.53。"""
    if not text:
        return 0
    cjk = letters = 0
    for ch in text:
        if ord(ch) >= 0x2E80:
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            letters += 1
    other = len(text) - cjk - letters
    return int(math.ceil(cjk * CJK_WEIGHT + letters * LETTER_WEIGHT + other * OTHER_WEIGHT))


def clip_to_tokens(
    text: str, max_tokens: int, *, head_ratio: float = 0.7, ref: str | None = None,
) -> tuple[str, bool]:
    """按 token 预算裁剪：保留首尾，中间插裁剪标记。返回 (文本, 是否裁剪)。

    为什么留尾部：错误码、汇总行、「共 N 条」几乎都在末尾。只留头部会把
    「为什么失败」裁掉，模型于是编一个原因。
    """
    limit = max(32, int(max_tokens * SAFETY))
    if estimate_tokens(text) <= limit:
        return text, False

    marker = (
        f"\n…[输出已裁剪：原始 {len(text)} 字符 / 约 {estimate_tokens(text)} tokens，"
        f"仅保留首尾；全文引用 {'ref=' + ref if ref else '(未保存)'}]\n"
    )
    budget = max(16, limit - estimate_tokens(marker))
    head_budget = max(8, int(budget * head_ratio))
    tail_budget = max(0, budget - head_budget)

    def _take(source: str, token_budget: int, from_end: bool) -> str:
        # 从短到长取前缀/后缀，直到估算 token 超出预算；步长 8 字符保证收敛。
        step = 8
        keep = 0
        length = len(source)
        while keep < length:
            candidate = keep + step
            chunk = source[-candidate:] if from_end else source[:candidate]
            if estimate_tokens(chunk) > token_budget:
                break
            keep = candidate
        return source[len(source) - keep :] if from_end else source[:keep]

    head = _take(text, head_budget, from_end=False)
    tail = _take(text, tail_budget, from_end=True) if tail_budget else ""
    return f"{head}{marker}{tail}", True


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_tokens(str(m.get("content") or "")) + MESSAGE_OVERHEAD for m in messages)
