"""降级台账：把「悄悄降级」变成「看得见的降级」。

起因是一个真实的口径漏洞（`docs/10` §5 最后两行）：
Redis 不可用时，会话锁会自动退回进程内锁，日志只有一行 WARNING；知识后端从
Milvus 退回本地时只留一个 `backend_reason` 字符串。**能力是悄悄少一块的**，
而 `/api/metrics` 上一切正常——多实例部署时锁语义已经不成立，监控却看不出来。

这里的规则很简单：
1. 任何降级都必须登记（组件 / 原因 / 影响 / 严重度），并在启动日志里成组输出；
2. `/api/metrics` 暴露 `degraded` 与明细，Prometheus 暴露 `visittong_degraded`；
3. `STRICT_DEGRADE=1` 时**启动失败**——宁可起不来，也不要带着错误的语义对外服务。

第 3 条是刻意的：企业环境里「服务能启动但语义是错的」比「启动失败」危险得多，
因为前者没人修。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Degradation:
    component: str          # redis | knowledge | db | lock | ...
    reason: str             # 为什么退了
    impact: str             # 语义上少了什么（这一栏是给值班的人看的）
    severity: str = "warn"  # warn | critical


@dataclass
class DegradationLog:
    items: list[Degradation] = field(default_factory=list)

    def add(self, component: str, reason: str, impact: str, severity: str = "warn") -> None:
        item = Degradation(component=component, reason=reason, impact=impact, severity=severity)
        if item in self.items:
            return
        self.items.append(item)

    @property
    def degraded(self) -> bool:
        return bool(self.items)

    @property
    def critical(self) -> bool:
        return any(item.severity == "critical" for item in self.items)

    def as_dicts(self) -> list[dict]:
        return [asdict(item) for item in self.items]

    def summary(self) -> str:
        if not self.items:
            return "none"
        return "; ".join(f"{item.component}:{item.reason}" for item in self.items)


def collect_runtime_degradations(*, settings, knowledge, state, db) -> DegradationLog:
    """从各组件当前状态里汇总降级项（启动时调用一次，结果挂到 ctx 上）。"""
    log = DegradationLog()

    reason = getattr(knowledge, "backend_reason", "")
    if reason:
        log.add(
            "knowledge", reason,
            "向量检索退化为本地 SQLite 全表扫描，召回质量与延迟都不代表生产 Milvus 表现",
            severity="warn",
        )

    backend = getattr(state, "backend", "memory")
    if backend != "redis":
        impact = (
            "会话状态与锁只在单进程内有效：多实例部署时会话可能串台、锁互斥不成立"
            if settings.redis_url
            else "未配置 Redis，按单实例运行"
        )
        log.add("redis", f"backend={backend}", impact, severity="critical" if settings.redis_url else "warn")

    if getattr(db, "dialect", "sqlite") != "mysql" and getattr(settings, "mysql_dsn", ""):
        log.add("db", f"dialect={db.dialect}", "MySQL 配置存在但未生效，写并发语义按 SQLite 执行", severity="critical")

    return log
