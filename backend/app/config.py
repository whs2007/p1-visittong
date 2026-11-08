"""P1 配置：拜访通 Agent（端口 8020，SQLite 单文件，Redis 可选）。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]      # projects/p1-visittong/backend
PROJECT_DIR = ROOT_DIR.parent                        # projects/p1-visittong
DATA_DIR = PROJECT_DIR / "data"
FRONTEND_DIR = PROJECT_DIR / "frontend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_DIR / ".env"), str(ROOT_DIR / "app" / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "拜访通 · BD 拜访 Agent"
    version: str = "0.8.0"      # /api/config 与冒烟输出都会读它，别再让它和文档漂开

    # ---------- 鉴权 ----------
    auth_mode: str = "strict"                    # strict | demo（demo 额外允许 X-User-Id，仅本地演示）
    auth_secret: str = "dev-only-change-me"      # 生产必须通过 AUTH_SECRET 覆盖
    auth_token_ttl_h: int = 8
    demo_password: str = "123456"                # demo 账号初始密码（README 已注明）
    login_max_attempts: int = 5                  # 同一账号+IP 连续失败次数上限
    login_lock_seconds: int = 300                # 触发后的锁定秒数
    # 跨域白名单，逗号分隔。**默认空 = 不放行任何跨域**（前端与后端同源，不需要）。
    # 旧默认是 "*"，配合 allow_credentials 等于任何站点都能带凭据调接口。
    # 本地前后端分离开发时显式设 ALLOWED_ORIGINS=http://127.0.0.1:5173。
    allowed_origins: str = ""

    # ---------- LLM ----------
    llm_mode: str = "mock"          # mock | live（MVP 默认离线演示）
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4.1-flash"
    llm_temperature: float = 0.2
    llm_timeout_s: float = 60.0

    # ---------- 存储 ----------
    redis_url: str = "redis://127.0.0.1:6379/0"
    sqlite_path: str = str(DATA_DIR / "visittong.db")
    db_backend: str = "sqlite"          # sqlite | mysql（MySQL 需 pymysql + 真实实例）
    mysql_dsn: str = ""                 # mysql://user:password@host:3306/dbname
    session_ttl_s: int = 1800
    context_limit: int = 40

    # ---------- 运行时 ----------
    approval_timeout_s: int = 120
    tool_timeout_s: float = 20.0
    max_loop: int = 6
    # ---------- 路由（总控 Skill）----------
    router_mode: str = "hybrid"        # rules | hybrid | llm
    router_confidence_min: float = 0.6
    # ---------- 上下文工程（对齐 Pi compaction）----------
    enable_compaction: bool = True
    context_token_budget: int = 6000      # 超过预算触发压缩
    compaction_keep_recent: int = 12      # 保留最近 N 条（切点会避开 tool 结果）
    # 压缩摘要是否交给真模型（对齐 Pi：摘要由模型生成）；失败/超时自动回退确定性摘要
    llm_summary: bool = True

    # ---------- 记忆（Pi 的 AGENTS.md 分层 + 长期记忆）----------
    enable_memory: bool = True
    memory_dir: str = "memory"            # 相对项目根：AGENTS.md / agents/<name>.md
    memory_recall_k: int = 4              # 单轮注入的动态记忆条数上限
    memory_min_score: float = 0.16        # 召回相似度下限，低于视为不相关
    memory_layer_max_chars: int = 1600    # 单层记忆文件注入上限（防止挤占上下文）
    memory_entry_max_chars: int = 400     # 单条动态记忆长度上限
    # ---------- 成本（可选，用于 done 事件展示）----------
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    # ---------- 录音 / ASR / 纪要 ----------
    asr_backend: str = "mock"      # mock | faster_whisper | command
    asr_model: str = "small"
    asr_language: str = "zh"
    asr_command: str = ""
    audio_dir: str = str(DATA_DIR / "audio")
    # ---------- 知识检索（生产 Milvus；本地降级向量索引）----------
    knowledge_backend: str = "local"   # local | milvus
    knowledge_dim: int = 256
    milvus_uri: str = ""               # 例如 http://127.0.0.1:19530
    knowledge_top_k: int = 5
    knowledge_min_score: float = 0.35
    seed_demo: bool = True
    distance_ok_m: int = 500         # 范围内直接签到
    distance_approval_m: int = 2000  # 区间内走审批，超过直接拒绝
    host: str = "127.0.0.1"
    port: int = 8020

    @property
    def demo_user(self) -> str:
        return "sales-001"

    @property
    def demo_manager(self) -> str:
        return "manager-001"


@lru_cache
def get_settings() -> Settings:
    return Settings()