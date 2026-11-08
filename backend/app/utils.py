"""通用小工具：时间戳、ID 生成、延迟统计。"""
from __future__ import annotations

import statistics
import uuid
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config import get_settings


@lru_cache(maxsize=1)
def business_tz() -> ZoneInfo:
    """业务时区（默认 Asia/Shanghai，配置项 business_tz）。

    为什么不直接用服务器本地时区：`planned_at` 这类业务时间的语义必须
    在任何部署机上一致。踩过的坑：开发机 Windows 是 +08:00、CI 的
    Ubuntu 是 UTC，同一段「naive datetime + .astimezone()」在两边写出
    两种数据，同一个测试一边绿一边红。lru_cache 是因为配置运行期不变，
    ZoneInfo 解析也不必重复做。
    """
    return ZoneInfo(get_settings().business_tz)


def business_today() -> date:
    """业务时区的「今天」。「明天/下周三」这类相对日期必须以业务时区起算，
    否则 UTC 机器在 0 点~8 点之间算出的「今天」会比北京时间慢一天。"""
    return datetime.now(business_tz()).date()


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