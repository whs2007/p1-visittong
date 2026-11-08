"""混合检索（hybrid）与旧向量检索（vector）的行为差异。

这些用例钉住的是「为什么换掉哈希向量」的三个具体机制，而不是「分数好看」：
1. ASCII + CJK 混合实体（B站）哈希向量召不回，切词后能；
2. 高频片段（出现在几十篇文档里的「科技」）不应压过稀有片段（「光通信」）；
3. 同样命中时，短文档（标题命中）应排在长文档（顺带提到）前面——长度归一化。

同时保留 vector 模式可用性断言：A/B 的前提是两套都能跑，不是「新的把旧的删了」。
"""
from __future__ import annotations

import pytest
from app.adapters.knowledge import LocalKnowledge, hybrid_scores
from app.domain.db import Database


@pytest.fixture()
def knowledge(tmp_path):
    db = Database(str(tmp_path / "kb.db"))
    db.init()
    adapter = LocalKnowledge(db, mode="hybrid")
    docs = [
        ("c_1", "customer", "上海哔哩哔哩科技有限公司", "上海哔哩哔哩科技有限公司 哔哩哔哩 B站 视频"),
        ("c_2", "customer", "成都新易盛通信技术股份有限公司", "成都新易盛通信技术股份有限公司 新易盛 光通信"),
        ("c_3", "customer", "北京示例科技有限公司", "北京示例科技有限公司 示例 科技 互联网"),
        ("c_4", "customer", "上海示例网络有限公司", "上海示例网络有限公司 示例 科技 互联网"),
        ("c_5", "customer", "深圳示例数据有限公司", "深圳示例数据有限公司 示例 科技 互联网"),
        ("s_1", "store", "中关村店", "中关村店 北京市海淀区中关村大街1号 北京"),
        ("s_2", "store", "上地店", "上地店 北京市海淀区上地信息路2号 北京"),
    ]
    for doc_id, kind, title, text in docs:
        adapter.upsert(doc_id=f"{kind}:{doc_id}", kind=kind, ref_id=doc_id, title=title, text=text, force=True)
    return adapter


def test_hybrid_recalls_ascii_plus_cjk_entity(knowledge):
    hits = knowledge.search("B站", kind="customer", top_k=3)
    assert hits and hits[0]["ref_id"] == "c_1"
    # 短串整体作为一个 token，"b站" 直接命中，覆盖率不会因为拆词腰斩
    assert hits[0]["coverage"] >= 0.6


def test_rare_token_beats_common_token(knowledge):
    """「光通信」只出现在一篇里，「科技」出现在三篇里：稀有词必须更有分量。"""
    hits = knowledge.search("成都光通信公司", kind="customer", top_k=3)
    assert hits[0]["ref_id"] == "c_2"


def test_short_title_beats_long_body(knowledge):
    """查询同时命中标题与正文时，短文档应靠前（长度归一化）。"""
    hits = knowledge.search("上地店", kind="store", top_k=3)
    assert hits[0]["ref_id"] == "s_2"
    assert all(hit["ref_id"] != "s_1" or hit["coverage"] < hits[0]["coverage"] for hit in hits)


def test_vector_mode_still_available(knowledge, tmp_path):
    """A/B 的前提：两套检索都能跑；旧实现没有被删掉。"""
    db = Database(str(tmp_path / "kb2.db"))
    db.init()
    legacy = LocalKnowledge(db, mode="vector")
    legacy.upsert(doc_id="customer:c_1", kind="customer", ref_id="c_1", title="上海哔哩哔哩科技有限公司",
                  text="上海哔哩哔哩科技有限公司 哔哩哔哩 B站 视频", force=True)
    hits = legacy.search("哔哩哔哩", kind="customer", top_k=3)
    assert hits and hits[0]["ref_id"] == "c_1"
    assert hits[0]["retriever"] == "vector"


def test_empty_query_returns_nothing(knowledge):
    assert knowledge.search("   ", kind="customer") == []


def test_scores_are_bounded_and_explainable(knowledge):
    hits = knowledge.search("北京中关村", kind="store", top_k=3)
    for hit in hits:
        assert 0.0 <= hit["coverage"] <= 1.0
        assert 0.0 <= hit["score"] <= 1.0
        assert hit["retriever"] == "hybrid"


def test_hybrid_scores_exposes_coverage_and_bm25():
    docs = [
        {"doc_id": "a", "title": "望京店", "text": "望京店 北京市朝阳区望京街10号"},
        {"doc_id": "b", "title": "国贸店", "text": "国贸店 北京市朝阳区建国门外大街1号"},
    ]
    scores = hybrid_scores("望京街", docs)
    assert scores["a"][0] > 0
    # 完全没命中的文档不进结果表——不是「0 分参与排序」，是直接不返回
    assert "b" not in scores
