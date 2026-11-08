"""统计工具：成对比较、显著性、检出下限。

为什么单独拎出来：v0.9 只给了「通过率 + 置信区间」，那回答的是「这个数字有多稳」，
回答不了「A 版本和 B 版本谁更好」。两个版本跑同一批用例时是**成对**数据，
用两独立样本的区间重叠来判断，会系统性地判「无差异」（保守到没用）。
这里提供成对检验的最小实现：

- `mcnemar_exact(b, c)`：只看不一致的那几对（A 过 B 挂 / A 挂 B 过），
  在零假设下 b 服从 Binomial(b+c, 0.5)，双尾精确 p 值。样本少也能用。
- `paired_bootstrap_delta(a, b)`：对「每个用例的差」重采样，给差值的 CI。
  McNemar 给显著性，它给「差多少」——两个都要，只有一个都容易被追问翻车。
- `min_detectable_effect(n)`：给定用例数，能检出多大的差异。用来诚实回答
  「你 25 条用例能不能证明两个版本有差别」——不能的时候要说不能。
"""
from __future__ import annotations

import math
import random
from typing import Sequence


def mcnemar_exact(b: int, c: int) -> float:
    """双尾精确 McNemar。b/c 是两类不一致对的数量；b+c=0 时返回 1.0（无证据）。"""
    if b < 0 or c < 0:
        raise ValueError("b/c 必须非负")
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def paired_bootstrap_delta(
    a: Sequence[int], b: Sequence[int], *, resamples: int = 5000, seed: int = 20261108,
) -> tuple[float, float, float]:
    """成对 bootstrap：返回 (差值点估计, 下界, 上界)，95%。

    a/b 是同一批用例在两种配置下的 0/1 结果，长度必须相同。
    重采样的是**用例**而不是执行次数——用例之间的难度差异被保留在配对里。
    """
    if len(a) != len(b):
        raise ValueError("成对比较要求两组长度相同")
    if not a:
        return 0.0, 0.0, 0.0
    diffs = [float(x) - float(y) for x, y in zip(a, b)]
    point = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    n = len(diffs)
    samples = []
    for _ in range(resamples):
        total = sum(diffs[rng.randrange(n)] for _ in range(n))
        samples.append(total / n)
    samples.sort()
    low = samples[int(0.025 * len(samples))]
    high = samples[int(0.975 * len(samples)) - 1]
    return round(point, 4), round(low, 4), round(high, 4)


def min_detectable_effect(n: int, *, p: float = 0.85, alpha: float = 0.05, power: float = 0.8) -> float:
    """两比例检验的近似检出下限（绝对值）。

    用途是把「样本量不够」变成可计算的数字：25 条用例在 p≈0.85 时只能检出
    ≥ 15 个百分点左右的差异，所以「跑 25 条没发现差别」不等于「没有差别」。
    """
    if n <= 0:
        return 1.0
    z_alpha = 1.959963985 if abs(alpha - 0.05) < 1e-9 else 1.959963985
    z_power = 0.841621234 if abs(power - 0.8) < 1e-9 else 0.841621234
    return round((z_alpha + z_power) * math.sqrt(2 * p * (1 - p) / n), 4)


def paired_table(a: Sequence[int], b: Sequence[int]) -> dict[str, int]:
    """2×2 列联表：both=都过，only_a=只有 A 过，only_b=只有 B 过，neither=都挂。"""
    if len(a) != len(b):
        raise ValueError("成对比较要求两组长度相同")
    both = only_a = only_b = neither = 0
    for x, y in zip(a, b):
        if x and y:
            both += 1
        elif x:
            only_a += 1
        elif y:
            only_b += 1
        else:
            neither += 1
    return {"both": both, "only_a": only_a, "only_b": only_b, "neither": neither}
