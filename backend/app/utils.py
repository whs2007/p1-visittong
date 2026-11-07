"""通用小工具：时间戳、ID 生成、延迟统计。"""
from __future__ import annotations

import statistics
import uuid
from datetime import datetime


def now_iso() -> str:
    """本地时区 ISO8601，例如 2026-10-30T21:20:00+08:00。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def percentile(values: list[int], p: float) -> int:
    """线性插值分位数；空列表返回 0。"""
    if not values:
        return 0
    data = sorted(values)
    if len(data) == 1:
        return int(data[0])
    k = (len(data) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(data) - 1)
    frac = k - lo
    return int(round(data[lo] + (data[hi] - data[lo]) * frac))


def avg_int(values: list[int]) -> int:
    return int(round(statistics.fmean(values))) if values else 0