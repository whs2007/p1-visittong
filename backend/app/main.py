"""FastAPI 应用工厂：装配组件、挂载路由与前端静态文件。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.adapters.knowledge import build_knowledge, sync_knowledge
from app.api import routes_audio, routes_auth, routes_chat, routes_knowledge, routes_obs, routes_visit
from app.approvals import ApprovalManager
from app.config import FRONTEND_DIR, Settings, get_settings
from app.context import AppContext
from app.domain.db import Database
from app.domain.audio import AudioStore
from app.domain.pipeline import EventPipeline
from app.domain.tasks import TaskStore
from app.errors import ApiError, api_error_handler
from app.llm import build_llm
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionState
from app.sessions.tree import SessionTree, ensure_columns
from app.skills.registry import registry
from app.trace.seed import seed_traces
from app.trace.store import TraceStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("visittong")


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings: Settings = application.state.settings

    db = Database(
        settings.sqlite_path, settings.demo_password,
        backend=settings.db_backend, dsn=settings.mysql_dsn,
    )
    db.init()
    if settings.seed_demo:
        db.seed_demo()
    db.ensure_auth_columns()
    tree = SessionTree(db)
    ensure_columns(db)

    state = SessionState(settings)
    await state.init()

    traces = TraceStore(settings.sqlite_path)
    traces.init()
    traces.ensure_schema_columns()
    reconciled = await traces.reconcile_stale()
    if reconciled["traces_failed"] or reconciled["approvals_cancelled"]:
        log.warning("reconciled stale state after restart: %s", reconciled)

    tasks = TaskStore(db)
    interrupted = tasks.reconcile_interrupted()
    if interrupted:
        log.warning("reconciled %s running task(s) -> interrupted (resume available)", interrupted)

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

    policy = PolicyEngine(registry, settings)
    llm = build_llm(settings)
    approvals = ApprovalManager(traces, settings.approval_timeout_s)
    ctx = AppContext(
        settings=settings, db=db, tree=tree, state=state, registry=registry,
        policy=policy, llm=llm, traces=traces, approvals=approvals, tasks=tasks, audio=audio,
        knowledge=knowledge, events=events,
    )
    application.state.ctx = ctx
    if settings.auth_mode == "strict" and settings.auth_secret == "dev-only-change-me":
        log.warning("AUTH_SECRET 使用默认值，生产环境必须通过环境变量覆盖")
    log.info(
        "visittong ready: llm=%s redis=%s auth=%s db=%s",
        llm.name, state.backend, settings.auth_mode, settings.sqlite_path,
    )
    try:
        yield
    finally:
        log.info("visittong shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.state.settings = settings
    app.add_exception_handler(ApiError, api_error_handler)
    origins = [item.strip() for item in settings.allowed_origins.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(routes_auth.router)
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