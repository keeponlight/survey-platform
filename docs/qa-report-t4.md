# QA 报告 — T4（控制 API 与状态收敛）对抗性复核

- **复核人**：QA 严过关（software-qa-engineer），任务 #2 / 节点 N5 的 QA 关卡
- **被复核对象（T4 交付）**：
  - `backend/app/api/routes.py`（第 491–578 行 6 个控制端点）
  - `backend/app/runs/service.py`（`pause_run/resume_run/cancel_run/retry_failed/update_budget` + `ControlOutcome`）
  - `backend/app/api/schemas.py`（`RunBudgetPatchRequest`）
  - `backend/tests/test_run_controls.py`（22 用例）、`backend/tests/test_api.py`（+`test_api_restart_does_not_affect_worker`）
- **方法**：以**证伪**为主 —— 变异测试（植入违规实现看用例是否变红）/ 独立 mock + DB 原生 SQL 交叉采样 / 非法输入灌入 / 逐条核对需求覆盖度。**不复跑工程师命令。**
- **测试库**：`survey_test`（复核期间独占；`pg_stat_activity` 无其它 pytest）。新增用例：`backend/tests/test_qa_t4.py`（18 条）。
- **基线（我亲手跑，非采信）**：`cd backend && uv run pytest -q` → **217 passed in 32.28s**（与主理人实测一致）。

---

## 0. 结论摘要

- **缺陷计数：0 P0 / 2 P1 / 2 P2。**
- **一句话判断**：**T4 不建议直接验收**——发现 1 个**可达的 500 泄漏**（`cancel` 命中活动批次唯一索引未映射为 409），另 1 条主理人裁决（Q6）**确实未落地**。两类问题修完后可复验。工程师的 22 条控制用例在 6 个变异下**全部变红**（无纸老虎），红线 #1 有牙齿。

| 级别 | 数量 | 一句话 |
|---|---|---|
| **P0** | 0 | 红线 #1、丢行、写错库、约束形同虚设均未被攻破。 |
| **P1** | 2 | ① `cancel` 对 `ready` run 在已有活动批次时 → **HTTP 500**（非 409）；② 主理人裁决 **Q6**（`start` 前校验模型配置）**未实现**。 |
| **P2** | 2 | ③ 无在途时 `pause` 不自收敛（停留 `pausing`，且 `pausing` 无 `resume` 动作 → 无 worker 时死角）；④ 用例名 `test_illegal_transitions_409[I1..I12]` 与事实不符（I9=201、I11=422）。 |

### 0.1 复核过程事故（与产品无关，但影响「全量数字」解读）

复核中段我用一次性脚本（P1-1 / P2-1 的复现脚本）向 `survey_test` 写数据后**只在开头 `TRUNCATE`、未在结尾清库**，残留了一个 `pausing`（活动态）run。其后的**一次** T4 验收命令（`pytest tests/test_run_controls.py tests/test_api.py`）首个用例 `test_pause_stops_new_requests` 因此**假失败一次**（`_started_run` 的 `start` 撞上残留活动批次 → 409，而非 202）。该用例自身 teardown 随即清库，**其后 9 次重跑（3 次单跑 + 6 次合并跑）全部 44/44 绿**。

- **结论**：这是**我自己的复核残留**导致的假失败，**非工程师源码/用例缺陷**；清理后 T4 验收命令 **44 passed in ~6s**（原始输出见 §7）。
- **顺带观察（测试基础设施脆弱性，未计为产品缺陷）**：`tests/conftest.py:140-152` 的 `db_session` **只在 teardown 清库、不在 setup 清库**；若 `survey_test` 残留活动批次（上次 suite 被 kill/崩溃），**首个用例会假失败**。这正是 TEAM-BRIEF §7.6.1「测试库独占」要防的「假失败」之一。**建议**（不属本轮范围）：`db_session` setup 也做一次清库，或在 `prepared_test_db` 会话前置清库。

---

## 1. 变异测试结果（M1–M6，逐项：改了哪一行 / 命令 / 原始失败断言 / 是否还原）

> 每个变异：改前 `cp <file> <file>.qa-bak`，测完 `cp` 还原并 `diff` 逐字节确认。**所有变异均已还原，源码零残留**（`find app -name "*.qa-bak"` 为空、`grep -rn "MUTANT|# M1..M6" app/` 无命中）。

| # | 植入的违规实现 | 命令 | 原始失败断言（节选） | 还原(diff) | 结论 |
|---|---|---|---|---|---|
| **M1** | `repository.py` 的 `claim_members` 两处状态守卫改为允许 `pausing/paused`；`worker/main.py` 的 `_consumer` 去掉「遇 `PAUSING_RUN_STATUSES` 即 break」 | `pytest "tests/test_run_controls.py::test_pause_stops_new_requests" -q` | ```> assert provider.entered == entered_before_pause```<br>```E assert 5 == 2```<br>`tests/test_run_controls.py:282` | ✅ 干净 | **有牙齿**。批量跑另带红 `test_pause_converges_after_inflight`(`assert 4 == 2`)、`test_resume_only_remaining`(`assert 4 == 2`)。 |
| **M2** | `service.py` `retry_failed` 删除幂等重放循环（同 key 不再返回原结果） | `pytest "tests/test_run_controls.py::test_repeated_retry_failed_no_extra_attempts" -q` | ```> assert second.status_code == 200```<br>```E assert 409 == 200```<br>（`INVALID_RUN_STATE`）`tests/test_run_controls.py:548` | ✅ 干净 | **有牙齿**。 |
| **M3** | `service.py` `resume_run` 增加「把**全部**成员重置为 `pending`」 | `pytest "tests/test_run_controls.py::test_resume_only_remaining" -q` | ```> assert provider.entered == 4```<br>```E assert 6 == 4```<br>`tests/test_run_controls.py:376` | ✅ 干净 | **有牙齿**（已成功成员被重跑）。 |
| **M4** | `service.py` `update_budget` 删除「只许提高 / 不得改币种 / request_limit 只增」三条守卫 | `pytest "tests/test_run_controls.py::test_illegal_transitions_409[I11]" "...::test_budget_increase_keeps_snapshots" -q` | ```> assert lower.status_code == 422```<br>```E assert 200 == 422```<br>`tests/test_run_controls.py:621` | ✅ 干净 | **有牙齿**（2 failed，含 I11）。 |
| **M5** | `worker/execute.py` 在 `except InvalidOutputError` 分支植入「补默认答案 `unsure` → `_record_success`」（复刻上游 `_normalize_value`） | `pytest test_worker.py::test_invalid_output_never_becomes_valid_answer test_reliability_fixes.py::test_failed_member_answer_is_sql_null_not_json_null test_reliability_fixes.py::test_distribution_has_no_null_bucket -q` | `test_worker.py:446` ```E assert 'completed' == 'completed_with_errors'```；<br>`test_reliability_fixes.py:192` ```E assert 'completed' == 'failed'```；<br>`test_reliability_fixes.py:218` 同上 | ✅ 干净 | **有牙齿**（3 failed）。**红线 #1 不是纸面约束 → 不构成 P0**。 |
| **M6** | `service.py` `pause_run` 状态守卫改为 `if True`（任意状态都可暂停） | `pytest test_run_controls.py::test_illegal_transitions_409[I3] [I4] [I5] ...::test_repeated_pause_cancel_idempotent -q` | ```> assert second_pause.status_code == 200```<br>```E assert 202 == 200```<br>`tests/test_run_controls.py:653` | ✅ 干净 | **有牙齿**（4 failed：I3/I4/I5 + 重复 pause）。 |

**另：Q6 覆盖度实验（第 5.3 节）** —— 在 `start_run` 注入裁决要求的「模型未配置 → 422」后，**16 条既有用例变红**（`test_run_creation.py` 3 条、`test_reliability_fixes.py` 4 条、`test_run_controls.py` 9 条）。已还原、diff 干净。**结论：6 个变异 + 1 个覆盖度实验，无一「用例不变红」→ 本次未发现纸老虎用例。**

---

## 2. 独立交叉验证结果（§5.2 的 1–9 + 补项）

全部落在 `backend/tests/test_qa_t4.py`（自写、18 条），用**独立 mock（`QAGatedProvider`）与 DB 原生 SQL**，不复用工程师断言方式。执行命令：`uv run pytest tests/test_qa_t4.py -q`。

| # | 项 | 我实际用的手段 | 原始结论 |
|---|---|---|---|
| 1 | **并发幂等（retry-failed）** | 两个并发 `POST /retry-failed` 带**同一** `Idempotency-Key`（`asyncio.gather`）；DB 原生 SQL 读 `attempt_limit` | 返回码 `sorted == [200, 202]`（行锁串行化）；`SELECT DISTINCT attempt_limit = [6]`（3+3，**只加一次**）；`control_events_json` 中该 action+key 记录**恰 1 条**。**成立** |
| 2 | **pause DB 侧交叉采样** | `QAGatedProvider` 在途计数 vs `SELECT count(*) ... status='running'` 逐点比对 | 采样 8 次恒为 `db_running == provider.entered == 2`；收敛后 `run_members running=0`、`attempts running=0`。**成立** |
| 3 | **快照逐字段不变** | 提预算前后原生 SQL 取 `survey_snapshot, model_snapshot, sample_size, prompt_version, source_version, import_id, survey_id, request_hash, idempotency_key, budget_currency` 十列逐字段 diff | `diffs == {}`（无字段变化）；唯 `budget_limit` 10→20、`request_limit` 9→12。**成立** |
| 4 | **状态机全量重放 I1–I12** | **自建**构造路径（非照抄工程师参数表），16 个断言点 | 结果 == 期望：I1/I2/I3abc/I4abc/I5abc/I6/I10/I12=**409**，I11a(降)/I11b(改币种)=**422**；且 **重复 `pause`/`cancel` 已达目标 → 200，重复 `start` → 409**。**成立** |
| 5 | **不得泄漏 500** | 6 端点 ×（坏 UUID / 坏 JSON / 未知字段 / 空 body / 缺 Key / 超长 Key / 错误 Content-Type）共 **48 个探针** | 48 个探针**全部 < 500**。**但见下方边界项——语义边界 `cancel` 泄漏 500。** |
| 5b | **`cancel` 命中 `uq_runs_single_active`** | 独立一次性 ASGI 脚本（`raise_app_exceptions=False` 捕获真实状态码；可重复用例见 `tests/test_qa_t4.py`） | ```start A : 202``` / ```start B : 409``` / ```cancel B: 500 {"code":"INTERNAL_ERROR",...}``` / ```DB runs: [(A,'running'), (B,'ready')]```。**攻破 → P1-1** |
| 6 | **活动批次唯一性双向** | ① paused run 占位；② 5 个并发 start；③ DB 计数 | ① `start` 第二个 → **409 ACTIVE_RUN_EXISTS**；② 5 并发 → **恰 1×202、4×409**；③ DB 活动行 == 1。**成立** |
| 7 | **取消收敛责任方** | 造「在途」成员（租约已过期）→ `cancel`（无 worker）→ 轮询 status；再调 worker 的 `scan_expired_once` | 无 worker 时恒为 `cancelling`（5 次采样）；`scan_expired_once` 返回 `recovered == 1` 后 → `cancelled`。**责任方 = worker 的过期扫描/收敛循环**；无 worker 时停留在 `cancelling` 直至 worker（重）启动——此为「可从 DB 恢复」的设计，主文档 §6.4「直至在途收敛才显示 cancelled」**未构成缺陷**（见 §5 采信项）。 |
| 8 | **失败不可隐藏** | `completed_with_errors` 下读 `GET /runs/{id}` 与 `/summary` | `status == "completed_with_errors"`（**未被归一化兜成 `completed`**）；`allowed_actions == ["retry-failed"]`；`valid_count == 0`。**成立** |
| 9 | **resume 清理 pause_reason** | DB 原生 SQL 读 `pause_reason` | resume 前 `user` → resume(202) 后 **`NULL`**；出参 `pause_reason/pause_hint` 均 `None`。**成立** |
| 补 | **幂等记录落库位置** | 原生 SQL 读 `runs.control_events_json::text` | 含该 `Idempotency-Key` 与 `"retry-failed"`（**已落库、非内存**）。**成立** |

**全量集成**：`uv run pytest -q` → **235 collected，234 passed / 1 failed**（唯一 failed = 我用于抓 P1-1 的 `test_qa_cancel_ready_while_another_active_does_not_500`）。即工程师 217 条全绿 + 我的 18 条中 17 绿、1 红。

---

## 3. 缺陷清单

### 🟠 P1-1（重要）— `cancel` 对 `ready` run 在已有活动批次时泄漏 **HTTP 500**（应 409）

- **内容**：存在活动批次 A（`running`）时，对另一 `ready` 的 run B 调 `POST /runs/{B}/cancel`，服务端尝试把 B 置为 `cancelling`（**活动态**）→ 违反 DB 部分唯一索引 `uq_runs_single_active` → **未捕获的 `IntegrityError` → 500**。
- **证据（文件:行号 + 原始输出）**：
  - `backend/app/runs/service.py:491` `await session.flush()`（`cancel_run` 内，未 try/except `IntegrityError`）
  - `backend/app/api/routes.py:536` `outcome = await run_service.cancel_run(session, run_id)`
  - 独立复现（一次性 ASGI 脚本 + `raise_app_exceptions=False`；等价可重复用例 = `tests/test_qa_t4.py::test_qa_cancel_ready_while_another_active_does_not_500`）原始输出：
    ```
    start A : 202
    start B : 409 (期望 409)
    cancel B: 500 {"code":"INTERNAL_ERROR","message":"internal server error","details":{}}
    DB runs : [('d90b39b1', 'running'), ('af6677db', 'ready')]
    ... unique constraint "uq_runs_single_active" ... [SQL: UPDATE runs SET status=$1::VARCHAR, control_events_json=$2::JSONB ...]
    ```
  - 对比：`start B` **正确**返回 409 —— 因为 `start_run` 有「前置 `find_other_active_run` 校验 + `IntegrityError→ActiveRunConflictError` 兜底」；`cancel_run` **两者都没有**。
- **违反**：`docs/architecture.md` §2.3 / §6.4「命中冲突时 `IntegrityError` **必须映射为 409**，不得泄漏 500」；主文档 §8.2 冲突语义。
- **可达性**：正常流程（建 A、start A、建 B、cancel B）即可触发；B 状态 `ready`，非畸形输入。
- **复现命令**：`cd backend && uv run pytest tests/test_qa_t4.py::test_qa_cancel_ready_while_another_active_does_not_500 -q`
- **建议修法**：`cancel_run` 在对 `ready`（非活动）run 置 `cancelling` 前，复用 `self._repo.find_other_active_run(...)` 前置校验；并给 `flush()/commit()` 包 `try/except IntegrityError → ActiveRunConflictError(source="db_index")`（与 `start_run` 一致，映射 409）。**不得**靠吞异常返回 200。

### 🟠 P1-2（重要）— 主理人裁决 **Q6**（`start` 前校验模型配置，否则 422）**未落地**

- **内容**：`Settings.is_model_configured()`（`backend/app/config.py:142-144`）存在但**全库无任何调用点**（`grep -rn is_model_configured app/` 仅命中定义处）。`start_run`（`service.py:322-370`）从不校验模型配置。
- **独立确认「加校验会破坏既有用例」（工程师自述属实）**：把 Q6 校验注入 `start_run` 后：
  ```
  FAILED tests/test_run_creation.py::test_concurrent_start_only_one_active_run
  FAILED tests/test_run_creation.py::test_start_with_existing_active_run_conflicts
  FAILED tests/test_run_creation.py::test_start_non_ready_run_invalid_state
  FAILED tests/test_reliability_fixes.py::test_failed_member_answer_is_sql_null_not_json_null
  FAILED tests/test_reliability_fixes.py::test_distribution_has_no_null_bucket
  FAILED tests/test_reliability_fixes.py::test_inflight_never_exceeds_cap_when_consumers_equal_cap
  FAILED tests/test_reliability_fixes.py::test_inflight_never_exceeds_cap_when_consumers_exceed_cap
  （另 test_run_controls.py 9 条）
  E  app.runs.service.RunValidationError: model is not configured   ← app/runs/service.py:345
  ```
  根因（行号证据）：`test_run_creation.py` 与 `test_reliability_fixes.py` **绕过 HTTP 直接调 `run_service.start_run`**（`test_run_creation.py:325/364/366/381/384/391`、`test_reliability_fixes.py:165`），且 `test_run_controls.py::make_settings`（`:66-77`）与 `test_api.py::make_settings`（`:67-76`）**未设 `model_endpoint`**（`config.py:115` 默认 `""`）→ `is_model_configured()` 恒 `False`。故 Q6 一旦落地，**16 条既有用例由绿变红**。
- **我实测的变红清单**：3（run_creation）+ 4（reliability）+ 9（run_controls）= **16 条**（原始输出见上）。
- **定级建议**：**P1**（裁决「违反即返工」+ 有真实后果：模型未配置时 `start` 放行 → worker 用空 endpoint 调用 → 该批次全失败，而非即时 422）。**但**修法不能只加校验——须**同批**在测试/环境把 `model_endpoint`（与 `MODEL_NAME`）配好（或在 `make_settings` 注入 `model_endpoint="mock://…"`），否则既有用例必红。这正是工程师当时未做的取舍；建议由主理人裁决「补环境 + 加校验」与「明确豁免 Q6」二选一。

### 🟡 P2-1（建议）— 无在途时 `pause` 不自收敛，且 `pausing` 无 `resume` → 无 worker 时死角

- **内容**：`pause_run`（`service.py:378-419`）**只**把 `running → pausing` 并提交，**不调用 `converge_run`**；而 `cancel_run`（`:493`）会立即调 `converge_run`。故当一个 run 无「running」成员时，`pause` 后停留 `pausing`，直到 worker 收敛。而 `allowed_actions` 对 `pausing` **只有 `cancel`**（`app/api/actions.py:18`），**没有 `resume`** → worker 不在时用户无法恢复，只能取消。
- **证据（原始输出，独立一次性脚本）**：
  ```
  start : 202
  pause : 202
    status (no worker) = pausing
    status (no worker) = pausing
    status (no worker) = pausing
    status (no worker) = pausing
  ```
- **定级理由**：正常工作流有 worker 驱动，影响有限；但属状态机不对称（pause 不收敛而 cancel 收敛），且在 worker 停摆时形成「pausing 不可 resume」的死角。
- **建议修法**：`pause_run` 置 `pausing` 后同事务调 `converge_run`（与 `cancel_run` 对齐），使「零在途」时直接落 `paused`。

### 🟡 P2-2（建议）— 用例名 `test_illegal_transitions_409[I1..I12]` 与事实不符（命名诚实性）

- **内容**：`backend/tests/test_run_controls.py:797-827`。用例名含 `409`，但参数表 `ILLEGAL_TRANSITIONS` 中 **I9 期望 201、I11 期望 422**（12 条里 2 条非 409）。工程师已在 docstring 注明「I9 为合法幂等重放对照」。
- **评估**：按项目纪律「不许用全称/笼统表述」，名字用 `_409` 概括一个含 201/422 的矩阵，属**名实不符**；虽 docstring 有澄清，但 names 是聚合视图（CI 列表、`-k 409` 会误导）。**不构成功能缺陷**。
- **定级建议**：**P2**。建议改名 `test_illegal_and_idempotent_transitions`（或拆成 `test_illegal_transitions_409[I1,I2,I4..I8,I10,I12]` + `test_idempotent_replay_201[I9]` + `test_budget_guard_422[I11]`）。

---

## 4. 需求覆盖度核对表

### 4.1 主文档 §8.2 接口表（逐行，路由以 `app/main.py:25 API_PREFIX="/api/v1"` 挂载）

| Method / path | 路由证据（routes.py 行） | 状态 |
|---|---|---|
| `POST /surveys` | :258（201） | ✅ |
| `GET /surveys`、`GET /surveys/{id}` | :267、:288 | ✅ |
| `PATCH /surveys/{id}`（expected_revision，409） | :296 | ✅ |
| `POST /imports` | :325（201） | ✅ |
| `GET /imports/{id}` | :360 | ✅ |
| `POST /runs/preview` | :375 | ✅ |
| `POST /runs`（必须 `Idempotency-Key`） | :456（201） | ✅ |
| `GET /runs`、`GET /runs/{id}`（含 `allowed_actions`） | :580、:605 | ✅ |
| `POST /runs/{id}/start` | :491（202） | ✅（并发兜底 409 有测） |
| `POST /runs/{id}/pause`、`resume`、`cancel` | :502、:515、:528 | ⚠️ 端点齐；`cancel` 有 500 泄漏（P1-1） |
| `POST /runs/{id}/retry-failed`（必须 `Idempotency-Key`） | :541（202） | ✅ |
| `PATCH /runs/{id}/budget`（只增、不改币种） | :562（200） | ✅（降/改币种 422 实测） |
| `GET /runs/{id}/results`（默认 50/最大 200） | :615 | ✅ |
| `GET /runs/{id}/summary` | :655 | ✅ |
| `GET /runs/{id}/export.csv` | :663 | ✅ |
| `GET /health/live`、`GET /health/ready` | :234、:240 | ✅ |

**16/16 端点齐全**；状态码语义与 §8.2 一致（201 创建 / 202 控制 / 200 读取或幂等重放 / 422 / 404 / 409），**唯 `cancel` 冲突路径为 500（P1-1）**。

### 4.2 TEAM-BRIEF §7 裁决逐条

| 编号 | 裁决 | 落地 | 证据/说明 |
|---|---|---|---|
| C1 | Python 3.12.13 钉版 | ✅ | `uv run python -c` → `(3,12,13)`（实测） |
| C2 | API/worker 同镜像不同命令 | N/A | 属部署（T6/deploy），本轮未验 |
| C3 | worker 重启按 attempts 60s 重建限流窗口 | ✅（T3 已验，未退化） | `test_restart_rebuilds_rate_window` 在基线全量中绿 |
| Q5 | DSN/容器就绪 | ✅ | `pg_isready` → accepting；库名 `survey/survey_test/survey_test_qa` |
| **Q6** | `start` 前校验模型配置否则 422 | ❌ **未落地** | `is_model_configured` 死代码（`config.py:142`）；见 **P1-2** |
| Q7 | 前端不用重型 UI 框架 | N/A | 不在本轮范围 |
| Q8 | CSV `reason` 列固定 | N/A | 属 T5，不在本轮范围 |
| **A1** | `uq_runs_single_active` + `IntegrityError→409`（不得 500） | ⚠️ **部分** | 索引存在、`start` 映射 409（实测）；**`cancel` 未映射 → 500**（**P1-1**） |
| §7.6.1 | 测试库独占 | ✅（流程） | 复核期间 `pg_stat_activity` 无其它 pytest |
| §7.6.4 | 守库断言放宽为前缀匹配 | ✅ | `conftest.py:102-114` |

---

## 5. 无法证伪、暂时采信的项（**不得当作「已证明」**）

1. **`cancelling` 的最终收敛依赖 worker**：我证明了「无 worker → 停留 `cancelling`；worker 的 `scan_expired_once` 能推进到 `cancelled`」。**「在真实部署（worker 常驻）下排队在途必收敛、且不会误判为 `completed`」未做长时压测证伪** —— 采信设计（可从 DB 恢复）。
2. **`uq_runs_single_active` 的并发兜底**：本轮以 5 并发 `start` 实测「恰 1×202」，但**未在真正多进程/独立连接池高并发下重复**；沿用主理人 N4 结论。
3. **`allowed_actions` 表与主文档期望的完全一致性**：主文档 §8.2 未逐状态枚举 `allowed_actions`，我仅核对了呈现值（`ready/running/paused/completed/completed_with_errors/failed/cancelled` 与 `test_api.py::test_allowed_actions_present` 一致）；`pausing`→只有 `cancel` 的取舍见 P2-1，未断言其为缺陷。
4. **`resume` 用「同一成员/模型快照且只跑剩余」**：我验证了「已成功成员 `attempt_count` 不变、不被重跑」（M3 反证）与「`pause_reason` 清空」；**「快照未被任何路径重建」未逐字段断言**（builder/worker 缓存路径未注入）。
5. **API 进程重启不影响 worker**：采信 `test_api.py::test_api_restart_does_not_affect_worker`（基线绿），**本复核未独立重造双 app 实例证伪**。
6. **T0–T2 的历史采信项**（`extra="forbid"` 全面生效、provider 关不透明重试的计数实测等）本轮**未复验**，沿用 `qa-report-t3.md` §6。

---

## 6. 测试可信度评估

- **skip / xfail 数**：全 `tests/` **0**（`grep -rn "pytest.mark.skip\|xfail\|skipif" tests/` 无命中）。
- **`assert True` 数**：**0**（`grep -rn "assert True" tests/` 无命中）。
- **`raises(Exception)` 数**：**0**（`grep -rn "raises(Exception)" tests/` 无命中）。
- **恒真用例排查结论**：以 6 个变异 + 1 个覆盖度实验**逐一攻击**「最易恒真」的 pause/resume/retry-failed/budget/red-line **全部变红**（见 §1），**未发现纸老虎用例**。工程师的 22 条 T4 用例在行为级（DB 侧断言 + 真实 worker + 可控 mock），非配置值断言。
- **`test_illegal_transitions_409` 命名**：技术上有 2/12 参数名实不符（P2-2），但断言本身正确（我独立重放 I1–I12 结果一致）。
- **新增用例**：`backend/tests/test_qa_t4.py`，18 条（13 常规 + 5 参数化 bad-uuid）；跳过/xfail 0。其中 1 条（`test_qa_cancel_ready_while_another_active_does_not_500`）为**故意红**（抓 P1-1），修好 P1-1 后应转绿。

---

## 7. 附：复核过程卫生

- 6 个变异 + 1 个 Q6 实验均**先备份、后还原**，每步 `diff` 逐字节确认一致；结束态 `find app -name "*.qa-bak"` 为空、`grep -rn "MUTANT" app/` 无命中。**源码零残留**。
- 源码还原后（清理完 §0.1 残留）重跑 T4 验收命令原始输出：
  ```
  $ cd backend && uv run pytest tests/test_run_controls.py tests/test_api.py -q
  ............................................                             [100%]
  44 passed in 6.67s        （连续 6 次合并跑均 44 passed）
  ```
  全量：`uv run pytest -q` → **235 collected，234 passed / 1 failed**（唯一 failed = 我用于抓 P1-1 的用例）。
- 仅新建 `backend/tests/test_qa_t4.py` 与本报告；临时证据脚本置于 `backend/.qa_evidence/`（复核结束后**已删除**；其原始输出已在本报告内联）。**未改动 `frontend/`、`tests/load/`、`deploy/` 及既有 `docs/`**。
- 未调用任何真实第三方 API（本机无 key）。
- 复核期间 `survey_test` 由我独占（`pg_stat_activity` 无其它 pytest 会话）。
