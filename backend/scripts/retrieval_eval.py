"""检索质量 A/B：同一批标注查询，跑 vector（v0.8）与 hybrid（v0.10）两套打分。

为什么要有这个脚本：docs/10 §5 写着「Embedding 是 n-gram，同义改写召不回」——
这是一句自我批评，但没有数字，也就没法证明「改完之后好了多少」。
这里把「改之前到底差在哪」变成可复现的表格，并给出两个语料规模：
60 篇（标注语料）与 600 篇（叠 540 组合成干扰实体）。规模那一行是关键——
哈希向量的碰撞与长度稀释**随库变大而恶化**，小库上 A/B 打平只说明库太小。

语料是合成的（40 家客户 + 20 家门店，没有真实客户数据可用），查询是手写的，
覆盖 7 类：exact / alias / address / natural / distract / semantic / negative。
其中 semantic 类是**用来量天花板的**：同义改写与错别字两种模式都不过，
留在报表里而不是删掉，否则通过率会虚高。

用法：
    python backend/scripts/retrieval_eval.py
    python backend/scripts/retrieval_eval.py --json backend/evals/retrieval_eval.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters.knowledge import LocalKnowledge  # noqa: E402
from app.domain.db import Database  # noqa: E402
from app.stats import mcnemar_exact, min_detectable_effect, paired_bootstrap_delta, paired_table  # noqa: E402

CORPUS = ROOT / "backend/evals/retrieval_corpus.jsonl"
CASES = ROOT / "backend/evals/retrieval_cases.jsonl"
MIN_SCORE = 0.38      # 线上默认阈值（settings.knowledge_min_score）
AUTOBIND_COVERAGE = 0.5   # 自动绑定要求覆盖率过半（settings.knowledge_autobind_coverage）
TOP_K = 3

DISTRACTOR_CITIES = ("北京", "上海", "杭州", "深圳", "成都", "广州", "武汉", "南京", "西安", "天津", "苏州", "重庆")
DISTRACTOR_BRANDS = (
    "环宇", "恒通", "睿安", "安泰", "新程", "正邦", "远洋", "锦程", "博远", "同辉",
    "启元", "明泽", "德胜", "瑞丰", "金桥", "海川", "宏图", "朗程", "鼎盛", "长风",
)
DISTRACTOR_INDUSTRY = ("制造", "物流", "传媒", "教育", "医疗", "金融", "能源", "零售", "软件", "硬件", "咨询", "建筑")
DISTRACTOR_DISTRICT = ("高新区", "开发区", "老城区", "新城区", "产业园", "科技园")
DISTRACTOR_ROAD = ("建设路", "人民路", "解放路", "工业大道", "创业路", "复兴路", "长江路", "黄河路")


def load_corpus() -> list[dict]:
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def distractors(n_customers: int) -> list[dict]:
    """压测语料：合成「不相干公司 / 门店」把库撑大，不改动标注查询的目标文档。

    为什么需要它：哈希向量的问题（碰撞、长度稀释）**随语料规模放大**，
    在 60 篇的小库里几乎看不出来。真实 CRM 里客户是几万条量级，
    所以必须看规模曲线，否则「A/B 打平」只是因为库太小。
    """
    docs: list[dict] = []
    brands, industries = len(DISTRACTOR_BRANDS), len(DISTRACTOR_INDUSTRY)
    for index in range(n_customers):
        brand = DISTRACTOR_BRANDS[index % brands]
        industry = DISTRACTOR_INDUSTRY[(index // brands) % industries]
        city = DISTRACTOR_CITIES[(index // (brands * industries)) % len(DISTRACTOR_CITIES)]
        name = f"{city}{brand}{industry}有限公司"
        docs.append({
            "doc_id": f"customer:x{index:03d}", "kind": "customer", "ref_id": f"x{index:03d}",
            "title": name, "text": f"{name} {brand}{industry} {brand} {industry}",
        })
        district = DISTRACTOR_DISTRICT[index % len(DISTRACTOR_DISTRICT)]
        road = DISTRACTOR_ROAD[index % len(DISTRACTOR_ROAD)]
        store = f"{brand}{district}店"
        docs.append({
            "doc_id": f"store:x{index:03d}", "kind": "store", "ref_id": f"x{index:03d}",
            "title": store, "text": f"{store} {city}{district}{road}{100 + index}号 {city}",
        })
    return docs


def build(mode: str, extra: int = 0) -> LocalKnowledge:
    workdir = Path(tempfile.mkdtemp(prefix=f"retrieval_{mode}_"))
    db = Database(str(workdir / "knowledge.db"))
    db.init()
    adapter = LocalKnowledge(db, mode=mode)
    for doc in load_corpus() + distractors(extra):
        adapter.upsert(
            doc_id=doc["doc_id"], kind=doc["kind"], ref_id=doc["ref_id"],
            title=doc["title"], text=doc["text"], force=True,
        )
    return adapter


def run(adapter: LocalKnowledge, cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        started = time.perf_counter()
        hits = adapter.search(case["query"], kind=case["kind"], top_k=TOP_K, min_score=MIN_SCORE)
        elapsed = (time.perf_counter() - started) * 1000
        top1 = hits[0]["ref_id"] if hits else None
        hit_ids = [hit["ref_id"] for hit in hits]
        top_coverage = hits[0].get("coverage", 0.0) if hits else 0.0
        if case["expect"] is None:
            ok = not hits                      # 负例：一个都不该召回才算对
            in_top3 = ok
        else:
            ok = top1 == case["expect"]
            in_top3 = case["expect"] in hit_ids
        rows.append(
            {
                "id": case["id"], "query": case["query"], "category": case["category"],
                "expect": case["expect"], "top1": top1, "hits": hit_ids,
                "scores": [hit["score"] for hit in hits],
                "pass": ok, "hit_at_3": in_top3, "latency_ms": round(elapsed, 2),
                "top_coverage": round(float(top_coverage), 4),
                "auto_bind": bool(case["expect"] is None and len(hit_ids) == 1 and top_coverage >= AUTOBIND_COVERAGE),
            }
        )
    return {"rows": rows, "metrics": _metrics(rows)}


def _metrics(rows: list[dict]) -> dict:
    positives = [r for r in rows if r["expect"] is not None]
    negatives = [r for r in rows if r["expect"] is None]
    recall1 = sum(1 for r in positives if r["pass"]) / len(positives) if positives else 0.0
    recall3 = sum(1 for r in positives if r["hit_at_3"]) / len(positives) if positives else 0.0
    mrr = 0.0
    for row in positives:
        if row["expect"] in row["hits"]:
            mrr += 1.0 / (row["hits"].index(row["expect"]) + 1)
    mrr = mrr / len(positives) if positives else 0.0
    false_positive = sum(1 for r in negatives if r["hits"]) / len(negatives) if negatives else 0.0
    # 「有一个就不许」是苛刻口径；产品上真正危险的是**恰好一条命中**——
    # 调用方会把它当成唯一实体直接绑定。多条命中会走候选卡，人还要点一下。
    auto_bind = sum(1 for r in negatives if r["auto_bind"]) / len(negatives) if negatives else 0.0
    by_category: dict[str, dict] = {}
    for row in rows:
        bucket = by_category.setdefault(row["category"], {"n": 0, "pass": 0})
        bucket["n"] += 1
        bucket["pass"] += 1 if row["pass"] else 0
    for bucket in by_category.values():
        bucket["rate"] = round(bucket["pass"] / bucket["n"], 4)
    return {
        "n_queries": len(rows),
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "recall_at_1": round(recall1, 4),
        "recall_at_3": round(recall3, 4),
        "mrr": round(mrr, 4),
        "negative_false_positive_rate": round(false_positive, 4),
        "negative_auto_bind_rate": round(auto_bind, 4),
        "latency_p50_ms": round(statistics.median(r["latency_ms"] for r in rows), 2),
        "by_category": by_category,
    }


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def _paired(vector: dict, hybrid: dict) -> dict:
    """两组逐用例结果 → 2×2 表 + McNemar p + 配对 bootstrap 差值区间。"""
    a = [1 if row["pass"] else 0 for row in vector["rows"]]
    b = [1 if row["pass"] else 0 for row in hybrid["rows"]]
    matrix = paired_table(a, b)
    delta, low, high = paired_bootstrap_delta(a, b)
    return {
        "table": matrix,
        "mcnemar_p": mcnemar_exact(matrix["only_b"], matrix["only_a"]),
        "delta": delta, "delta_ci": [low, high],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="", help="把结果写成 JSON（含成对检验）")
    parser.add_argument("--scale", type=int, default=540, help="额外注入多少组「客户+门店」合成压测实体（0 = 只用标注语料）")
    args = parser.parse_args()

    cases = load_cases()
    scales = [0] if args.scale <= 0 else [0, args.scale]
    table: list[dict] = []
    for extra in scales:
        table.append({
            "distractor_pairs": extra,
            "docs": len(load_corpus()) + extra * 2,
            "vector": run(build("vector", extra), cases),
            "hybrid": run(build("hybrid", extra), cases),
        })

    print(f"\n标注集：{len(cases)} 条查询 / 阈值 {MIN_SCORE} / top{TOP_K}\n")
    header = (
        f"{'语料规模':<10}{'模式':<16}{'Recall@1':>10}{'Recall@3':>10}{'MRR':>8}"
        f"{'负例误召回':>12}{'错绑定':>8}{'P50 延迟':>11}"
    )
    print(header)
    print("-" * 100)
    for row in table:
        for name, result in (("vector (v0.8)", row["vector"]), ("hybrid (v0.10)", row["hybrid"])):
            m = result["metrics"]
            print(
                f"{str(row['docs']) + ' 篇':<10}{name:<16}{_pct(m['recall_at_1']):>10}"
                f"{_pct(m['recall_at_3']):>10}{m['mrr']:>8.3f}"
                f"{_pct(m['negative_false_positive_rate']):>12}"
                f"{_pct(m['negative_auto_bind_rate']):>8}{m['latency_p50_ms']:>9.2f}ms"
            )
        print()

    vector, hybrid = table[-1]["vector"], table[-1]["hybrid"]
    print("分类型（最大规模下，通过 / 总数）：")
    for category in sorted(vector["metrics"]["by_category"]):
        v = vector["metrics"]["by_category"][category]
        h = hybrid["metrics"]["by_category"][category]
        print(f"  {category:<10} vector {v['pass']:>2}/{v['n']:<3} hybrid {h['pass']:>2}/{h['n']:<3}")

    a = [1 if row["pass"] else 0 for row in vector["rows"]]
    b = [1 if row["pass"] else 0 for row in hybrid["rows"]]
    matrix = paired_table(a, b)
    p_value = mcnemar_exact(matrix["only_b"], matrix["only_a"])   # 只计不一致对
    delta, low, high = paired_bootstrap_delta(a, b)
    print(
        f"\n成对比较（hybrid − vector，最大规模）：Δ {delta:+.4f}，95% CI [{low:+.4f}, {high:+.4f}]；"
        f"不一致对 {matrix['only_b']} vs {matrix['only_a']}，McNemar p = {p_value:.2e}"
    )
    print(f"该样本量（n={len(cases)}）在 p≈0.85 时能检出的最小差异 ≈ {min_detectable_effect(len(cases)):.1%}（绝对）")

    if args.json:
        payload = {
            "cases": len(cases),
            "min_score": MIN_SCORE,
            "top_k": TOP_K,
            "scales": [
                {
                    "docs": row["docs"], "distractor_pairs": row["distractor_pairs"],
                    "vector": row["vector"]["metrics"], "hybrid": row["hybrid"]["metrics"],
                    "paired": _paired(row["vector"], row["hybrid"]),
                }
                for row in table
            ],
            "vector": {**vector["metrics"], "rows": vector["rows"]},
            "hybrid": {**hybrid["metrics"], "rows": hybrid["rows"]},
            "paired": {
                "table": matrix, "mcnemar_p": p_value,
                "delta": delta, "delta_ci": [low, high],
                "mde": min_detectable_effect(len(cases)),
            },
            "per_case": [
                {"id": left["id"], "vector": left["pass"], "hybrid": right["pass"]}
                for left, right in zip(vector["rows"], hybrid["rows"])
            ],
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    # 门禁：hybrid 不能比 vector 差（差就直接红，不接受「差不多」）
    return 0 if hybrid["metrics"]["recall_at_1"] >= vector["metrics"]["recall_at_1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
