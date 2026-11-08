# 拜访通 · BD 拜访 Agent

> 面向企业销售 / BD 的**拜访任务 Agent**：一句话完成「查待办 → 建计划 → 改约 / 取消（人工审批）→ 签到 → 签退 → 录音纪要 → 日志」。
> 底层是一套可拆插的 **Agent Runtime**：显式装配层（AgentSpec / ResourceLoader / AgentSession）、会话树与上下文压缩、分层 Skill 与混合意图路由、Tool Policy / HITL 审批 / 操作审计、Durable Execution（检查点、续跑、幂等）、全链路 Trace 与指标。
> v0.5 起，能力集由配置在**装配期**决定，同一套 Runtime 可装配出不同能力集的 Agent，能力白名单在**执行期**硬校验；检查点绑定能力指纹，跨装配续跑会被显式拒绝。
> 边界先说清：内置两套声明是**同一套 Skill 的不同子集**（全能力 / 只读），不是两个不同领域；执行期 guard 只在**能力被收紧**的 Agent 上才会真正拦截。

| 项 | 状态 |
|---|---|
| 版本 | v0.6 · 2026-11-08 |
| 运行 | ✅ 可运行；Mock 模式无需 API Key，Redis / MySQL 不可用时自动降级 |
| 测试 | `pytest` **86 passed**（FSM、断点续跑与能力指纹、路由回退、权限治理、执行期能力边界、人工通道豁免、ASR、知识检索、SQL 方言、观测） |
| 演示 | `backend/scripts/smoke_scenarios.py` 7 组可复现场景（含双通道治理与能力快照），看板 100% 成功率 / 5 条种子 Trace |
| 技术栈 | Python 3.11+ / FastAPI / SQLite（可选 MySQL、Redis）/ Vue3 / SSE / pytest |

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
pip install -r backend\requirements.lock   # 首次也可先用 requirements.txt
Copy-Item .env.example .env                # 生产环境必须修改 AUTH_SECRET
python backend\run.py                     # http://127.0.0.1:8020
```

Linux / macOS：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.lock
cp .env.example .env
python backend/run.py
```

- **演示账号**：`sales-001`（销售）/ `manager-001`（主管），初始密码见 `.env.example` 的 `DEMO_PASSWORD`。
- 首次启动自动建库并写入种子数据（4 条待办 / 3 个计划 / 5 条历史 Trace）；重置：停服后删除 `data/` 再启动。
- `GET /api/health` 会给出 Redis 降级原因；`/api/config` 可查看当前运行配置。
- 跑测试：`cd backend && python -m pytest`（Windows 下建议先把 `TMP` / `TEMP` 指到 `data\_pytest_tmp`）。

## 4. 架构与目录

```text
p1-visittong/
├─ assets/                 架构图
├─ docs/                   需求 → PRD → 技术方案 → 数据模型 → 验收 → 架构对照
├─ backend/
│  ├─ run.py               启动入口
│  ├─ requirements*.txt    依赖锁定
│  ├─ agents/              Agent 能力声明（JSON，启动期读取 / 可热更新）
│  ├─ scripts/             Pi 架构对照实证、端到端冒烟（均可复现）
│  ├─ tests/               单元 / 接口测试
│  └─ app/
│     ├─ agent/            事件包络、工具流水线、hooks、上下文压缩、纪要
│     ├─ runtime/           显式装配层：AgentSpec / ResourceLoader / AgentSession
│     ├─ orchestrator/     Turn 状态机 + 混合意图路由
│     ├─ domain/           db / fsm / tasks / audio / pipeline
│     ├─ adapters/         crm / poi / asr / notify / knowledge（可替换边界）
│     ├─ api/              auth / chat / visit / audio / knowledge / obs / sse
│     ├─ policy/           工具风险、角色、危险模式
│     ├─ sessions/         会话树（entries + leaf 指针）
│     ├─ skills/           8 个 Skill / 10 个工具与注册表
│     ├─ llm/              mock + OpenAI 兼容客户端
│     └─ trace/            Trace 存储与种子数据
├─ frontend/               Vue3 单页控制台（含登录、工作台、审批、看板）
├─ scripts/                备份 / 恢复脚本
└─ data/                   运行时 SQLite（已忽略，不入库）
```

**一次对话怎么走**：HTTP/SSE 请求 → 鉴权与归属校验 → 混合路由（规则置信度 ≥0.9 直通，其余走 LLM Function Calling 分类，低置信或异常回退规则）→ 选中一个 Skill → 槽位填充 / 指代消解 → Tool Policy（读写 scope、角色、风险）→ 高危操作挂起审批 → 工具执行与检查点落盘 → 卡片 / 增量事件回流 → Trace 与指标。

**能力集在启动期装配，不在代码里写死**：`ResourceLoader` 读 `backend/agents/*.json` → 校验（未知 Skill / 未注册工具 / 孤儿工具直接 fail fast）→ `AgentSession` 持有本次运行的 Skill 白名单、工具白名单、提示词覆写 → `Orchestrator` 只通过它取能力。边界共四层：**装配期裁剪 → 路由期降级（越权 Skill 回落 `chat` 并写 `capability_denied` 事件）→ 执行期 `capability_guard` 硬校验（模型即便吐出白名单外的工具名也不会执行，且写审计）→ PolicyEngine 管角色与风险**。这样「少给模型看几个 schema」不等于「能力边界」，两者都有实现。

**guard 的生效条件（避免夸大）**：默认 `visit` 装配的是全量已注册工具，`allows_tool()` 恒为真，guard 在这条路径上一次都不会触发——全能力 Agent 本就不该拦任何已注册工具。它真正防的是**能力收紧后的 Agent 被幻觉 / 提示词注入 / 回放污染诱导调用白名单外工具**；`ops_readonly` 下由 `test_execution_guard_blocks_unauthorized_tool` 断言假 handler 一次都没执行。此外幂等回放与 `resume` 两条历史旁路已接回 guard；每次写检查点都落 `agent_name` + `capability_fingerprint` + 能力快照，`reload` 后能力集不一致时 `POST /api/tasks/{id}/resume` 返回 `AGENT_CAPABILITY_CHANGED`，`GET /api/tasks/{id}` 的 `resume_compat` 可直接查原因。指纹只绑定**装配授权集**，不含运行期工具启停（否则关一个工具会让所有检查点失效），启停仍由策略引擎每次调用硬拦。

```bash
cd backend
python scripts/pi_evidence.py                   # 离线 7 组实证，无需服务 / 无需 API Key

AUTH_MODE=demo python run.py                    # 默认 visit；AGENT_NAME=ops_readonly 切只读 Agent
# 另开一个终端（demo 模式用 X-User-Id，严格模式改 Bearer Token）：
curl -H "X-User-Id: manager-001" http://127.0.0.1:8020/api/agents
curl -H "X-User-Id: manager-001" http://127.0.0.1:8020/api/governance/manual-overrides
curl -X POST -H "X-User-Id: manager-001" http://127.0.0.1:8020/api/agents/reload
python scripts/smoke_scenarios.py                # 端到端 7 组（服务需保持运行）
```

内置两套声明：`visit`（全能力）与 `ops_readonly`（只装配 `todo_query` + 两个只读工具），用来证明**同一套 Runtime、不同能力集**。详见 [docs/07](docs/07-Pi-Agent架构对照与优化.md)。

## 5. 关键机制

| 能力 | 实现落点 |
|---|---|
| 显式业务 FSM | `domain/fsm.py`：计划 `new→planned→checked_in→checked_out→done/cancelled`；会话 `idle→routing→executing→done/failed`；迁移写 `state_transitions`，`GET /api/plans/{id}` 可查 |
| Durable Execution | `domain/tasks.py`：`task_runs` / `turn_checkpoints` / `tool_executions` 每步落检查点；`POST /api/tasks/{turn_id}/resume` 断点续跑；工具级 `(turn_id, step, call_id)` 唯一约束保证幂等回放；启动把遗留 `running` 标为 `interrupted` |
| 混合意图路由 | `orchestrator/router.py`，`ROUTER_MODE=rules/hybrid/llm` 可切，含 LLM 异常回退测试 |
| 分层 Skill / 工具 | 8 个 Skill、10 个工具，注册即用；`ToolSpec.scope=read/write` + `roles` 拒绝越权调用 |
| 显式装配 / 能力边界 | `runtime/`：`AgentSpec`（JSON 能力声明）、`ResourceLoader`（校验 + reload + 渐进披露）、`AgentSession`（能力视图 + 指纹）；`agent/pipeline.py` 的 `capability_guard` 执行期硬校验（含回放 / resume 路径）；`GET /api/agents`、`GET /api/agents/{name}/skills` 可查 |
| 人工降级通道 | `POST /api/plans/{id}/cancel` 仅 manager/admin，**显式豁免 Agent 审批**并写 `waiver_reason`；`GET /api/governance/manual-overrides` 拉豁免台账（主管）。仍走 FSM 与审计 |
| 治理与审计 | `audit_log` + `GET /api/audit`（主管）；写工具、审批决策、手工接口、知识同步、ASR 均留痕；`Idempotency-Key` 防重放 |
| 录音 → 纪要 | 分片上传 → Redis/内存聚合 → ASR（mock / faster_whisper / command）→ 结构化纪要 |
| 知识检索 | 本地向量索引（hash-gram embedding + 余弦）+ Milvus 适配接口；精确匹配失败时语义兜底 |
| 数据管道 | `domain/pipeline.py` 用事件箱 + `drain()` 模拟 Hive → Kafka 同步 |
| 可观测 | `GET /api/metrics`、`/api/metrics/prometheus`、`/api/traces?status=&skill=`，日志带 trace/session/user |

## 6. 鉴权与权限

| 能力 | 规则 |
|---|---|
| 登录 | `POST /api/auth/login` → Bearer Token（HMAC-SHA256，默认 8h） |
| 匿名访问 | 除 `login` / `health` / `config` 外一律 401 |
| 会话 | 仅本人可读写，主管可查看 |
| 审批 | 仅 `manager/admin` 可查看 / 决策，**不能审批自己发起的操作**（`SELF_APPROVAL_FORBIDDEN`） |
| 审计 / 指标 / 知识同步 / 工具启停 | 仅 `manager/admin` |
| 登录限流 | 同账号 + IP 连续 5 次失败锁定 5 分钟 |
| demo 模式 | `AUTH_MODE=demo` 额外允许 `X-User-Id`（仅本地调试，默认关闭） |

## 7. 验证结果

- 冒烟测试：`python backend/scripts/smoke_scenarios.py`（需先 `AUTH_MODE=demo python backend/run.py`），7 组全通，含双通道治理与能力快照；看板 100% 成功率、5 条种子 Trace。脚本在公开仓库内，数字可现场复现。
- `pytest` **86 passed**：覆盖 FSM、模拟崩溃后断点续跑与能力指纹、路由与 LLM 异常回退、权限治理、Agent 显式装配与执行期能力边界、人工通道豁免台账、ASR、知识检索、MySQL 方言翻译、可观测性。
- 指标口径（演示集）：计划创建 4 轮内完成、平均对话轮次 ≤3、**Agent 自主通道**的高危动作 100% 进入审批、100% Turn 有 Trace。人工降级通道是登记在册的显式豁免，不计入该分母（见下）。

> **关于「100% 审批」的准确口径**：模型自主发起的高危动作（`cancel_visit_plan` 等）无例外进入 HITL；但系统保留一条**有意建模**的人工通道——主管在模型不可用时可直接取消计划，不经过 Agent 审批。这不是未声明的绕过：限定 manager/admin、销售本人不可替代操作、仍走显式 FSM 迁移表、仍写审计，且豁免理由 `explicit_human_action_by_manager` 与台账端点都可举证。设计权衡是：审批约束的是「模型自主发起的可能误判动作」，不是「有身份、有明确意图的人」；若主管不在时模型又不可用，把人工通道也锁进审批等于业务停摆。详见 `routes_visit.py:cancel_plan_manual` 与 `test_governance.py`。

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [需求与场景](docs/01-需求与场景.md) | 背景、角色、5 个核心场景、边界、指标 |
| [PRD](docs/02-PRD.md) | 28 条功能（F1.x）、页面与交互 |
| [技术方案](docs/03-技术方案.md) | 选型、架构、会话树、Skill 编排、审批、避坑 |
| [数据模型与接口契约](docs/04-数据模型与接口契约.md) | 13 张表 DDL、Redis Key、REST/SSE/卡片协议、错误码 |
| [里程碑与验收](docs/05-里程碑与验收.md) | 7 天拆分、AC-01~AC-22、DoD |
| [Pi Agent 架构对照与优化](docs/07-Pi-Agent架构对照与优化.md) | 按 Pi（earendil-works/pi）对齐事件流、工具扩展点、会话树、压缩 |

> 演示脚本与求职话术类文档、简历截图属于非项目素材，未收录进公开仓库。

## 9. 边界与已知限制

- 仅 Mock CRM / POI；ASR 默认 mock，真实转写需安装 `faster-whisper` 或配置 `ASR_COMMAND` 网关。
- 知识库为本地向量索引，Milvus 仅为接口占位；Kafka / Hive 为事件箱模拟。
- MySQL 只完成方言翻译与驱动接线，未在真实 MySQL 实例验证；默认仍是 SQLite 单机，多实例需换 MySQL 并共享 Redis。
- 前端用 `localStorage` 保存登录态：双人审批演示时主管在第二个标签登录会覆盖共享存储，销售标签请勿刷新（token 仍在内存中）。
- 本项目只做「同构最小实现 / 原理复现」，不声称重写公司系统；数据 mock，但流程、表结构与治理口径对齐真实系统。

## 10. 本系列项目

三个仓库互相独立、不共享代码，只共享方法论（上下文分层注入、工具执行治理、状态可追溯）：

| 仓库 | 定位 | 形态 |
|---|---|---|
| **p1-visittong**（本仓库） | 业务侧 Agent Runtime：会话 / Skill / 审批 / Trace | Web 工作台 + 后台观测 |
| [p2-ai-native-platform](https://github.com/whs2007/p2-ai-native-platform) | 研发侧流程工程化：阶段 / 门控 / 产物 / Hooks | CLI + 本地 Web 控制台 |
| [p3-agentops](https://github.com/whs2007/p3-agentops) | 通用 Agent 管控原型（P1 后端由其演化） | Web 控制台 |

---

代码与文档仅用于学习、演示与技术交流，不含任何公司内部资产。
