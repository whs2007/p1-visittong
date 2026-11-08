"""FastAPI 应用工厂：装配组件、挂载路由与前端静态文件。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.adapters.knowledge import build_knowledge, sync_knowledge
from app.api import (
    routes_agents,
    routes_audio,
    routes_auth,
    routes_auth_ext,
    routes_chat,
    routes_knowledge,
    routes_obs,
    routes_visit,
)
from app.approvals import ApprovalManager
from app.config import FRONTEND_DIR, Settings, get_settings
from app.context import AppContext
from app.domain.audio import AudioStore
from app.domain.db import Database
from app.domain.governance import GovernanceStore
from app.domain.idempotency import IdempotencyStore
from app.domain.migrations import apply as apply_migrations
from app.domain.pipeline import EventPipeline
from app.domain.tasks import TaskStore
from app.errors import ApiError, api_error_handler
from app.llm import build_llm
from app.obs.logging import setup as setup_logging
from app.obs.security import LoginThrottleMiddleware, RequestContextMiddleware
from app.policy.engine import PolicyEngine
from app.runtime import ResourceLoader
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree, ensure_columns
from app.skills.registry import registry
from app.trace.seed import seed_traces
from app.trace.store import TraceStore

setup_logging()
log = logging.getLogger("visittong")

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _check_startup_safety(settings: Settings) -> None:
    """启动即失败的安全配置检查（不给「先跑起来再说」留口子）。

    - `strict` 模式下仍用默认密钥 → 直接拒绝启动：默认密钥是公开的，
      任何人都能伪造任意用户与角色的 token。
    - `demo` 模式下监听非本机地址 → 拒绝启动：demo 允许 `X-User-Id` 免密，
      暴露到内网等于把系统交给所有人。
    """
    if settings.auth_mode != "demo" and settings.auth_secret == "dev-only-change-me":
        # 测试环境（显式开关）允许默认密钥，其余一律拒绝启动
        if os.getenv("AUTH_ALLOW_INSECURE_SECRET") == "1":
            log.warning("使用默认 AUTH_SECRET（仅限本地/测试，禁止用于部署）")
        else:
            raise RuntimeError(
                "AUTH_SECRET 仍是默认值，拒绝启动。请设置环境变量 AUTH_SECRET（随机 32+ 字符）"
            )
    host = (settings.host or "127.0.0.1").split(":")[0]
    if settings.auth_mode == "demo" and host not in _LOOPBACK:
        raise RuntimeError(
            f"demo 模式（免密 X-User-Id）只能绑定本机，当前 HOST={host}。"
            "请改用 AUTH_MODE=strict，或设置 HOST=127.0.0.1。"
        )


def _should_reconcile() -> bool:
    """多 worker 下只有「第一个起来的进程」该做崩溃对账。

    `reconcile_interrupted` 会把 running 任务标 interrupted，如果每个 worker
    启动都跑一遍，会把**其他 worker 正在执行**的任务误判为崩溃。
    运维约定：`RECONCILE_ON_START=0` 用于非首个 worker（或容器副本），
    也可以单独跑一次 `python -m app.maintenance reconcile`。
    """
    return os.getenv("RECONCILE_ON_START", "1") == "1"


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings: Settings = application.state.settings
    _check_startup_safety(settings)

    db = Database(
        settings.sqlite_path, settings.demo_password,
        backend=settings.db_backend, dsn=settings.mysql_dsn,
    )
    db.init()
    if settings.seed_demo:
        db.seed_demo()
    db.ensure_auth_columns()
    # 版本化迁移：governance_events / idempotency_effects / revoked_tokens /
    # login_attempts / task_runs.pending_approval_* 等
    applied = apply_migrations(settings.sqlite_path)
    if applied:
        log.info("schema migrations applied: %s", applied)
    tree = SessionTree(db)
    ensure_columns(db)

    state = SessionState(settings)
    await state.init()

    traces = TraceStore(settings.sqlite_path)
    traces.init()
    traces.ensure_schema_columns()
    # 审批过期收敛是幂等的（只改过期单），任何时候都可以跑；
    # running trace 的收敛与任务对账一样，多 worker 下只能由一个进程执行。
    reconcile = _should_reconcile()
    reconciled = await traces.reconcile_stale(include_traces=reconcile)
    if reconciled["traces_failed"] or reconciled["approvals_expired"]:
        log.warning("reconciled stale state after restart: %s", reconciled)

    tasks = TaskStore(db)
    if reconcile:
        interrupted = tasks.reconcile_interrupted()
        if interrupted:
            log.warning("reconciled %s in-flight task(s) -> interrupted (resume available)", interrupted)
        # 工具执行对账：崩溃窗口里「声明了但没写终态」的记录标 unknown，而不是静默重放
        attempts = tasks.reconcile_tool_attempts()
        if attempts["count"]:
            log.warning(
                "reconciled %s orphaned tool attempt(s) -> unknown", attempts["count"]
            )
    else:
        log.info("startup reconcile skipped (RECONCILE_ON_START=0)")

    audio = AudioStore(settings)
    await audio.init()

    knowledge = build_knowledge(settings, db)
    events = EventPipeline(db)
    if knowledge.backend == "local" and knowledge.count() == 0:
        bootstrap_event = events.emit("hive2kafka.knowledge_sync", {"mode": "full", "bootstrap": True})
        sync_knowledge(db, knowledge)
        events.mark_processed(bootstrap_event)
        log.info("knowledge bootstrap: docs=%s backend=%s", knowledge.count(), knowledge.backend)
    if settings.seed_demo:
        await seed_traces(traces)

    import app.skills.tools as _builtin_tools  # noqa: F401  注册内置工具

    # 显式装配（对齐 Pi createAgentSession）：能力集由配置决定，而非模块级全局字典
    resource_loader = ResourceLoader(registry)
    agent_name = os.getenv("AGENT_NAME", "visit")
    try:
        session = resource_loader.create_session(agent_name)
    except KeyError:
        log.warning("unknown agent %r, fallback to visit", agent_name)
        session = resource_loader.create_session("visit")
    log.info("agent assembled: %s skills=%s", session.name, list(session.skills))

    policy = PolicyEngine(registry, settings)
    llm = build_llm(settings)
    approvals = ApprovalManager(traces, settings.approval_timeout_s)
    idem = IdempotencyStore(settings.sqlite_path)
    governance = GovernanceStore(settings.sqlite_path)
    ctx = AppContext(
        settings=settings, db=db, tree=tree, state=state, registry=registry,
        policy=policy, llm=llm, traces=traces, approvals=approvals, tasks=tasks, audio=audio,
        knowledge=knowledge, events=events,
        session=session, resource_loader=resource_loader,
        idem=idem, governance=governance,
    )
    application.state.ctx = ctx
    log.info(
        "visittong_ready llm=%s redis=%s auth=%s db=%s agent=%s fingerprint=%s",
        llm.name, state.backend, settings.auth_mode, settings.sqlite_path,
        session.name, session.capability_fingerprint()[:16],
        extra={"event": "startup", "agent": session.name},
    )
    try:
        yield
    finally:
        log.info("visittong_shutdown", extra={"event": "shutdown"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.state.settings = settings
    app.add_exception_handler(ApiError, api_error_handler)
    # CORS 默认拒绝：旧实现 origins 为空时回落到 ["*"]，等于任何站点都能带
    # 凭据调接口；默认值改成空串后，未配置就**不装中间件**（连响应头都不给），
    # 需要跨域时通过 ALLOWED_ORIGINS 显式列白名单。
    origins = [item.strip() for item in settings.allowed_origins.split(",") if item.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-User-Id"],
            allow_credentials=True,
        )
    app.add_middleware(LoginThrottleMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(routes_auth.router)
    app.include_router(routes_auth_ext.router)
    app.include_router(routes_agents.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_visit.router)
    app.include_router(routes_audio.router)
    app.include_router(routes_knowledge.router)
    app.include_router(routes_obs.router)

    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

        @app.get("/")
        async def index():
            # 禁止缓存入口 HTML，避免前端修复后浏览器仍加载旧版本
            return FileResponse(
                str(FRONTEND_DIR / "index.html"),
                headers={"Cache-Control": "no-store, must-revalidate"},
            )

    return app


app = create_app()