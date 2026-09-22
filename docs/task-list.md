# 文件级有序任务列表（对齐主文档 T0–T7）

> 阶段 2 交付物（架构师高见远）。**有序、带依赖、按实现顺序**；节点 N1–N8 对应 T0–T7。
> 说明：本清单**严格对齐主文档 §10 的 T0–T7 八阶段契约**（团队任务 #3–#10 亦按此分组）。架构师通用「≤5 任务」上限在此**被项目固化的 T0–T7 契约覆盖**（team-lead 明确要求对齐），故保留八阶段。
> 每阶段先写行为测试并观察失败 → 实现最小代码 → 跑阶段命令 → 记录结果 → 提交可审查变更。**不得以「页面能打开」代替任务恢复与结果正确性验收。**

## 0. 依赖总览

```
T0 (契约/边界)
 ├─ T1 (DB+导入+问卷草稿)
 │   └─ T2 (单 persona 作答闭环)
 │       └─ T3 (批次/幂等/可靠执行)   ← 与 T5 的统计口径接口在本文定义
 │           └─ T4 (控制 API/状态收敛)
 │               └─ T5 (报告/导出)
 │                   └─ T6 (四页前端 + 部署)
 │                       └─ T7 (万级验收)
```
- **硬依赖链：** T0→T1→T2→T3→T4→T5→T6→T7（严格串行，无跳阶段）。
- **可并行（在同一阶段内部，不同文件）：** T1 内 `personas/source.py` 与 `surveys/service.py` 可并行；T3 内 `worker/limits.py` 与 `runs/repository.py` 可并行；T6 内前端四页可并行。**跨阶段不可并行**（快照/契约/统计口径逐级依赖）。

---

## T0：固定上游复用边界与契约（节点 N1）

| 项 | 内容 |
|---|---|
| **文件** | `docs/upstream-audit.md`、`THIRD_PARTY_NOTICES.md`、`backend/app/contracts.py`、`backend/tests/test_contracts.py` |
| **依赖** | 无 |
| **行为要点** | ① 记录上游 commit/真实路径/采用-改编-拒绝项，重点标注默认补值问题（已完成于 `upstream-audit.md`）。② 定义 Pydantic v2 模型：`PersonaSnapshot`、`SurveyInput`、`RunCreate`、`ModelRequest`、`ModelResponse`、`ValidatedAnswer`，命名用主文档 snake_case。③ `SurveyInput` 只允许**1 道题**、**5 个固定 value**（`definitely_not/probably_not/unsure/probably_yes/definitely_yes`）；`purchase_intent` 固定 ID。④ `ValidatedAnswer` 用 `model_config = ConfigDict(extra="forbid")`。`THIRD_PARTY_NOTICES.md` 记 MIT + 源路径 + commit `3633d8d`。 |
| **必写测试** | `test_contracts.py::test_single_question_only`、`::test_five_fixed_values`、`::test_reject_missing_price`、`::test_reject_missing_persona_id`、`::test_reject_missing_profile_text`、`::test_row_no_and_persona_id_preserved`、`::test_no_auto_age_filter`、`::test_extra_field_forbidden` |
| **验收命令** | `cd backend && uv run pytest tests/test_contracts.py -q` |
| **完成条件** | 准备改编的内容**不要求**安装 Harbor、浏览器运行时或原画像生成管线 |

---

## T1：数据库、问卷草稿与现有画像入口（节点 N2）

| 项 | 内容 |
|---|---|
| **文件** | `backend/pyproject.toml`、`backend/.python-version`（内容 `3.12.13`）、`.gitignore`（项目根）、`backend/app/config.py`、`backend/app/db.py`、`backend/app/models.py`、`backend/app/personas/source.py`、`backend/app/surveys/service.py`、`backend/migrations/`、`backend/tests/test_personas.py`、`backend/tests/test_surveys.py`、`backend/tests/conftest.py` |
| **依赖** | T0 |
| **行为要点** | ① `pyproject.toml`：`requires-python=">=3.12,<3.13"`，锁 FastAPI/Pydantic v2/SQLAlchemy 2.x async/asyncpg/Alembic/httpx。② **`backend/.python-version` = `3.12.13`**（消除多个 3.12 目录的解析歧义，**不依赖** `UV_PYTHON_INSTALL_DIR`）＋ 项目根 `.gitignore` **至少覆盖 6 类**：(1) Python（`.venv/`、`__pycache__/`、`*.pyc`、`*.pyo`、`.pytest_cache/`、`.ruff_cache/`、`.mypy_cache/`）；(2) 解释器（`.pythons/`）；(3) **环境变量（含密钥，绝不入库）**（`.env`、`.env.*`，但**必须保留 `!.env.example`**——主文档 §9 交付物；⚠️ `!.env.example` **必须排在 `.env.*` 之后**，否则否定规则不生效）；(4) 前端（`node_modules/`、`dist/`、`.vite/`）；(5) Playwright（`playwright-report/`、`test-results/`、`playwright/.cache/`）；(6) 其他（`.DS_Store`）。`.gitignore` 为 T1 一次性产物，T6 出现前端/Playwright 产物时不再回改。③ `config.py`：从环境读两个 DSN（dev=`…/survey`、test=`…/survey_test`），key 环境变量名。④ `models.py`：五表（见 `architecture.md` §2）+ CHECK + UNIQUE + 索引（**含 `uq_runs_single_active` 部分唯一索引**）。⑤ Alembic 初始迁移 `upgrade head`。⑥ `personas/source.py`：CSV（UTF-8 可带 BOM）/XLSX 指定工作表导入，列映射，行快照，`row_no`/`persona_id` 保留，重复 ID/空画像报 `INVALID_USER_TABLE` 并给工作表/行号/列名，**不静默丢行**。⑦ 20,000 人开发 fixture 生成器。⑧ `surveys/service.py`：问卷 CRUD、`revision`、`PATCH` 带 `expected_revision` 冲突 409。 |
| **必写测试** | `test_personas.py::test_row_order_preserved`、`::test_no_silent_row_drop`、`::test_duplicate_id_rejected_with_position`、`::test_empty_profile_rejected`、`::test_xlsx_sheet_selection`、`::test_20000_fixture_import_rowcount`、`::test_test_db_is_separated`、**`::test_interpreter_matches_pinned_patch`**（断言 `sys.version_info[:3] == (3,12,13)`）；`test_surveys.py::test_revision_conflict_409`、`::test_draft_edit_does_not_pollute_history` |
| **验收命令** | 干净 shell（**未设** `UV_PYTHON_INSTALL_DIR`）：`cd backend && uv run python -c "import sys; print(sys.version_info[:2])"` → 期望 `(3, 12)`；随后 `uv run alembic upgrade head && uv run pytest tests/test_personas.py tests/test_surveys.py -q` |
| **完成条件** | 能导入用户表并显示正确行数与 3 行预览；不做额外数据治理 |

---

## T2：单 persona 作答闭环（节点 N3）

| 项 | 内容 |
|---|---|
| **文件** | `backend/app/inference/prompt.py`、`backend/app/inference/provider.py`、`backend/app/inference/validation.py`、`backend/tests/test_inference.py`、`backend/tests/fakes/fake_provider.py` |
| **依赖** | T1（复用 contracts） |
| **行为要点** | ① `prompt.py`：中文单题模板（主文档 §5.2 系统指令语义），`prompt_version="purchase-intent-zh-v1"` + hash；user 消息按 `persona_snapshot`/`product`/`question`/`output_schema` JSON 序列化；**只传当前 persona**。② `provider.py`：`async answer(ModelRequest)->ModelResponse`，httpx.AsyncClient，**关闭不透明重试**，错误分类（timeout/429/401/403/5xx/协议错误）。③ `validation.py`：拒绝非对象/缺题/错误 question_id/非法 value/额外字段；**仅**可剥单层 Markdown 代码围栏；**不**正则猜答案、**不**补首项、**不**改中立。 |
| **必写测试** | `test_inference.py::test_valid_json`、`::test_fenced_json_single_layer`、`::test_missing_question_rejected`、`::test_unknown_value_rejected`、`::test_extra_field_rejected`、`::test_non_json_rejected`、`::test_429_classified`、`::test_401_classified`、`::test_timeout_classified`、`::test_usage_missing_allowed`、`::test_no_default_answer_fill`、`::test_no_fallback_to_unsure_or_first_option`、`::test_two_personas_isolated_context` |
| **验收命令** | `cd backend && uv run pytest tests/test_inference.py -q` |
| **完成条件** | 单次作答接口稳定、结果严格；业务**不依赖**供应商专有响应格式；无 key 时仅 mock 验证并在 acceptance 标注真实调用未测 |

---

## T3：批次创建、幂等与可靠执行（节点 N4）

| 项 | 内容 |
|---|---|
| **文件** | `backend/app/runs/service.py`、`backend/app/runs/repository.py`、`backend/app/worker/main.py`、`backend/app/worker/execute.py`、`backend/app/worker/limits.py`、`backend/tests/test_run_creation.py`、`backend/tests/test_worker.py`、`backend/tests/test_recovery.py`、`tests/load/mock_provider.py` |
| **依赖** | T2 |
| **行为要点** | ① 同事务建 run 快照 + N 成员（20,000 分块 insert 但**同一事务**提交，失败全回滚）。② 幂等键：同 key 同 hash 返回原 run；同 key 异 hash 409。③ 单 worker advisory lock；第二 worker 退出并留日志。④ 领取事务（锁 run 行 + `FOR UPDATE SKIP LOCKED`，提交后发请求，**不持有事务等待模型**）；120s 租约；20s 续租；10s 扫过期。⑤ CAS 保存（`status='running' AND lease_token=:token`）。⑥ RPM/TPM 平滑限流 + 重启按 attempts 最近 60s 重建窗口。⑦ 预算预留 + 未知费用保留（不按 0 释放）。⑧ 重试：最多 3 次（退避 2s/8s+抖动，Retry-After 优先），`next_attempt_at` 不占并发位。 |
| **必写测试** | `test_run_creation.py::test_snapshot_and_members_same_txn`、`::test_midway_failure_rolls_back`、`::test_idempotent_create_returns_same_run`、`::test_same_key_diff_hash_409`、**`::test_concurrent_start_only_one_active_run`**（并发发起 N 个 `start`，**恰好 1 个 202、其余全 409**；验证 `uq_runs_single_active` + `IntegrityError→409`）;`test_worker.py::test_advisory_lock_second_worker_fails`、`::test_lease_renewal_and_expiry_scan`、`::test_stale_token_cannot_overwrite`、`::test_retry_success_yields_single_valid_answer`、`::test_peak_concurrency_is_100_with_rolling_refill`、`::test_budget_reservation_and_unknown_retained`、`::test_restart_rebuilds_rate_window`；`test_recovery.py::test_kill_worker_recovers_without_losing_success`、`::test_expired_lease_becomes_unknown_attempt` |
| **验收命令** | `cd backend && uv run pytest tests/test_run_creation.py tests/test_worker.py tests/test_recovery.py -q` |
| **完成条件** | 100 个 mock persona 后每成员状态明确，成功数 == 有效答案数；中途杀 worker 可恢复且不丢已成功答案；**恰有 100 在途、峰值 ≤100、释放后滚动补位**（barrier mock + 真实 PostgreSQL） |

---

## T4：控制 API 与状态收敛（节点 N5）

| 项 | 内容 |
|---|---|
| **文件** | `backend/app/main.py`、`backend/app/api/routes.py`、`backend/app/runs/service.py`、`backend/tests/test_run_controls.py`、`backend/tests/test_api.py` |
| **依赖** | T3 |
| **行为要点** | ① 绑定主文档 §8.2 全部接口，统一前缀 `/api/v1`；错误体 `{"code","message","details"}`；201/202/200/422/404/409 语义。② `allowed_actions` 服务端下发。③ 状态校验与非法转移 409（见 `architecture.md` §3.3）。④ 暂停后不发新请求、在途继续保存、收敛 paused；恢复用同一快照只跑剩余。⑤ 取消收敛 cancelled；`retry-failed` 只处理失败成员；提预算不改模型/问卷快照。⑥ 分页（list 默认 20/最大 100；results 默认 50/最大 200）。⑦ 幂等记录落库（非内存）。 |
| **必写测试** | `test_run_controls.py::test_pause_stops_new_requests`、`::test_pause_converges_after_inflight`、`::test_resume_only_remaining`、`::test_cancel_converges`、`::test_retry_failed_only_failed_members`、`::test_budget_increase_keeps_snapshots`、`::test_second_active_run_start_409`、`::test_repeated_pause_cancel_idempotent`、`::test_repeated_retry_failed_no_extra_attempts`；`test_api.py::test_error_body_shape`、`::test_allowed_actions_present`、`::test_pagination_bounds`、`::test_api_restart_does_not_affect_worker`、`::test_illegal_transitions_409[I1..I12]` |
| **验收命令** | `cd backend && uv run pytest tests/test_run_controls.py tests/test_api.py -q` |
| **完成条件** | 所有控制动作均能从数据库恢复；页面不参与调度 |

---

## T5：报告与导出（节点 N6）

| 项 | 内容 |
|---|---|
| **文件** | `backend/app/reports/service.py`、`backend/app/reports/export.py`、`backend/tests/test_reports.py` |
| **依赖** | T4 |
| **行为要点** | ① 一致性读事务聚合：五档计数、比率、`top2box`、`mean_score`、`coverage`、`progress`（口径见 `architecture.md` 与主文档 §8.3）。② 固定用例：5 档各 2 人 + 2 failed ⇒ `planned=12/valid=10/每档20%/top2box=40%/均分=3/coverage=10/12`。③ `valid=0` 比率/均分返回 **null**（不显示 0%）。④ 分组：18–24/25–29/30–35/其他/未知；一线/二线/其他/未知；每组含 planned/valid/coverage；缺列隐藏。⑤ 流式 CSV：字段固定（`run_id,row_no,persona_id,age,city_tier,member_status,question_id,value,score,reason,attempt_count,error_code,model,prompt_version,as_of`），UTF-8 BOM，失败行 value 空，`= + - @` 开头转义，**不导出完整画像**。⑥ ReportService **只 SQL 聚合，不调用 LLM**。 |
| **必写测试** | `test_reports.py::test_fixed_12_planned_10_valid`、`::test_each_bucket_20pct`、`::test_top2box_40pct`、`::test_mean_score_3`、`::test_coverage_10_of_12`、`::test_failed_not_change_denominator`、`::test_zero_valid_returns_null`、`::test_age_city_grouping`、`::test_retry_not_double_counted`、`::test_csv_special_chars_escaped`、`::test_csv_formula_prefix_escaped`、`::test_csv_contains_all_12_rows` |
| **验收命令** | `cd backend && uv run pytest tests/test_reports.py -q` |
| **完成条件** | 页面 API 与 CSV 可按相同快照复核；统计不使用模型生成 |

---

## T6：四页前端与部署（节点 N7）

| 项 | 内容 |
|---|---|
| **文件** | `frontend/src/api.ts`、`frontend/src/pages/SurveyEditor.tsx`、`frontend/src/pages/RunCreate.tsx`、`frontend/src/pages/RunDetail.tsx`、`frontend/src/pages/RunResults.tsx`、`frontend/tests/survey-flow.spec.ts`、`deploy/compose.yaml`、`.env.example`、`docs/runbook.md` |
| **依赖** | T5 |
| **行为要点** | ① 四页真实调用 API；仅运行详情页 3s 轮询，切页清理计时器。② 展示样本数/有效数/错误率/已知-未知费用/**「模拟购买意向」标识**；失败不可隐藏；`completed_with_errors` 原样展示。③ 启动期间禁用重复按钮（服务端真正防重）。④ Compose 起 web/api/worker/postgres + DB 卷持久化；**mock 模式与真实 provider 模式分离**。⑤ 关闭浏览器任务继续；重启 API 可查询原进度。 |
| **必写测试** | `survey-flow.spec.ts::test_full_flow`（上传→配置产品与题目→100 mock 人→暂停/恢复→完成→报告→导出）、`::test_empty_table`、`::test_budget_pause`、`::test_auth_failure_hint`、`::test_page_close_task_continues` |
| **验收命令** | `cd frontend && npm run build && npx playwright test tests/survey-flow.spec.ts` |
| **完成条件** | 浏览器关闭任务仍运行；重启 API 后能查询原进度；用户可从 UI 完成整个模拟流程 |

---

## T7：万级验收与实测报告（节点 N8）

| 项 | 内容 |
|---|---|
| **文件** | `tests/load/mock_provider.py`（自 T3 演进）、`tests/load/run_10000.py`、`docs/acceptance.md`、`docs/runbook.md` |
| **依赖** | T6 |
| **行为要点** | ① mock provider 支持固定延迟、按 `(persona_id, attempt_no)` 确定的前两次 429/5xx/非法输出、永久错误、连接超时（见 `architecture.md` §5）。② 固定并发 100：跑 `normal`（10,000）、`recoverable`（10,000）、`permanent`（10,000），各独立 run；另跑 20,000 正常场景。③ 执行中杀 worker、重启 API、暂停/恢复并重复控制命令，核对 DB 不变量与请求日志。④ 记录机器配置、RSS、DB 体积、实际在途曲线、实测速率、API p95、恢复耗时；目标：状态/报告 API p95 < 1s（10 并发页面请求）。⑤ **真实 API 100→1,000→10,000 在本机无凭据 ⇒ 标记未执行**并写进 `acceptance.md`。 |
| **必写测试/断言** | `run_10000.py --scenario normal`：10,000 有效答案、无重复；`--scenario recoverable`：次数允许内最终 10,000 有效；`--scenario permanent`：预设失败数准确，run=`completed_with_errors`/`failed`；每个场景独立 run 且断言不变量。**（R2）每个场景须同时断言**：(a) 故障按 `(persona_id, attempt_no)` 计划**按预期触发**（核对 `MockCallLog`）；(b) 该链路上**没有任何补值分支产出有效答案**——即非法输出/429/5xx 只落到 `retry_wait`/`failed`，绝不进入 `succeeded` 的 `answer_json`。 |
| **验收命令** | `docker compose -f deploy/compose.yaml up -d --build`；`cd backend && uv run pytest -q`；`uv run python ../tests/load/run_10000.py --scenario {normal,recoverable,permanent} --size 10000` |
| **完成条件** | 100/20,000 mock 场景不变量核对通过；真实调用如实标注未测 |

---

## 8. 共享知识（跨文件约定，禁止各自发明）

### 8.1 枚举与常量（照抄主文档）

| 类别 | 值 |
|---|---|
| 题型 | `single_choice`（首版唯一）；上游 4 类为参考 |
| 题目 ID | `purchase_intent`（固定） |
| 五档 value | `definitely_not`(1) / `probably_not`(2) / `unsure`(3) / `probably_yes`(4) / `definitely_yes`(5) |
| Run 状态 | `ready,running,pausing,paused,cancelling,cancelled,completed,completed_with_errors,failed` |
| Member 状态 | `pending,running,succeeded,retry_wait,failed,cancelled` |
| Attempt 状态 | `running,succeeded,failed,abandoned` |
| pause_reason | `user,api_auth,api_unavailable,budget` |
| 错误码 | `INVALID_OUTPUT`、`CONTEXT_TOO_LONG`、`INVALID_USER_TABLE`（+ provider 映射：`TIMEOUT`/`RATE_LIMITED`/`AUTH`/`SERVER_ERROR`/`PROTOCOL`） |
| 关键常量 | `AGENT_CONCURRENCY=100`、`prompt_version=purchase-intent-zh-v1`、超时 60s、`max_output_tokens=256`、租约 120s、退避 2s/8s、重试上限 3、`request_limit=3*sample_size` |

### 8.2 字段命名（snake_case，照抄主文档）

`row_no`、`persona_id`、`profile_text`、`source_version`、`persona_snapshot`、`survey_snapshot`、`model_snapshot`、`sample_size`、`valid_count`、`coverage`、`attempt_count`、`attempt_limit`、`next_attempt_at`、`lease_token`、`lease_expires_at`、`last_error_code`、`request_limit`、`requests_reserved`、`reserved_cost`、`actual_cost`、`unknown_cost_count`、`idempotency_key`、`request_hash`、`pause_reason`、`report_revision`、`as_of`。

### 8.3 规范化

- **时间**：统一 UTC，存 `timestamptz`；前端按本地时区显示；API 出参 ISO 8601 UTC。
- **金额**：`Decimal` / `numeric(18,6)`；每 run 固定 `budget_currency` 与计价版本；**`unknown` 不写成 `0`**（`actual_cost=NULL`、页面显示「未知」）。
- **错误体**：`{"code","message","details"}`；ID 为 UUID。
- **ID 与行**：`persona_id` 由输入表提供；`run_id`/`persona_id` 由服务端绑定，**不信任模型返回的身份字段**。
- **API 状态码**：创建 201；控制命令接受 202；读取/更新 200；非法 422；未找到 404；幂等冲突/非法转移 409。

### 8.4 红线（不得越界）

1. **禁止补默认答案**：无效输出只能记 `INVALID_OUTPUT` 有限重试，**绝不**补首项/中点/中立/`unsure`（对齐 `upstream-audit.md` §2）。
   - **边界说明（R2）**：`tests/load/mock_provider.py` 按 T7 要求**确定性注入**非法输出 / 429 / 5xx **属正常故障模拟，不属于本红线**。本红线禁止的是**平台**消化无效输出后**补默认答案**。T7 测试须**同时**断言：(a) 故障按预期触发；(b) 该链路上**没有任何补值分支产出有效答案**（即非法输出只产生 `retry_wait`/`failed`，绝不出现在 `succeeded` 的 `answer_json` 中）。
2. **禁止在 HTTP 线程跑万人调用**：worker 必须独立进程；HTTP 只返回批次 ID。
3. **禁止暴露 API key**：key 只在后端环境/受控文件；不进前端、日志、快照、CSV、`model_snapshot`；endpoint 不开放为普通问卷输入项。
4. **禁止把 mock 容量说成真实模拟**：`acceptance.md` 必须显式声明真实调用未测；不得声称完成真实万人模拟。
5. **禁止把 100 说成总数**：100 = 并发槽位数，不是样本数/机器数/进程数；不得把 100 个排队任务宣称为 100 个在途。
6. **禁止把「估算」写成「实际」**：无可靠 token 上界时预算标「估算软上限」，UI 不得声称严格零超支。
7. **禁止统计用 LLM**：ReportService 只做 SQL 聚合；比率/均分在展示层四舍五入。
8. **禁止测试连生产库**：测试连接必须指向 `survey_test`。**可验收断言（R3）**：`conftest.py` 启动时执行 `SELECT current_database()`，断言等于 `'survey_test'`，否则 **fail fast**（`RuntimeError`），且在建立任何连接后、跑任何用例前完成。
9. **禁止自创枚举/字段名**：一律照抄本文 §8.1/§8.2；冲突先写明 + 最小修改理由并经主理人裁决。

### 8.5 目录约定

- **可写区根**：`/Users/zhao/WorkBuddy/2026-09-20-20-00-42/`（沙箱约束边界）。
- **项目根**：可写区**之下**的 `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/`（所有工程文件在此项目根之下创建）。
- 主文档 §9 相对路径以**项目根**展开；包目录按需补 `__init__.py`。
