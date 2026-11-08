# 拜访通 · BD 拜访 Agent

> 面向企业销售 / BD 的**拜访任务 Agent**：一句话完成「查待办 → 建计划 → 改约 / 取消（人工审批）→ 签到 → 签退 → 录音纪要 → 日志」。
> 底层是一套可拆插的 **Agent Runtime**：显式装配层（AgentSpec / ResourceLoader / AgentSession）、会话树与上下文压缩、分层 Skill 与混合意图路由、Tool Policy / HITL 审批 / 操作审计、Durable Execution（检查点、续跑、幂等）、全链路 Trace 与指标。
> v0.5 起，能力集由配置在**装配期**决定，同一套 Runtime 可装配出不同能力集的 Agent，能力白名单在**执行期**硬校验；检查点绑定能力指纹，跨装配续跑会被显式拒绝。
> v0.7 补的是「出事之后怎么自证」：不可变执行契约、版本化迁移、效果级幂等 + 真 `kill -9` 崩溃注入、审批等待态持久化、治理台账、结构化日志与 Prometheus、行为评测回归门禁，以及一条命令的发布门禁 `make verify`。
> 边界先说清：内置两套声明是**同一套 Skill 的不同子集**（全能力 / 只读），不是两个不同领域；执行期 guard 只在**能力被收紧或被污染**的路径上才会真正拦截。

| 项 | 状态 |
|---|---|
| 版本 | v0.7 · 2026-11-08 |
| 运行 | ✅ 可运行；Mock 模式无需 API Key，Redis / MySQL 不可用时自动降级 |
| 测试 | `pytest` **115 passed**（FSM、断点续跑与能力指纹、执行契约、幂等与崩溃对账、路由回退与会话卫生、权限治理、执行期能力边界、人工通道豁免、ASR、知识检索、SQL 方言、观测） |
| 评测门禁 | `run_eval.py` 25 条行为用例 + 基线比对（路由 1.0 / 审批覆盖 1.0 / 违规 0），1 条需真模型的用例显式跳过 |
| 崩溃注入 | `chaos_resume.py` 真 `kill -9`，6/6 用例满足「副作用恰好一次」 |
| 演示 | `backend/scripts/smoke_scenarios.py` 7 组可复现场景（含双通道治理与能力快照），看板 100% 成功率 / 5 条种子 Trace |
| 工程化 | `verify_all.py` / `make verify`（lint + test + eval + smoke + chaos）、Dockerfile（非 root + healthcheck）、docker-compose（app + redis）、GitHub Actions、依赖哈希锁定 |
| 技术栈 | Python 3.11+ / FastAPI / SQLite（可选 MySQL、Redis）/ Vue3 / SSE / pytest / ruff |

> [!IMPORTANT]
> 本项目是**个人同构最小实现（原理复现），非公司代码**，不含任何公司内部资产；取材于 2026.04–08 的 Agent 应用开发经历（原技术栈为 Pi Agent SDK / TypeScript，此处用 Python 复现）。
> 公开仓库只收录源码、项目文档与架构图；`.env`、`data/*.db`、测试临时产物、简历与求职话术类素材均已排除。

---

## 1. 解决什么问题

销售 / BD 每天的工作是「跑店 + 拜访」，但信息散落在 CRM、日历、主管消息等四五个系统里：

| 现状 | 代价 |
|---|---|
| 待办分散在多个系统各一份 | 漏拜访、重复拜访 |
| 建计划要填 8 个字段，一单 3~5 分钟 | 销售抵触、数据不全 |
| 改约要重走表单，冲突靠人脑判断 | 撞单、放客户鸽子 |
| 签到靠定位截图，日志靠回工位回忆 | 过程无证据、无法核验 |
| 批量取消、误改他人计划没有拦截 | 数据事故 |

项目的思路是：把**多点选、多表单压缩成一句话**，把业务规则收敛进 **Skill + Tool Policy + 人工审批**，把整个过程沉淀为**可回放的 Trace**。面向三类角色——销售 / BD（少填表、不漏拜访）、销售主管（计划真实性、异常取消）、平台管理员（能力可控、问题可查）。

## 2. 能做什么

| 场景 | 输入 | 系统行为 |
|---|---|---|
| S1 今日待办 | 「查一下我今天的拜访待办」 | 路由到待办 Skill，返回聚合列表卡片 |
| S2 创建计划 | 「帮我约下周三上午拜访字节跳动」 | 槽位缺失最多追问 2 轮，冲突提示，返回计划卡片 |
| S3 修改计划 | 「把刚才那个改到下午」 | 从会话记忆消解指代，增量修改，回显变更前后 |
| S4 签到 | 「我到店了，签到」 | 定位校验；超范围触发人工审批 |
| S5 拜访日志 | 「客户对新产品感兴趣，下周寄样品」 | 结构化摘要并关联计划 |

扩展能力：**签退、提醒、计划取消、录音纪要**（分片上传 → 聚合 → ASR 多后端 → 结构化纪要 → 日志），以及原生表单降级通道（模型不可用时手工走同一套 FSM 与审计）。

最典型的演示是**双人审批**：销售发起「取消计划」后 Turn 挂起，主管在另一个标签页批准 / 拒绝，原页面通过 SSE 自动继续；审批有 120 秒超时、拒绝路径，且硬性禁止自我审批。

## 3. 快速开始

在本仓库根目录执行（Windows PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r backend\requirements.lock   # 首次也可先用 requirements.txt
Copy-Item .env.example .env                # 生产环境必须修改 AUTH_SECRET
python backend\run.py                     # http://127.0.0.1:8020
```

Linux / macOS：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r backend/requirements.lock
cp .env.example .env
python backend/run.py
```

锁文件里每个包都带 `sha256`，安装时强制校验：依赖被投毒换包会直接失败，而不是悄悄装上一个同名不同内容的版本。

- **演示账号**：`sales-001`（销售）/ `manager-001`（主管），初始密码见 `.env.example` 的 `DEMO_PASSWORD`。
- 首次启动自动建库并写入种子数据（4 条待办 / 3 个计划 / 5 条历史 Trace）；重置：停服后删除 `data/` 再启动。
- `GET /api/health` 会给出 Redis 降级原因；`/api/config` 可查看当前运行配置与版本号。
- 跑测试：**在仓库根目录** `python -m pytest`（配置只有一份，在 `pyproject.toml`；不再需要 `cd backend`）。
- 一条命令验收：`make verify`（Linux / macOS / WSL）。Windows 没有 GNU make 时用等价的一条：`python backend\scripts\verify_all.py`。
  它依次跑 ruff → pytest → 评测门禁 → 冒烟（自带隔离服务）→ 崩溃注入；CI 的步骤与顺序和它一致。

安全默认（会拦住随手起服务，是故意的）：

| 场景 | 行为 | 绕过方式 |
|---|---|---|
| `AUTH_MODE=strict` 且 `AUTH_SECRET` 仍是默认值 | 拒绝启动并报错 | 生产必须设随机 `AUTH_SECRET`；仅本地/CI 可设 `AUTH_ALLOW_INSECURE_SECRET=1` |
| `AUTH_MODE=demo` 且监听非回环地址 | 拒绝启动 | demo 只允许 `127.0.0.1` 演示 |
| 多 worker | 每个 worker 都会跑启动对账，互相抢标记 | 设 `WEB_CONCURRENCY=n` 时同时设 `RECONCILE_ON_START=0`，由单独一次进程对账 |

## 4. 架构与目录

```text
p1-visittong/
├─ .github/workflows/ci.yml 同一套门禁（lock 安装 → 审计 → lint → test → eval → smoke → chaos）
├─ Dockerfile / docker-compose.yml   非 root 运行 + healthcheck；app + redis
├─ Makefile                 本地验收单一入口（make verify）
├─ pyproject.toml           ruff / pytest 唯一配置源
├─ backend/
│  ├─ run.py               启动入口（支持 WEB_CONCURRENCY 多 worker）
│  ├─ requirements*.txt    依赖区间 + 锁定版本
│  ├─ agents/              Agent 能力声明（JSON，启动期读取 / 可热更新）
│  ├─ evals/               行为评测用例（cases.jsonl）与已评审基线（baseline.json）
│  ├─ scripts/             Pi 对照实证、端到端冒烟、行为评测、崩溃注入、备份、测试数据清理
│  ├─ tests/               单元 / 接口测试
│  └─ app/
│     ├─ agent/            事件包络、工具流水线、hooks、上下文压缩、纪要
│     ├─ runtime/          显式装配层：AgentSpec / ResourceLoader / AgentSession / ExecutionContract
│     ├─ orchestrator/     Turn 状态机 + 混合意图路由
│     ├─ domain/           db / fsm / tasks / migrations / idempotency / governance / audio / pipeline
│     ├─ adapters/         crm / poi / asr / notify / knowledge（可替换边界）
│     ├─ api/              auth / chat / visit / audio / knowledge / obs / sse
│     ├─ obs/              结构化日志、request_id、登录限流中间件、Prometheus 指标
│     ├─ policy/           工具风险、角色、危险模式、最严决策合并
│     ├─ sessions/         会话树（entries + leaf 指针）+ 分布式会话锁
│     ├─ skills/           8 个 Skill / 10 个工具与注册表
│     ├─ llm/              mock + OpenAI 兼容客户端
│     └─ trace/            Trace 存储与种子数据
├─ frontend/               Vue3 单页控制台（含登录、工作台、审批、看板）
├─ scripts/                备份 / 恢复脚本
└─ data/                   运行时 SQLite（已忽略，不入库）
```

**一次对话怎么走**：HTTP/SSE 请求 → 鉴权与归属校验 → 混合路由（规则置信度 ≥0.9 直通，其余走 LLM Function Calling 分类，低置信或异常回退规则）→ 选中一个 Skill → 槽位填充 / 指代消解 → Tool Policy（读写 scope、角色、风险）→ 高危操作挂起审批 → 工具执行与检查点落盘 → 卡片 / 增量事件回流 → Trace 与指标。

**能力集在启动期装配，不在代码里写死**：`ResourceLoader` 读 `backend/agents/*.json` → 校验（未知 Skill / 未注册工具 / 孤儿工具直接 fail fast）→ `AgentSession` 持有本次运行的 Skill 白名单、工具白名单、提示词覆写 → `Orchestrator` 只通过它取能力。边界共四层：**装配期裁剪 → 路由期降级（越权 Skill 回落 `chat` 并写 `capability_denied` 事件）→ 执行期 `capability_guard` 硬校验（模型即便吐出白名单外的工具名也不会执行，且写审计）→ PolicyEngine 管角色与风险**。这样「少给模型看几个 schema」不等于「能力边界」，两者都有实现。

**guard 的生效条件（v0.7 修正，避免夸大）**：`AgentSession.authorized_tools()` 现在算的是**实际授权集**——当前技能声明的工具 ∩ Agent 白名单 ∩ 未停用，`allows_tool()` 就是「名字在不在这张表里」，不再有「`spec.tools is None` 就全放行」的隐式通道（那等于让未声明的工具自动获批）。guard 判三件事，任一不满足即拒：**技能已授权 ∧ 工具归属当前技能 ∧ 工具在授权集内且未停用**，拒绝原因落到 `skill_not_authorized / tool_not_registered / cross_skill_denied / not_in_agent_capability / tool_disabled`，可区分「越权」和「配错」。

它真正防的是**能力收紧 / 提示词注入 / 幂等回放污染**下的越权调用；正常全能力 `visit` 装配下 guard 不会误伤，因为那张表就是它该有的集合。回放与 `resume` 两条历史旁路已接回 guard：`ops_readonly` 收紧能力后，用旧检查点回放会在 `test_execution_contract.py` 里被断言拒绝。每次写检查点落 `agent_name` + `capability_fingerprint` + 能力快照，`reload` 后能力集不一致时 `POST /api/tasks/{id}/resume` 返回 `AGENT_CAPABILITY_CHANGED`，`GET /api/tasks/{id}` 的 `resume_compat` 可直接查原因。指纹只绑定**装配授权集**，不含运行期工具启停（否则关一个工具会让所有检查点失效），启停仍由策略引擎每次调用硬拦。

补一条容易踩的：PolicyEngine 现在**必经且取最严**（`deny > approval > allow`），扩展 hook 只能加严不能放宽。旧实现是 `if decision is None: policy.evaluate(...)`，任何一个扩展返回 allow 就能把整段策略判定跳过——角色、风险、距离全失效；`test_hook_cannot_override_policy_deny` 钉住这一点。

```bash
cd backend
python scripts/pi_evidence.py                   # 离线 7 组实证，无需服务 / 无需 API Key
python scripts/run_eval.py --verbose            # 行为评测（与 evals/baseline.json 比对）
python scripts/chaos_resume.py                  # 真 kill -9 崩溃注入，验证副作用恰好一次

AUTH_MODE=demo AUTH_ALLOW_INSECURE_SECRET=1 python run.py   # 默认 visit；AGENT_NAME=ops_readonly 切只读 Agent
# 另开一个终端（demo 模式用 X-User-Id，严格模式改 Bearer Token）：
curl -H "X-User-Id: manager-001" http://127.0.0.1:8020/api/agents
curl -H "X-User-Id: manager-001" http://127.0.0.1:8020/api/governance/manual-overrides
curl -X POST -H "X-User-Id: manager-001" http://127.0.0.1:8020/api/agents/reload
python scripts/smoke_scenarios.py                # 端到端 7 组（服务需保持运行）
```

内置两套声明：`visit`（全能力）与 `ops_readonly`（只装配 `todo_query` + 两个只读工具），用来证明**同一套 Runtime、不同能力集**。装配层实现见 `backend/app/runtime/`，实证见 `backend/scripts/pi_evidence.py`。

## 5. 关键机制

| 能力 | 实现落点 |
|---|---|
| 显式业务 FSM | `domain/fsm.py`：计划 `new→planned→checked_in→checked_out→done/cancelled`；会话 `idle→routing→executing→done/failed`；迁移写 `state_transitions`，`GET /api/plans/{id}` 可查 |
| 执行凭证 | `runtime/contract.py`：`ExecutionContract` 是 `frozen` 数据类，绑定 `turn_id / step / call_id / agent / 能力指纹 / 技能 / 工具 / 主体`；`idem_key = turn:step:call`。契约一旦签发不可改，工具想换工具只能 `with_tool()` 派生新契约——避免「执行到一半凭证被改」这类说不清的事故 |
| Durable Execution | `domain/tasks.py`：`task_runs` / `turn_checkpoints` / `tool_executions` 每步落检查点；`POST /api/tasks/{turn_id}/resume` 断点续跑；启动对账把遗留 `running` 标 `unknown`（不是删除、也不是当没发生），`attempt` 递增留痕 |
| 效果级幂等 | `domain/idempotency.py`：`idempotency_effects(idem_key, effect_type, state)` 先 `running` 后 `done`，重放直接返回同一输出；叠加「同时段唯一计划」这类业务自然唯一性 = **effectively-once**。语义边界见 §8 |
| 崩溃注入 | `scripts/chaos_resume.py`：子进程跑真实工具调用并在 `create_plan` 前后撑开窗口，父进程按 5/25/45/65/90/120ms 偏移 `TerminateProcess`（等同 `kill -9`），重启后同契约重放，断言「计划恰好 1 条、账本 1 行 ok」 |
| 审批等待态持久化 | `approvals.py` + `task_runs.pending_approval_*`：审批以 DB 为唯一事实源，本地 Event 只做加速、最多等一个轮询间隔；重启后 `resume` 能按 `pending_approval_id` 接回那一步，而不是重跑整个 ReAct |
| 混合意图路由 | `orchestrator/router.py`，`ROUTER_MODE=rules/hybrid/llm` 可切，含 LLM 异常回退测试；`awaiting_slots` 显式标记「上一轮是否问了问题」，避免历史槽位把闲聊劫持成业务动作 |
| 分层 Skill / 工具 | 8 个 Skill、10 个工具，注册即用；`ToolSpec.scope=read/write` + `roles` 拒绝越权调用 |
| 显式装配 / 能力边界 | `runtime/`：`AgentSpec`（JSON 能力声明）、`ResourceLoader`（校验 + reload + 渐进披露）、`AgentSession`（授权集 + 指纹 + guard）；`agent/pipeline.py` 执行期硬校验（含回放 / resume 路径）；`GET /api/agents`、`GET /api/agents/{name}/skills` 可查 |
| 人工降级通道 | `POST /api/plans/{id}/cancel` 仅 manager/admin，**显式豁免 Agent 审批**并写 `waiver_reason`；`GET /api/governance/manual-overrides` 拉豁免台账（主管），同一次豁免只计一条（`governance_events` 优先于 `audit_log` 历史记录）。仍走 FSM 与审计 |
| 治理与审计 | 两张表分工：`audit_log` 记「谁做了什么」，`governance_events` 只追加、记「治理状态为什么变了」（工具启停、Agent 热更新、审批豁免、Token 撤销）；`GET /api/audit`、`GET /api/governance/events`（主管）；写工具、审批决策、手工接口、知识同步、ASR 均留痕；`Idempotency-Key` 防重放 |
| 录音 → 纪要 | 分片上传 → Redis/内存聚合 → ASR（mock / faster_whisper / command）→ 结构化纪要 |
| 知识检索 | 本地向量索引（hash-gram embedding + 余弦）+ Milvus 适配接口；精确匹配失败时语义兜底 |
| 数据管道 | `domain/pipeline.py` 用事件箱 + `drain()` 模拟 Hive → Kafka 同步 |
| schema 演进 | `domain/migrations.py`：版本化迁移（`schema_migrations` + `BEGIN IMMEDIATE` 单事务），不再散落 `PRAGMA` + `ALTER`；逐条执行而非 `executescript`（后者隐式 COMMIT，会把「要么全成」变成「成一半」） |
| 可观测 | `GET /api/metrics`、`/api/metrics/prometheus`（带 `# HELP` / `# TYPE`）、`/api/traces?status=&skill=`；日志为单行 JSON + `request_id` + `trace_id`，`X-Request-Id` 可跨端对齐 |

## 6. 鉴权与权限

| 能力 | 规则 |
|---|---|
| 登录 | `POST /api/auth/login` → Bearer Token（HMAC-SHA256，默认 8h，含 `jti`） |
| 登出 / 全端下线 | `POST /api/auth/logout` 按 `jti` 撤销单个 token；`POST /api/auth/revoke-all` 把用户 `token_epoch` +1，历史 token 全失效 |
| 匿名访问 | 除 `login` / `health` / `config` 外一律 401 |
| 会话 | 仅本人可读写，主管可查看 |
| 审批 | 仅 `manager/admin` 可查看 / 决策，**不能审批自己发起的操作**（`SELF_APPROVAL_FORBIDDEN`） |
| 审计 / 指标 / 知识同步 / 工具启停 | 仅 `manager/admin` |
| 登录限流 | 同账号 + IP 连续 5 次失败锁定 5 分钟；计数落 `login_attempts` 表（进程重启不清零） |
| 跨域 | CORS 只放行配置里的 `cors_origins`，**不再回落到 `*`**；未配置即不放行 |
| demo 模式 | `AUTH_MODE=demo` 额外允许 `X-User-Id`（仅本地回环地址，默认关闭） |
| 身份缺失 | 角色或用户为空一律 `deny`（`identity_unverified`）——fail-closed，不猜 |

## 7. 验证结果

全部可现场复现；命令就是 `make verify`（少了任何一环都不算过）。

| 环节 | 命令 | 结果 |
|---|---|---|
| 静态检查 | `ruff check backend` | 0 告警（E/F/I 全开） |
| 单元 / 集成 | `python -m pytest` | **115 passed**（179s） |
| 行为评测 | `python backend/scripts/run_eval.py` | 25 条用例：路由 1.0 / 审批覆盖 1.0 / 禁调工具违规 0 / 用例通过率 1.0；1 条需真模型的用例显式跳过 |
| 端到端冒烟 | `python backend/scripts/smoke_scenarios.py` | **7/7**（含双通道治理、能力快照、断点续跑兼容） |
| 崩溃注入 | `python backend/scripts/chaos_resume.py` | **6/6** 满足「副作用恰好一次」 |
| Pi 架构对照 | `python backend/scripts/pi_evidence.py` | 7 组实证，指纹 `16e6fe692461…` 稳定 |

崩溃注入的实际分布（这才是它有价值的地方，不是「全绿」两个字）：

| 击杀偏移 | 崩溃时业务表 | 崩溃时账本 | 恢复后 | 判定 |
|---|---|---|---|---|
| 5 / 25 / 45ms | 0 条（副作用未提交） | `running` | 建 1 条，账本 `ok` | 0+1 ✔ |
| 65 / 90 / 120ms | 1 条（已提交、账本未终态） | `running` | 认领已有那条，仍是 1 条 | 1+0 ✔ |

> **这组实验抓到了一个真 bug**：认领分支读 `conflict["customer_name"]`，而 `plan_conflict` 只 `SELECT` 了三列 → `KeyError`，副作用明明成功、恢复却报 `error`。修完才 6/6。写测试的价值就在这里，全绿之前先让测试把自己打疼。

> **评测 1.0 该怎么读**：用例与 mock 规则同源（mock LLM 只有关键词规则），1.0 说明的是「这套行为没被我改坏」，不说明线上语义理解力。它作为**回归门禁**有效——把用例改坏 / 把规则改坏都会红；它不能当作模型能力的证据。需要真模型才能判定的用例（如「见一下国贸的客户」这类语义泛化）已标 `requires: llm` 并显式跳过，不把 mock 的天花板记成系统退化。

> **p95 为什么不做门禁**：同一份代码在同一台机器连跑两次，P95 从 996ms 到 1439ms（+44%）。mock LLM 下的 P95 量的是测试进程调度抖动，不是产品性能。会随机变红的门禁最后一定被人关掉，那比没有门禁更糟——所以延迟只报告，卡性能请用专门的基准测试。

- 指标口径（演示集）：计划创建 4 轮内完成、平均对话轮次 ≤3、**Agent 自主通道**的高危动作 100% 进入审批、100% Turn 有 Trace。人工降级通道是登记在册的显式豁免，不计入该分母（见下）。

> **关于「100% 审批」的准确口径**：模型自主发起的高危动作（`cancel_visit_plan` 等）无例外进入 HITL；但系统保留一条**有意建模**的人工通道——主管在模型不可用时可直接取消计划，不经过 Agent 审批。这不是未声明的绕过：限定 manager/admin、销售本人不可替代操作、仍走显式 FSM 迁移表、仍写审计，且豁免理由 `explicit_human_action_by_manager` 与台账端点都可举证。设计权衡是：审批约束的是「模型自主发起的可能误判动作」，不是「有身份、有明确意图的人」；若主管不在时模型又不可用，把人工通道也锁进审批等于业务停摆。详见 `routes_visit.py:cancel_plan_manual` 与 `test_governance.py`。

## 8. 边界与已知限制

- 仅 Mock CRM / POI；ASR 默认 mock，真实转写需安装 `faster-whisper` 或配置 `ASR_COMMAND` 网关。
- 知识库为本地向量索引，Milvus 仅为接口占位；Kafka / Hive 为事件箱模拟。
- MySQL 只完成方言翻译与驱动接线，未在真实 MySQL 实例验证；默认仍是 SQLite 单机。**版本化迁移目前只对 SQLite 执行**（`migrations.apply(sqlite_path)`），走 MySQL 部署前需要补一套等价 DDL —— 这条没有做，不算已支持。
- **幂等是「有效一次」不是分布式事务**：`create_visit_plan / checkin / checkout / update_visit_plan` 有业务自然唯一键（同时段唯一计划、计划状态机），崩溃重放能认领已有结果；`save_visit_log` 没有自然唯一键，极端情况下（副作用已提交、账本未终态、进程死）重放可能留下重复日志。这是取舍，不是疏漏：给它编一个唯一键反而会把「同一计划写两条真实日志」这种合法场景挡掉。
- 多 worker 需显式设 `RECONCILE_ON_START=0`，并由一次单独进程做启动对账：`python -m app.maintenance reconcile`（或 `make reconcile`）。否则每个 worker 都会把别人的 `running` 记成 `unknown`。
- 会话锁在 Redis 上（`SET NX` + owner token + Lua 校验删除）。Redis 不可用时降级为内存锁，**单机语义正确、多实例语义不成立**。
- 前端用 `localStorage` 保存登录态：双人审批演示时主管在第二个标签登录会覆盖共享存储，销售标签请勿刷新（token 仍在内存中）。
- `AUTH_ALLOW_INSECURE_SECRET=1` 只用于本地与 CI：它绕过「默认密钥拒绝启动」这条安全检查，任何非测试环境开启都等于自废鉴权。
- 本项目只做「同构最小实现 / 原理复现」，不声称重写公司系统；数据 mock，但流程、表结构与治理口径对齐真实系统。

## 9. 本系列项目

三个仓库互相独立、不共享代码，只共享方法论（上下文分层注入、工具执行治理、状态可追溯）：

| 仓库 | 定位 | 形态 |
|---|---|---|
| **p1-visittong**（本仓库） | 业务侧 Agent Runtime：会话 / Skill / 审批 / Trace | Web 工作台 + 后台观测 |
| [p2-ai-native-platform](https://github.com/whs2007/p2-ai-native-platform) | 研发侧流程工程化：阶段 / 门控 / 产物 / Hooks | CLI + 本地 Web 控制台 |
| [p3-agentops](https://github.com/whs2007/p3-agentops) | 通用 Agent 管控原型（P1 后端由其演化） | Web 控制台 |

---

代码与文档仅用于学习、演示与技术交流，不含任何公司内部资产。
