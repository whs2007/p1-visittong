"""Phase 6：知识语义检索 + 数据管道（Hive/Kafka 模拟）。

覆盖：
- 本地向量索引：客户/门店文档 upsert、余弦 top-k（门店/客户可区分）；
- Milvus 不可用时自动降级 local 并带原因；
- /api/knowledge/search、/sync、/status、/events；
- sync 通过 ingest_events 模拟 hive2kafka -> consumer，重复 sync 幂等；
- 语义兜底：精确/别名都匹配不到时，用向量召回客户。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.skills.tools  # noqa: F401
from app.adapters.knowledge import LocalKnowledge, build_knowledge, cosine, embed, sync_knowledge
from app.config import Settings
from app.domain.db import Database
from app.domain.pipeline import EventPipeline
from app.main import create_app


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "test.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        approval_timeout_s=2,
    )
    base.update(overrides)
    return Settings(**base)


def make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "knowledge.db"))
    db.init()
    db.seed_demo()
    return db


def test_embedding_and_local_search(tmp_path) -> None:
    db = make_db(tmp_path)
    knowledge = LocalKnowledge(db)
    counts = sync_knowledge(db, knowledge)
    assert counts["docs"] == counts["customers"] + counts["stores"]
    # 门店：地址片段召回
    stores = knowledge.search("中关村", kind="store", top_k=3)
    assert stores and stores[0]["kind"] == "store"
    # 客户：品牌/简称召回
    customers = knowledge.search("字节", kind="customer", top_k=3)
    assert customers and customers[0]["kind"] == "customer"
    # 向量自身相似度
    vector = embed("字节跳动", 256)
    assert 0.99 < cosine(vector, vector) <= 1.0001


def test_milvus_unavailable_falls_back_local(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path, knowledge_backend="milvus", milvus_uri="http://127.0.0.1:1")
    knowledge = build_knowledge(settings, db)
    assert knowledge.backend == "local"
    assert knowledge.backend_reason


def test_pipeline_events_are_idempotent(tmp_path) -> None:
    db = make_db(tmp_path)
    events = EventPipeline(db)
    knowledge = LocalKnowledge(db)
    event_id = events.emit("hive2kafka.knowledge_sync", {"mode": "full"})
    processed = events.drain(lambda event: sync_knowledge(db, knowledge))
    assert processed == 1
    assert events.pending() == []
    first = knowledge.count()
    events.emit("hive2kafka.knowledge_sync", {"mode": "full"})
    events.drain(lambda event: sync_knowledge(db, knowledge))
    assert knowledge.count() == first  # doc_id upsert，不重复
    assert any(row["event_id"] == event_id for row in events.recent(10))


@pytest.fixture()
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        login = test_client.post("/api/auth/login", json={"user_id": "sales-001", "password": "123456"})
        assert login.status_code == 200, login.text
        test_client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield test_client


@pytest.fixture()
def manager_client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        login = test_client.post("/api/auth/login", json={"user_id": "manager-001", "password": "123456"})
        assert login.status_code == 200, login.text
        test_client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield test_client


def test_knowledge_api_search_status_sync(client, manager_client) -> None:
    found = client.get("/api/knowledge/search", params={"q": "中关村", "kind": "store"})
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["backend"] == "local" and body["results"]
    assert client.get("/api/knowledge/search", params={"q": ""}).status_code == 400

    status = client.get("/api/knowledge/status").json()
    assert status["docs"] >= 1 and status["events"]

    assert client.post("/api/knowledge/sync").status_code == 403
    synced = manager_client.post("/api/knowledge/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["processed"] == 1
    events = manager_client.get("/api/knowledge/events").json()["events"]
    assert events and events[0]["status"] == "processed"


def test_semantic_customer_fallback(tmp_path) -> None:
    """精确 LIKE 失败时，用向量召回（构造一个只在品牌字段出现的客户）。"""
    db = make_db(tmp_path)
    db._exec(  # noqa: SLF001
        "UPDATE customers SET alias='', brand='星河数据云' WHERE customer_id='c_yunqi_a'"
    )
    knowledge = LocalKnowledge(db)
    sync_knowledge(db, knowledge)
    hits = knowledge.search("星河数据云", kind="customer", top_k=3)
    assert hits and hits[0]["ref_id"] == "c_yunqi_a"
    # 直接用工具解析：精确匹配不到，语义兜底命中
    from app.sessions.store import SessionState
    from app.sessions.tree import SessionTree
    from app.skills.registry import ToolContext
    from app.skills.tools import _resolve_customer

    settings = make_settings(tmp_path)
    state = SessionState(settings)
    asyncio.run(state.init())
    ctx = ToolContext(
        settings=settings, session_id="s", trace_id="t", user_id="sales-001",
        db=db, tree=SessionTree(db), state=state, knowledge=knowledge,
    )
    customer, error = _resolve_customer(ctx, "星河数据云")
    assert error is None and customer and customer["customer_id"] == "c_yunqi_a"