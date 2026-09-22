# QA 报告 — T3（批次创建、幂等与 100 槽位可靠执行）对抗性复核

- **复核人**：QA 严过关（software-qa-engineer）
- **对象**：`backend/app/runs/{service,repository}.py`、`backend/app/worker/{main,execute,limits}.py`、`tests/load/mock_provider.py`
- **方法**：以**证伪**为主 —— 配置注入 / 变异测试 / 独立并发探测 + 数据库侧交叉采样 / 跨连接原子性核对；不复跑工程师命令。
- **测试库**：`survey_test`（fail-fast 断言已在）；变异/隔离实验用一次性库 `survey_test_mut`（用后已删除）。
- **新增用例**：`backend/tests/test_qa_t3.py`，**10 条**。

---

## 0. 复跑环境事故（影响"官方全量数字"，与产品无关）

复核期间发现**同一 `survey_test` 被另一个 pytest 进程并发使用**（团队内其他成员/施工在跑 T3 套件），造成：

1. 我的 `TRUNCATE ... RESTART IDENTITY CASCADE` 与其事务**死锁**：
   ```
   sqlalchemy.exc.DBAPIError (asyncpg.exceptions.DeadlockDetectedError): deadlock detected
   DETAIL: Process 7294 waits for AccessExclusiveLock on relation 16449 ... blocked by process 7271.
           Process 7271 waits for RowExclusiveLock on relation 16474 ... blocked by process 7294.
   [SQL: TRUNCATE TABLE attempts, run_members, runs, surveys, imports RESTART IDENTITY CASCADE]
   ```
2. `pg_stat_activity` 显示对方持 30+ 连接在跑 `BEGIN/ROLLBACK/SELECT run_members.status,count(*)`（T3 套件特征）。
3. 观测到的并发进程（同一 session/workspace）：`... pytest tests/test_worker.py tests/test_recovery.py tests/test_run_creation.py -q`、`.venv/bin/pytest -q`（全量）等，反复出现。
4. 后果：**同一套用例在安静窗口绿、在并发窗口红**。一次官方全量在 `survey_test` 上得
   `9 failed, 178 passed`，其中包含**工程师自己的** `test_peak_concurrency_is_100_with_rolling_refill` 与
   `test_kill_worker_recovers_without_losing_success` 变红 —— 均为并发截断/锁等待所致，**非产品缺陷**。

> 已按主理人指示上报，并建议把 `survey_test` 上的 pytest **串行化**。为拿到**可信**数字，下列关键实验在**独立库**
> `survey_test_mut`（独立目录 `.mutate_check/`，用后删除）里跑：
> **隔离全量 = 185 tests：183 passed，2 failed**；2 个 failed 是 T0–T2 的「库名守卫」用例
> （`test_personas.py::test_test_db_is_separated`、`test_qa_t0_t2.py::test_qa_running_against_isolated_test_db`）
> 硬断言 `current_database()='survey_test'`，在隔离库上必然不匹配 —— **即产品代码在隔离库上实际 185/185 全绿**。

---

## 1. 逐项实验（声称 → 实验 → 原始输出 → 结论）

### ① 并发上限测试是不是"假跑"？【最高优先】

**工程师声称**：`test_peak_concurrency_is_100_with_rolling_refill` 真实验证并发上限（峰值 100、滚动补位）。

**实验**：把 `AGENT_CONCURRENCY` 临时改为 **50**（`test_worker.py` 的 `make_settings()` 读 `Settings.from_env()`，故经由 config 注入），跑该用例；再改 **150**；最后恢复默认。

**原始输出**：
```
########## EXP1a: AGENT_CONCURRENCY=50 ##########
>       assert reached, f"barrier never reached target=100 (current={provider.current})"
E       AssertionError: barrier never reached target=100 (current=50)
FAILED tests/test_worker.py::test_peak_concurrency_is_100_with_rolling_refill
1 failed in 21.22s

########## EXP1b: AGENT_CONCURRENCY=150 ##########
.                                                                        [100%]
1 passed in 10.22s
########## restore ##########
.                                                                        [100%]
1 passed in 9.11s
```

**结论**：**成立（非假跑）**。`AGENT_CONCURRENCY=50` 让它**真的变红**，报 `current=50`（而非仅断言配置值）；
`AGENT_CONCURRENCY=150` 仍绿，因为消费者数（100）＝构造参数是更紧的约束 —— 说明它**尊重 config 且不硬编码 100**。
配置注入确能改变**实际在途**，此用例是有效防线，**不是**自欺测试。

---

### ② 独立并发探测 + 数据库侧交叉采样（不复用工程师 mock）

**实验**：自带 `QABarrierProvider`（共享计数器 + `asyncio.Event`），并用**独立连接**反复采样
`attempts.status='running'` / `run_members.status='running'`，与 mock 在途计数逐点比对。

**原始输出**（隔离库，`test_qa_t3.py`）：
```
tests/test_qa_t3.py::test_qa_concurrency_cap_tracks_constructed_limit_with_db_crosscheck PASSED
tests/test_qa_t3.py::test_qa_inflight_never_exceeds_100_at_db_level PASSED
```
- `cap=7`：`assert observed == {7}` 通过 → DB 侧在途在整个闸门期间**恒为 7**，且 == mock。
- 默认 100、300 成员：`db_peak <= 100` 且 `db_peak == provider.current == 100`。

**结论**：**成立**。DB 侧在途与 mock 计数一致且恒定 ≤上限（**默认配置**）。两侧一致，证伪失败。
（但见 **P2-2**：该一致性依赖 `consumers == agent_concurrency`。）

---

### ③ 限流窗口重建（裁决 C3）

**工程师声称**：worker 启动依据 `attempts` 最近 60s **读表**保守重建窗口（重启≠清空）。

**实验**：claim 6 个成员 → 3 条改到 10s 前、3 条改到 3600s 前 → 新 limiter 重建；随后**删光 `attempts`** 再重建。

**原始输出**（`test_qa_rate_window_rebuild_is_table_driven`，隔离库 PASSED）：断言
`added == 3`（窗口内）→ 通过；`DELETE FROM attempts` 后 `added2 == 0` → 通过。

**结论**：**成立**。「删表即归零 / 插表即计入」证明它**读表而非内存**，不是"函数被调用过"的恒真断言。

---

### ④ 租约 CAS（迟到结果不覆盖 / 不产生第二条答案）

**实验**：claim → 过期 → `scan_expired_once`（attempt→`abandoned`、member→`retry_wait`）→ 用**原 claim** 迟到 `finalize_success`。

**原始输出**（`test_qa_stale_result_no_second_answer_and_late_cost_settled`，隔离库 PASSED）：断言
`saved is False`；member 仍 `retry_wait` 且 `answer_json` 无 value；attempt 仍 `abandoned`；
**迟到已知计费** `attempt.actual_cost == late_cost`、`run.actual_cost == late_cost`（如实结算、不重复）。

**结论**：**成立**。旧 token 影响 0 行 → 丢弃，不产生第二条有效答案，账目如实计入。

---

### ⑤ 重试耗尽后绝不补默认答案（红线）+ 变异测试

**工程师声称**：无效输出只记 `INVALID_OUTPUT` 有限重试；终态 `failed`、无答案、尝试次数封顶 3。

**实验 A（端到端）**：`invalid_personas={"p_000002"}` 恒非法输出 + 一个正常成员，跑到底。

**原始输出**（`test_qa_retry_exhaustion_never_fills_default` + `test_qa_all_invalid_run_converges_to_failed`，隔离库 PASSED）：
```
STATUS: completed_with_errors        # p2 恒非法→failed，p1 正常→succeeded
MEMBERS: [('p_000001','succeeded', '{...definitely_yes...}'),
          ('p_000002','failed',    'null')]      # 无答案
整 run 恒非法 → SERVE STATUS: failed
```
断言通过：`p_000002` → `failed`、`answer_json` 无 value、`attempt_count == 3`、全库无 `unsure`；全非法 run 收敛 `failed`。

**实验 B（变异测试，关键）**：在隔离副本 `app/worker/execute.py` 的 `except InvalidOutputError` 分支**故意植入**
「补默认答案 `unsure` → `_record_success`」（复刻上游 `_normalize_value` 行为）：

**原始输出**：
```
=== MUTANT (invalid->unsure) : tests SHOULD go RED ===
FAILED tests/test_worker.py::test_invalid_output_never_becomes_valid_answer
FAILED tests/test_qa_t3.py::test_qa_retry_exhaustion_never_fills_default
FAILED tests/test_qa_t3.py::test_qa_all_invalid_run_converges_to_failed
3 failed, 1 passed in 3.02s
```
（变异后 `serve` 返回 `'completed'` 而非 `'failed'`，坏成员被填 `unsure`。）

**结论**：**成立**。红线测试**真的抓得住**违规实现（含工程师自己的用例），**不是自欺**。

---

### ⑥ 建批原子性（同事务，不许半批）

**实验**：`chunk_size=1` + `flush_hook` 在第 2 块抛错；失败后**从另一条独立连接**核对 `runs` / `run_members`。

**原始输出**（`test_qa_batch_atomicity_no_partial_commit`，隔离库 PASSED）：
```
# 独立会话（全新连接）核对
assert int(runs) == 0
assert int(members) == 0
```

**结论**：**成立**。中途失败**无任何该批次行被提交**（跨连接核对，非同一回滚会话内自证）。

---

### ⑦ 预算与未知计费

- 预算耗尽 → `pause_reason='budget'`（工程师 `test_budget_exhaustion_requests_pause`，隔离库 PASSED）。
- 未知计费（超时/崩溃）→ **保留预留**、`actual_cost` 不写 0、`unknown_cost_count` 增加
  （工程师 `test_budget_reservation_and_unknown_retained`、`test_abandoned_attempt_retains_reserved_cost`，隔离库 PASSED）。
- **静态证伪**：全 `app/` 无任何把未知计费按 0 结清的路径：
  ```
  $ grep -rn 'actual_cost=Decimal("0")|actual_cost or 0|Decimal(0)' app/
  (none)
  ```
  `apply_attempt_cost`（`repository.py:776`）在 `actual_cost is None` 时**只** `unknown_cost_count += 1` 并 `return`，不触碰预留。

**结论**：**成立**（记账不按 0 释放，未知计量单调增）。
**补充观察（P2-c）**：`_settle_late_attempt`（`repository.py:808`）对**迟到且未知**的结果直接 `return False`，
**不** `unknown_cost_count += 1` —— 迟到未知计费只保留预留、不进计数（指标轻微少计）。

---

### ⑧ `next_attempt_at` 不占并发位

**原始输出**（`test_qa_deferred_retry_not_claimed_no_slot`，隔离库 PASSED）：5 成员、3 个 `retry_wait` 且
`next_attempt_at=now+1h` → 只领取到 `row_no ∈ {1,2}`；`worker.limiter.in_flight == 0`（领取不占槽）；
未来重试成员**无 attempt 行**。

**结论**：**成立**。

---

### ⑨ 单 worker 锁

**实验**：两 worker 同时 `start()`；断言第二个失败并**留 ERROR 日志**、`serve()` 立即返回 `locked`（非空转）；释放后可接管。

**原始输出**（`test_qa_second_worker_logs_and_serve_returns_locked`，隔离库 PASSED）：
`second.start() is False`；观测到 `ERROR: another worker holds advisory lock 728100001; exiting without scheduling`；
`second.serve(run_id) == "locked"` 且未领取任何成员。

**结论**：**成立**（但日志可观测性有缺陷，见 **P2-1**）。

---

## 2. 测试可信度审查

1. **恒真 / 过宽断言**：`test_run_creation.py`(10) / `test_worker.py`(13) / `test_recovery.py`(4) 中
   **无** `assert True`、**无** `pytest.raises(Exception)`、**无**过度宽泛的异常断言。
   仅有 2 处 `assert x is not None`（`test_worker.py:235/311`），为 `run_id`/`claim_b` 的空值守卫，**benign**。
2. **skip/xfail**：全 `tests/` **0 个**（`grep pytest.mark.skip/xfail/skipif` 无命中）。
3. **三条"最易恒真"用例复核**：
   - `test_stale_token_cannot_overwrite`：**行为级**（真调 `finalize_success` 错 token → 断言 `rowcount` 语义 + DB 状态），非恒真。
   - `test_abandoned_attempt_retains_reserved_cost`：**行为级**（断言 `reserved_cost` 保留、`actual_cost is None`、`unknown_cost_count==1`），非恒真。
   - `test_restart_rebuilds_rate_window`：**行为级**（断言 `in_window_requests` 由 0→5，且窗口外→0），非"函数被调用过"。
4. `conftest.py` 覆盖 `tmp_path` 到 `backend/.pytest_tmp/`：本仓用例仍真读写文件（如 20k CSV、XLSX），**未掩盖**真实 IO。
5. **契约完整性（T0 遗留，T3 复核确认未退化）**：五档 value↔score 与主文档 §5.1 一致（`compute_cost`/`_value_for` 均按固定五档）。

---

## 3. 缺陷清单

### P0 阻断缺陷：**0 个**
红线 #1（绝不补默认答案）、丢行、写错库、约束形同虚设 —— 均**未被攻破**。

### 🟠 P1-1（重要）— `answer_json` 未作答时存 **JSONB `'null'`** 而非 SQL `NULL`

- **文件:行号**：`backend/app/models.py:191`（`answer_json: ... JSONB, nullable=True`，
  SQLAlchemy `JSONB` 默认把 Python `None` 渲染为 JSON `null`）+
  `backend/app/runs/repository.py:185`（`"answer_json": None` 走 Core insert）。
- **复现实验（原始输出，隔离库）**：
  ```
  MEMBERS: [('p_000001','succeeded','{"score": 5, "value": "definitely_yes", ...}', False),
            ('p_000002','failed',   'null',                                              False)]
  REPORT-TALLY (value|count): [('definitely_yes', 1), (None, 1)]
  PENDING(answer_json IS NULL): [(False, 'null')]     # 未作答成员一开始就不是 SQL NULL
  ```
  即 failed 成员 `answer_json IS NULL = False`、`answer_json::text = 'null'`。
- **影响（不是红线，但破坏"无答案"语义）**：
  - `app/reports/service.py:331` 的 `AND m.answer_json IS NOT NULL` 会把 **failed 成员计入答复统计**，
    产生多余的 `(value=NULL, count=1)` 桶 —— 报表按档位计数**错误**。
  - 任何以 SQL `answer_json IS NULL` 判定"N 进 N 出 / 未答复"的代码都会**判错**（T4 接口/导出有风险）。
  - **现有测试全用 ORM**（`answer_json is None`），而 JSON `null` 经 ORM 反序列化同为 `None`，
    故**看不出**与 SQL NULL 的差异 —— 正是"测试通过≠正确"的典型。
- **建议修复**：`answer_json: Mapped[...] = mapped_column(JSONB(none_as_null=True), nullable=True)`，
  并**一次性迁移**把既有 `'null'::jsonb` 更新为 SQL `NULL`（`UPDATE run_members SET answer_json=NULL WHERE answer_json='null'::jsonb`）。

### 🟡 P2-1（建议）— `app.worker` 日志器被 alembic `fileConfig` **置为 disabled**，ERROR 日志静默丢失

- **文件:行号**：`backend/migrations/env.py:21` `fileConfig(config.config_file_name)`
  （默认 `disable_existing_loggers=True`）；受害者 `backend/app/worker/main.py:45` `logging.getLogger("app.worker")`
  （`app/api/errors.py:39` 同理）。
- **复现实验（原始输出）**：
  ```
  PROBE app.worker: disabled=True level=0 propagate=True handlers=[]
  caplog did not capture: []
  ```
  即凡**进程内跑过 alembic 迁移**（如本仓 `conftest.prepared_test_db`，或启动时编程迁移）之后，
  `app.worker` 的日志**全部被丢弃**（"另一个 worker 持有锁"的 ERROR 日志、过期租约恢复告警等）。
- **影响**：直接削弱"第二个 worker 必须**留明确日志**"这一要求（生产若在启动时编程迁移亦会中招）。非数据问题。
- **建议修复**：`fileConfig(config.config_file_name, disable_existing_loggers=False)`。

### 🟡 P2-2（建议）— 领取在**槽位之前**，`consumers > agent_concurrency` 时 DB 在途可**超过槽位上限**

- **文件:行号**：`backend/app/worker/main.py:309`（`_claim_once` 先写 `running`）→ `:336`（`_process` 才 `self._limiter.slot()`）；
  容量 `slot_capacity = min(concurrency, agent_concurrency)`（`:162`）。
- **复现实验（原始输出，隔离库）**：
  ```
  limiter.capacity = 10 | consumers = 20
  barrier reached: True | mock current/peak: 10 10     # 模型在途正确封顶 10
  DB running members = 10
  DB running members = 20   (×5 采样)                  # 但 DB 侧 running 达 20 > 容量 10
  ```
- **影响**：**模型调用并发**上限未被突破（mock 峰值=10，✅核心不变量成立）；但 DB 侧会出现
  "已领取/持租/已预留但未真正调用"的成员数 > 槽位上限，导致预留虚占、崩溃时未知计费面更大。
  **默认配置 `consumers == agent_concurrency == 100` 不触发**，属配置脆弱性。
- **建议**：领取前先占槽，或把消费者数硬性 `min(consumers, capacity)`；文档明确二者须相等。

### 🟡 P2-3（建议）— `SmoothRateLimiter.rebuild_from_history` **非幂等**，重复调用会重复计数

- **文件:行号**：`backend/app/worker/limits.py:231-256`（`merged = list(self._events)` 后 `self._events = deque(merged)`，**不清空既有**）；
  调用点 `main.py:230-244` `rebuild_rate_window()`（本由 `start()` 调一次）。
- **复现实验（原始输出，纯函数）**：
  ```
  call#1 added= 2 in_window= 2
  call#2 added= 2 in_window= 4
  call#3 added= 2 in_window= 6
  ```
- **影响**：若 `start()` 被再次触发（失锁后重取，见 `main.py:262-265`），窗口会被**重复累加** →
  过度保守限流（自愈，非数据问题）。建议 `rebuild_from_history` 先重置 `self._events`。

### 🟡 P2-c（建议）— 迟到且**未知**计费不进 `unknown_cost_count`
`repository.py:808-840`：`_settle_late_attempt` 在 `actual_cost is None` 时直接 `return False`（保留预留✅），
但**不** `unknown_cost_count += 1` → 指标轻微少计。建议与 `apply_attempt_cost` 对齐。

---

## 4. 新增用例（`backend/tests/test_qa_t3.py`，10 条）

| # | 用例 | 意图 |
|---|---|---|
| 1 | `test_qa_concurrency_cap_tracks_constructed_limit_with_db_crosscheck` | 容量=min(concurrency,config)；DB 在途逐点==mock==上限 |
| 2 | `test_qa_inflight_never_exceeds_100_at_db_level` | 300 成员、DB 侧 running 峰值恒≤100 且==mock |
| 3 | `test_qa_rate_window_rebuild_is_table_driven` | 删表→0 / 窗口内→计入：证明读表非内存 |
| 4 | `test_qa_stale_result_no_second_answer_and_late_cost_settled` | 迟到结果丢弃、无第二条答案、迟到已知计费如实结算 |
| 5 | `test_qa_retry_exhaustion_never_fills_default` | 恒非法：failed / 无答案 / attempt=3 / 无 unsure |
| 6 | `test_qa_all_invalid_run_converges_to_failed` | 全非法 → run 收敛 `failed`、零答案 |
| 7 | `test_qa_no_answer_is_semantically_null_but_not_sql_null` | **固定 P1-1**：语义无答案、但非 SQL NULL |
| 8 | `test_qa_batch_atomicity_no_partial_commit` | 跨独立连接核对无半批提交 |
| 9 | `test_qa_second_worker_logs_and_serve_returns_locked` | 抢锁失败留 ERROR 日志 + serve 返回 `locked` |
| 10 | `test_qa_deferred_retry_not_claimed_no_slot` | 未来 `next_attempt_at` 不占槽、不被领取 |

> 变异实验（`execute.py` 植入补默认答案）在项目内临时目录 `.mutate_check/` 完成（独立库 `survey_test_mut`），
> **已删除**；未改动 `backend/app/` 生产源码。

---

## 5. 全量最终统计

- **隔离库 `survey_test_mut`（无争用，作者权威数）**：
  ```
  185 tests collected: 183 passed, 2 failed
  2 failed = T0-T2 库名守卫（硬断言 current_database()=='survey_test'，隔离库上必然不匹配）
  ```
  等价于产品代码在 `survey_test` 上 **185/185 全绿**（含工程师 27 + QA 10 条 T3）。
- **`survey_test` 官方全量**：复核窗口内**持续被并发套件争用**，三次尝试全部出现**并发伪红**，
  且失败项**随并发方进度漂移**（同一用例时而绿时而红），可判定为环境争用而非产品缺陷：
  ```
  尝试#1: 178 passed /  9 failed   (含工程师 test_peak_concurrency…、test_kill_worker_recovers…)
  尝试#2: 183 passed /  4 failed / 2 errors   (ActiveRunConflictError；test_create_get_list assert 3==1)
  尝试#3: 175 passed / 12 failed / 4 errors   (SystemExit:1 on 20k 用例)
  ```
  其中 `test_create_get_list - assert 3 == 1`、`ActiveRunConflictError`、`SystemExit: 1` 均为
  「并发方插入/截断中途被读」的典型伪红。安静窗口下单跑 `test_qa_t3.py` = **10 passed**。
- **QA T3 = 10 条**（`--collect-only` 确认）；跳过/xfail = **0**。

---

## 6. 我**无法证伪、因而暂时采信**的项

1. **默认配置下并发上限=100 真实有效**：`AGENT_CONCURRENCY` 注入可令其变红/绿，DB 侧交叉采样一致 → 采信。
2. **100 槽位"滚动补位"**为真：`test_peak...` 断言 `started>100 且 current==100`（放行 1 即补位），未证伪。
3. **崩溃恢复不丢已成功答案**：工程师 `test_kill_worker...`/`test_no_duplicate_valid_answer_after_recovery`
   在隔离库绿；语义合理，未证伪。
4. **租约续期/过期扫描语义**：`renew_lease`/`abandon_expired` 的 CAS 与保留预留，未证伪。
5. **`requests_reserved` 单调累计**（不清零）：与"request_limit=3×sample_size 为总请求上限"一致，采信。
6. `uq_runs_single_active` 并发兜底：主理人已独立验证，本次未重复。

> 若 T4 需要用 SQL `answer_json IS NULL` 判定"未答复"，**P1-1 会立刻升级为 P0**，建议先修。
