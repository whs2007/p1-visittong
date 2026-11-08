"""知识同步链路：版本守卫 / 幂等 Upsert / 重试 / DLQ 重放（v0.9）。

链路的每一条性质都要能被单独推翻——否则「异步同步」只是个说法：
- 重复投递：第 2、3 次必须被判 stale，而不是把同一份数据再写一遍；
- 乱序到达：v2 晚于 v3 到，库里的内容必须还是 v3；
- 删除后回流：delete(v4) 之后来 upsert(v3)，不能把数据复活；
- 处理失败：重试有上界，超了进 DLQ，并且**原始事件不能算处理成功**；
- 死信重放：重放要重新走完整链路，成功后 DLQ 状态是 replayed。
"""
from __future__ import annotations

import app.skills.tools  # noqa: F401
import pytest
from app.adapters.knowledge import LocalKnowledge
from app.config import Settings
from app.domain.db import Database
from app.domain.knowledge_sync import KnowledgeSync
from app.domain.pipeline import EventPipeline
from app.main import create_app
from fastapi.testclient import TestClient


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(tmp_path / "sync.db"),
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        auth_mode="strict",
    )
    base.update(overrides)
    return Settings(**base)


def _login(client: TestClient, user: str, password: str) -> dict[str, str]:
    token = client.post("/api/auth/login", json={"user_id": user, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _stack(tmp_path, *, max_retries: int = 2) -> tuple[Database, LocalKnowledge, KnowledgeSync]:
    db = Database(str(tmp_path / "sync.db"))
    db.init()
    knowledge = LocalKnowledge(db)
    pipeline = EventPipeline(db)
    return db, knowledge, KnowledgeSync(db, knowledge, pipeline, max_retries=max_retries)


def _doc(knowledge: LocalKnowledge, doc_id: str = "customer:c_demo") -> dict:
    rows = knowledge.db._rows(  # noqa: SLF001
        "SELECT doc_id,text,source_version,deleted_at FROM knowledge_docs WHERE doc_id=?", (doc_id,),
    )
    return rows[0]


def test_duplicate_delivery_writes_once(tmp_path) -> None:
    _db, knowledge, sync = _stack(tmp_path)
    for _ in range(3):
        sync.emit(doc_id="customer:c_demo", title="demo", text="第一版", version=1)
    stats = sync.drain()
    assert stats["processed"] == 1
    assert stats["stale"] == 2          # 重复投递被版本守卫挡掉
    assert _doc(knowledge)["source_version"] == 1
    assert knowledge.count() == 1


def test_out_of_order_event_cannot_overwrite_newer(tmp_path) -> None:
    _db, knowledge, sync = _stack(tmp_path)
    sync.emit(doc_id="customer:c_demo", title="demo", text="第三版", version=3)
    sync.drain()
    sync.emit(doc_id="customer:c_demo", title="demo", text="第二版", version=2)
    stats = sync.drain()
    assert stats["stale"] == 1
    assert _doc(knowledge)["text"] == "第三版"
    assert _doc(knowledge)["source_version"] == 3


def test_sql_guard_rejects_lower_version_even_without_consumer(tmp_path) -> None:
    """绕过 consumer 直接写也必须被挡：守卫在 SQL 里，不在调用方。"""
    _db, knowledge, _sync = _stack(tmp_path)
    assert knowledge.upsert(doc_id="customer:x", kind="customer", ref_id="x", title="x", text="v5", version=5) is True
    assert knowledge.upsert(doc_id="customer:x", kind="customer", ref_id="x", title="x", text="v4", version=4) is False
    assert _doc(knowledge, "customer:x")["text"] == "v5"


def test_delete_is_versioned_and_search_excludes_it(tmp_path) -> None:
    _db, knowledge, sync = _stack(tmp_path)
    sync.emit(doc_id="customer:c_demo", title="望京便利店", text="望京便利店 朝阳", version=1)
    sync.drain()
    assert knowledge.search("望京便利店", top_k=1)

    sync.emit(doc_id="customer:c_demo", op="delete", version=4)
    sync.drain()
    assert knowledge.search("望京便利店", top_k=1) == []
    assert _doc(knowledge)["deleted_at"]      # 墓碑在，版本号也还在

    # 迟到的旧事件不能复活已删除数据
    sync.emit(doc_id="customer:c_demo", title="望京便利店", text="旧数据", version=3)
    assert sync.drain()["stale"] == 1
    assert knowledge.search("望京便利店", top_k=1) == []


def test_failed_event_goes_to_dlq_and_can_be_replayed(tmp_path, monkeypatch) -> None:
    _db, knowledge, sync = _stack(tmp_path, max_retries=2)
    original = knowledge.upsert
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("embedding service unavailable")
        return original(**kwargs)

    monkeypatch.setattr(knowledge, "upsert", flaky)
    event_id = sync.emit(doc_id="customer:c_demo", title="demo", text="重试后成功", version=1)
    stats = sync.drain()
    assert stats["retried"] == 2          # 前两次失败被重试
    assert stats["processed"] == 1
    assert _doc(knowledge)["text"] == "重试后成功"
    assert sync.list_dlq() == []          # 第三次成功，没有进 DLQ
    assert event_id


def test_exhausted_retries_land_in_dlq_then_replay(tmp_path, monkeypatch) -> None:
    _db, knowledge, sync = _stack(tmp_path, max_retries=2)
    boom = {"on": True}
    original = knowledge.upsert

    def broken(**kwargs):
        if boom["on"]:
            raise RuntimeError("milvus timeout")
        return original(**kwargs)

    monkeypatch.setattr(knowledge, "upsert", broken)
    sync.emit(doc_id="customer:c_demo", title="demo", text="重放成功", version=7)
    stats = sync.drain()
    assert stats["dead"] == 1
    assert stats["retried"] == 2          # max_retries=2 → 1 次首发 + 2 次重试
    dead = sync.list_dlq()
    assert len(dead) == 1 and dead[0]["reason"].startswith("RuntimeError")
    assert sync.stats()["events"]["dead"] == 1

    boom["on"] = False
    result = sync.replay(dead[0]["id"])
    assert result["result"] == "requeued"
    assert sync.stats()["dlq"]["replayed"] == 1
    # 重放只是重新入队；真正写回要靠 consumer 再跑一次（不做「直接写库」的捷径）
    assert sync.drain()["processed"] == 1
    assert _doc(knowledge)["text"] == "重放成功"
    assert _doc(knowledge)["source_version"] == 7


def test_invalid_op_lands_in_dlq(tmp_path) -> None:
    _db, _knowledge, sync = _stack(tmp_path)
    sync.emit(doc_id="customer:c_demo", title="demo", version=2, op="truncate")
    stats = sync.drain()
    assert stats["dead"] == 1
    assert "unsupported_op" in sync.list_dlq()[0]["reason"]


def test_api_requires_manager_and_exposes_state(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        manager = _login(client, "manager-001", ctx.settings.demo_password)
        sales = _login(client, "sales-001", ctx.settings.demo_password)

        assert client.post("/api/knowledge/events", json={"doc_id": "customer:x"},
                           headers=sales).status_code == 403
        created = client.post(
            "/api/knowledge/events",
            json={"doc_id": "customer:c_byte", "title": "字节跳动", "text": "字节跳动 北京", "version": 9},
            headers=manager,
        )
        assert created.status_code == 200
        drained = client.post("/api/knowledge/sync/drain", headers=manager)
        assert drained.status_code == 200
        assert drained.json()["stats"]["processed"] >= 1
        state = client.get("/api/knowledge/sync/status", headers=manager).json()["state"]
        assert state["docs"]["active"] >= 1
        assert client.get("/api/knowledge/dlq", headers=manager).status_code == 200
        assert client.post("/api/knowledge/dlq/999999/replay", headers=manager).status_code == 404


@pytest.mark.parametrize("version", [1, 2, 10])
def test_version_zero_baseline_ignores_newer_only(tmp_path, version: int) -> None:
    """版本 0 是「全量刷新」的基座：比它大的事件能覆盖，比它小的不能。"""
    _db, knowledge, sync = _stack(tmp_path)
    knowledge.upsert(doc_id="customer:z", kind="customer", ref_id="z", title="z", text="baseline", version=0)
    sync.emit(doc_id="customer:z", title="z", text=f"v{version}", version=version)
    sync.drain()
    expected = f"v{version}" if version > 0 else "baseline"
    assert _doc(knowledge, "customer:z")["text"] == expected
