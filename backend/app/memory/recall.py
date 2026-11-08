"""记忆召回：可解释的相似度，不引入外部 embedding 依赖。

为什么不用向量库：项目本身是离线可跑的（Mock 模式无 Key、无网络），召回必须
在没有模型的情况下也能测试和复现。这里用**字符二元组余弦**——中文不需要分词，
短句上表现够用，而且能直接给出「命中了哪些片段」，召回理由可以进 Trace。

边界：这是词汇级相似度，不是语义相似度。同义改写（「下午」vs「午后」）召不回，
需要真 embedding 才能解决；本项目的取舍是「可解释、可离线」优先，并在文档里
写明这条天花板，而不是假装它是语义检索。
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.memory.store import MemoryStore

_PUNCT = re.compile(
    r'[\s。，、；：？！…—～·“”‘’（）()\[\]{}<>《》"\'!@#$%^&*_+=|\\/,.;:?~`-]+'
)


def _norm(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def _grams(text: str) -> set[str]:
    norm = _norm(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def score(query: str, content: str, tags: list[str] | None = None) -> float:
    """混合相似度：查询覆盖率优先 + 余弦兜底，取值 [0, 1]。

    为什么不是纯余弦：短查询对长记忆做余弦会被长度摊薄。实测「下午有空吗」对
    记忆「拜访尽量排在下午，上午要开晨会」的余弦只有 0.13，低于任何合理阈值，
    明明命中关键词却召不回。覆盖率（命中二元组 / 查询二元组）对短查询更稳。
    """
    q = _grams(query)
    c = _grams(content + " " + " ".join(tags or []))
    if not q or not c:
        return 0.0
    inter = len(q & c)
    if not inter:
        return 0.0
    coverage = inter / len(q)
    cosine = inter / math.sqrt(len(q) * len(c))
    return 0.65 * coverage + 0.35 * cosine


def _matched(query: str, content: str, limit: int = 3) -> list[str]:
    hits = sorted(_grams(query) & _grams(content))
    hits.sort(key=lambda gram: (-content.count(gram), gram))
    return hits[:limit]


def recall(
    store: MemoryStore,
    *,
    query: str,
    user_id: str,
    agent_name: str,
    k: int = 4,
    min_score: float = 0.16,
) -> list[dict[str, Any]]:
    """按当前输入召回事实类记忆，附带命中片段与来源。"""
    if not (query or "").strip():
        return []
    scored: list[dict[str, Any]] = []
    for memory in store.candidates(user_id=user_id, agent_name=agent_name):
        if memory.get("kind") == "preference":
            continue      # 偏好走常驻通道，不占召回名额
        sim = score(query, memory["content"], memory.get("tags"))
        if sim < min_score:
            continue
        scored.append(
            {
                **memory,
                "score": round(sim, 4),
                "matched": _matched(query, memory["content"]),
            }
        )
    scored.sort(key=lambda item: (-item["score"], item["created_at"]))
    return scored[: max(1, k)]


def pinned(store: MemoryStore, *, user_id: str, agent_name: str, limit: int = 3) -> list[dict[str, Any]]:
    """偏好常驻：这类记忆「什么时候都该生效」，不应依赖当轮措辞能否命中。

    实测教训：「拜访尽量排在下午」在「帮我安排明天的拜访」这句里一个关键词都
    对不上，但它恰恰是这句话最需要的信息。靠相似度召回必然漏，所以偏好单独
    走一条常驻通道，并用「最近 N 条」而不是「全部」限制预算。
    """
    items = [
        memory
        for memory in store.candidates(user_id=user_id, agent_name=agent_name)
        if memory.get("kind") == "preference"
    ]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[: max(1, limit)]


def render_recall(items: list[dict[str, Any]], *, title: str = "记忆（动态层，按当前输入召回）") -> str:
    """把召回结果渲染成提示词片段；每条带 id 与来源，便于模型引用与事后核查。"""
    if not items:
        return ""
    lines = [f"# {title}"]
    for item in items:
        origin = item.get("source_session_id") or "-"
        lines.append(
            f"- [{item['memory_id']}]（{item['kind']}/{item['scope']}，来源 {origin}）"
            f"{item['content']}"
        )
    return "\n".join(lines)
