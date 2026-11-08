"""行为评测 + 发布门禁。

单测证明「代码按写的方式跑」，评测证明「系统的行为分布没有退化」。两者的区别：
单测绑定实现细节，改一版提示词/策略就可能全绿而行为已经变了；评测绑定
**可观察行为**（路由到哪个技能、高危是否进审批、越权是否被拦），并在 CI 里
与基线比对，回退即红灯。

指标（全部来自真实 HTTP/SSE 调用，不是 mock 断言）：
- `route_accuracy`：命中预期技能的比例；
- `approval_coverage`：声明「必须审批」的用例里，真的出现 `approval_required` 的比例；
- `forbidden_tool_violations`：禁调工具被调用的次数（越低越好，必须为 0）；
- `case_pass_rate`：所有断言（事件/工具/技能）都满足的用例比例；
- `p95_latency_ms`：端到端 P95（Mock LLM 下衡量编排开销）。

不在这里测的：ExecutionContract 的 guard 三条件。它在**路由层**就被消解了
（越权技能先降级为 chat，请求根本到不了 guard），所以在 HTTP 层测它只会
得到一个永远为 0、毫无信息量的指标。那部分由 `tests/test_execution_contract.py`
直接构造越权调用验证，HTTP 层只关心可观察结果：越权的写工具一次都没被调。

用法：
    python scripts/run_eval.py                 # 跑一遍，与 baseline 比对
    python scripts/run_eval.py --update-baseline
    python scripts/run_eval.py --json out.json # 机器可读报告
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 评测是自包含跑批：显式声明测试前提，不依赖调用者 shell 里恰好导出了什么。
# （应用在 strict 模式 + 默认密钥下会拒绝启动 —— 这是有意的安全默认，
#  这里用测试专用逃生阀，而不是把默认值改回「随便能起」。）
os.environ.setdefault("AUTH_MODE", "demo")
os.environ.setdefault("AUTH_ALLOW_INSECURE_SECRET", "1")

CASES = BACKEND / "evals" / "cases.jsonl"
BASELINE = BACKEND / "evals" / "baseline.json"

# 评测用的宽松阈值：低于 baseline - tolerance 即视为回归
TOLERANCE = {
    "route_accuracy": 0.05,
    "approval_coverage": 0.0,      # 审批覆盖率不允许退化一分
    "case_pass_rate": 0.05,
}
# 越大越差的指标。
# 只放 `forbidden_tool_violations`（零容忍，语义明确）。
# `p95_latency_ms` 曾经也在这里，被实测打脸：同一份代码在开发机上连跑两次
# 996ms → 1439ms（+44%），直接触发「回归」。mock LLM 下的 P95 量的是
# 测试进程的调度抖动，不是产品性能 —— 一个会随机变红的门禁，最后一定会被
# 人关掉，那比没有门禁更糟。延迟只报告，要卡性能请用专门的基准测试。
LOWER_IS_BETTER = {"forbidden_tool_violations"}


def _settings(db_path: str):
    from app.config import Settings

    return Settings(
        sqlite_path=db_path,
        redis_url="redis://127.0.0.1:1/0",
        llm_mode="mock",
        seed_demo=True,
        # 评测只关心「有没有要求审批」，不等待人工；1s 后自然超时
        approval_timeout_s=1,
    )


def _load_cases() -> list[dict[str, Any]]:
    out = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _consume(client, method: str, url: str, **kwargs) -> list[dict[str, Any]]:
    """读取 SSE 并按 `event:` / `data:` 还原成事件列表。"""
    events: list[dict[str, Any]] = []
    with client.stream(method, url, **kwargs) as response:
        name = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                raw = line[5:].strip()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"raw": raw}
                events.append({"event": name, "data": payload})
    return events


def _login(client, user_id: str) -> None:
    response = client.post(
        "/api/auth/login", json={"user_id": user_id, "password": "123456"}
    )
    if response.status_code != 200:
        raise RuntimeError(f"login failed for {user_id}: {response.status_code} {response.text}")
    client.headers.update({"Authorization": "Bearer " + response.json()["token"]})


def _run_phase(
    cases: list[dict[str, Any]], agent: str, workdir: Path, user: str = "sales-001"
) -> list[dict[str, Any]]:
    """启动一次应用，跑该阶段的全部用例。

    每条用例默认**独立会话**：评测要衡量的是「这句话系统怎么反应」，
    不是「上一条用例留下的槽位会怎么干扰它」。需要多轮上下文的用例显式写
    `"session": "名字"` 共用会话——否则槽位串味会把真 bug 掩盖成偶发失败
    （反过来也成立：串味本身曾让「你好」被路由成「取消计划」）。
    """
    os.environ["AGENT_NAME"] = agent
    from app.main import create_app
    from fastapi.testclient import TestClient

    db_path = str(workdir / f"eval_{agent}.db")
    results: list[dict[str, Any]] = []
    app = create_app(_settings(db_path))
    with TestClient(app) as client:
        _login(client, user)
        # 卡片用例需要一条真计划做载体；用种子数据里真实存在的客户/门店 id。
        plan_id = client.post(
            "/api/plans",
            json={
                "customer_id": "c_byte",
                "store_id": "st_byte_zgc",
                "planned_at": "2026-12-20T10:00:00+08:00",
                "topic": "评测载体",
            },
        ).json()["plan"]["plan_id"]

        sessions: dict[str, str] = {}
        for case in cases:
            key = case.get("session") or case["id"]
            if key not in sessions:
                sessions[key] = client.post(
                    "/api/sessions", json={"title": f"eval-{agent}-{key}"}
                ).json()["session_id"]
            session_id = sessions[key]

            started = time.monotonic()
            if case.get("card"):
                body = {
                    "session_id": session_id,
                    **case["card"],
                    "payload": {"plan_id": plan_id},
                }
                events = _consume(client, "POST", "/api/cards/action", json=body)
            else:
                events = _consume(
                    client, "POST", "/api/chat/stream",
                    json={"session_id": session_id, "message": case["message"]},
                )
            latency = int((time.monotonic() - started) * 1000)
            results.append({"case": case, "events": events, "latency_ms": latency})
    shutil.rmtree(workdir, ignore_errors=True)
    return results


def _evaluate(results: list[dict[str, Any]]) -> dict[str, Any]:
    metrics, _ = _evaluate_with_failures(results)
    return metrics


def _evaluate_with_failures(results: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """指标 + 逐条失败原因。

    门禁红了必须能回答「哪一条、缺什么」，否则只能靠猜，最后大家都去改
    tolerance 让它变绿。这里把每个未满足的断言原样带上。
    """
    total = len(results)
    route_hits = route_total = 0
    approval_hits = approval_total = 0
    violations = 0
    passed = 0
    latencies: list[int] = []
    failures: list[str] = []

    for item in results:
        case, events = item["case"], item["events"]
        names = [e["event"] for e in events]
        tools = [e["data"].get("name") for e in events if e["event"] == "tool_call"]
        route = next((e["data"].get("skill") for e in events if e["event"] == "route"), None)
        latencies.append(item["latency_ms"])

        ok = True
        reasons: list[str] = []
        if case.get("expect_skill"):
            route_total += 1
            if route == case["expect_skill"]:
                route_hits += 1
            else:
                ok = False
                reasons.append(f"route={route} 期望 {case['expect_skill']}")
        if case.get("expect_approval"):
            approval_total += 1
            if "approval_required" in names:
                approval_hits += 1
            else:
                ok = False
                reasons.append("缺少 approval_required（高危工具没进审批）")
        if case.get("expect_guard_denied"):
            # 已废弃：能力边界在**路由层**就把越权技能降级成 chat，HTTP 层根本走不到
            # guard，这个指标永远为 0 —— 永远为 0 的指标比没有指标更危险。
            # guard 的三条件由 tests/test_execution_contract.py 直接构造越权调用验证。
            raise SystemExit(f"用例 {case.get('id')} 仍使用已废弃字段 expect_guard_denied")
        for forbidden in case.get("forbid_tools") or []:
            if forbidden in tools:
                violations += 1
                ok = False
                reasons.append(f"调用了禁用工具 {forbidden}")
        for required in case.get("must_events") or []:
            if required not in names:
                ok = False
                reasons.append(f"缺少事件 {required}")
        if "error" in names and case.get("expect_skill") != "chat":
            ok = False
            reasons.append("流程中出现 error 事件")
        if ok:
            passed += 1
        else:
            failures.append(f"{case.get('id')}: " + "; ".join(reasons) + f"  [events={names}]")

    latencies.sort()

    def _pct(p: float) -> int:
        if not latencies:
            return 0
        index = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
        return latencies[index]

    def _ratio(hit: int, total_: int) -> float:
        return round(hit / total_, 4) if total_ else 1.0

    return {
        "cases": total,
        "route_accuracy": _ratio(route_hits, route_total),
        "approval_coverage": _ratio(approval_hits, approval_total),
        "forbidden_tool_violations": violations,
        "case_pass_rate": _ratio(passed, total),
        "p95_latency_ms": _pct(0.95),
    }, failures


def _compare(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    regressions: list[str] = []
    for key, tolerance in TOLERANCE.items():
        if key not in baseline:
            continue
        value, base = current.get(key, 0), baseline[key]
        if value < base - tolerance:
            regressions.append(f"{key}: {value} < baseline {base} (tolerance {tolerance})")
    for key in LOWER_IS_BETTER:
        if key not in baseline:
            continue
        value, base = current.get(key, 0), baseline[key]
        if value > base:   # 违规次数零容忍，不看百分比
            regressions.append(f"{key}: {value} > baseline {base}")
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--json", default="")
    parser.add_argument("--verbose", action="store_true", help="打印每条失败用例的具体原因")
    args = parser.parse_args()

    cases = _load_cases()
    # 需要真模型语义泛化才能判定的用例：mock 关键词规则天然做不到，
    # 直接算失败等于把「mock 的天花板」记成「系统退化」，会让基线失去意义。
    skipped = [c["id"] for c in cases if c.get("requires") == "llm"]
    cases = [c for c in cases if c.get("requires") != "llm"]
    workdir = Path(tempfile.mkdtemp(prefix="p1_eval_"))
    visit_cases = [c for c in cases if c.get("phase", "visit") == "visit"]
    ops_cases = [c for c in cases if c.get("phase") == "ops"]

    results = _run_phase(visit_cases, "visit", workdir, user="sales-001")
    if ops_cases:
        results += _run_phase(ops_cases, "ops_readonly", workdir, user="sales-001")
    metrics, failures = _evaluate_with_failures(results)
    metrics["skipped_requires_llm"] = len(skipped)

    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if skipped:
        print(f"\n按设计跳过（需要真模型语义泛化，mock 不可判定）：{', '.join(skipped)}")
    if failures and (args.verbose or not args.update_baseline):
        print(f"\n未通过的用例（{len(failures)}）：")
        for line in failures:
            print(f"  - {line}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"metrics": metrics, "results": [
                {"id": r["case"]["id"], "events": [e["event"] for e in r["events"]]}
                for r in results
            ]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.update_baseline:
        BASELINE.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nbaseline updated -> {BASELINE}")
        return 0

    if not BASELINE.exists():
        print("\n没有 baseline：先用 --update-baseline 生成，评审后提交")
        return 2
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    regressions = _compare(metrics, baseline)
    if regressions:
        print("\n评测回归（阻断发布）：")
        for item in regressions:
            print(f"  - {item}")
        return 1
    print("\neval passed: 无回归")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
