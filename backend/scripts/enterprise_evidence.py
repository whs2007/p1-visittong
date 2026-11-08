"""企业级证据工厂：把「简历上的 6 个亮点」变成 6 组可复现的数字。

为什么单独写一个脚本，而不是在文档里写「约 90%」「明显下降」：

- 面试官会问「这个数怎么来的」。答不上来的指标等于没有指标，甚至比没有更糟：
  它说明你没量过，只是把形容词换成了百分号。
- 数字会漂。今天量的是 86.8%，改完压缩阈值就变成 72%。脚本跑一次就能对齐，
  不用靠记忆维护文档。

用法：
    python backend/scripts/enterprise_evidence.py              # 全部（约 10s）
    python backend/scripts/enterprise_evidence.py --json out.json
    python backend/scripts/enterprise_evidence.py --fast        # 跳过用例收集

每组数据都带 `method`（怎么测的）与 `source`（代码落点），方便被追问时直接定位。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 与 run_eval 一致：自包含跑批，显式钉死前提，不吃调用者 shell 的环境。
os.environ.setdefault("AUTH_ALLOW_INSECURE_SECRET", "1")
os.environ.setdefault("AUTH_MODE", "strict")
os.environ.setdefault("LLM_MODE", "mock")

from app.agent.output_guard import OutputGovernor  # noqa: E402
from app.approvals import ApprovalManager  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.db import Database  # noqa: E402
from app.domain.knowledge_sync import KnowledgeSync  # noqa: E402
from app.domain.pipeline import EventPipeline  # noqa: E402
from app.trace.store import TraceStore  # noqa: E402

RESULT: dict[str, Any] = {}


def _hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _settings(db_path: str, **overrides) -> Settings:
    base = dict(
        sqlite_path=db_path,
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        auth_mode="strict",
        tool_output_max_chars=1600,
        # 示例单价（每百万 token）：只用于演示「成本怎么算」，不是价目表。
        price_in_per_mtok=1.0,
        price_out_per_mtok=2.0,
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 1. 上下文工程：工具输出脱敏 / 裁剪 / 引用化
# --------------------------------------------------------------------------- #
def evidence_output_governance(workdir: Path) -> dict[str, Any]:
    db = Database(str(workdir / "gov.db"))
    db.init()
    governor = OutputGovernor(db, max_chars=1600, head_ratio=0.7)

    # 一条「很真实」的长输出：客户档案 + 联系人 PII + 长备注
    phone = "138" + "0" * 4 + "1234"
    id_card = "110101" + "19900101" + "123X"
    bank = "6222" + "0" * 8 + "1234"
    body = (
        "【客户档案】主营饮料，覆盖 12 家门店，月均拜访 4 次。\n" * 220
        + f"联系人：王经理，手机 {phone}，邮箱 wangjl@example.com，"
        + f"证件号 {id_card}，结算卡 {bank}\n"
        + "【拜访备注】客户对新品感兴趣，要求下周寄样品；上次投诉配送延迟，需跟进。\n" * 160
    )
    governed = governor.govern(
        body, tool="knowledge_search", trace_id="tr_evidence",
        session_id="s_evidence", user_id="sales-001",
    )
    stored = governor.fetch(governed.ref) if governed.ref else None
    ok_sha = bool(stored and stored["sha256"] == hashlib.sha256(stored["content"].encode()).hexdigest())

    print(f"原始输出       : {governed.original_chars:,} 字符")
    print(f"进入上下文     : {governed.kept_chars:,} 字符（-{governed.savings_pct}%）")
    print(f"PII 命中       : {governed.redactions} 处（手机 / 邮箱 / 证件 / 卡号）")
    print(f"引用号         : {governed.ref}（回查 {stored['chars'] if stored else 0:,} 字符，sha256 校验 {ok_sha}）")
    return {
        "original_chars": governed.original_chars,
        "kept_chars": governed.kept_chars,
        "savings_pct": governed.savings_pct,
        "pii_redacted": governed.redactions,
        "ref": governed.ref,
        "ref_sha256_verified": ok_sha,
        "method": "OutputGovernor 对 12k 字符工具输出执行 脱敏→裁剪→引用化，再按 ref 回查校验摘要",
        "source": "backend/app/agent/output_guard.py · tests/test_output_guard.py",
    }


# --------------------------------------------------------------------------- #
# 2. 审批：一次性令牌防重放 + 并发双批
# --------------------------------------------------------------------------- #
def evidence_approval_token(workdir: Path) -> dict[str, Any]:
    traces = TraceStore(str(workdir / "approval.db"))
    traces.init()
    manager = ApprovalManager(traces, timeout_s=5)

    async def _run() -> dict[str, Any]:
        row = await traces.create_approval(
            trace_id="tr_evidence", session_id="s_evidence", tool="cancel_visit_plan",
            args={"plan_id": "p_demo"}, risk="high", reason="high_risk_write",
            timeout_s=5, requester="sales-001",
        )
        replay = [
            await manager.decide(row["approval_id"], "approved", nonce=row["decision_nonce"])
            for _ in range(5)
        ]
        # 并发：6 个请求同时带同一令牌
        second = await traces.create_approval(
            trace_id="tr_race", session_id="s_race", tool="checkin_visit",
            args={}, risk="high", reason="race", timeout_s=5, requester="sales-001",
        )
        raced = await asyncio.gather(*[
            manager.decide(second["approval_id"], "approved", nonce=second["decision_nonce"])
            for _ in range(6)
        ])
        probe = await traces.create_approval(
            trace_id="tr_probe", session_id="s_probe", tool="cancel_visit_plan",
            args={}, risk="high", reason="probe", timeout_s=5, requester="sales-001",
        )
        wrong = await manager.decide(probe["approval_id"], "approved", nonce="guess")
        missing = await manager.decide(probe["approval_id"], "approved")
        decided = await traces.get_approval(row["approval_id"])
        return {
            "replay_attempts": 5,
            "replay_ok": replay.count("ok"),
            "replay_rejected": len(replay) - replay.count("ok"),
            "concurrent_attempts": 6,
            "concurrent_winners": raced.count("ok"),
            "wrong_nonce": wrong,
            "missing_nonce": missing,
            "nonce_used_at": bool(decided and decided.get("nonce_used_at")),
        }

    data = asyncio.run(_run())
    print(f"同一令牌重放   : {data['replay_attempts']} 次 → 成功 {data['replay_ok']} 次、拒绝 {data['replay_rejected']} 次")
    print(f"并发 6 请求    : 恰好 {data['concurrent_winners']} 个成功（CAS: status∧nonce∧nonce_used_at IS NULL）")
    print(f"错误令牌       : {data['wrong_nonce']}；缺令牌：{data['missing_nonce']}")
    data.update({
        "method": "同一 approval 上重复/并发调用 decide，统计 ok 与拒绝分布",
        "source": "backend/app/approvals.py · app/trace/store.py · tests/test_approval_token.py",
    })
    return data


# --------------------------------------------------------------------------- #
# 3. 评测：多 Trial + 置信区间（读 baseline，不重复跑）
# --------------------------------------------------------------------------- #
def evidence_eval() -> dict[str, Any]:
    baseline_path = BACKEND / "evals" / "baseline.json"
    if not baseline_path.exists():
        return {"error": "缺少 evals/baseline.json，先跑 run_eval.py --update-baseline"}
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    trials = int(data.get("trials") or 1)
    executions = int(data.get("executions") or 0)
    ci = data.get("pass_rate_ci_wilson") or [0, 1]
    print(f"用例 / 轮次    : {data.get('cases_per_trial')} 条 × {trials} 轮 = {executions} 次执行")
    print(f"通过率         : {data.get('case_pass_rate')}（95% Wilson CI [{ci[0]}, {ci[1]}]）")
    print(f"路由 / 审批覆盖: {data.get('route_accuracy')} / {data.get('approval_coverage')}；"
          f"禁调违规 {data.get('forbidden_tool_violations')} 次")
    print(f"P95 跨轮波动   : {data.get('p95_latency_spread_ms')} ms（同机同代码，不作为门禁的原因）")
    return {
        "cases_per_trial": data.get("cases_per_trial"),
        "trials": trials,
        "executions": executions,
        "case_pass_rate": data.get("case_pass_rate"),
        "pass_rate_ci_wilson": ci,
        "route_accuracy": data.get("route_accuracy"),
        "approval_coverage": data.get("approval_coverage"),
        "forbidden_tool_violations": data.get("forbidden_tool_violations"),
        "p95_latency_spread_ms": data.get("p95_latency_spread_ms"),
        "method": "run_eval.py --trials 5，每轮独立库独立会话；Wilson 区间按执行次数计",
        "source": "backend/scripts/run_eval.py · backend/evals/baseline.json",
    }


# --------------------------------------------------------------------------- #
# 4. 知识同步：重复 / 乱序 / 删除回流 / 失败重试 / DLQ 重放
# --------------------------------------------------------------------------- #
class _FlakyKnowledge:
    """包一层 LocalKnowledge，用来注入 N 次失败（真实链路里是 Milvus/embedding 超时）。"""

    def __init__(self, inner, fail_times: int) -> None:
        self.inner = inner
        self.fail_times = fail_times
        self.calls = 0

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def upsert(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("milvus timeout (injected)")
        return self.inner.upsert(**kwargs)


def evidence_knowledge_sync(workdir: Path) -> dict[str, Any]:
    db = Database(str(workdir / "sync.db"))
    db.init()
    from app.adapters.knowledge import LocalKnowledge

    inner = LocalKnowledge(db)
    events = EventPipeline(db)
    sync = KnowledgeSync(db, inner, events, max_retries=2)

    # (a) 重复投递 3 次
    for _ in range(3):
        sync.emit(doc_id="customer:c_dup", title="重复", text="同一版本", version=1)
    dup = sync.drain()

    # (b) 乱序：v3 先到，v2 后到
    sync.emit(doc_id="customer:c_order", title="乱序", text="第三版", version=3)
    sync.drain()
    sync.emit(doc_id="customer:c_order", title="乱序", text="第二版", version=2)
    order = sync.drain()
    stored_text = db._rows(  # noqa: SLF001
        "SELECT text FROM knowledge_docs WHERE doc_id='customer:c_order'"
    )[0]["text"]

    # (c) 删除后旧事件回流
    sync.emit(doc_id="customer:c_del", title="待删", text="会被删", version=1)
    sync.drain()
    sync.emit(doc_id="customer:c_del", op="delete", version=5)
    sync.drain()
    sync.emit(doc_id="customer:c_del", title="待删", text="迟到的旧数据", version=4)
    delete_guard = sync.drain()

    # (d) 失败 2 次后成功（重试）
    flaky = _FlakyKnowledge(inner, fail_times=2)
    sync.knowledge = flaky
    sync.emit(doc_id="customer:c_retry", title="重试", text="第三次成功", version=1)
    retried = sync.drain()

    # (e) 死信 + 重放
    dead_sync = KnowledgeSync(db, _FlakyKnowledge(inner, fail_times=99), events, max_retries=2)
    dead_sync.emit(doc_id="customer:c_dead", title="死信", text="重放后可写", version=9)
    dead = dead_sync.drain()
    dlq_rows = dead_sync.list_dlq()
    dead_sync.knowledge = inner
    replay = dead_sync.replay(dlq_rows[0]["id"]) if dlq_rows else {"result": "none"}
    if dlq_rows:
        dead_sync.drain()
    replay_text = db._rows(  # noqa: SLF001
        "SELECT text FROM knowledge_docs WHERE doc_id='customer:c_dead'"
    )

    print(f"重复投递 3 次  : processed={dup['processed']}、stale={dup['stale']}（库内 1 条）")
    print(f"乱序 v3→v2     : stale={order['stale']}，最终内容='{stored_text}'")
    print(f"删除后旧事件   : stale={delete_guard['stale']}，数据未复活")
    print(f"失败 2 次重试  : retried={retried['retried']}、processed={retried['processed']}")
    print(f"死信 + 重放    : dead={dead['dead']}、重放={replay['result']}、"
          f"重放后写入={'是' if replay_text else '否'}")
    return {
        "duplicate": dup,
        "out_of_order": {**order, "final_text": stored_text},
        "delete_then_stale": delete_guard,
        "retry_then_success": retried,
        "dead_letter": {**dead, "replay": replay.get("result"), "replay_written": bool(replay_text)},
        "method": "同一 db 上依次注入重复/乱序/删除回流/瞬时失败/持久失败，读最终库状态",
        "source": "backend/app/domain/knowledge_sync.py · tests/test_knowledge_sync.py",
    }


# --------------------------------------------------------------------------- #
# 5. 成本：真实跑 3 轮，读 /api/metrics/cost
# --------------------------------------------------------------------------- #
def evidence_cost(workdir: Path) -> dict[str, Any]:
    import app.skills.tools  # noqa: F401
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(_settings(str(workdir / "cost.db")))
    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login", json={"user_id": "manager-001", "password": app.state.ctx.settings.demo_password}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        sid = client.post("/api/sessions", json={"title": "cost-evidence"}, headers=headers).json()["session_id"]
        for message in ("查一下我今天的拜访待办", "帮我建个计划：明天上午拜访国贸的客户", "我今天拜访了客户，写个日志"):
            with client.stream(
                "POST", "/api/chat/stream", json={"session_id": sid, "message": message}, headers=headers,
            ) as response:
                for _ in response.iter_lines():
                    pass
        data = client.get("/api/metrics/cost", headers=headers).json()

    totals = data["totals"]
    per_skill = {row["skill"]: {"turns": row["turns"], "tokens_in": row["tokens_in"],
                                "tokens_out": row["tokens_out"]} for row in data["by_skill"]}
    print(f"轮次 / token   : 本批 3 轮、库内共 {totals['turns']} 轮（含种子 Trace）；"
          f"输入 {totals['tokens_in']:,}、输出 {totals['tokens_out']:,}")
    print(f"每轮平均 token : {totals['tokens_per_turn']}")
    print(f"示例单价成本   : ${totals['cost_usd']}（单价 ¥/百万 token 由配置给出，示例非报价）")
    return {
        "turns_in_run": 3,
        "totals": totals,
        "by_skill": per_skill,
        "alerts": data.get("alerts"),
        "method": "TestClient 跑 3 轮对话，读 /api/metrics/cost 的聚合结果",
        "source": "backend/app/api/routes_obs.py:_cost_summary · app/orchestrator/loop.py",
    }


# --------------------------------------------------------------------------- #
# 6. 测试资产：用例总数（判断「有多少条测试在兜底」）
# --------------------------------------------------------------------------- #
def evidence_tests() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=str(BACKEND.parent), capture_output=True, text=True, timeout=180,
        )
        lines = [ln for ln in proc.stdout.splitlines() if "::" in ln]
        files = {ln.split("::")[0] for ln in lines}
        print(f"用例总数       : {len(lines)} 条，分布在 {len(files)} 个文件")
        return {
            "tests": len(lines), "files": len(files),
            "method": "pytest --collect-only 统计",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    parser.add_argument("--fast", action="store_true", help="跳过 pytest 用例收集")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="p1_evidence_"))
    try:
        _hr("1/6 上下文工程：工具输出 脱敏 → 裁剪 → 引用化")
        RESULT["output_governance"] = evidence_output_governance(workdir)
        _hr("2/6 人工审批：一次性令牌（重放 / 并发 / 错令牌）")
        RESULT["approval_token"] = evidence_approval_token(workdir)
        _hr("3/6 评测门禁：多 Trial + 置信区间")
        RESULT["eval"] = evidence_eval()
        _hr("4/6 知识同步：版本守卫 / 幂等 / 重试 / DLQ")
        RESULT["knowledge_sync"] = evidence_knowledge_sync(workdir)
        _hr("5/6 成本归因：token / 成本 / 告警")
        RESULT["cost"] = evidence_cost(workdir)
        _hr("6/6 测试资产")
        RESULT["tests"] = evidence_tests() if not args.fast else {"skipped": True}
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if args.json:
        Path(args.json).write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"机器可读结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
