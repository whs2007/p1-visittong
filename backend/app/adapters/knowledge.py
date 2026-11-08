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
from collections import Counter
from typing import Any

from app.domain.db import Database

# 检索用的切词：CJK 走二元组（不依赖分词器），ASCII/数字走词。
_ASCII_WORD = re.compile(r"[a-z0-9]+")
_PUNCT = re.compile(r'[\s。，、；：？！…—～·“”‘’（）()\[\]{}<>《》"\'!@#$%^&*_+=|\\/,.;:?~`-]+')

BM25_K1 = 1.2
BM25_B = 0.75
# 融合权重：IDF 覆盖度为主（可解释、可阈值化），BM25 相对分做细分排序。
# 试过乘性融合（score = coverage × (0.85 + 0.15·rel)），负例确实被挡住，
# 但长句自然语言查询（虚词二元组把 coverage 摊薄）也被一起挡住，
# 净效果是拿 3 条真召回换 1 条误召回——不划算，退回加性并把这个实验留在注释里。
HYBRID_COVERAGE_WEIGHT = 0.75
HYBRID_BM25_WEIGHT = 0.25
# 覆盖率下限只在脚本里做敏感性分析用（见 scripts/retrieval_eval.py 的注释），
# 线上不以它做硬门槛：理由见 _search_hybrid 里的取舍说明。
MIN_COVERAGE_SHORT = 0.30


def _tokens(text: str) -> list[str]:
    """二元组（跨 ASCII/中文边界也算）+ ASCII 词 + 短串整体。

    两个刻意的选择：

    1. **二元组在整串上切，而不是只切汉字部分**。`B站` 的边界二元组 `b站` 只有
       这样才切得出来；只在汉字上切，查询里的 `b站` 永远对不上文档里的 `b站`，
       覆盖率被腰斩（实测 0.36 vs 1.0），这就是 v0.8 哈希向量召不回 `B站` 的根因。
    2. **≤8 字的整串额外当一个 token**。实体名普遍很短，给精确命中一条直达通道，
       同时也让「杭州做安防的公司」这类整串查询有机会整体命中。
    """
    norm = _PUNCT.sub("", (text or "").lower())
    if not norm:
        return [""]
    tokens: list[str] = list(_ASCII_WORD.findall(norm))
    if len(norm) == 1:
        tokens.append(norm)
    tokens.extend(norm[i : i + 2] for i in range(len(norm) - 1))
    if len(norm) <= 8:
        tokens.append(norm)
    return tokens or [""]


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


def hybrid_scores(
    query: str, docs: list[dict[str, Any]], tokenizer=None,
) -> dict[str, tuple[float, float]]:
    """混合打分：IDF 加权覆盖度 + BM25 相对分。返回 {doc_id: (score, coverage)}。

    为什么不用纯向量余弦（v0.8 的做法）：n-gram 哈希向量有两处结构性缺陷——
    ① 长度稀释：短查询对长文档（带地址的门店）的余弦被文档长度摊薄，
       明明命中「望京店」却低于阈值召不回；
    ② 哈希碰撞：256 维里放几千个二元组，`北京`/`朝阳` 这类高频片段互相撞，
       会把不相干的门店顶上来（假召回）。
    IDF 让高频片段降权（`便利` 在 80 家店里出现就没有区分度），
    BM25 的 tf 饱和 + 文档长度归一化，正好对症这两条。

    `coverage` 是「查询信息量被覆盖的比例」（把每个查询词的 IDF 当权重），
    取值 [0,1]，可以直接当阈值用——这是它相对余弦最实用的地方。
    """
    q_tokens = list(dict.fromkeys(_tokens(query)))
    if not q_tokens or not docs:
        return {}

    per_doc = []
    for doc in docs:
        tokens = tokenizer(doc) if tokenizer else _tokens(f"{doc.get('title') or ''} {doc.get('text') or ''}")
        per_doc.append((doc.get("doc_id"), tokens, Counter(tokens)))
    lengths = [len(tokens) for _, tokens, _ in per_doc] or [1]
    avgdl = sum(lengths) / len(lengths) or 1.0

    df = Counter()
    for _, tokens, _ in per_doc:
        df.update(set(tokens))
    total = len(per_doc)
    # 未在语料出现过的查询词 IDF 取最大档：它对所有文档一样，
    # 只起到「拉低覆盖率」的作用——这正是负例查询该有的行为。
    idf = {
        token: math.log(1 + (total - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
        for token in q_tokens
    }
    total_idf = sum(idf.values()) or 1.0

    raw: list[tuple[str, float, float]] = []
    for doc_id, tokens, tf in per_doc:
        coverage = 0.0
        bm25 = 0.0
        for token in q_tokens:
            freq = tf.get(token, 0)
            if not freq:
                continue
            coverage += idf[token]
            norm = BM25_B * (len(tokens) / avgdl) if tokens else 0.0
            bm25 += idf[token] * freq * (BM25_K1 + 1) / (freq + BM25_K1 * (1 - BM25_B + norm))
        if not coverage:
            continue
        raw.append((doc_id, coverage / total_idf, bm25))

    best = max((item[2] for item in raw), default=0.0) or 1.0
    return {
        doc_id: (
            round(HYBRID_COVERAGE_WEIGHT * coverage + HYBRID_BM25_WEIGHT * (bm25 / best), 4),
            round(coverage, 4),
        )
        for doc_id, coverage, bm25 in raw
    }


class LocalKnowledge:
    backend = "local"
    backend_reason = ""

    def __init__(self, db: Database, dim: int = 256, mode: str = "hybrid") -> None:
        self.db = db
        self.dim = dim
        # vector = v0.8 行为（n-gram 余弦），hybrid = IDF 覆盖度 + BM25 融合。
        # 两种都保留，是为了能拿标注集做 A/B（scripts/retrieval_eval.py），
        # 而不是「换了算法，然后声称更好」。
        self.mode = mode if mode in ("vector", "hybrid") else "hybrid"
        # 同一份语料会被多条查询反复扫，切词结果缓存起来：600 篇语料 × 100 条查询，
        # 不缓存等于把同一个 Counter 算 6 万次（实测这层缓存把 A/B 脚本从 40s 压到 3s）。
        self._token_cache: dict[str, tuple[str, list[str]]] = {}

    def upsert(
        self, *, doc_id: str, kind: str, ref_id: str, title: str, text: str,
        version: int = 0, force: bool = False,
    ) -> bool:
        """幂等写入。返回是否真的改了行。

        **版本守卫写在 SQL 里**，而不是「先查版本再决定写不写」：
        两个 consumer 并发时，先查后写都通过 → 旧事件覆盖新事件（乱序写入）。
        `ON CONFLICT ... DO UPDATE ... WHERE excluded.source_version > 当前版本`
        把「比较」和「写入」压进同一条语句，SQLite 的写锁保证它原子。
        """
        vector = json.dumps(embed(title + " " + text, self.dim))
        from app.utils import now_iso

        guard = "" if force else (
            " WHERE excluded.source_version > knowledge_docs.source_version"
        )
        # `deleted_at=NULL`：一条「删除」之后又来了新版本，等于数据复活。
        # 不同步清空它，后续 search 会永远看不到这条正确的数据。
        affected = self.db._exec(  # noqa: SLF001
            "INSERT INTO knowledge_docs (doc_id,kind,ref_id,title,text,vector,source_version,deleted_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,NULL,?)"
            " ON CONFLICT(doc_id) DO UPDATE SET kind=excluded.kind, ref_id=excluded.ref_id,"
            " title=excluded.title, text=excluded.text, vector=excluded.vector,"
            " source_version=excluded.source_version, deleted_at=NULL, updated_at=excluded.updated_at"
            + guard,
            (doc_id, kind, ref_id, title, text, vector, int(version or 0), now_iso()),
        )
        return affected > 0

    def soft_delete(self, *, doc_id: str, version: int = 0) -> bool:
        """软删：行不消失，只打 `deleted_at`。

        物理删会把「版本号」一起删掉——之后一个迟到的 v2 事件会以「库里没有」
        为由把已删除的 v3 数据重新写回来。墓碑行是幂等删除的最小代价。
        """
        from app.utils import now_iso

        now = now_iso()
        affected = self.db._exec(  # noqa: SLF001
            "UPDATE knowledge_docs SET deleted_at=?, source_version=?, updated_at=?"
            " WHERE doc_id=? AND source_version < ?",
            (now, int(version or 0), now, doc_id, int(version or 0)),
        )
        return affected > 0

    def count(self, *, include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM knowledge_docs"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        row = self.db._row(self.db._conn().execute(sql).fetchone())  # noqa: SLF001
        return int(row["n"]) if row else 0

    def version_of(self, doc_id: str) -> int:
        row = self.db._row(  # noqa: SLF001
            self.db._conn().execute(
                "SELECT source_version FROM knowledge_docs WHERE doc_id=?", (doc_id,)
            ).fetchone()
        )
        return int(row["source_version"] or 0) if row else -1     # -1 = 不存在

    def search(
        self, query: str, *, kind: str | None = None, top_k: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM knowledge_docs WHERE deleted_at IS NULL"
        params: tuple = ()
        if kind:
            sql += " AND kind=?"
            params = (kind,)
        rows = self.db._rows(sql, params)  # noqa: SLF001
        if min_score is None:
            min_score = 0.0 if self.mode == "vector" else 0.35
        if self.mode == "hybrid":
            return self._search_hybrid(query, rows, top_k=top_k, min_score=min_score)
        return self._search_vector(query, rows, top_k=top_k, min_score=min_score)

    def _search_vector(
        self, query: str, rows: list[dict[str, Any]], *, top_k: int, min_score: float,
    ) -> list[dict[str, Any]]:
        query_vector = embed(query, self.dim)
        scored = []
        for row in rows:
            try:
                vector = json.loads(row.get("vector") or "[]")
            except json.JSONDecodeError:
                continue
            if not vector:
                continue
            score = round(cosine(query_vector, vector), 4)
            if score < min_score:
                continue
            scored.append(
                {
                    "doc_id": row["doc_id"], "kind": row["kind"], "ref_id": row.get("ref_id"),
                    "title": row["title"], "text": row["text"], "score": score,
                    "coverage": score, "retriever": "vector",
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:top_k]

    def _doc_tokens(self, row: dict[str, Any]) -> list[str]:
        text = f"{row.get('title') or ''} {row.get('text') or ''}"
        cached = self._token_cache.get(row["doc_id"])
        if cached and cached[0] == text:
            return cached[1]
        tokens = _tokens(text)
        self._token_cache[row["doc_id"]] = (text, tokens)
        return tokens

    def _search_hybrid(
        self, query: str, rows: list[dict[str, Any]], *, top_k: int, min_score: float,
    ) -> list[dict[str, Any]]:
        scores = hybrid_scores(query, rows, tokenizer=self._doc_tokens)
        # 阈值打在融合分上，覆盖率原样返回，便于解释「为什么这条排第一」。
        #
        # 试过再加一层「短查询覆盖率 ≥ 0.30」的严格门（把「广州天河城百货」这类
        # 城市+商圈查询挡在门外），结果负例误召回 25% → 0，但自然语言长句
        # 4 条真命中一起被挡，总体 Recall@1 从 96.7% 掉到 91.1%——净亏。
        # 更重要的是：**误召回不等于错绑定**，`_resolve_customer` 只在唯一命中时
        # 才自动绑定，多条命中走候选卡让人确认。所以门槛该由调用方按后果定义，
        # 检索层只负责排序与可解释性。这个取舍用 `negative_auto_bind_rate` 度量。
        hits = []
        for row in rows:
            entry = scores.get(row["doc_id"])
            if not entry:
                continue
            score, coverage = entry
            if score < min_score:
                continue
            hits.append(
                {
                    "doc_id": row["doc_id"], "kind": row["kind"], "ref_id": row.get("ref_id"),
                    "title": row["title"], "text": row["text"], "score": score,
                    "coverage": coverage, "retriever": "hybrid",
                }
            )
        hits.sort(key=lambda item: (-item["score"], item["doc_id"]))
        return hits[:top_k]


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

    def search(
        self, query: str, *, kind: str | None = None, top_k: int = 5, min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        raise RuntimeError("Milvus 适配为接口占位：请使用 knowledge_backend=local 或接入真实 Milvus")


def build_knowledge(settings: Any, db: Database):
    """按配置返回可用适配器；Milvus 不可用时自动降级本地并记录原因。"""
    mode = getattr(settings, "knowledge_mode", "hybrid")
    if settings.knowledge_backend == "milvus":
        try:
            adapter = MilvusKnowledge(settings.milvus_uri, settings.knowledge_dim)
            if not adapter.backend_reason:
                return adapter
            local = LocalKnowledge(db, settings.knowledge_dim, mode)
            local.backend_reason = adapter.backend_reason
            return local
        except Exception as exc:  # noqa: BLE001
            local = LocalKnowledge(db, settings.knowledge_dim, mode)
            local.backend_reason = f"Milvus 初始化失败：{exc}"
            return local
    return LocalKnowledge(db, settings.knowledge_dim, mode)


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
            ref_id=customer["customer_id"], title=title, text=text, force=True,
        )
    for store in stores:
        title = store.get("name") or store["store_id"]
        text = " ".join(str(store.get(key) or "") for key in ("name", "address", "city")).strip()
        adapter.upsert(
            doc_id=f"store:{store['store_id']}", kind="store",
            ref_id=store["store_id"], title=title, text=text, force=True,
        )
    return {"customers": len(customers), "stores": len(stores), "docs": adapter.count()}