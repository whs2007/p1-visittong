"""P1 公开冒烟脚本：7 组场景，逐组断言，输出可复现的通过率。

用法（两种，都只依赖本仓库）：
    python backend/scripts/smoke_scenarios.py --serve    # 自动拉起 demo 服务再跑（make verify 用）
    python backend/run.py                               # 或者自己起服务
    python backend/scripts/smoke_scenarios.py           # 再跑这一条

可选配置：
    P1_BASE_URL        默认 http://127.0.0.1:8020
    P1_SERVE_PORT      --serve 时使用的端口（默认 8021，避开常见占用）
    P1_TOKEN_SALES     Bearer Token（严格鉴权模式下）
    P1_TOKEN_MANAGER   主管 Bearer Token
    P1_DEMO_PASSWORD   demo 账号密码，脚本自行调用 /api/login
    AUTH_MODE=demo     此时可只用 X-User-Id，无需任何凭据

刻意不硬编码机器路径 / 数据库文件 / 端口：所有状态都经 HTTP 读取，
因此这份脚本和 README 里「7 组场景 100% 通过」的数字在别人机器上也复现得了。
`--serve` 存在的原因很实际：如果验收命令要求「先手动起服务」，那它就不是
一条命令，CI 里也没法原样跑（README 写着 `make verify`，就必须真的能一把过）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = os.getenv("P1_BASE_URL", "http://127.0.0.1:8020").rstrip("/")
SALES = os.getenv("P1_SALES_USER", "sales-001")
MANAGER = os.getenv("P1_MANAGER_USER", "manager-001")


def _start_server(port: int) -> tuple[subprocess.Popen, str]:
    """拉起一个隔离的 demo 服务：独立端口 + 独立数据库，不污染本地演示数据。"""
    root = Path(__file__).resolve().parents[2]
    db_dir = root / "data" / "_smoke"
    db_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "AUTH_MODE": "demo",
        "AUTH_ALLOW_INSECURE_SECRET": "1",
        "SQLITE_PATH": str(db_dir / "smoke.db"),
    })
    proc = subprocess.Popen(
        [sys.executable, str(root / "backend" / "run.py")],
        cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务启动失败：进程已退出（看 backend/data 日志或手动跑一次 run.py）")
        try:
            httpx.get(base + "/api/config", timeout=2)
            return proc, base
        except Exception:  # noqa: BLE001  还没起来，继续等
            time.sleep(0.4)
    proc.terminate()
    raise RuntimeError("服务 60s 内未就绪")


def auth_headers(client: httpx.Client, uid: str, role: str) -> dict[str, str]:
    """凭据全部来自环境或登录，不写死在仓库里。"""
    token = os.getenv(f"P1_TOKEN_{role.upper()}")
    if token:
        return {"Authorization": f"Bearer {token}"}
    mode = client.get("/api/config").json().get("auth_mode")
    if mode == "demo":
        return {"X-User-Id": uid}
    password = os.getenv("P1_DEMO_PASSWORD")
    if not password:
        raise RuntimeError(
            "严格鉴权模式：请设置 P1_TOKEN_SALES / P1_TOKEN_MANAGER，"
            "或 P1_DEMO_PASSWORD 让脚本自行登录；也可设 AUTH_MODE=demo。"
        )
    resp = client.post("/api/login", json={"user_id": uid, "password": password})
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RuntimeError(f"/api/login 未返回 token：{data}")
    return {"Authorization": f"Bearer {token}"}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def s1_service_and_assembly(c, hs, hm, st) -> str:
    health = c.get("/api/health")
    expect(health.status_code == 200, f"health {health.status_code}")
    agents = c.get("/api/agents", headers=hm)
    expect(agents.status_code == 200, f"/api/agents {agents.status_code}")
    active = agents.json()["active"]
    st["agent"] = active["name"]
    skills = c.get("/api/skills", headers=hs)
    expect(skills.status_code == 200 and len(skills.json()["skills"]) >= 8, "技能目录不全")
    return f"version={health.json().get('version')} agent={active['name']} skills={len(skills.json()['skills'])}"


def s2_auth_boundary(c, hs, hm, st) -> str:
    denied = c.get("/api/audit", headers=hs)
    expect(denied.status_code == 403, f"销售不应能看审计：{denied.status_code}")
    allowed = c.get("/api/audit", headers=hm)
    expect(allowed.status_code == 200, f"主管应能看审计：{allowed.status_code}")
    return "销售查审计 403 / 主管 200"


def s3_chat_stream(c, hs, hm, st) -> str:
    created = c.post("/api/sessions", json={"title": "冒烟脚本"}, headers=hs)
    expect(created.status_code == 200, f"创建会话 {created.status_code}: {created.text[:120]}")
    data = created.json()
    sid = data.get("session_id") or (data.get("session") or {}).get("session_id")
    expect(bool(sid), f"创建会话未返回 session_id：{data}")
    st["session_id"] = sid

    events: list[dict[str, Any]] = []
    event_names: list[str] = []
    pending_kind = ""
    with c.stream(
        "POST", "/api/chat/stream",
        json={"session_id": sid, "message": "我今天有哪些拜访待办？"}, headers=hs,
    ) as resp:
        expect(resp.status_code == 200, f"chat/stream {resp.status_code}")
        # sse.py 的帧是 `event: <name>` + `data: <json>` 两行，事件名不在 data 里
        for line in resp.iter_lines():
            if line.startswith("event:"):
                pending_kind = line[6:].strip()
                event_names.append(pending_kind)
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                events.append({"event": pending_kind, **payload})
    expect(bool(events), "SSE 未返回任何事件")
    for ev in events:
        if ev.get("trace_id"):
            st["turn_id"] = ev["trace_id"]
            break
    expect("done" in event_names, f"SSE 未见 done 事件：{sorted(set(event_names))}")
    expect("tool_result" in event_names or "route" in event_names, "未见路由 / 工具事件")
    return f"SSE {len(events)} 事件 / {len(set(event_names))} 类，含 done；turn_id={st.get('turn_id')}"


def s4_plan_fsm(c, hs, hm, st) -> str:
    # 每次用不同的未来时间，保证脚本可重复运行而不撞计划冲突
    planned_at = (
        datetime.now().astimezone() + timedelta(days=1, minutes=random.randint(0, 10000))
    ).isoformat(timespec="seconds")
    created = c.post(
        "/api/plans",
        json={"customer_id": "c_byte", "planned_at": planned_at, "topic": "冒烟计划"},
        headers=hs,
    )
    expect(created.status_code == 200, f"创建计划 {created.status_code}: {created.text[:120]}")
    plan_id = created.json()["plan"]["plan_id"]
    st["plan_id"] = plan_id
    updated = c.patch(f"/api/plans/{plan_id}", json={"topic": "冒烟计划-已改"}, headers=hs)
    expect(updated.status_code == 200, f"修改计划 {updated.status_code}")
    detail = c.get(f"/api/plans/{plan_id}", headers=hs).json()
    expect(detail["plan"]["status"] == "planned", "计划状态异常")
    events = [row.get("event") for row in detail.get("transitions", [])]
    expect("create" in events and "update" in events, f"状态迁移未落库：{events}")
    return f"plan={plan_id} transitions={events}"


def s5_dual_channel_governance(c, hs, hm, st) -> str:
    """人工降级通道：销售被拒 → 主管豁免 → 台账可举证。"""
    plan_id = st["plan_id"]
    before = c.get("/api/governance/manual-overrides", headers=hm).json()["count"]
    denied = c.post(f"/api/plans/{plan_id}/cancel", headers=hs)
    expect(denied.status_code == 403, f"销售不应能走降级通道：{denied.status_code}")
    expect(denied.json().get("code") == "MANAGER_ONLY", "错误码应为 MANAGER_ONLY")
    done = c.post(f"/api/plans/{plan_id}/cancel", headers=hm)
    expect(done.status_code == 200, f"主管取消 {done.status_code}: {done.text[:120]}")
    expect(done.json()["governance"]["approval"] == "waived", "豁免标记缺失")
    ledger = c.get("/api/governance/manual-overrides", headers=hm).json()
    expect(ledger["count"] == before + 1, "豁免未进台账")
    expect(any(row["target_id"] == plan_id for row in ledger["items"]), "台账缺少本次计划")
    terminal = c.patch(f"/api/plans/{plan_id}", json={"topic": "x"}, headers=hs)
    expect(terminal.status_code == 409, "终态应不可再改")
    return "销售 403 / 主管豁免并留痕 / 终态 409"


def s6_knowledge_and_observability(c, hs, hm, st) -> str:
    kw = c.get("/api/knowledge/search", params={"q": "客户", "top_k": 3}, headers=hs)
    expect(kw.status_code == 200, f"知识检索 {kw.status_code}")
    metrics = c.get("/api/metrics", headers=hm)
    expect(metrics.status_code == 200, f"指标 {metrics.status_code}")
    audit = c.get("/api/audit", headers=hm)
    expect(audit.status_code == 200 and len(audit.json()["audit"]) > 0, "审计为空")
    return f"knowledge.backend={kw.json().get('backend')} audit={len(audit.json()['audit'])} 条"


def s7_durable_and_capability(c, hs, hm, st) -> str:
    turn_id = st.get("turn_id")
    expect(bool(turn_id), "缺少 turn_id，无法验证检查点能力快照")
    task = c.get(f"/api/tasks/{turn_id}", headers=hs)
    expect(task.status_code == 200, f"任务查询 {task.status_code}")
    body = task.json()
    cp = body.get("checkpoint")
    expect(bool(cp), "没有检查点")
    expect(bool(cp.get("capability_fingerprint")), "检查点缺能力指纹")
    expect(bool(cp.get("capability", {}).get("tools")), "检查点缺能力快照")
    compat = body.get("resume_compat", {})
    expect(compat.get("status") == "ok", f"当前装配应可续跑：{compat.get('status')}")
    return f"agent={cp.get('agent_name')} fingerprint={cp['capability_fingerprint'][:12]}… resume_compat=ok"


SCENARIOS: list[tuple[str, Callable[..., str]]] = [
    ("服务与显式装配", s1_service_and_assembly),
    ("鉴权边界", s2_auth_boundary),
    ("对话主链路 SSE", s3_chat_stream),
    ("计划 FSM 与状态迁移", s4_plan_fsm),
    ("双通道治理与豁免台账", s5_dual_channel_governance),
    ("知识检索与可观测", s6_knowledge_and_observability),
    ("Durable 检查点与能力快照", s7_durable_and_capability),
]


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="自动拉起隔离的 demo 服务再跑")
    parser.add_argument("--port", type=int, default=int(os.getenv("P1_SERVE_PORT", "8021")))
    args = parser.parse_args()

    proc: subprocess.Popen | None = None
    try:
        if args.serve:
            proc, BASE = _start_server(args.port)
        return _run()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _run() -> int:
    print(f"P1 smoke → {BASE}（{len(SCENARIOS)} 组）")
    state: dict[str, Any] = {}
    passed = 0
    try:
        with httpx.Client(base_url=BASE, timeout=60) as client:
            hs = auth_headers(client, SALES, "sales")
            hm = auth_headers(client, MANAGER, "manager")
            for idx, (name, fn) in enumerate(SCENARIOS, 1):
                try:
                    detail = fn(client, hs, hm, state)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{idx}/{len(SCENARIOS)}] FAIL  {name}: {exc}")
                else:
                    passed += 1
                    print(f"  [{idx}/{len(SCENARIOS)}] PASS  {name} — {detail}")
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败：{exc}")
        return 1
    print(f"结果：{passed}/{len(SCENARIOS)} passed")
    return 0 if passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
