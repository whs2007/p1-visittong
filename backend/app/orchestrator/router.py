"""总控 Skill 路由：文本语义 / 卡片精确通道，单轮只激活一个子 Skill。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from app.adapters import crm
from app.domain.db import Database
from app.skills.visit_skills import SKILLS
from app.utils import business_today, business_tz

WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass
class RouteResult:
    skill: str
    confidence: float
    source: str = "text"          # text | card
    slots: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def _next_weekday(target: int, next_week: bool) -> date:
    # 相对日期以业务时区起算：UTC 机器在 0~8 点之间 date.today() 会慢一天
    today = business_today()
    delta = (target - today.weekday()) % 7
    if delta == 0:
        delta = 7
    if next_week:
        # "下周三" = 本周三之后的下一周
        delta = (target - today.weekday()) % 7 + 7
    return today + timedelta(days=delta)


def parse_time(text: str) -> str | None:
    """解析中文时间：今天/明天/下周三 + 上午/下午/具体小时。返回 ISO 字符串。"""
    today = business_today()
    m = re.search(r"(下{1,2})?(周|星期)([一二三四五六日天])", text)
    day: date | None = None
    if m:
        day = _next_weekday(WEEKDAYS[m.group(3)], bool(m.group(1)))
    elif "明天" in text:
        day = today + timedelta(days=1)
    elif "后天" in text:
        day = today + timedelta(days=2)
    elif "今天" in text:
        day = today
    else:
        m2 = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
        if m2:
            day = date(today.year, int(m2.group(1)), int(m2.group(2)))

    hour: int | None = None
    hm = re.search(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?", text)
    if hm:
        hour = int(hm.group(1))
        if hm.group(2):
            minute = int(hm.group(2))
        else:
            minute = 0
    else:
        minute = 0
        if "上午" in text:
            hour = 10
        elif "中午" in text:
            hour = 12
        elif "下午" in text:
            hour = 15
        elif "晚上" in text:
            hour = 19

    if day is None and hour is None:
        return None
    day = day or date.today()
    if hour is None:
        hour = 10
    # 下午 1~6 点按 24 小时制修正
    if hour < 12 and ("下午" in text or "晚上" in text):
        hour += 12
    # 业务时间挂业务时区，不挂部署机本地时区（CI 的 UTC 曾把 +08:00 写成 +00:00）
    return datetime.combine(day, time(hour, minute), tzinfo=business_tz()).isoformat(timespec="seconds")


def route_text(db: Database, text: str) -> RouteResult:
    t = (text or "").strip()
    if not t:
        return RouteResult("chat", 0.0, reason="empty")

    if re.search(r"取消|不去了|别去了", t):
        return RouteResult("plan_cancel", 0.95)
    if re.search(r"签退|离店|走了|结束拜访", t):
        return RouteResult("checkout", 0.95)
    if re.search(r"提醒|通知|告知|告诉", t):
        return RouteResult("reminder", 0.85)
    if re.search(r"签到|到店|到了", t):
        return RouteResult("checkin", 0.95)
    if re.search(r"日志|记一下|记录一下|复盘", t):
        return RouteResult("visit_log", 0.9)
    if re.search(r"改|推迟|调整|换个时间|改到", t):
        return RouteResult("plan_update", 0.85)
    if re.search(r"待办|安排|今天.*(拜访|计划)|行程", t):
        return RouteResult("todo_query", 0.9)
    if re.search(r"约|拜访|计划|见客户|排期", t):
        return RouteResult("plan_create", 0.85)

    return RouteResult("chat", 0.3, reason="fallback")


def match_store(db: Database, text: str) -> dict[str, Any] | None:
    """用门店名（去掉“店”字）在文本里做包含匹配，例如“北京中关村” -> 中关村店。"""
    for store in db._rows("SELECT * FROM stores"):  # noqa: SLF001
        token = (store["name"] or "").replace("店", "").strip()
        if token and token in text:
            return store
    return None


def build_slots(db: Database, text: str) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    customers = crm.match_customer(db, text)
    if len(customers) == 1:
        slots["customer"] = customers[0]["name"]
        slots["customer_id"] = customers[0]["customer_id"]
    elif len(customers) > 1:
        slots["customer"] = customers[0]["name"]

    planned_at = parse_time(text)
    if planned_at:
        slots["planned_at"] = planned_at
    # 「我到店了，距离大概 1200 米」里的距离必须被抽出来，否则签到永远按默认
    # 320 米走，远处的补签就绕过了审批。这是业务规则，不是模型该猜的。
    distance = re.search(r"(\d{1,5})\s*(?:米|m\b|M\b)", text)
    if distance:
        slots["distance_m"] = int(distance.group(1))
    store = match_store(db, text)
    if store:
        slots["store"] = store["name"]
        slots["store_id"] = store["store_id"]
    topic = re.search(r"(?:主题|聊|谈|沟通)([^，。；]{2,20})", text)
    if topic:
        slots["topic"] = topic.group(1)
    return slots


def route_card(
    db: Database, card_name: str, action_id: str, skill_name: str, payload: dict[str, Any],
    *, allowed: Iterable[str] | None = None,
) -> RouteResult:
    """卡片精确通道；allowed 为当前 Agent 可见技能，缺省用全局 SKILLS。"""
    valid = set(allowed) if allowed is not None else set(SKILLS)
    skill = skill_name if skill_name in valid else "chat"
    slots = dict(payload or {})
    if action_id and action_id.startswith("c_"):
        customer = db.get_customer(action_id)
        if customer:
            slots["customer"] = customer["name"]
            slots["customer_id"] = customer["customer_id"]
    return RouteResult(skill, 1.0, source="card", slots=slots, reason=f"{card_name}:{action_id}")


async def route_text_hybrid(
    db: Database, text: str, llm: Any, settings: Any,
    *, labels: list[str] | None = None,
) -> RouteResult:
    """总控路由：规则优先，规则不确定时交给 LLM 分类，再不行回退 chat。

    - router_mode=rules：只用规则（离线/测试）；
    - router_mode=llm：规则只提供置信度参考，统一走 LLM 分类；
    - router_mode=hybrid（默认）：规则置信度 >= 0.9 直接采用（明确指令），
      否则（典型是 chat/fallback）调用 LLM.classify，置信度 >= 阈值才采纳。
    """
    rule = route_text(db, text)
    if settings.router_mode == "rules":
        return rule
    if settings.router_mode == "hybrid" and rule.confidence >= 0.9:
        return rule

    # 候选标签来自当前 Agent 的能力集；未显式装配时退化为全局 SKILLS
    candidate_labels = [*labels, "chat"] if labels is not None else [*SKILLS, "chat"]
    try:
        result = await llm.classify(text, candidate_labels)
    except Exception:  # noqa: BLE001  路由失败必须可降级，不能拖垮整个 Turn
        result = None
    if isinstance(result, dict):
        skill = result.get("skill")
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if skill in candidate_labels and confidence >= float(settings.router_confidence_min):
            slots = build_slots(db, text)
            slots.update(result.get("slots") or {})
            return RouteResult(skill, confidence, "llm", slots, str(result.get("reason") or "llm_route"))
    return rule