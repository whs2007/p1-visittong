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
import random
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
# CI 下界的容差：区间本身有抽样误差，卡到小数点后 4 位会让门禁在噪声里飘。
CI_TOLERANCE = 0.02


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


# --------------------------------------------------------------------------- #
# 置信区间（v0.9）：单轮 25 条用例的 100% 通过，不等于「通过率 100%」
# --------------------------------------------------------------------------- #
def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 区间：小样本 + 极端比例（0/25、25/25）下比正态近似稳。

    25 条全过时，正态近似会给出 [1.0, 1.0]，等于宣称「绝不可能失败」；
    Wilson 给出约 [0.867, 1.0]——这才是 25 个样本能支撑的结论。
    """
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denom
    return max(0.0, round(center - margin, 4)), min(1.0, round(center + margin, 4))


def bootstrap_interval(values: list[float], *, resamples: int = 2000, seed: int = 20261108) -> tuple[float, float]:
    """按月 trial 均值做自助法区间：把「轮次」当抽样单位，比按用例更保守。

    两种区间的差别就是面试里最容易被追问的点：
    - Wilson 把每次执行当独立伯努利试验（n = 用例 × 轮次），区间窄、偏乐观；
    - bootstrap 重采样**轮次**，承认「同一轮内部用例相关」，区间宽、更保守。
    单轮时 bootstrap 退化为点估计，此时只报 Wilson，并显式标注 trials=1。
    """
    if len(values) < 2:
        return (values[0], values[0]) if values else (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    low = means[int(0.025 * (len(means) - 1))]
    high = means[int(0.975 * (len(means) - 1))]
    return round(low, 4), round(high, 4)


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


def _run_trials(cases: list[dict[str, Any]], trials: int, root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """跑 N 轮，每轮一个独立库 + 独立会话。

    为什么必须独立：评测会创建计划、触发审批、写日志，共用库时第二轮看到的是
    「上一轮留下的状态」，于是第二轮通过率天然更高——那不是稳定性，是污染。
    """
    visit_cases = [c for c in cases if c.get("phase", "visit") == "visit"]
    ops_cases = [c for c in cases if c.get("phase") == "ops"]
    per_trial: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    for index in range(trials):
        workdir = root / f"trial{index + 1}"
        workdir.mkdir(parents=True, exist_ok=True)
        results = _run_phase(visit_cases, "visit", workdir, user="sales-001")
        if ops_cases:
            results += _run_phase(ops_cases, "ops_readonly", workdir, user="sales-001")
        per_trial.append(_evaluate_with_failures(results)[0])
        all_results.extend(results)
    return per_trial, all_results


def _aggregate(per_trial: list[dict[str, Any]], all_results: list[dict[str, Any]], trials: int) -> tuple[dict, list[str]]:
    """合并多轮：点估计取全样本，区间同时给 Wilson 与 bootstrap。"""
    metrics, failures = _evaluate_with_failures(all_results)
    rates = [round(float(m.get("case_pass_rate") or 0.0), 4) for m in per_trial]
    passed = sum(round(rate * m["cases"]) for rate, m in zip(rates, per_trial))
    total = sum(int(m["cases"]) for m in per_trial)
    wilson_low, wilson_high = wilson_interval(int(passed), int(total))
    boot_low, boot_high = bootstrap_interval(rates)
    p95s = [int(m.get("p95_latency_ms") or 0) for m in per_trial]
    metrics.update(
        {
            "trials": trials,
            "cases_per_trial": int(total // trials) if trials else 0,
            "executions": int(total),
            "per_trial_pass_rate": rates,
            "per_trial_min_pass_rate": min(rates) if rates else 0.0,
            "pass_rate_ci_wilson": [wilson_low, wilson_high],
            "pass_rate_ci_bootstrap": [boot_low, boot_high],
            "p95_latency_spread_ms": [min(p95s), max(p95s)] if p95s else [0, 0],
        }
    )
    return metrics, failures


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
    # CI 下界比较：只在两边 trials 相同、且 baseline 记录过 CI 时才比。
    # 5 轮的区间和 1 轮的区间不是同一个统计量，混着比会造出假回归。
    if (
        baseline.get("trials") == current.get("trials")
        and baseline.get("pass_rate_ci_wilson")
        and current.get("pass_rate_ci_wilson")
    ):
        cur_low = current["pass_rate_ci_wilson"][0]
        base_low = baseline["pass_rate_ci_wilson"][0]
        if cur_low < base_low - CI_TOLERANCE:
            regressions.append(
                f"pass_rate_ci_wilson[low]: {cur_low} < baseline {base_low} (tolerance {CI_TOLERANCE})"
            )
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--json", default="")
    parser.add_argument("--verbose", action="store_true", help="打印每条失败用例的具体原因")
    parser.add_argument("--trials", type=int, default=1, help="重复轮次（每轮独立库；>1 才有多轮置信区间）")
    args = parser.parse_args()

    cases = _load_cases()
    # 需要真模型语义泛化才能判定的用例：mock 关键词规则天然做不到，
    # 直接算失败等于把「mock 的天花板」记成「系统退化」，会让基线失去意义。
    skipped = [c["id"] for c in cases if c.get("requires") == "llm"]
    cases = [c for c in cases if c.get("requires") != "llm"]
    trials = max(1, int(args.trials))
    root = Path(tempfile.mkdtemp(prefix="p1_eval_"))
    try:
        per_trial, results = _run_trials(cases, trials, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    metrics, failures = _aggregate(per_trial, results, trials)
    metrics["skipped_requires_llm"] = len(skipped)

    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    low, high = metrics["pass_rate_ci_wilson"]
    print(
        f"\n通过率 {metrics['case_pass_rate']:.4f}（{metrics['executions']} 次执行 = "
        f"{metrics['cases_per_trial']} 用例 × {trials} 轮）；"
        f"95% Wilson CI [{low}, {high}]；bootstrap CI {metrics['pass_rate_ci_bootstrap']}"
        + ("（单轮，bootstrap 退化为点估计）" if trials < 2 else "")
    )
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
