"""知识检索适配器：本地向量索引（零依赖）+ Milvus 生产接口占位。

对齐模板链路：Hive 全量/增量 → Kafka → consumer 异步 embedding → 向量库；
个人项目用「确定性 hash-gram embedding + SQLite 余弦检索」实现本地等价物，
`MilvusKnowledge` 保留与真实 Milvus 相同的方法签名（upsert/search），
生产只需实现这两个方法并把 `knowledge_backend` 切到 milvus。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from app.domain.db import Database


def embed(text: str, dim: int = 256) -> list[float]:
    """确定性字符 n-gram 哈希向量（无需模型/网络，语义近似靠字面片段）。"""
    normalized = re.sub(r"\s+", "", (text or "").lower())
    grams = list(normalized) or [""]
    if len(normalized) > 1:
        grams += [normalized[i : i + 2] for i in range(len(normalized) - 1)]
    vector = [0.0] * dim
    for gram in grams:
        digest = hashlib.md5(gram.encode("utf-8")).hexdigest()
        index = int(digest[:8], 16) % dim
        vector[index] += 1.0 if int(digest[8:10], 16) % 2 == 0 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def cosine(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


class LocalKnowledge:
    backend = "local"
    backend_reason = ""

    def __init__(self, db: Database, dim: int = 256) -> None:
        self.db = db
        self.dim = dim

    def upsert(self, *, doc_id: str, kind: str, ref_id: str, title: str, text: str) -> None:
        vector = json.dumps(embed(title + " " + text, self.dim))
        from app.utils import now_iso

        self.db._exec(  # noqa: SLF001
            "INSERT INTO knowledge_docs (doc_id,kind,ref_id,title,text,vector,updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(doc_id) DO UPDATE SET kind=excluded.kind, ref_id=excluded.ref_id,"
            " title=excluded.title, text=excluded.text, vector=excluded.vector, updated_at=excluded.updated_at",
            (doc_id, kind, ref_id, title, text, vector, now_iso()),
        )

    def count(self) -> int:
        row = self.db._row(self.db._conn().execute("SELECT COUNT(*) AS n FROM knowledge_docs").fetchone())  # noqa: SLF001
        return int(row["n"]) if row else 0

    def search(self, query: str, *, kind: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        sql = "SELECT * FROM knowledge_docs"
        params: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        rows = self.db._rows(sql, params)  # noqa: SLF001
        query_vector = embed(query, self.dim)
        scored = []
        for row in rows:
            try:
                vector = json.loads(row.get("vector") or "[]")
            except json.JSONDecodeError:
                continue
            if not vector:
                continue
            scored.append(
                {
                    "doc_id": row["doc_id"], "kind": row["kind"], "ref_id": row.get("ref_id"),
                    "title": row["title"], "text": row["text"],
                    "score": round(cosine(query_vector, vector), 4),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:top_k]


class MilvusKnowledge:
    """生产实现占位：方法签名与 LocalKnowledge 一致，部署时替换内部实现。"""

    backend = "milvus"

    def __init__(self, uri: str, dim: int = 256) -> None:
        self.uri = uri
        self.dim = dim
        self.backend_reason = ""
        try:
            import pymilvus  # type: ignore  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self.backend_reason = f"未安装 pymilvus：{exc}"

    def upsert(self, **_: Any) -> None:
        raise RuntimeError("Milvus 适配为接口占位：请安装 pymilvus 并按 docs 接入真实 collection")

    def count(self) -> int:
        raise RuntimeError("Milvus 适配为接口占位")

    def search(self, query: str, *, kind: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        raise RuntimeError("Milvus 适配为接口占位：请使用 knowledge_backend=local 或接入真实 Milvus")


def build_knowledge(settings: Any, db: Database):
    """按配置返回可用适配器；Milvus 不可用时自动降级本地并记录原因。"""
    if settings.knowledge_backend == "milvus":
        try:
            adapter = MilvusKnowledge(settings.milvus_uri, settings.knowledge_dim)
            if not adapter.backend_reason:
                return adapter
            local = LocalKnowledge(db, settings.knowledge_dim)
            local.backend_reason = adapter.backend_reason
            return local
        except Exception as exc:  # noqa: BLE001
            local = LocalKnowledge(db, settings.knowledge_dim)
            local.backend_reason = f"Milvus 初始化失败：{exc}"
            return local
    return LocalKnowledge(db, settings.knowledge_dim)


def sync_knowledge(db: Database, adapter: Any) -> dict[str, Any]:
    """把客户/门店同步成知识文档（模拟 Hive 全量刷新后的 consumer 写入）。"""
    if adapter.backend == "milvus":
        raise RuntimeError("Milvus 适配为接口占位，sync 请使用 local")
    customers = db._rows("SELECT * FROM customers")  # noqa: SLF001
    stores = db._rows("SELECT * FROM stores")  # noqa: SLF001
    for customer in customers:
        title = customer.get("name") or customer["customer_id"]
        text = " ".join(
            str(customer.get(key) or "") for key in ("name", "alias", "brand", "industry", "level")
        ).strip()
        adapter.upsert(
            doc_id=f"customer:{customer['customer_id']}", kind="customer",
            ref_id=customer["customer_id"], title=title, text=text,
        )
    for store in stores:
        title = store.get("name") or store["store_id"]
        text = " ".join(str(store.get(key) or "") for key in ("name", "address", "city")).strip()
        adapter.upsert(
            doc_id=f"store:{store['store_id']}", kind="store",
            ref_id=store["store_id"], title=title, text=text,
        )
    return {"customers": len(customers), "stores": len(stores), "docs": adapter.count()}