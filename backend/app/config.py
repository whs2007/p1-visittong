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
    # ---------- 工具输出治理（脱敏 / 裁剪 / 引用化）----------
    tool_output_max_chars: int = 1600     # 字符上限（max_tokens=0 时生效，作为兼容路径）
    # token 上限（默认走这条）：字符上限对 token 的换算随输出形态浮动 1.79 倍，
    # 上下文预算却是按 token 算的。900 ≈ 中文 780 字 / 英文 2500 字符。
    tool_output_max_tokens: int = 900
    tool_output_head_ratio: float = 0.7   # 裁剪后首部占比（尾部保留现场，如错误码 / 汇总行）
    pii_redact: bool = True               # 手机号 / 身份证 / 银行卡 / 邮箱掩码
    # ---------- 成本（按 token 计量 + 可配置单价；单价留空则只报 token）----------
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    # ---------- 告警阈值（失败率 / 审批超时率，超过即在 /api/metrics 置警告）----------
    alert_failure_rate: float = 0.15
    alert_approval_timeout_rate: float = 0.10
    alert_min_samples: int = 20

    # ---------- 降级策略 ----------
    # 1 = 只要出现 critical 降级就拒绝启动（多实例语义不成立时，宁可起不来）
    strict_degrade: bool = False
    # ---------- 录音 / ASR / 纪要 ----------
    asr_backend: str = "mock"      # mock | faster_whisper | command
    asr_model: str = "small"
    asr_language: str = "zh"
    asr_command: str = ""
    audio_dir: str = str(DATA_DIR / "audio")
    # ---------- 知识检索（生产 Milvus；本地降级向量索引）----------
    knowledge_backend: str = "local"   # local | milvus
    # vector = v0.8 的 n-gram 余弦；hybrid = IDF 覆盖度 + BM25 融合（默认）
    # 保留两种是为了能对标注集做 A/B（scripts/retrieval_eval.py 出数）
    knowledge_mode: str = "hybrid"
    knowledge_dim: int = 256
    milvus_uri: str = ""               # 例如 http://127.0.0.1:19530
    knowledge_top_k: int = 5
    # 0.35 → 0.38：标注集（1140 篇语料）上负例误召回 16.7% → 8.3%，召回不变
    knowledge_min_score: float = 0.38
    # 自动绑定（唯一命中直接当成客户）额外要求覆盖率过半，
    # 只匹配到城市/商圈这种低信息量片段时只给候选卡，不自动绑定
    knowledge_autobind_coverage: float = 0.5
    # ---------- 时区语义 ----------
    # 业务时区：拜访计划/待办的时间跟业务走，不跟部署机走。
    # 踩过的坑：开发机 +08:00、CI 的 Ubuntu 是 UTC，同一个 naive datetime
    # 经 .astimezone() 在两边写出两种 planned_at，同一个测试一边绿一边红。
    business_tz: str = "Asia/Shanghai"

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