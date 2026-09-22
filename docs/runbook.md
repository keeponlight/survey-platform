# 运行手册（Runbook）

> 万级智能体模拟问卷平台 MVP。覆盖：启动、环境变量、迁移、恢复、备份、已知限制。
> 需求契约：`/Users/zhao/Desktop/2026-09-20-survey-platform.md`；落地决策见 `docs/architecture.md`。

## 0. 术语与口径（避免误读）

| 术语 | 含义 |
|---|---|
| **100** | **并行执行槽位数**（`AGENT_CONCURRENCY=100`），**不是**样本数/机器数/进程数 |
| 总样本数 | = 输入表行数（默认全表），上限 20,000；不筛选、不抽样 |
| 1 万智能体 | 1 万个独立 persona 的作答任务，每人独立上下文，正常调用模型一次 |
| **数据行号（`row_no`）** | 从 **1** 起，**不含表头**（主文档 §4 `"row_no": 1` 的定义） |
| 「表格行号（含表头）」 | = `row_no + 1`；UI 另起一列展示，**不得**覆盖或替换 `数据行号` |

> ⚠️ **`row_no` 语义差 1**：若在 Excel 中按物理行号定位，需用 `row_no + 1`。结果页两列分别标注，避免错位。

---

## 1. 快速启动（Docker Compose，推荐）

```bash
# 默认 = mock 模式（不发起任何真实付费调用）
docker compose -f deploy/compose.yaml up -d --build

# 状态
docker compose -f deploy/compose.yaml ps

# 前端：http://127.0.0.1:8080   API：http://127.0.0.1:8000/api/v1/health/ready
```

服务构成：

| 服务 | 说明 | 端口 |
|---|---|---|
| `web` | nginx 托管前端静态资源，并把 `/api` 反代到 `api` | `8080:80` |
| `api` | FastAPI（`uvicorn app.main:app`）；启动前执行 `alembic upgrade head` | `8000:8000` |
| `worker` | 独立进程（`python -m app.worker.main`）承载 100 槽位长任务 | 不暴露 |
| `postgres` | `postgres:16`，命名卷 `survey_pgdata` 持久化 | **不映射宿主端口**（裁决 N7-c） |

> **api 与 worker 复用同一镜像，仅启动命令不同**（`docs/architecture.md` D1/C2）。
> 严禁把长任务放进 HTTP 请求线程。
>
> ⚠️ **postgres 端口（N7-c）**：本机容器 `survey-pg` 已独占宿主 `55432`，故 compose 内的
> `postgres` **不再**映射 `55432:5432`（否则 `docker compose up` 因端口占用失败）。
> Compose 网络内其它服务用服务名访问 `postgres:5432`。若需在宿主连库，用
> `docker compose -f deploy/compose.yaml exec postgres psql -U postgres -d survey`。

### 1.1 切换到真实第三方模型

```bash
cp deploy/real.env.example deploy/real.env      # 填入真实 endpoint/model/key 环境变量名
docker compose -f deploy/compose.yaml -f deploy/compose.real.yaml up -d --build
```

- `deploy/real.env` 被 `.gitignore` 忽略（规则 `*.env`，并否定回补 `!deploy/mock.env` 与 `!*.env.example`），
  **不会入库**；`deploy/real.env.example`（模板）与 `deploy/mock.env`（无密钥）仍会被跟踪。
- API key 只在部署环境注入；**不进代码、镜像、日志、快照、CSV、`model_snapshot`**。
- 无网关时仅绑定 `localhost` / 受控内网，用服务端访问口令保护；**固定管理密钥不进前端静态代码**。

### 1.2 模式分离说明（mock vs real）

| 模式 | env 文件 | 说明 |
|---|---|---|
| mock（默认） | `deploy/mock.env` | 与真实 provider 同接口的 mock，不发起付费调用 |
| real | `deploy/real.env`（由 `.example` 复制） | 对接用户实际第三方 API |

> **模式切换键**：只由后端实际消费的 `MODEL_PROVIDER` + `MODEL_CONFIG_ID`（+ `MODEL_ENDPOINT`）驱动
> —— 见 `backend/app/config.py:Settings.from_env`，它只读取 `MODEL_*` 系列。
> （早期草稿曾引入 `SURVEY_PROVIDER_MODE`，但 T1 未定义、后端也不读取，属"幽灵变量"，已从本项目所有
> env/compose 中移除，避免误导。）
>
> **已落地（N7-b）**：`MODEL_PROVIDER == "mock"` 现已是**运行时可用的**分支
> （`backend/app/inference/mock_provider.py`，由 `build_provider()` 按 `MODEL_PROVIDER` 选择；
> 确定性档位、不发起真实调用）。`deploy/mock.env`（无密钥）与 `deploy/real.env.example`
> （仅占位符）见 `deploy/`；真实 provider 用 `MODEL_PROVIDER=openai_compatible`。

---

## 2. 本地开发（非 Docker）

### 2.1 Python 解释器（**重要**）

```bash
cd backend
uv venv --python 3.12      # uv 依据 backend/.python-version 精确选择
uv sync
uv run python -V           # 期望：Python 3.12.13
```

- 项目用 **`backend/.python-version = 3.12.13`** 精确钉版；`requires-python = ">=3.12,<3.13"`。
- ⚠️ **不要再使用 `UV_PYTHON_INSTALL_DIR`**：该变量为**会话级**，新 shell 会静默回退到其它解释器，
  且早期「把 3.12 装进项目内 `.pythons/`」的方案**已废弃**（项目内 `.pythons/` 已删除）。
  钉版统一由 `backend/.python-version` 承担，**不需要**任何环境变量。
- 缺失解释器时 uv 会按 `.python-version` 自动拉取。

### 2.2 数据库

```bash
# 本机已有容器（TEAM-BRIEF §3）
docker run -d --name survey-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=survey -p 55432:5432 postgres:16
# 已存在则：docker start survey-pg
# 创建测试库（若尚未创建）
docker exec -it survey-pg createdb -U postgres survey_test
```

`DATABASE_URL` / `TEST_DATABASE_URL` 见 `.env.example`。**测试连接禁止指向开发/生产库**。

### 2.3 迁移

```bash
cd backend
uv run alembic upgrade head        # 五张业务表 + 约束 + 索引（含 uq_runs_single_active）
uv run alembic downgrade base      # 回退全部（谨慎）
uv run alembic current             # 当前版本
```

### 2.4 测试

```bash
cd backend
uv run pytest -q
```

`tests/conftest.py` 启动即执行 `SELECT current_database()` 并断言数据库名**以**
`survey_test` **开头**（裁定 §7.6.4 已把原「等于」放宽为「前缀匹配」，以允许
`survey_test_qa`、`survey_test_t7` 等**有意隔离**的库），否则 **fail fast**（`RuntimeError`）
——dev 库 `survey` **不以** `survey_test` 开头，仍被拒绝，故仍能挡住误连开发/生产库。

> **`.pytest_tmp/` 可能残留，属预期，勿手工批量删除。** 用例临时目录固定在 `backend/.pytest_tmp/`。
> 本机沙箱的 `safe-delete` bulk-guard 会在删除数超阈值时拒绝删除（报 `SAFE_DELETE_BULK_REJECTED`），
> 故每轮测试后目录可能**仍残留**（含主理人自身的 `rm` 也会被拦）。这是**沙箱安全守卫**所致，
> **不影响测试正确性**：`tests/conftest.py` 已把该清理设计为「失败即忽略、绝不中断 teardown」，
> 且 `db_session` 在**用例起点与终点都清库**，故残留的临时目录不会污染 DB、也不会造成假失败。
> 请**勿**手工批量删除该目录（会被守卫拦截）。

### 2.5 前端

```bash
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173（/api 代理到 127.0.0.1:8000）
npm run build      # tsc --noEmit && vite build → dist/
```

E2E（Playwright，属 devDependency）：

```bash
cd frontend
npx playwright install chromium                       # 下载浏览器（写入 ~/Library/Caches/ms-playwright）
docker compose -f deploy/compose.yaml up -d --build   # E2E 依赖整套服务
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npx playwright test tests/survey-flow.spec.ts
```

- 若浏览器下载被沙箱拦截，如实记录「E2E 未执行」及其原因，**不得**声称已通过。
- 沙箱环境可用 `E2E_TMP_DIR` 指定工作区内的临时目录（默认 `os.tmpdir()`）。
- ⚠️ **当前状态：E2E 未执行**——**T4（HTTP 层）与 T5（报表）均已落地**；E2E 未执行的真实
  原因是它**依赖 `docker compose up` 实跑整套服务**（api/worker/postgres/web）**并需要
  Chromium 浏览器**（本机尚未安装/启动），故尚未执行。**不得**因其他层已落地而声称 E2E 已通过。

---

## 3. 日常运维

### 3.1 停止 / 清理

```bash
docker compose -f deploy/compose.yaml down            # 停止，保留数据卷
docker compose -f deploy/compose.yaml down -v         # ⚠️ 同时删除数据卷（清库！）
```

### 3.2 日志

```bash
docker compose -f deploy/compose.yaml logs -f api
docker compose -f deploy/compose.yaml logs -f worker
```

> 日志中**不得**出现 API key、完整画像或整份 `raw_output`；`attempts.raw_output` 有 20,000 字符上限。

### 3.3 崩溃恢复（worker）

- worker 用数据库会话级 **advisory lock** 保证首版只有一个调度进程；第二个 worker 启动失败并留明确日志。
- 租约 120s、每 20s 续租、每 10s 扫描过期；崩溃后过期运行记录成为一次「结果未知」的 attempt，
  按剩余次数恢复为 `retry_wait` 或 `failed`。
- **重启即清空限流视为缺陷**：worker 启动必须依据 `attempts` 最近 60 秒记录**保守重建** RPM/TPM 窗口。
- 恢复：`docker compose -f deploy/compose.yaml restart worker`（任务事实源为 PostgreSQL，不依赖浏览器）。

### 3.4 备份 / 恢复（PostgreSQL）

```bash
# 备份（容器内 pg_dump，输出到宿主当前目录）
docker exec survey-pg pg_dump -U postgres -d survey -Fc > survey_$(date +%Y%m%d_%H%M%S).dump

# 恢复（先建空库或使用现库）
docker exec -i survey-pg pg_restore -U postgres -d survey --clean --if-exists < survey_YYYYmmdd_HHMMSS.dump

# compose 环境请用对应服务名与持久卷；备份前建议先停 worker 以冻结写入
docker compose -f deploy/compose.yaml stop worker
```

---

## 4. 已知限制

1. **真实第三方 API 万人实测未执行**（本机无凭据）。mock 容量测试**不等于**真实模型运行；
   不得以「mock 通过」宣称「真实万人模拟已完成」。
2. 首版固定：单题（`purchase_intent`）、五档购买意向、单工作空间、**同一时刻一个运行批次**、
   一个 worker 进程、最大 20,000 行、默认全表。
3. 不做：画像生成/补全、模型训练、RAG、向量库、Agent 对话、浏览器仿真、通用问卷编辑器、
   多租户/计费、自动长篇 AI 报告、Kubernetes。无需 GPU。
4. **无有效输出绝不补默认答案**：无效 JSON / 非法 value / 缺题一律记 `INVALID_OUTPUT` 有限重试，
   不回落首选项/中点/中立（`unsure`）。
5. 金额「未知」不写成 `0`；供应商无法给出可靠 token 上界时，预算只能标为**估算软上限**，
   同时配置供应商账户硬额度与本地请求次数上限。
6. 结果页与 CSV 均标注「模拟购买意向」；Top-2-Box **不得**命名为真实购买转化率。
7. 万级样本只验证**工程规模**，不能凭规模证明与真人结果一致。
8. 「耗时」为**累计时长（含暂停/重试等待）**，不是纯执行时长。
9. `PATCH /surveys/{id}` 需携带 `expected_revision`，冲突返回 409；已有批次使用各自冻结快照，不受影响。

---

## 5. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `health/ready` 失败 | 数据库不可达 | 检查 `DATABASE_URL` 与 postgres 容器健康 |
| 上传返回 `INVALID_USER_TABLE` | 空表/超 20,000 行/重复 ID/空画像/损坏文件/公式单元格 | 按响应中的工作表/行号/列名修复后重传 |
| 批次 `paused`, `pause_reason=api_auth` | 401/403 鉴权失败 | 修复服务端模型配置后 `resume` |
| 批次 `paused`, `pause_reason=api_unavailable` | 连续 429/5xx | 降低速率或稍后 `resume` |
| 批次 `paused`, `pause_reason=budget` | 预算/请求额度不足 | 提高预算（只允许提高、不改币种）后 `resume` |
| 第二批次启动 409 | 已有活动批次 | 先 `resume` 或 `cancel` 原批次 |
| worker 无法启动 | 另一 worker 已持 advisory lock | 单 worker 是首版约束；确认没有重复启动 |
