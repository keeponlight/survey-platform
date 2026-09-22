# 架构确认与落地决策（万级智能体模拟问卷平台 MVP）

> 阶段 2 交付物（架构师高见远）。对应主文档 §3 模块边界、§6 持久化与状态机、§9 目录；节点 N1–N8 的架构基线。
> 与主文档冲突处一律「写明冲突 + 最小修改理由」；本文**不改需求**，只做落地细化。

## 1. 模块边界确认（主文档 §3）

主文档 §3 定义的 6 个模块边界，本项目**全部确认采纳**，并补足落地接口：

| 模块 | 输入 | 输出 | 明确不负责 | 落地位置（主文档 §9 路径） |
|---|---|---|---|---|
| PersonaSource | 用户表、列映射 | 原始行号、用户 ID、画像快照 | 不生成/不补全/不自动过滤 | `backend/app/personas/source.py` |
| Survey | 产品信息、单题 | 可编辑草稿、运行时不可变快照 | 不做跳题/多题 | `backend/app/surveys/service.py` |
| RunService | 问卷、人群、模型配置、预算 | 批次与成员任务 | 不在 HTTP 请求里跑万人调用 | `backend/app/runs/service.py` |
| Worker | 可执行成员、冻结配置 | 有效答案或明确错误、调用记录 | 不产出汇总「推测值」 | `backend/app/worker/{main,limits,execute}.py` |
| ProviderAdapter | 独立 messages、输出约束、超时 | 原文、usage、请求 ID、错误类型 | 不自动更换供应商/模型 | `backend/app/inference/provider.py` |
| ReportService | 数据库里的有效结果 | 统计、分组、逐人 CSV | 不调用 LLM 计算百分比 | `backend/app/reports/{service,export}.py` |

**确认结论：** 边界清晰、职责正交，**无需调整**。补一条落地约束（源自 §3 正文）：FastAPI 收到请求**只返回批次 ID**；worker 为**独立进程**，禁止用 Web 请求线程或简单后台回调承载长任务。

### 1.1 目录确认（主文档 §9）与冲突点

主文档 §9 目录**整体确认采纳**。仅记录 2 处**非阻断性**提示（不构成需求冲突）：

| 编号 | 提示点 | 最小修改理由 / 处理 |
|---|---|---|
| D1 | §9 同时含 `backend/app/main.py`（HTTP 服务）与 `backend/app/worker/main.py`（worker） | 非冲突：**同一镜像、不同启动命令**（`uvicorn app.main:app` vs `python -m app.worker.main`）。文档中显式标注，避免 engineer 把 worker 循环写进 API 进程 |
| D2 | §9 未列 `backend/app/inference/__init__.py`、`worker/__init__.py` 等包标记 | 最小补充：实现时按需补空 `__init__.py`，不改变 §9 语义 |

> 与 PRD v1 §7 冲突记录对齐：C1（Python 版本）见本文 §4；C2/C3 与本文 D1/§7 限流一致。

---

## 2. 五张业务表 DDL 要点（照抄主文档 §6.1 命名，不得改名）

**通用约定（主文档 §6.1）：** 金额用 `Decimal`/数据库 `numeric`；每 run 固定币种与计价版本；`unknown` 不写成 `0`；`status` 用**检查约束**；时间统一 `UTC`（用 `timestamptz`）；成员状态/`next_attempt_at`、`run_id`、`lease_expires_at` 建索引；`raw_output` 有长度上限；错误日志不复制整份画像。表名/字段名 = 主文档 §6.1 原文。

### 2.1 `imports`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `uuid` | PK |
| `file_hash` | `text` | NOT NULL（保留内容 hash，原文件解析后可删） |
| `sheet_name` | `text` | NULL（CSV 为 NULL；XLSX 为用户指定工作表） |
| `column_mapping_json` | `jsonb` | NOT NULL |
| `row_count` | `integer` | NOT NULL，`CHECK (row_count >= 0 AND row_count <= 20000)` |
| `snapshots_json` | `jsonb` | NOT NULL（映射后必要字段的行快照，含 `row_no`/`persona_id`/`profile_text`/可选 `age`/`city_tier`/`source_version`） |
| `created_at` | `timestamptz` | NOT NULL DEFAULT `now()` |

- 索引：`idx_imports_created_at (created_at DESC)`。
- 首版限定 20 MB（应用层校验，非 DDL）。

### 2.2 `surveys`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `uuid` | PK |
| `title` | `text` | NOT NULL |
| `product_json` | `jsonb` | NOT NULL（名称、说明、**实际人民币价格 + 计价单位**、购买时间范围、可选购买条件） |
| `question_json` | `jsonb` | NOT NULL（单题 `purchase_intent` + 五档） |
| `revision` | `integer` | NOT NULL DEFAULT `1`，`CHECK (revision >= 1)` |
| `created_at` | `timestamptz` | NOT NULL DEFAULT `now()` |
| `updated_at` | `timestamptz` | NOT NULL DEFAULT `now()` |

- `PATCH` 携带 `expected_revision`；不匹配 → 409；成功则 `revision += 1` 并更新 `updated_at`。

### 2.3 `runs`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `uuid` | PK |
| `survey_id` | `uuid` | NOT NULL，FK → `surveys(id)` |
| `survey_snapshot` | `jsonb` | NOT NULL（冻结题目快照） |
| `import_id` | `uuid` | NOT NULL，FK → `imports(id)` |
| `model_snapshot` | `jsonb` | NOT NULL（**不含密钥**的模型参数 + 配置 hash） |
| `prompt_version` | `text` | NOT NULL（首版 `purchase-intent-zh-v1`） |
| `source_version` | `text` | NOT NULL（= import 的 `source_version`） |
| `sample_size` | `integer` | NOT NULL，`CHECK (sample_size > 0 AND sample_size <= 20000)` |
| `status` | `text` | NOT NULL，`CHECK (status IN ('ready','running','pausing','paused','cancelling','cancelled','completed','completed_with_errors','failed'))`，DEFAULT `'ready'` |
| `pause_reason` | `text` | NULL，`CHECK (pause_reason IS NULL OR pause_reason IN ('user','api_auth','api_unavailable','budget'))` |
| `budget_limit` | `numeric(18,6)` | NULL（可空 = 未设金额上限） |
| `budget_currency` | `text` | NOT NULL DEFAULT `'CNY'` |
| `actual_cost` | `numeric(18,6)` | NOT NULL DEFAULT `0` |
| `reserved_cost` | `numeric(18,6)` | NOT NULL DEFAULT `0` |
| `unknown_cost_count` | `integer` | NOT NULL DEFAULT `0` |
| `request_limit` | `integer` | NOT NULL DEFAULT `(3 * sample_size)`（由应用写入） |
| `requests_reserved` | `integer` | NOT NULL DEFAULT `0` |
| `control_events_json` | `jsonb` | NOT NULL DEFAULT `'[]'`（锁 run 行事务内追加 action/idempotency_key/request_hash/结果/时间） |
| `idempotency_key` | `text` | **UNIQUE** |
| `request_hash` | `text` | NOT NULL |
| `created_at` | `timestamptz` | NOT NULL DEFAULT `now()` |
| `started_at` | `timestamptz` | NULL |
| `finished_at` | `timestamptz` | NULL |

- 索引：`idx_runs_status (status)`、`idx_runs_created_at (created_at DESC)`。
- **活动批次唯一性 —— DB 级兜底（改判 A1，必须有）：**
```sql
CREATE UNIQUE INDEX uq_runs_single_active ON runs ((true))
  WHERE status IN ('running','pausing','paused','cancelling');
```
  - **不能**写成 `UNIQUE(status)`：`running` 与 `paused` 不互相冲突，漏洞依旧；必须用 `((true))` 表达式 + 部分索引，使「至多一行处于活动状态」成为 DB 硬约束。
  - 命中冲突时 `IntegrityError` **必须映射为 409**，不得泄漏 500。
  - 应用层条件查询（§6.4）**保留**，作为可读的**前置快速校验**；DB 索引为最终裁决。
  - **为什么不能用纯应用层**：`SELECT … WHERE status IN (活动态) … FOR UPDATE` 在**不存在**其他活动批次时结果集为空，`FOR UPDATE` **不锁任何行**，两个并发 `start` 事务会双双通过检查 → 竞态。故必须有 DB 级兜底。
- `started_at` 语义：**首次开始时间（审计用，`retry-failed` 后保留原值不清空）**；`finished_at` 在 `retry-failed` 转 `ready` 时清空。**运行详情页的「耗时」须标注为「累计时长（含暂停/重试等待）」**，避免被读成纯执行时长。

### 2.4 `run_members`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `uuid` | PK |
| `run_id` | `uuid` | NOT NULL，FK → `runs(id)` ON DELETE CASCADE |
| `row_no` | `integer` | NOT NULL，`CHECK (row_no >= 1)` |
| `persona_id` | `text` | NOT NULL |
| `persona_snapshot` | `jsonb` | NOT NULL（含 `profile_text`、可选 `age`/`city_tier`） |
| `status` | `text` | NOT NULL，`CHECK (status IN ('pending','running','succeeded','retry_wait','failed','cancelled'))`，DEFAULT `'pending'` |
| `answer_json` | `jsonb` | NULL（成功时写 `{"question_id":"purchase_intent","value":...,"reason":...}`） |
| `attempt_count` | `integer` | NOT NULL DEFAULT `0` |
| `attempt_limit` | `integer` | NOT NULL DEFAULT `3` |
| `next_attempt_at` | `timestamptz` | NULL（`retry_wait` 时使用） |
| `lease_token` | `uuid` | NULL |
| `lease_expires_at` | `timestamptz` | NULL |
| `last_error_code` | `text` | NULL |
| `updated_at` | `timestamptz` | NOT NULL DEFAULT `now()` |

- **UNIQUE(run_id, persona_id)**（主文档明确要求）。
- 另补 `UNIQUE(run_id, row_no)`（**裁定 #2 接受，最小必要补充**）：它与 `UNIQUE(run_id, persona_id)` **合起来**保证 run 内 `row_no ↔ persona_id` **双射**，即「N 进 N 出、逐行对齐」的 DB 级保证——既无重号也不丢行，且不存在两个 `row_no` 指向同一 `persona_id`。
- 索引：`idx_members_run_status_next (run_id, status, next_attempt_at)`、`idx_members_lease (run_id, lease_expires_at)`（恢复扫描用）、`idx_members_run_row (run_id, row_no)`（结果排序）。
- **每个成功成员只有一条有效答案**：由 `UNIQUE(run_id, persona_id)` + CAS 保存（§6.6）共同保证。

### 2.5 `attempts`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `uuid` | PK |
| `member_id` | `uuid` | NOT NULL，FK → `run_members(id)` ON DELETE CASCADE |
| `attempt_no` | `integer` | NOT NULL，`CHECK (attempt_no >= 1)` |
| `lease_token` | `uuid` | NOT NULL |
| `started_at` | `timestamptz` | NOT NULL |
| `finished_at` | `timestamptz` | NULL |
| `status` | `text` | NOT NULL，`CHECK (status IN ('running','succeeded','failed','abandoned'))`，DEFAULT `'running'` |
| `provider_request_id` | `text` | NULL |
| `raw_output` | `text` | NULL（**长度上限**：`CHECK (raw_output IS NULL OR length(raw_output) <= 20000)`） |
| `error_code` | `text` | NULL |
| `usage_json` | `jsonb` | NULL（`input_tokens`/`output_tokens` 可空；空 ≠ 0） |
| `reserved_cost` | `numeric(18,6)` | NOT NULL DEFAULT `0` |
| `actual_cost` | `numeric(18,6)` | NULL（**未知费用保持 NULL 而非 0**） |
| `duration_ms` | `integer` | NULL |

- **UNIQUE(member_id, attempt_no)**（主文档明确要求）。
- `abandoned`（**裁定 #3 接受该命名**）语义：租约过期恢复时，原 attempt 标记为「结果未知」；其 **`reserved_cost` 必须保留、不得按 0 释放**，直至供应商对账或管理员明确结算；成员随后按剩余次数转 `retry_wait` 或 `failed`（与 §6.3 一致）。
- 索引：`idx_attempts_started (started_at DESC)`（worker 重启重建最近 60s 限流窗口用）、`idx_attempts_member (member_id)`。

> **落库顺序提醒：** 领取事务先写 `attempts(status='running')` 再提交；结果事务更新 `attempts` + 写答案 + 结算成本。旧 attempt 迟到费用可按 `id` 单独结算，`UPDATE` 幂等（`WHERE id=:id AND actual_cost IS NULL`），不漏账不重复。

---

## 3. 状态机（Run / Member）

### 3.1 Run 状态与合法转移

主文档 §6.2 原文：`ready → running → completed / completed_with_errors / failed`；控制分支 `running → pausing → paused → running`；`ready/running/pausing/paused → cancelling → cancelled`。

| 当前状态 | 合法目标 | 触发 |
|---|---|---|
| `ready` | `running` | `POST /runs/{id}/start`（须无其它活动批次） |
| `ready` | `cancelling` | `POST /runs/{id}/cancel` |
| `running` | `pausing` | `pause` |
| `running` | `cancelling` | `cancel` |
| `running` | `completed` | 收敛：无 pending/running/retry_wait 且**全部成功** |
| `running` | `completed_with_errors` | 收敛：部分成功、部分失败 |
| `running` | `failed` | 收敛：全部失败 |
| `pausing` | `paused` | 在途请求全部返回/超时/租约收敛后 |
| `pausing` | `cancelling` | `cancel`（暂停中途取消） |
| `paused` | `running` | `resume`（用同一成员与模型快照，只跑剩余） |
| `paused` | `cancelling` | `cancel` |
| `cancelling` | `cancelled` | 在途收敛后；用户取消**优先**收敛到 cancelled |
| `completed_with_errors` / `failed` | `ready` | `retry-failed`（失败成员 +3 额度回 `pending`，需再显式 `start`） |

**终态：** `completed`、`completed_with_errors`、`failed`（可经 `retry-failed` 回 `ready`）、`cancelled`（封存）。

### 3.2 Member 状态与合法转移

主文档 §6.2：`pending → running → succeeded / retry_wait / failed`；`retry_wait → running`；取消未成功任务 → `cancelled`（已成功答案保留）。

| 当前状态 | 合法目标 | 触发 |
|---|---|---|
| `pending` | `running` | 被领取（生成 lease_token / attempt_no） |
| `pending` | `cancelled` | run 取消且尚未开始 |
| `running` | `succeeded` | 校验通过并 CAS 保存成功 |
| `running` | `retry_wait` | 可重试错误且 `attempt_count < attempt_limit`，写 `next_attempt_at` |
| `running` | `failed` | 永久错误 / 次数耗尽 / `CONTEXT_TOO_LONG` |
| `running` | `pending` | `retry-failed` 返还额度（经 run `ready→start`） |
| `running` | `cancelled` | 租约过期恢复且 run 处于取消分支（未成功） |
| `retry_wait` | `running` | 到达 `next_attempt_at` 后重新领取 |
| `retry_wait` | `cancelled` | run 取消 |
| `retry_wait` | `failed` | （恢复路径）次数耗尽 |

**不变量：** `running` 必须有非空 `lease_token` + `lease_expires_at`；`succeeded` 必须有非空 `answer_json`；`retry_wait` 必须有非空 `next_attempt_at`。

### 3.3 非法转移清单（供 QA 写 409 用例）

| # | 非法操作 | 期望 |
|---|---|---|
| I1 | 对 `running` run 再次 `start` | 409（已运行，非 `ready`） |
| I2 | 存在活动批次时启动新 run（§6.2「不增加一般性 `queued` 状态」） | **409** |
| I3 | 对 `ready` run `pause` / `resume` / `retry-failed` | 409 |
| I4 | 对 `completed` run `pause` / `resume` / `cancel` | 409 |
| I5 | 对 `cancelled` run `start` / `resume` / `pause` | 409 |
| I6 | 对 `completed`（**全成功**）run `retry-failed` | 409（仅 `completed_with_errors`/`failed` 允许） |
| I7 | `PATCH /surveys/{id}` 且 `expected_revision` 不匹配 | 409 |
| I8 | 同 `Idempotency-Key` 但 `request_hash` 不同 | 409 |
| I9 | 同 `Idempotency-Key`、`request_hash` **相同** | **不冲突**：返回原 run（幂等成功） |
| I10 | 对非 `retry_wait`/`pending` 且无失败成员的 run `retry-failed` | 409 |
| I11 | `PATCH /runs/{id}/budget` 降低预算或改币种 | 422（只允许提高、币种不变） |
| I12 | 对 `paused` run 直接 `start`（未先 `resume`） | 409（须 `resume`） |

**幂等重复语义（主文档 §8.2）：** `pause`/`cancel`/`resume` 重复点击且已达目标状态 → 返回**已达状态**（200，非 409）；`start` 重复 → I1 409；`retry-failed`/`POST /runs` 以幂等键只生效一次。

---

## 4. 落地选型与版本锁定（写入本文，全团队统一）

| 组件 | 锁定值 | 理由 |
|---|---|---|
| 语言 | **Python 3.12.x**（锁定补丁版 `3.12.13`） | 与主文档 §Tech Stack 一致；本机已有多个 3.12 目录，**用 `backend/.python-version` 固定补丁版**（见下） |
| 依赖管理 | `uv` `0.12.14` | 本机实测可用；`uv.lock` 纳入版本控制 |
| Web 框架 | `fastapi`（最新稳定）+ `uvicorn[standard]` | 主文档 §3 明确异步 |
| 校验/契约 | **Pydantic v2**（`pydantic>=2`） | 严格类型、`model_config` 禁止额外字段 |
| ORM/驱动 | **SQLAlchemy 2.x async** + **asyncpg** | `FOR UPDATE SKIP LOCKED`、`text()` 原生 SQL |
| 迁移 | **Alembic** | 五表 + 约束 + 索引 |
| HTTP 客户端 | **httpx**（`AsyncClient`，连接池 ≥ 100） | 异步调第三方 API；显式关闭不透明重试 |
| 前端 | **Vite** + **React** + **TypeScript** | 主文档 §9；四页最小实现 |
| 数据库 | Docker `postgres:16`，端口 **55432** | TEAM-BRIEF §3 已就绪 |

### 4.1 Python 版本裁决（解决 PRD C1；含改判 A2 + R1）

- 主文档写 Python 3.12；TEAM-BRIEF 记 managed 为 3.13.12。本机存在**多个 3.12 目录**（旧命名 `cpython-3.12-macos-aarch64-none` 及多个补丁版），`uv venv --python 3.12` 的解析**不稳定**（实测一次 3.12.12、一次 3.12.13）。
- **裁定（A2）：锁定补丁版 `3.12.13`**，并以**项目级 pin 文件**消除解析歧义，**不依赖** `UV_PYTHON_INSTALL_DIR`（项目内 `.pythons/` 已删除）。
- **决策落地（R1）：**
  - `backend/.python-version` 内容 = `3.12.13`（uv 据此确定性选解释器）。
  - 项目根 `.gitignore` 忽略 `.venv/`、`.pythons/`、`__pycache__/` 等。
  - `backend/pyproject.toml` 写 `requires-python = ">=3.12,<3.13"`；建 venv：`cd backend && uv venv --python 3.12`。
- **偏差记录：** 无。managed 默认解释器 3.13.12 仅用于宿主，**项目 venv 固定 3.12.x（补丁 3.12.13）**。文档与 CI 均以此为准。

**验证命令（R1：在未设 `UV_PYTHON_INSTALL_DIR` 的干净 shell 中执行）：**
```bash
# 冒烟：主/次版本必须为 (3, 12)
cd backend && uv run python -c "import sys; print(sys.version_info[:2])"   # 期望 (3, 12)
# 锁定校验（由 pytest 断言补丁版，见 task-list.md T1::test_interpreter_matches_pinned_patch）
cd backend && uv run python -V                                            # 期望 Python 3.12.13
```

### 4.2 数据库 DSN 与测试库分离

| 用途 | DSN |
|---|---|
| 开发 | `postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey` |
| 测试 | `postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test`（**同实例不同 database**） |

**建库命令（测试库务须分离，主文档 T1 要求）：**
```bash
# 启动实例（TEAM-BRIEF §3 统一命令）
docker run -d --name survey-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=survey -p 55432:5432 postgres:16
# 已存在则 docker start survey-pg
docker exec -it survey-pg createdb -U postgres survey_test
```
- 测试用环境变量 `TEST_DATABASE_URL`；pytest fixture 启动时 `alembic upgrade head`，用例结束回滚/清库。
- **硬约束：** 测试连接**禁止**指向真实生产库（fixture 断言 URL 含 `survey_test`，否则 `RuntimeError`）。

---

## 5. Mock Provider 设计（`tests/load/mock_provider.py`）

本机无真实第三方 API key ⇒ T0–T6 与 T7 mock 容量全走 mock；同接口、可注入故障、**确定性可复现**（对齐项目 R1/R2 精神）。

### 5.1 契约

```python
class MockProvider:                      # 实现与真实 ProviderAdapter 相同的 async answer(ModelRequest) -> ModelResponse
    def __init__(self,
        fixed_latency_s: float = 0.2,     # 固定延迟（可选确定性正态抖动，seed 固定）
        *,
        fault_plan: str = "none",         # "none" | "recoverable" | "permanent"
        seed: int = 0,
        timeout_personas: set[str] = frozenset(),   # 连接超时成员
        fail_personas: set[str] = frozenset(),      # 永久错误成员（含额度耗尽）
    ) -> None: ...
```

### 5.2 确定性故障注入规则（同输入同故障序列）

故障仅由 `(persona_id, attempt_no)` 决定，**与调用顺序/并发无关**，故可复现：

| 场景 `fault_plan` | 规则 |
|---|---|
| `none` | 恒返回合法 `{"question_id":"purchase_intent","value":<确定性档位>}`；`value` 由 `hash(persona_id) % 5` 映射五档，保证分布可预期 |
| `recoverable` | 对每个 `persona_id` 的**前两次** `attempt_no∈{1,2}` **确定性**注入故障：`attempt_no==1` → 429（带 `Retry-After`）或 5xx；`attempt_no==2` → 非法输出（非 JSON / 缺题 / 错误 `question_id` / 非法 value / 额外字段，各按 `hash(persona_id)%5` 选一种）；`attempt_no>=3` → 合法 |
| `permanent` | `persona_id ∈ fail_personas` 恒返回永久错误（如 400 模型不存在 / 每次非法输出直至额度耗尽）→ 最终 `failed`；其余成员合法 |

- **固定延迟**：默认 `fixed_latency_s=0.2`，用 `await asyncio.sleep` 模拟在途窗口。
- **连接超时**：`timeout_personas` 成员 `await asyncio.sleep(>timeout)` 触发 `TIMEOUT` 映射。
- **usage**：可返回 `input_tokens/output_tokens` 或 `None`（覆盖「usage 缺失」路径，此时费用记未知）。

### 5.3 可复现性要求

- 所有随机性以 `seed` + `(persona_id, attempt_no)` 派生，**禁止全局 `random`**（避免并发下顺序敏感）。
- 提供 `MockCallLog`：记录 `(persona_id, attempt_no, status, error_code, ts)`，供压测后核对「重试成功只产生一个有效答案」「永久失败数准确」。
- **红线：** mock 输出**不得**被写成「真实模型模拟结果」；`docs/acceptance.md` 明确标注真实调用未测。

---

## 6. 可靠执行的落地实现路径

### 6.1 「不持有数据库事务等待模型响应」

**实现路径（主文档 §6.3 原文落实）：**
1. Worker 调度器为空闲槽位（`< AGENT_CONCURRENCY=100`）领取成员。
2. **事务 T1（短、快提交）**：`BEGIN; SELECT … FROM runs WHERE id=:id FOR UPDATE;` 锁 run 行 → 检查 `status='running'`、预算/额度、`reserved_cost + 新预留 <= budget_limit` → `SELECT … FROM run_members WHERE run_id=:id AND status IN ('pending','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=now()) ORDER BY row_no FOR UPDATE SKIP LOCKED LIMIT :free_slots;` → 对每个领取成员生成新 `lease_token`、`attempt_no=attempt_count+1`、`lease_expires_at=now()+120s`，写 `attempts(status='running')`，`UPDATE run_members SET status='running', lease_token, lease_expires_at`。
3. **`COMMIT`（此时**不**持有任何事务/行锁）。**
4. 提交后**才** `await provider.answer(request)`（网络等待完全脱离事务）。
5. **事务 T2（结果）**：CAS 保存（§6.6）。

**禁止：** 在 `BEGIN … COMMIT` 之间发起 HTTP 请求；禁止一个长事务覆盖多个模型调用。

### 6.2 领取 SQL（队列式消费）

```sql
SELECT id, row_no, persona_id, persona_snapshot, attempt_count, attempt_limit
FROM run_members
WHERE run_id = :run_id
  AND status IN ('pending','retry_wait')
  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
ORDER BY row_no
FOR UPDATE SKIP LOCKED
LIMIT :free_slots;              -- free_slots = 100 - in_flight，取领取数量 <= 空闲槽位
```

> `SKIP LOCKED` 处理锁竞争；**不替代**幂等、预算与状态检查（主文档引用 PostgreSQL SELECT 文档）。

### 6.3 租约续期与过期扫描

- 每 **20 秒**续租：`UPDATE run_members SET lease_expires_at = now()+interval '120 second' WHERE id=:id AND lease_token=:token AND status='running';`（影响行数=0 表示失租，立即停止后续处理）。
- 每 **10 秒**扫描过期：`SELECT … WHERE run_id=:id AND status='running' AND lease_expires_at < now();` → 视为「一次结果未知的 attempt」：写 `attempts(status='abandoned')`，按剩余次数将成员转 `retry_wait`（`next_attempt_at=now()`）或 `failed`。

### 6.4 单 worker 锁与活动批次检查

- **单 worker 锁：** worker 启动 `SELECT pg_try_advisory_lock(:app_lock_key);`（会话级）。失败 → 记录明确日志后**退出**；锁连接断开 → 立即停止领取。首版只允许一个调度进程。
- **活动批次唯一性（改判 A1）：** 分两层。
  - **第一层（可读前置校验，启动事务内）：**
```sql
SELECT id FROM runs
WHERE status IN ('running','pausing','paused','cancelling')
  AND id <> :this_run
FOR UPDATE;
-- 返回非空 → 409（不新增 queued 状态；paused 仍占位）
```
  - **第二层（DB 硬兜底，最终裁决）：** 依赖 §2.3 的部分唯一索引 `uq_runs_single_active ON runs ((true)) WHERE status IN ('running','pausing','paused','cancelling')`。当**不存在**其他活动批次时，上述 `SELECT … FOR UPDATE` **不锁任何行**，两个并发 `start` 事务会同时通过——此时**只有**该唯一索引能拦住第二个事务。`start` 必须捕获 `IntegrityError` 并 **映射为 409**（不得 500）。
  - 并发验证见 `task-list.md` T3 `test_concurrent_start_only_one_active_run`（断言**恰好 1 个 202、其余全 409**）。

### 6.5 限流与预算预留（`worker/limits.py`）

- 全局 **100 槽位计数器** 统一约束「初次调用 + 重试」在途数 ≤ 100。
- **RPM/TPM**：平滑发出（避免分钟边界突发）；进程内维护滑动窗口，**worker 重启必须**依据 `attempts.started_at` 最近 **60 秒**记录保守重建窗口（不重启即清空）。
- **预算预留**：每次发请求**前**在**领取事务（T1）内**预留费用上界（输入保守估计 + `max_output_tokens` + 供应商全部计费类别）；`actual_cost + reserved_cost + 新预留 <= budget_limit`；有 usage 后结算释放；未知计费**保留预留**，直至对账/管理员结算，**不自动按 0 释放**。
- `request_limit` 默认 `3 * sample_size`；领取时原子 `requests_reserved += 1`；不足 → 按 `budget` 暂停。

### 6.6 结果 CAS 保存（防止失租覆盖）

**成功保存（事务 T2，单一原子事务）：**
```sql
UPDATE run_members
SET status='succeeded', answer_json=:ans, attempt_count=:n, updated_at=now(),
    lease_token=NULL, lease_expires_at=NULL, last_error_code=NULL
WHERE id=:member_id
  AND status='running'
  AND lease_token=:my_lease_token;     -- ← CAS 条件；0 行 = 失租，丢弃本次结果
```
同事务内：`UPDATE attempts SET status='succeeded', finished_at=now(), usage_json=:u, actual_cost=:c WHERE id=:attempt_id AND actual_cost IS NULL;` 并结算 run 成本。

**失败/重试（同 CAS 条件）：** `attempt_count < attempt_limit` → `status='retry_wait', next_attempt_at=now()+backoff(2s,8s + 抖动; 有 Retry-After 时取其值)`；否则 `status='failed', last_error_code=:code`。

**旧 attempt 迟到费用：** 不覆盖答案/状态，仅 `UPDATE attempts SET actual_cost=:c, usage_json=:u WHERE id=:attempt_id AND actual_cost IS NULL` 单独结算，不漏账不重复。

### 6.7 100 路并发可验证性设计（真实 PostgreSQL 集成测试）

**目标：** 在有至少 1,000 个待执行任务、充足配额时，用 **barrier mock** 断言：
- **恰有 100 个请求同时在途**（峰值 == 100）；
- **峰值始终 ≤ 100**（任何时刻不越界）；
- **释放后滚动补位**（不是「取 100 行等整轮」）。

**测试骨架要点：**
```
# tests/test_worker.py::test_peak_concurrency_is_100_with_rolling_refill
1. 建 run + 1,000 个 pending 成员；AGENT_CONCURRENCY=100。
2. 注入 BarrierMockProvider：
   - 内部 threading/asyncio.Barrier(100)（或 asyncio.Event + 计数）；
   - answer() 进入时 in_flight += 1（原子），更新 peak = max(peak, in_flight)；
   - 到达 barrier 后 await 一个「统一释放」信号（测试侧控制），返回后 in_flight -= 1。
3. 启动 worker（真实 PostgreSQL，真实领取事务）。
4. 等待所有 100 槽位全部在途（barrier 到齐 / 超时 fail）：
   → 断言 instant_in_flight == 100 且 peak == 100。
5. 释放第一批：断言同一时刻 in_flight 立刻补位到 100（滚动，而非降到 0 再重取）。
6. 全程采样 peak：断言 peak <= 100 恒成立（记录曲线供 T7）。
7. 跑到收敛：断言 succeeded == 1,000、每成员仅 1 条有效答案、attempts 无重复 attempt_no。
```
- **附加用例：** 两个 worker 只有一个获 advisory lock（另一个退出并留日志）；过期 token 返回不能覆盖新结果（构造旧 lease 后 CAS 为 0 行）。
- **红线：** 该测试用 mock，**不得**把结果表述为真实万人模拟（文档措辞须为「工程并发容量验证」）。

### 6.8 控制动作幂等与审计

- `POST /runs` / `retry-failed` 必须 `Idempotency-Key`；幂等记录保存在 run / 控制记录**事务内**（`runs.idempotency_key` UNIQUE + `control_events_json`），**不能只存内存**。
- 控制动作在**锁定 run 行的事务**内追加 `control_events_json`（action、idempotency_key、request_hash、结果、时间）；**不可事务外读改写**。
- 同 key + 同 `request_hash` → 返回原 run；同 key + 异 `request_hash` → 409。

---

## 7. ProviderAdapter 落地（`inference/provider.py`）

- 统一业务接口：`async def answer(request: ModelRequest) -> ModelResponse`（字段见主文档 §5.3 与 `contracts.py`）。
- **首版只对接用户实际第三方 API**：若为 OpenAI-compatible → 实现该协议适配器；其他协议 → 保持同一业务接口换适配器。**不假设协议完整兼容**；`build_json_client` 式的「前缀 → 客户端」仅作边界参考（见 `upstream-audit.md` §3.2）。
- **显式关闭** httpx/供应商 SDK 的不透明重试（`max_retries=0` / 不启用内部重试），重试统一由 worker 控制。
- 启动能力检查：模型名、鉴权、返回字段、JSON 模式支持、usage 存在性、timeout/429 格式、RPM/TPM。**用真实单次请求验证，凭接口名称不认定「兼容」**（无 key 时跳过并记录）。
- 配置仅由后端环境/受控文件维护：endpoint/model/key 环境变量名/RPM/TPM/并发/超时/输出上限/价格/币种；**key 不下发前端、不进日志/快照**；保存配置 hash 与不含秘密的模型参数。
- 默认：超时 60s、输出上限 256 tokens、`AGENT_CONCURRENCY=100`、连接池 ≥ 100。

---

## 8. 与主文档的一致性核对

| 主文档要求 | 本文落实 | 状态 |
|---|---|---|
| §3 六模块边界 | §1 全确认 | ✅ |
| §6.1 五表命名 | §2 照抄，未改名 | ✅ |
| §6.2 状态机 | §3 全状态 + 非法转移清单 | ✅ |
| §6.3 领取/租约/CAS | §6 完整 SQL | ✅ |
| Tech Stack 版本 | §4 锁定（Python **3.12.13**，`.python-version` pin） | ✅ |
| §9 目录 | §1.1 确认（2 处非阻断提示） | ✅ |
| 无 Redis/Celery、单 worker | §6.4 advisory lock | ✅ |
| mock 非真实模拟 | §5.3 红线 + §6.7 红线 | ✅ |
| 「同一时刻一个运行批次」 | §2.3 `uq_runs_single_active` 部分唯一索引 + §6.4 双层校验（改判 A1） | ✅ |
