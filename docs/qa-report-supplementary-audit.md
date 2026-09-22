# 只读补充审查报告（A–E）

> **审查人**：QA 工程师 严过关（`software-qa-engineer-4`）
> **审查时间**：2026-09-22（本机 CST=UTC+8；下文中 DB 时间戳一律为 **UTC**，文件 mtime 一律为 **本机 CST**，两者换算已在文中注明）
> **性质**：**只读**补充审查。未修改 `backend/app/**`、`backend/migrations/**`、`backend/tests/**`、`deploy/**`、`frontend/**` 与任何既有文档；未执行任何基线测试命令；未删除/回滚/清理任何文件、数据行或容器。
> **被审查批次**：run_id `1aae0492-a2b6-48a1-bf32-2b019dd324bd`，`sample_size=400`，终态 `cancelled`，位于 `survey` 开发库（容器 `survey-pg`，`127.0.0.1:55432`）。
> **结论纪律**：只采信本人亲手跑出的原始输出；不使用「全部/已确认」全称表述；无法验证的项一律写「**未验证**」。

---

## 0. 执行摘要

| 补充项 | 结论（一句话） |
|---|---|
| **A 生命周期闭环** | **完全闭环**。401 = 396×1 + 1×2 + 2×1 + 1×1；唯一发生额外重试的是 `ILB_d8ab440cb00a5fd6`（row 16）；「3 length = 2 failed + 1 abnormal running」这个等式**不成立**。 |
| **B 状态机一致性** | **发现 1 处措辞与状态机冲突 + 1 处真实不一致**。措辞：报告写的「2 failed」实为 `retry_wait`（`failed` 是终态）。不一致：全库唯一 1 条「attempt `running` 但成员非 `running`」的孤儿 attempt。 |
| **C force-cancel 语义** | **确认为「状态解释能力缺口」**：`runs` 无 `cancel_reason`/`abort_reason` 列；本轮 `control_events_json` **根本没有 cancel 事件**（只有 create/start）；四种中止来源**无法区分**。 |
| **D 性能解释** | **Little 定律足以解释，无需引入任何瓶颈假设**。同边界口径下误差 **0.01%**；报告 §2.2 的 12.6% 偏差来自「槽位口径 L」配「HTTP 口径 W」的**边界错配**。且「在途未稳在 100」是**伪命题**（pending 存在期间 limiter 恒为 100）。 |
| **E 仓库基线完整性** | `test_reason_length_fix.py` **当前不存在，但曾经存在并被跑过**（`.pytest_cache` 有 10 条 nodeid、8 条 lastfailed）。`backend/app`、`backend/migrations` 在 400 人运行窗口之后**无改动**（正面结论）。差异清单见 §E。 |

**问题分类计数**：已证实平台缺陷 **5** 条 / 原设计与实现偏差 **2** 条 / 报告措辞错误 **8** 条 / 潜在风险 **5** 条 / 未验证优化候选 **3** 条 / 未验证事项 **7** 条 / 开发执行越权·仓库治理 **7** 条。

---

## 补充 A：3 个 `length` 的逐成员生命周期闭环

### A.1 `finish_reason` 的证据来源（先讲清口径，再给数字）

**结论：`finish_reason` 未落库，DB 侧无法直接读到。**

- 代码事实：`backend/app/inference/provider.py:232` 把 `first_choice.get("finish_reason")` 写进 `ModelResponse.finish_reason`（`app/contracts.py:333`）；**但全项目无任何落库路径**——`app/models.py` 的 `attempts` 表无 `finish_reason` 列，`attempts.usage_json` 在本轮 400 条里**只有 `input_tokens` / `output_tokens` 两个键**（已逐条核对）。
- 因此本报告的「3 次 `length`」**实测来源**是驱动脚本 `run_400_cap100.json` 的逐请求记录（脚本侧 `ProbeTransport` 抓取），**不是 DB 字段**。该 JSON 的 `network.summary.finish_reason_breakdown = {"stop": 398, "length": 3}`。
- **DB 侧的间接证据（推断，非实测字段）**，三条互证：
  1. 本轮 `attempts` 中 `output_tokens ≥ 3800` 的**恰好 3 条、全部=4096、全部为 `error_code='INVALID_OUTPUT'` 的失败 attempt**；所有 succeeded attempt 的 `output_tokens` 最大仅 **3693**，无一接近上限（SQL 见 §F-Q5）。
  2. `runs.model_snapshot->>'max_output_tokens' = '4096'`（DB 实测）⇒ `output_tokens == max_output_tokens` 即「撞上限」。
  3. 第 3 条（row 223）的 `raw_output` 是 **81 字符、被截断的 JSON**：`{"question_id":"purchase_intent","value":"probably_yes","reason":"对怡兰葆品牌有好感，关注成分且` —— 无闭合括号，是**物理层面的截断证据**。
  - ⚠️ 另 2 条（row 16 / row 210）的 `raw_output` 为**空字符串**（`length=0`，非 NULL）—— 与 `TEAM-BRIEF §7.9` 已记录的「3 条里 2 条空串」同一现象，**成因未定性**（见 U-02）。

### A.2 逐 persona 表（用户要求的 6 个字段）

| # | persona_id | attempt 1 结果 | 是否产生 `INVALID_OUTPUT` | 是否发生 retry | retry 是第几次请求 | 最终 member status |
|---|---|---|---|---|---|---|
| 1 | `ILB_d8ab440cb00a5fd6`<br>(row_no **16**) | HTTP 200；`finish_reason=length`；`duration 22083 ms`；`completion_tokens=4096`（=上限）；`reasoning_tokens=4096`；`raw_output` 空串 → 校验判无效 | **是**（attempt 1 落 `error_code=INVALID_OUTPUT`，`attempts.status=failed`） | **是** | **第 401 次请求**（`attempt_no=2`，全局唯一一次重试） | **`succeeded`**（attempt 2：started `15:57:33.214697`，`duration 17092 ms`，`output_tokens=3094`，答案 `probably_yes` / score 4） |
| 2 | `ILB_72cb653d1d21ce90`<br>(row_no **210**) | HTTP 200；`finish_reason=length`；`duration 21715 ms`；`completion_tokens=4096`；`reasoning_tokens=4096`；`raw_output` 空串 → 校验判无效 | **是** | **否**（已排 `next_attempt_at = 2026-09-21 15:57:20.674219+00`，未到点即被中止） | — | **`cancelled`**（中止时刻状态为 `retry_wait`） |
| 3 | `ILB_3444806ecc743eae`<br>(row_no **223**) | HTTP 200；`finish_reason=length`；`duration 21573 ms`；`completion_tokens=4096`；`reasoning_tokens=4067`；`raw_output` = 81 字符截断 JSON → 校验判无效 | **是** | **否**（已排 `next_attempt_at = 2026-09-21 15:57:21.362501+00`） | — | **`cancelled`**（中止时刻状态为 `retry_wait`） |

**另需单列的那 1 个「abnormal running」成员（与 3 个 `length` 无关）**：

| persona_id | attempt 1 结果 | 是否产生 `INVALID_OUTPUT` | 是否发生 retry | retry 是第几次请求 | 最终 member status |
|---|---|---|---|---|---|
| `ILB_1b0598a7c1df9bb3`<br>(row_no **87**) | HTTP 200；**`finish_reason=stop`**；`duration 2084 ms`；`completion_tokens=332`；`reasoning_tokens=232`；`t_start=2.358s / t_end=4.443s` —— **模型调用已成功返回**，但 finalize 阶段抛 pydantic `ValidationError`（`reason` >100 字符）被 `worker/main.py:361-364` 的兜底 `except Exception` 吞掉 | **否**（`error_code=NULL`、`usage_json=NULL`、`raw_output=NULL`、`duration_ms=NULL`） | 否 | — | **`cancelled`**（中止时刻状态为 `running`，attempt 至今仍为 `running`） |

### A.3 三个计数问题的解释

#### Q1：为什么 400 个 member 产生 401 次 request？

**唯一一次重试来自 row 16（`ILB_d8ab440cb00a5fd6`）。** 逐项对账（DB 原始行，无四舍五入）：

```
396 个成员 × 1 次  = 396      （succeeded 且 attempt_count=1）
  1 个成员 × 2 次  =   2      （row 16：attempt1 失败 → attempt2 成功）
  2 个成员 × 1 次  =   2      （row 210 / 223，retry_wait）
  1 个成员 × 1 次  =   1      （row 87，悬挂的 running attempt）
                    ----
                    401  ✅
```
- DB 交叉验证：`runs.requests_reserved = 401`（SQL §F-Q1）；`attempts` 按 `attempt_no` 分组为 `{1: 400, 2: 1}`（SQL §F-Q2）。
- 脚本侧交叉验证：`invariants.raw.retry_distribution = {"1": 396, "2": 1}`、`invariants.raw.counts = {"succeeded":397, "retry_wait":2, "running":1}`、`attempts_total = 401` ⇒ 396×1 + 1×2 + 1×1 + 2×1 = 401，**两侧逐项吻合**。

#### Q2：哪一个 member 发生了额外一次 retry？

**`ILB_d8ab440cb00a5fd6`（row_no 16）**，即 3 个 `length` 中的第 1 个、也正是触发看门狗中止的那一条（`run_400_cap100.json` 的 `abort.detail.persona_id` 就是它）。它是全局**第 401 次**请求，started `2026-09-21 15:57:33.214697+00`（距 attempt 1 失败 `15:57:10.091179` 约 23.1 s，符合 `RetryPolicy.backoff_seconds=(2.0, 8.0)` 叠加 `Retry-After`/抖动后的量级），`duration_ms=17092`，结果 `succeeded`。

#### Q3：3 个 `length` 与最终「2 failed + 1 abnormal running」是什么关系？

**申报中的这个等式不成立。** 正确映射是：

```
3 个 length（3 条 INVALID_OUTPUT attempt）
   ├─ 1 个 → 重试成功（row 16）            → 计入 succeeded 397
   └─ 2 个 → 排在 retry_wait，未执行即被中止 → 后被 force-cancel 置 cancelled（row 210 / 223）

1 个 abnormal running（row 87）
   └─ 与 length 完全无关：它 finish_reason=stop、HTTP 200、模型调用已成功返回，
      属独立的平台缺陷（D-03）导致的悬挂
```
即：`3 = 1（重试成功） + 2（待重试后被取消）`；**「2」不是 `failed`**（见 B 节）；**「1」不是 `length` 的后果**。

### A.4 闭环结论

> ✅ **数字完全闭环。** 400 members / 401 requests / `length`=3 / `INVALID_OUTPUT`=3 / succeeded=397 / 非成功 3（2 `retry_wait`→`cancelled` + 1 `running`→`cancelled`）/ peak 100 / wall 40.97 s —— 每一项都能落到 DB 原始行或脚本逐请求记录上，**无残余、无矛盾、无需要四舍五入补齐的缺口**。
> ⚠️ 唯一需要改的是**措辞**：申报中的「2 failed」在 DB 与脚本两侧都是 `retry_wait`（`failed_count = 0`），详见 W-01。

---

## 补充 B：状态机一致性

### B.1 代码依据：`failed` 是终态，仍可重试的成员必须是 `retry_wait`

- `backend/app/worker/execute.py:340-344`：
  ```python
  retry_allowed = (not force_permanent) and claim.attempt_no < claim.attempt_limit
  status = "failed"
  if retry_allowed:
      status = "retry_wait"
      ...
      next_attempt_at = now + timedelta(seconds=delay)
  ```
  ⇒ **`attempt_no < attempt_limit` 时写 `retry_wait` 并写 `next_attempt_at`；只有额度耗尽才写 `failed`（终态）**。
- `failed` 的复活路径只有 `retry-failed`（`runs/service.py:618-624`，把成员重置为 `pending` 并 `attempt_limit += 3`），无自动重试。
- 本轮 3 个非成功成员的 `attempt_count = 1`、`attempt_limit = 3` ⇒ `1 < 3` ⇒ **只能是 `retry_wait`，不可能是 `failed`**。
- DB 实测印证：row 210 / 223 的 `next_attempt_at` **非空**（`15:57:20.674219` / `15:57:21.362501`），`last_error_code=INVALID_OUTPUT`；`final.member_status_counts`（脚本在 15:57:50 抓取）= `{"running":1, "retry_wait":2, "succeeded":397}`，`summary_endpoint.failed_count = 0`。
- 补充一个代码事实（非缺陷，但易误读）：`RECOVERABLE_ERROR_CODES`（`execute.py:50-52`）**不含** `INVALID_OUTPUT`，但无效输出路径（`execute.py:210-222`）调用 `_record_failure_outcome` 时**未传 `force_permanent`**（默认 `False`）⇒ 仍会重试。**当前行为符合主文档 §6.4「无效输出有限重试」，行为正确**，但与常量集合的字面语义不一致（列为 V-02）。

### B.2 SQL 反例扫描（全 `survey` 库，非仅本轮）

| 探针 | 结果 | 判定 |
|---|---|---|
| `status='failed' AND attempt_count < attempt_limit` | **0 行** | ✅ 无「可重试却被判终态」 |
| `status='retry_wait' AND next_attempt_at IS NULL` | **0 行** | ✅ 无「待重试却无排期」 |
| `status='running'` 但无 `running` attempt | **0 行** | ✅ |
| `status='running'` 且租约已过期 | **0 行** | ✅ |
| **`attempts.status='running'` 但成员非 `running`** | **1 行** | ❌ **唯一真实不一致**（本轮 row 87） |
| `succeeded` 成员无成功 attempt | **0 行** | ✅ |
| `attempt_count` ≠ 实际 attempt 行数 | **0 行** | ✅ |
| 有答案但零成功 attempt | **0 行** | ✅（无凭空答案，红线 #1 守住） |
| `answer_json` 为 JSONB `'null'` | **0 行** | ✅ §7.6.2 的 P1 在本库已修复 |
| `answer_json IS NULL`（SQL NULL） | 53 行 | ✅ 预期（= 5 个 cancelled 10 人批次 50 条 + 本轮 3 条） |

### B.3 那个 abnormal running 成员：三种可能性的裁定

用户要求判定它是「在途被 force-cancel 打断」还是「租约过期未回收」。**实测答案是第三种**：

| 候选 | 判定 | 证据 |
|---|---|---|
| ① 在途被 force-cancel 打断 | ❌ **不成立** | 该成员的 HTTP 调用在 `t=4.443 s` 就已成功返回（脚本 `seq=12`：`HTTP 200`、`finish_reason=stop`、`duration 2084 ms`）。force-cancel 发生在 `runs.finished_at = 15:57:50.637674`（≈ t=41 s）。调用早已结束，**不是被网络中断**。 |
| ② 租约过期未回收 | ❌ **不成立** | `DEFAULT_LEASE_SECONDS = 120.0`（`worker/main.py:68`）。该成员 claim 于 `15:57:09.976843`，租约应到期于 `15:59:09.98`；force-cancel 发生在 `15:57:50.64`，**仅过去 40.66 s，租约尚未过期**。且 `abandon_expired`（`repository.py:499-508`）要求成员 `status='running'` 且 `lease_expires_at IS NOT NULL`；该成员现为 `cancelled`、`lease_token=NULL`、`lease_expires_at=NULL` ⇒ **永远不会被回收**。 |
| ③ **调用已完成但 finalize 抛异常被吞 ⇒ 悬挂**（真实原因） | ✅ **成立** | 三方证据闭合：(a) DB attempt 全字段为 NULL（`error_code`/`usage_json`/`raw_output`/`duration_ms`/`provider_request_id`）而 `status='running'`；(b) 脚本记录该请求 HTTP 200 + `stop` + 2084 ms 成功返回；(c) 代码路径 `validation.py:117` → `ValidationError` → 穿透 `execute.py` → 被 `worker/main.py:361-364` 的 `except Exception` 吞掉，仅留一行 `unexpected error executing member`。 |

**⇒ 该成员不是「在途被打断」，也不是「租约过期未回收」，而是「模型调用已成功返回、平台未能 finalize」导致的悬挂**（即 run 报告 §7 已立项的 P1 缺陷 D-03）。

---

## 补充 C：force-cancel 的状态语义（能力缺口判定）

### C.1 平台当前能力（DB + 代码实测）

| 检查项 | 实测结果 |
|---|---|
| `runs` 表是否有 `cancel_reason` / `abort_reason` 列 | **没有**。`\d runs` 全列中无任何中止原因字段（SQL §F-Q7）。 |
| 本轮 `control_events_json` 内容 | **只有 2 条事件**：`create`（15:57:09.389527）与 `start`（15:57:09.438592）。**没有任何 cancel 事件**（SQL §F-Q6）。 |
| 控制事件的字段结构 | `{at, action, result, request_hash, idempotency_key}` —— **无 actor / source / reason 字段**。 |
| `cancel_run` 是否接受原因入参 | **不接受**。`routes.py:552-562` 的 `POST /runs/{id}/cancel` 只传 `run_id`，`run_service.cancel_run(session, run_id)` 无 reason 参数。全项目仅此一个取消端点，**无 force-cancel 变体**。 |
| `pause_reason` 取值域 | `ck_runs_pause_reason` = `NULL \| 'user' \| 'api_auth' \| 'api_unavailable' \| 'budget'`（DB 约束实测）。注意：**「暂停」有来源区分，「取消」没有**。 |

### C.2 由此得到的一条硬事实

`runs/service.py` 的 `cancel_run` 在 **三个分支**（`ready`→:499、`running/pausing/paused`→:523、已达目标态→:547）**都会追加一条 `action='cancel'` 的控制事件**。本轮 `control_events_json` **没有**该事件，⇒ **本轮的 `cancelled` 不是经 `cancel_run` 产生的**。

再叠加 B.2 的证据：代码里**没有任何路径**能把 `running` 成员直接置为 `cancelled`（`repository.py:883` 与 `:944` 只处理 `pending` / `retry_wait`），而本轮 row 87 恰好是 `running → cancelled` 且其 attempt 仍是 `running`。
⇒ **本轮终态由服务层之外的手段（最可能是直接 SQL）产生**。具体命令/脚本我**未能验证**（U-04）。

### C.3 缺口判定

> ❌ **确认为「状态解释能力缺口」**：`用户主动取消` / `质量门禁中止` / `人工运维中止` / `系统异常中止` —— **四种来源在平台内一律只呈现为 `cancelled`，且本轮连一条 cancel 审计事件都没有留下。**

后果（真实，非假设）：
1. 事后无法回答「这个批次为什么停了」——本轮实际是「质量看门狗按 `finish_reason_length` 中止 + force-cancel 收敛」，但**平台内零痕迹**，只能靠项目树之外的 `run_400_cap100.json` 才能还原。
2. 无法区分「用户主动放弃」与「平台/运维异常」——对结论可信度（397/400 是否可信）的判断完全依赖外部文档。
3. 绕服务层的 force-cancel 会留下**不可回收的孤儿 attempt**（D-04）且**不写控制事件**，一旦成为常规运维手段，审计链将整体断裂。

### C.4 未来建议（**不实施**，仅供主理人裁决）

**最小方案（建议）**：

1. **新增列** `runs.cancel_reason TEXT NULL`，取值域建议：
   `user_cancel` / `quality_gate` / `ops_force` / `system_error` / `unknown`（`unknown` 用于兼容历史行与外部 SQL 直接置位的情况）。
   加 CHECK 约束 `ck_runs_cancel_reason CHECK (cancel_reason IS NULL OR cancel_reason = ANY (ARRAY[...]))`，与 `pause_reason` 同构。
2. **写入位置**：只在服务层写入。建议：
   - `cancel_run` 增加可选入参 `reason`（默认 `user_cancel`），写列**并**写入控制事件；
   - 新增显式的 `force_cancel_run(reason=...)` 服务方法（供运维/看门狗调用，走同一事务、同一审计路径），**从根上消除「直接 SQL 置位」的需求**；
   - 看门狗类中止（本轮的 `finish_reason_length`）应走 `quality_gate` 这一取值，把「谁中止的」变成平台内一等公民。
3. **与 `control_events_json` 的关系**：列负责**可查询/可聚合**（一个字段即可做 `GROUP BY cancel_reason`），事件负责**可追溯**（时间线）。两者**同事务写入**，列是事件的冗余投影 —— 这与现有 `retry-failed` 事件（`service.py:621-624`）既写事件又改 `attempt_limit` 的模式一致，不引入新范式。
4. **对 `uq_runs_single_active` 等约束无影响**：该索引是 `UNIQUE, btree((true)) WHERE status IN ('running','pausing','paused','cancelling')`，只依赖 `status` 列。新增一个可空 TEXT 列**不改变索引的 WHERE 谓词、不改变任何行锁语义**，也不会引入新的唯一冲突面（它不是唯一列）。`cancel_run` 现有的「`ready` 一次性收敛、不写 `cancelling`」结构性修复（§7.9.1）不受影响。
5. **迁移注意**：历史 `cancelled` 行（本库现有 6 个）应回填 `unknown`，并在 `KNOWN-ISSUES.md` 注明「历史 cancelled 批次的中止来源不可考」。

---

## 补充 D：性能结论防过度解释

### D.1 先校正两个口径（这是本节的关键）

本轮有两个「在途」数字，语义完全不同，**混用就会得出错误结论**：

| 指标 | 值 | 语义（实测来源） |
|---|---|---|
| **L_limiter**（槽位占用） | 峰值 **100**，采样均值 **58.771** | 进程内每 100 ms 读 `ConcurrencyLimiter.in_flight`（402 个采样点）。**包含**领取 / 限流等待 / 构造请求 / HTTP / 落库全过程。脚本字段：`concurrency.peak_in_flight_limiter`、series 均值。 |
| **L_http**（HTTP 在途） | 峰值 **100**（区间精确重建），时间加权均值 **52.198** | 由 401 条 `[t_start, t_end]` 请求区间重建。脚本字段：`rebuilt_peak_in_flight_from_requests=100`；时间加权均值 = Σduration / 窗口 = 2150.852 s / 41.205 s。 |
| **W_http**（平均 HTTP 延迟） | **5.363 s** | `duration_ms_sum / 401 = 2,150,616 / 401 = 5363.1 ms`。 |

两者差：`58.771 × 41.2535 − 2150.852 = 273.4` 人·秒 ⇒ **每个请求额外占用槽位 0.682 s**（领取+限流+建请求+落库）。

### D.2 Little 定律核算（用户要求的算式 + 修正后的算式）

**用户给出的算式（混合边界）**：
```
λ_实测  = 400 / 40.97           = 9.763  rows/s
λ_Little = L / W = 58.77 / 5.363 = 10.959 rows/s
差异     = 10.959 / 9.763 - 1    = +12.3%
```
（若分子用 401 而非 400：`401/40.97 = 9.790`，差异 +12.0%。）

**修正后的算式（同边界：都取 HTTP 口径）**：
```
L_http  = 52.198（时间加权，窗口 41.205 s）
W_http  = 5.363 s
λ_Little = 52.198 / 5.363 = 9.7328 rows/s
λ_实测   = 401 / 41.205  = 9.7318 rows/s
差异     = 0.01%
```

**⇒ 12% 的偏差不是「未被解释的吞吐损失」，而是「槽位口径 L」配「HTTP 口径 W」的边界错配。** 同边界下 Little 定律几乎精确成立（0.01%），**无需引入任何额外瓶颈来解释**。

顺带校正报告 §2.2 的一处推导：报告写「`401 / 10.96 = 36.6 s` + 启动爬坡 ≈ 41 s，与实测吻合」。在同边界口径下 `401 / 9.7328 = 41.2 s` **本身就等于实测窗口**，**不存在需要手工补的「爬坡项」**——那 ~4.4 s 正是边界错配的产物。

### D.3 「槽位是否长期空闲」——直接核查（本节最关键）

用户要求：升级为「性能缺陷」的唯一条件是「pending 充足、槽位长期空闲、领取/连接等待占主要时间」。实测：

| 窗口 | limiter 采样点数 | 均值 | 最小 | 最大 | `<90` 占比 | `<80` 占比 |
|---|---|---|---|---|---|---|
| **pending 存在期间**（t ∈ [1.0 s, 19.575 s]，19.575 s = 最后一条 attempt_no=1 的启动时刻） | **185** | **100.0** | **100** | **100** | **0.0%** | **0.0%** |
| 全窗口（t ∈ [0, 41.25]） | 402 | 58.771 | 0 | 100 | — | — |

> **⇒ 只要有活可干，100 个槽位是 100% 饱和、恒为 100 的（185/185 个采样点无一例外）。**
> 均值 58.771 完全由**收尾排水段**（t > 19.575 s，此时已无待领成员，请求数从 92 单调衰减到 1）与最开始的爬坡段拉低，与「槽位空闲而没人干活」无关。

（DB 侧独立重建可交叉印证：按 `started_at → finished_at + duration_ms` 重建，在 pending 存在期间 34 个 0.5 s 采样点上均值 **92.97**、峰值 98、最低 80，利用率 93.0%。之所以低于 100，是因为 4 条 attempt 的 `duration_ms` 为 NULL（D-02 + D-03）导致区间被截断——这本身就是 D-02 的后果。）

### D.4 三条「未验证优化候选」的裁定

| 候选 | 本轮证据 | 裁定 |
|---|---|---|
| **`claim_members(limit=1)` 领取路径串行化**（KNOWN-ISSUES C-01） | **无任何支持证据**：pending 存在期间槽位 100% 饱和；稳态 λ ≈ 100/5.363 ≈ **18.6 req/s**，而领取路径实测上限 ≈ **90 rows/s**（§7.17.3 已裁定事实）⇒ 结构上不可能绑定（余量约 4.8×）。 | **维持「未验证优化候选」+「已裁定不改代码」。不得升级为性能缺陷。** |
| **run 行锁** | 同上，λ≈18.6 ≪ 90 rows/s。 | 已裁定不改代码（§7.17.3），本轮数据复核一致，不重复论证、不推翻。 |
| **HTTP keep-alive / TLS 握手 / 连接复用**（脚本观测 `distinct_http_connections=400` / 401 请求） | **只有现象，无因果、无量化**。且槽位已满负荷，说明即使有额外握手开销，也**没有表现为槽位空闲**。 | **维持「未验证优化候选」（脚本自己也标 U-05/U-10 未验证）。严禁写成瓶颈结论。** |

### D.5 结论

> ✅ **Little 定律足以解释本轮吞吐，不需要也不应该引入额外瓶颈假设。**
> - 同边界下预测误差 **0.01%**；
> - 稳态吞吐 ≈ **18.6 req/s**，与 `C/W = 100/5.363` 完全一致（槽位饱和）；
> - 全程平均 9.73 req/s 的“低”完全由**收尾排水段**（无待领成员时的自然排空）造成，属批次的固有形态，不是缺陷；
> - 「在途未稳在 100」是**伪命题**（W-05）；报告 §2.2 的两个候选假设（领取路径 / 连接复用）**既无证据、也无必要**。

---

## 补充 E：仓库基线完整性（只报告，不清理）

### E.0 方法限制（必须写明）

**本项目不是 git 仓库**（项目根与各层目录均无 `.git`，与 `TEAM-BRIEF §7.7 环境备注` 一致）。因此**无法用 `git diff` / `git status` 判定未授权改动**。本节只能用：① 目录/文件 **mtime** 与项目文档记录的交付物清单对照；② 内容级比对。**该方法不能证明「没有改动」，只能证明「在给定时间点之后没有 mtime 变化」。**

### E.1 `test_reason_length_fix.py`：确认不存在，但曾经存在且被跑过

| 判据 | 实测 |
|---|---|
| `ls -la backend/tests/` | **22 个 `test_*.py`，无 `test_reason_length_fix.py`** ✅ 主理人的结论复核无误 |
| `backend/.pytest_cache/v/cache/nodeids` | 共 **273** 条，其中含 **10 条** `tests/test_reason_length_fix.py::*` ⇒ **该文件曾被 pytest 收集过** |
| `backend/.pytest_cache/v/cache/lastfailed` | **8 条** 来自 `tests/test_reason_length_fix.py` 的失败记录（`test_reason_over_limit_raises_invalid_output`、`test_repro_pydantic_validation_error_must_not_escape`、`test_reason_boundary_100_ok_101_rejected`、`test_no_fabricated_answer_when_reason_too_long`、`test_long_reason_member_enters_retry_wait_not_stuck_running`、`test_long_reason_member_is_never_left_without_error_code`、`test_long_reason_member_eventually_failed_with_invalid_output`、`test_member_recovers_after_long_reason_then_valid`） |
| 基线数目核对 | 273 = **263**（PROGRESS-HANDOFF §3 记录的当前基线）+ **10**（该文件的用例数）⇒ **精确吻合** |
| 文件去向 | `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/pending-reason-length-fix-test.py`（**项目树之外**，mtime `2026-09-22 00:18` CST）。AST 解析得 **10 个 `test_*` 函数，与 `.pytest_cache` 的 10 条 nodeid 逐条同名吻合** ⇒ 它就是被移出的那个文件 |
| **是否本应存在** | **是。** 它是本轮 §7 / D-03 那个 P1 缺陷（`reason` 超长 → 成员静默卡 `running`）的回归测试，run 报告 §7 明确「已立项 P1、已派新工程师修复」，文件 docstring 亦写明「P1 缺陷回归」。**当前项目树内缺失该文件，意味着该 P1 的回归防线不在基线内。** |

### E.2 mtime 差异清单（逐项，含判定依据）

> 时间换算：400 人批次运行窗口 = **2026-09-21 15:57:09 → 15:57:50 UTC = 本机 CST 2026-09-21 23:57 → 2026-09-22 00:37 前后**（实际 `runs.finished_at` 对应 CST `23:57:50`）。

| # | 路径 | mtime（CST） | 判定 |
|---|---|---|---|
| 1 | `backend/app/`（目录） | 2026-09-20 23:40 | ✅ **在 400 人运行窗口之前**，窗口后无改动。`app/runs`、`app/worker` = 2026-09-21 00:15（更早）。**与 run 报告「未改动 survey-platform/ 下任何文件」的红线自检一致（正面结论）。** |
| 2 | `backend/migrations/`（目录） | 2026-09-20 23:40 | ✅ 同上，窗口后无改动。 |
| 3 | **`backend/tests/`（目录）** | **2026-09-22 00:18** | ⚠️ **差异**。目录内**所有文件** mtime ≤ 2026-09-21 00:22，均早于该时刻 ⇒ 目录项在 `00:18` 发生过**增删**。与 E.1 的 `test_reason_length_fix.py` 移出吻合。 |
| 4 | **`backend/.pytest_tmp/`（目录）** | **2026-09-22 00:19** | ⚠️ **差异**。目录内两个子目录为 2026-09-21 01:54，父目录在 `00:19` 变化 ⇒ 该时刻有 tmp 目录的增删。 |
| 5 | `backend/.pytest_cache/`（目录 + 内容） | 2026-09-21 01:54 | ⚠️ 内含指向**已不存在文件**的 273 条 nodeids 与 8 条 lastfailed（见 E.1）。属残留状态。 |
| 6 | `backend/scripts/capability_check.py` | 2026-09-21 20:33 | ✅ 属 §7.7 交付物（`docs/real-provider-capability.md` 的同批产出），非新增越权文件。 |
| 7 | `tests/load/fixtures/sample50_head10.csv` | 2026-09-21 20:52 | ✅ 早于 400 人运行窗口，属既有产物。 |
| 8 | `frontend/test-results/.last-run.json` | 2026-09-21 02:04 | ⚠️ **残留物**。`TEAM-BRIEF §7.11.5` 记录「临时物已用 `mv` 移出项目树」，但 `frontend/test-results` 仍在树内。 |
| 9 | 多处 `.DS_Store`：项目根（09-21 09:51）、`backend/`（09-21 09:51）、`deploy/`（09-20 23:39）、`frontend/`（09-21 09:51）、`tests/`（09-21 09:51） | — | ⚠️ macOS 噪声文件，非本团队有意产物；是否计入交付物由主理人裁定。 |
| 10 | `deploy/real.env` | 2026-09-21 20:31 | ✅ 已知唯一含真实密钥的文件（`TEAM-BRIEF §7.7`），早于运行窗口，`.gitignore` 的 `*.env` 规则覆盖。**未发现新增 `.env` 类文件。** |
| 11 | 项目根 / `deploy/` 下的临时脚本、探针、备份、日志 | **未发现** | ✅ `deploy/` 仅 `compose.yaml`、`compose.real.yaml`、`mock.env`、`real.env`、`real.env.example`、`initdb/01-create-test-db.sql`；项目根仅交付文档与目录。 |

### E.3 数据库侧：`survey` 开发库批次清单

`SELECT id, sample_size, status, created_at FROM runs ORDER BY created_at`（SQL §F-Q1）实测 **9 个批次**：

| # | run_id | size | status | created_at (UTC) | 归属 |
|---|---|---|---|---|---|
| 1 | `06b3aa4b-b0f3-4eb6-98ea-e41c14cf9389` | 10 | cancelled | 2026-09-21 12:39:17 | **归属待确认**（主理人已查的 8 个之外未知批次之一） |
| 2 | `739a83cf-da35-41d0-b0ef-0d6cf0bdffd1` | 10 | cancelled | 2026-09-21 12:39:39 | **归属待确认** |
| 3 | `615cef28-8d5a-4508-b56d-12bbaa3e4733` | 10 | cancelled | 2026-09-21 12:40:54 | **归属待确认** |
| 4 | `52b9b440-1baa-4548-be1f-9504598a2a67` | 10 | cancelled | 2026-09-21 12:44:50 | **归属待确认** |
| 5 | `f6c6a643-d2ac-4474-94e7-560b0f815b32` | 50 | completed | 2026-09-21 12:48:07 | **已知**：TEAM-BRIEF §7.8 的 50 人真实运行第 1 轮 |
| 6 | `fe3b7074-e3fe-465a-bb7a-2054b7d5fb51` | 50 | completed | 2026-09-21 12:51:55 | **归属待确认**（疑为 §7.8 R-2 重跑，未独立核实） |
| 7 | `b7b2156d-b5a2-4347-8862-e8939ee072b6` | 10 | completed | 2026-09-21 12:52:34 | **归属待确认** |
| 8 | `70a30c11-7ef6-4c3c-9383-ed17eb9f7e63` | 10 | cancelled | 2026-09-21 15:56:11 | **归属待确认**（紧邻 400 人批次前 58 s，疑为预检） |
| 9 | `1aae0492-a2b6-48a1-bf32-2b019dd324bd` | **400** | **cancelled** | 2026-09-21 15:57:09 | **本轮**（被审查对象） |

> 说明：主理人已查到「8 个 size 10/50 批次 + 本轮 400」，与我实测的 **8 + 1 = 9** 一致。**我不掌握这些批次的创建者信息，DB 中亦无 actor 字段，故一律标注「归属待确认」，不作断言。**

**其它库只读核对**（与主理人描述一致，无新增）：`survey_test` 空、`survey_test_qa` 空、`survey_test_t7` 有 4 个 completed（2000 / 10000 / 10000 / 20000，created_at 2026-09-20 17:29–17:43 UTC，**只读审计证据**）、`survey_test_t7qa` 有 2 个（200 cancelled / 200 completed，2026-09-20 18:02 UTC）。

### E.4 恢复干净仓库后**应执行**的基线命令（**本报告未执行任何一条**）

```bash
# 0) 环境自检
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform
docker exec survey-pg pg_isready -U postgres                       # → accepting connections
cd backend && uv run python -c "import sys; print(sys.version_info[:2])"   # → (3, 12)

# 1) 测试库独占确认（跑测试前必须，§7.6.1）
docker exec survey-pg psql -U postgres -tAc \
  "SELECT datname, count(*) FROM pg_stat_activity WHERE datname LIKE 'survey%' GROUP BY 1"

# 2) 后端基线（原样，不设任何环境变量）—— 期望 263 passed
cd backend && uv run pytest -q

# 3) 静态检查 —— 期望 All checks passed
cd backend && uv run ruff check app/ tests/

# 4) T7 可审计 run（只读！）
docker exec survey-pg psql -U postgres -d survey_test_t7 -tAc \
  "SELECT sample_size, status, count(*) FROM runs GROUP BY 1,2"

# 5) 前端 E2E（需 Chromium，本轮未执行）
cd frontend && npx playwright test
```

> ⚠️ 若决定把 `pending-reason-length-fix-test.py` 放回 `backend/tests/test_reason_length_fix.py`，基线将变为 **273 条**（263+10），其中 **8 条预期为红**（它们是 D-03 未修复前的 fail-first 回归用例）。**红的数量与集合必须先经主理人确认，再据此调整基线口径**，不得为了让基线变绿而丢弃或 `skip` 这些用例。

---

## 七类问题清单

### 类别 1：已证实平台缺陷（5 条）

| 编号 | 缺陷 | 证据来源 |
|---|---|---|
| **D-01** | **`attempts.finished_at` 记的是「调用前」时刻，不是完成时刻**。`worker/execute.py:156` 的 `moment` 在 `:175 await provider.answer()` **之前**取值，并被 `:305-316` / `:359-370` 一路传给 `finalize_*`（`repository.py:620` / `:691`）与 `converge_run`（`:890` / `:916` / `:949`）。DB 实测：本轮 401 条 attempt 中有 duration 的 397 条里 **397/397** 满足 `finished_at - started_at < duration_ms`（**0 反例**），平均跨度 70.9 ms vs 平均 duration 5247 ms。极端样例：run `b7b2156d` 的 `runs.finished_at = 12:52:34.739253`，而其最后一条 attempt 的真实结束 ≈ `12:52:49.099`（早约 **14.4 s**），且该 run 有 2 条 attempt 的 `started_at`（`12:52:36.934` / `12:52:37.232`）**晚于** `runs.finished_at`。 | SQL §F-Q3/Q4；`execute.py:156,175,316,370`；`repository.py:620,691,890,916,949` |
| **D-02** | **`INVALID_OUTPUT` 路径的 `attempts.duration_ms` 恒为 NULL**。`execute.py:212-222` 调 `_record_failure_outcome` 时**未传 `duration_ms`**（`:646` 默认 `None`）。DB 实测：本轮 3 条 `error_code='INVALID_OUTPUT'` 的 attempt `duration_ms` 全 NULL（但 `usage_json` 有值）。后果：DB 侧延迟分布**系统性丢失最慢的一批**（本轮丢 22083 / 21715 / 21573 ms 三条）。可精确核对：`2,150,616 − 2,083,161 = 67,455 ms = 22083+21715+21573+2084`（第 4 条为 D-03 的孤儿 attempt）。 | SQL §F-Q2/Q8；`execute.py:210-222`；`repository.py:646` |
| **D-03** | **`reason` > 100 字符 → pydantic `ValidationError`（不是 `InvalidOutputError`）穿透 → 被 `worker/main.py:361-364` 兜底 `except Exception` 吞掉 → 成员静默卡 `running`、attempt 无 error_code/usage/raw/duration。** 本轮命中 1 例（row 87，0.25%）。DB 侧与脚本侧三方闭合：attempt 全字段 NULL 而 `status='running'`；脚本 `seq=12` 记录该请求 **HTTP 200 / `finish_reason='stop'` / 2084 ms / `completion_tokens=332`** 已成功返回。代码：`validation.py:117` 构造 `ValidatedAnswer`（`reason` 有 `max_length=100`）。 **本项目独立复核确认 run 报告 §7 的这条成立（已立项 P1）。** | SQL §F-Q9；`validation.py:117`；`worker/main.py:359-364`；`run_400_cap100.json` seq=12 |
| **D-04** | **force-cancel 后遗留不可回收的孤儿 `running` attempt**。全库 `attempts.status='running' AND run_members.status<>'running'` **唯一 1 行**（本轮 row 87）。`abandon_expired`（`repository.py:499-508`）只扫成员 `status='running'` 且 `lease_expires_at IS NOT NULL`；该成员现为 `cancelled`、`lease_token=NULL`、`lease_expires_at=NULL` ⇒ **永远不会被回收**。且代码里**没有任何路径**能把 `running` 成员直接置 `cancelled`（`:883` / `:944` 只处理 `pending`/`retry_wait`）⇒ 该状态只能由服务层之外的手段产生。 | SQL §F-Q10；`repository.py:499-508, 883, 944` |
| **D-05** | **`finish_reason` 从不落库**。`provider.py:232` 取值写入 `ModelResponse.finish_reason`（`contracts.py:333`），但 `models.py` 的 `attempts` 无该列，`usage_json` 只有 `input_tokens`/`output_tokens`。后果：本轮「3 次 length」在平台内**无法自查**，主文档 §10 的「格式无效率 >2% 应停下修复」这类阈值判断只能依赖外部脚本。 | `provider.py:232`；`contracts.py:333`；`models.py` attempts 表结构；SQL §F-Q5 |

### 类别 2：原设计与实现偏差（2 条）

| 编号 | 偏差 | 证据 |
|---|---|---|
| **V-01** | **中止来源不可区分**：主文档 §6.2/§6.4 的取消语义隐含「可解释」，但 `runs` 无 `cancel_reason` 列、`cancel_run` 无 reason 入参、控制事件无 source 字段；而「暂停」侧却有 `pause_reason` 的四值域 ⇒ 同一类控制动作的可解释性**不对称**。本轮连 cancel 事件都没留下。详见 §C。 | `models.py` runs 表；`routes.py:552-562`；`service.py:499/523/547`；SQL §F-Q6/Q7 |
| **V-02** | **`INVALID_OUTPUT` 是否可重试的表达与常量集合不一致**：`RECOVERABLE_ERROR_CODES`（`execute.py:50-52`）不含 `INVALID_OUTPUT`，但无效输出路径（`:210-222`）调用 `_record_failure_outcome` 时**未传 `force_permanent`**（默认 `False`）⇒ 实际**会**重试。**当前行为正确**（符合主文档 §6.4「无效输出有限重试」），但判定逻辑分散在两处且语义相反。风险：将来若有人「顺手」补上 `force_permanent=True`，行为会静默改变且无测试能捕获。 | `execute.py:50-52, 210-222, 340-344` |

### 类别 3：报告/措辞错误（8 条）

> 均针对 `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/run-report-400-at-cap100.md`（下称「报告」）。

| 编号 | 位置 | 问题 | 应为 |
|---|---|---|---|
| **W-01** | §5 表格「`failed`（INVALID_OUTPUT，待重试） \| 2」 | **与状态机冲突**。`failed` 是终态（仅 `attempt_no >= attempt_limit` 才会写入，`execute.py:340-344`）；这 2 个成员 `attempt_count=1 < attempt_limit=3` ⇒ 只可能是 `retry_wait`。DB 实测 `next_attempt_at` 非空；脚本 `final.member_status_counts = {"running":1,"retry_wait":2,"succeeded":397}`、`summary_endpoint.failed_count = 0`。 | 「2 个 **`retry_wait`**（`INVALID_OUTPUT`，已排 `next_attempt_at`）→ 后被 force-cancel 置 **`cancelled`**」 |
| **W-02** | §5「卡 `running`（缺陷，见 §7）\| 1」 | 未说明真实性质，读者极易理解为「请求超时/网络未返回」。实测该请求 **HTTP 200、`finish_reason=stop`、2084 ms 已成功返回**，是 finalize 阶段被异常吞掉。 | 「1 个成员的模型调用**已成功返回**，因 §7 缺陷未被 finalize，悬挂在 `running`」 |
| **W-03** | §2.1「在途：峰值 / 采样均值 **100 / 58.77**」 | 未注明 58.77 是 **limiter 槽位占用**（含领取/限流/建请求/落库），不是 HTTP 在途。按请求区间重建的时间加权均值是 **52.198**。不注明会让读者误以为「槽位平均只用了 59%」。 | 分别标注两个口径并给出换算（槽位比 HTTP 每请求多占 **0.682 s**） |
| **W-04** | §2.2 的 Little 定律核算 | **系统边界错配**：`L=58.77`（槽位口径）配 `W=5.363`（HTTP 口径）⇒ λ 预测偏高 **12.6%**；其「+ 启动爬坡 ≈ 41 s」是在手工补偿这个错配。 | 同边界（都用 HTTP 口径）：`52.198/5.363 = 9.7328` vs 实测 `401/41.205 = 9.7318`，**误差 0.01%，无需任何补偿项** |
| **W-05** | §2.2「在途未稳在 100」及两个候选假设 | **伪命题**。实测 limiter 序列在 pending 存在区间（t∈[1.0, 19.575] s，**185 个采样点**）**恒为 100**（min=max=100，`<90` 占比 **0.0%**）。均值 58.77 完全由收尾排水段拉低。 | 删除该问题及其两个候选假设，或改写为「稳态 100% 饱和；均值低是批次收尾排水的固有形态」 |
| **W-06** | §4 与 §5 未对齐 | 3 个 `length`、1 次重试、2+1 个非成功成员之间**没有给出映射**（读者无法知道「多出来的第 401 次请求是谁」、也无法知道「3 个 length 与 2+1 是什么关系」）。 | 按本报告 §A.2 / §A.3 的表与映射补齐 |
| **W-07** | §2.1「延迟 mean **5363**」 | 未说明这是 **401 条**口径；DB `attempts` 只能算到 397 条（**5247 ms**），复核者按 DB 复核会对不上（差 116 ms），根因见 D-02。 | 标注「mean=5363 ms 为脚本侧 401 条口径；DB 侧因 D-02 缺失 4 条，均值为 5247 ms」 |
| **W-08** | `run_400_cap100.json` 的 `budget.request_limit_formula = '3 * sample_size'` | 与实测值不符：DB `request_limit = 480`，而 `3 × 400 = 1200`；报告 §1 记为 `ceil(1.2×400)=480`。属元数据描述错误（不影响本轮结论，401 ≤ 480 未触闸）。 | 修正为实际取值来源，或注明「已被显式覆盖」 |

### 类别 4：潜在风险（5 条）

| 编号 | 风险 | 依据 |
|---|---|---|
| **R-01** | **force-cancel（绕过服务层的直接 SQL）一旦成为常规运维手段，审计链与状态机将同时受损**：不写控制事件（C）、留下不可回收孤儿 attempt（D-04）、`running→cancelled` 这一转移在代码里不存在因而无测试覆盖。 | §C.2 + D-04 |
| **R-02** | **账不平**：`runs.requests_reserved = 401` 但 `runs.unknown_cost_count = 400`。差额 1 正是 D-03 那条从未结算的孤儿 attempt ⇒ **有 1 次成本预留既未结算、也未计入未知计数**。在真实计费配置下会表现为「预留未释放」。 | SQL §F-Q1；`repository.py:776-806` |
| **R-03** | **2 条 `raw_output` 为空字符串（`length=0`，非 NULL）** 的成因未定性：可能是「模型在思维链耗尽 `max_tokens` 时 `content` 本就为空」（模型行为），也可能是「我们未落盘」（存储缺陷）。二者性质完全不同；若为后者，则**截断证据丢失**，直接影响 D-03/D-05 类缺陷的可诊断性。`TEAM-BRIEF §7.9` 已把同现象列为「待查」，**至今未闭环**。 | SQL §F-Q8（row 16 / row 210 的 attempt 1） |
| **R-04** | **本轮 400 人批次的全部证据都在项目树之外**：`run_400_cap100.py` / `.json` / `run-report-400-at-cap100.md` / `run-plan-400-at-cap100.md` / `concurrency-100-feasibility.md` 位于 `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/`；`tests/load/results/` **无** 400 人结果文件；交付树内**零产物**。换会话/换机器后不可复核（重现 `KNOWN-ISSUES` B-05 的审计缺口教训）。 | `ls -la /Users/zhao/WorkBuddy/2026-09-20-20-00-42/`；`ls tests/load/results/` |
| **R-05** | **`finish_reason` 不入库（D-05）使「长度截断率」类阈值无法在平台内自查**，只能靠外部脚本。若未来真实批次规模化（1k/10k），缺少该字段意味着「是否需要上调 `max_tokens`」的裁决将失去平台内依据。 | D-05；主文档 §10 的 2% 阈值 |

### 类别 5：性能优化候选（**一律为「未验证优化候选」，均不得写成瓶颈结论**）（3 条）

| 编号 | 候选 | 本轮证据状态 |
|---|---|---|
| **P-01** | `claim_members(limit=1)` 领取路径串行化（对应 `KNOWN-ISSUES` C-01） | **无支持证据**：pending 存在期间槽位 100% 饱和（185/185 采样点 = 100）；稳态 λ≈**18.6 req/s** ≪ 领取路径实测上限 ≈**90 rows/s**（§7.17.3 已裁定事实，余量约 4.8×）⇒ 结构上不可能绑定。**维持「未验证优化候选」+「已裁定不改代码」。** |
| **P-02** | HTTP keep-alive / TLS 握手 / 连接复用（脚本观测 401 请求 / 400 条去重连接） | **只有现象，无因果、无量化**。且槽位已满负荷，说明即使存在额外握手开销也**未表现为槽位空闲**。**维持「未验证优化候选」。** |
| **P-03** | run 行锁（`_lock_run_counters ... with_for_update()`） | 同 P-01，λ≈18.6 ≪ 90 rows/s。§7.17.3 已裁定**不改代码**；本轮数据复核一致，不重复论证、不推翻。 |

### 类别 6：未验证事项（7 条）

| 编号 | 事项 |
|---|---|
| **U-01** | 「3 次 `length`」在 **DB 侧无法直接验证**（无 `finish_reason` 列）。其实测来源是脚本 JSON；我**未做网络回放**，无法与 provider 二次对账。DB 侧仅有间接证据（`output_tokens=4096` 恰等于上限 + 1 条被截断的 `raw_output`）。 |
| **U-02** | 2 条 `raw_output` 为空串的**成因未定性**（模型行为 vs 存储缺陷），见 R-03。 |
| **U-03** | row 87 触发 D-03 的具体 `reason` 文本**未落盘**（`raw_output=NULL`），无法从 DB 复现该缺陷的输入；只能以脚本侧 token 数（332 completion / 232 reasoning）旁证。 |
| **U-04** | `runs.finished_at = 15:57:50.637674` 的**确切产生路径未验证**。我只能证明它**不是**经 `cancel_run` 产生（无 cancel 控制事件），不能证明是哪条 SQL/哪个脚本产生。 |
| **U-05** | 「为什么 3 条 `INVALID_OUTPUT` 恰好是最慢的 3 条」属**因果相关**（`length` ⇒ 吃满 4096 ⇒ 耗时长），非独立验证；我没有构造对照实验。 |
| **U-06** | `distinct_http_connections = 400` 的**统计口径**（脚本如何定义「一条连接」）未验证。 |
| **U-07** | 本轮**未独立重跑**任何真实调用（不发起付费调用）。本报告全部结论均为对既有 DB 行 + 既有脚本 JSON 的复核，未产生新的实测数据。 |

### 类别 7：开发执行越权 / 仓库治理问题（7 条）

| 编号 | 事项 | 证据 |
|---|---|---|
| **G-01** | **`backend/tests/test_reason_length_fix.py` 当前不存在，但曾经存在并被 pytest 跑过**：`.pytest_cache/v/cache/nodeids` 含 **10 条**该文件用例（总数 273 = 263 基线 + 10，精确吻合）；`lastfailed` 含其中 **8 条失败**。文件现以 `pending-reason-length-fix-test.py` 存在于**项目树之外**（mtime `2026-09-22 00:18` CST，AST 解析出同样 10 个 `test_*` 函数，逐条同名吻合）。**它是 D-03 那个 P1 的回归测试，本应存在于基线内。** | §E.1 |
| **G-02** | **`backend/tests/` 目录 mtime = 2026-09-22 00:18**，而目录内所有文件 mtime ≤ 2026-09-21 00:22 ⇒ 目录项在该时刻发生过增删，与 G-01 的文件移出吻合。 | §E.2-3 |
| **G-03** | **`backend/.pytest_tmp/` mtime = 2026-09-22 00:19**（子目录为 2026-09-21 01:54）⇒ 该时刻有 tmp 目录增删。 | §E.2-4 |
| **G-04** | **移出行为是否经授权，我无法判定**（无 git、无变更记录）。客观后果有二：① 基线 `263 passed` 口径下**缺了 10 条 P1 回归用例**；② `.pytest_cache` 残留 8 条 `lastfailed` 指向**已不存在的文件**，若使用 `--lf` / `--ff` 会异常。**建议主理人明确该文件的归属、去向与是否回填。** | §E.1；§E.4 注 |
| **G-05** | **方法限制（必须写明）**：项目**不是 git 仓库**（无 `.git`），无法用 `git diff`/`git status` 判定未授权改动；本节结论全部基于 mtime + 文档记录对照，**不能证明「没有改动」，只能证明「某时刻之后 mtime 未变」**。 | §E.0 |
| **G-06** | **交付树内残留物**：`frontend/test-results/.last-run.json`（2026-09-21 02:04）、`backend/.pytest_tmp/`（2 个子目录）、`backend/.pytest_cache/`（含已失效 nodeids）、5 处 `.DS_Store`（root / backend / deploy / frontend / tests）。`TEAM-BRIEF §7.11.5` 记录「临时物已用 `mv` 移出项目树」，但 `frontend/test-results` 仍在树内。 | §E.2-5/8/9 |
| **G-07** | **正面结论（对照用）**：`backend/app/`（mtime 2026-09-20 23:40）、`backend/migrations/`（2026-09-20 23:40）、`app/runs` 与 `app/worker`（2026-09-21 00:15）**在 400 人运行窗口（CST 2026-09-21 23:57 起）之后无任何 mtime 变化**；`deploy/` 无新增 `.env` 类文件；项目根与 `deploy/` 无临时脚本/探针/备份/日志。与 run 报告「未改动 `survey-platform/` 下任何文件」的红线自检**一致**。 | §E.2-1/2/10/11 |

---

## 附录 F：本次执行过的全部只读命令（可复现）

> 全部为 **只读**。`docker exec ... psql -c "SELECT ..."` / 文件读取 / `docker logs` / `docker ps`。**未执行**任何写操作、任何 `pytest`、任何 `ruff`、任何 `rm`/`mv`。

### F.1 只读 SQL（`survey` 开发库，容器 `survey-pg`）

| # | 用途 | 命令 |
|---|---|---|
| Q1 | 表结构（确认无 `cancel_reason` 列、`pause_reason` 取值域、成员状态 CHECK） | `docker exec survey-pg psql -U postgres -d survey -c "\d runs"`、`\d run_members`、`\d attempts` |
| Q2 | 全库批次清单 / 本轮 run 行 | `... -c "SELECT id, sample_size, status, pause_reason, requests_reserved, request_limit, unknown_cost_count, created_at, started_at, finished_at FROM runs ORDER BY created_at;"` |
| Q3 | 本轮成员状态分布 | `... -c "SELECT status, count(*), min(attempt_count), max(attempt_count), count(next_attempt_at), count(lease_token), count(last_error_code) FROM run_members WHERE run_id='1aae...' GROUP BY status;"` |
| Q4 | 本轮 attempt 状态×错误码分布 | `... -c "SELECT a.status, a.error_code, count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id WHERE m.run_id='1aae...' GROUP BY 1,2;"` |
| Q5 | 非 succeeded 成员明细 | `... -c "SELECT row_no, persona_id, status, attempt_count, attempt_limit, next_attempt_at, last_error_code, lease_token, lease_expires_at, (answer_json IS NULL) FROM run_members WHERE run_id='1aae...' AND status<>'succeeded';"` |
| Q6 | `attempt_count>1` 的成员及其全部 attempt | `... -c "SELECT m.row_no, m.persona_id, m.status, m.attempt_count, a.attempt_no, a.status, a.error_code, a.started_at, a.finished_at, a.duration_ms, a.usage_json, length(a.raw_output), left(a.raw_output,120) FROM run_members m LEFT JOIN attempts a ON a.member_id=m.id WHERE m.run_id='1aae...' AND m.attempt_count>1;"` |
| Q7 | 非 succeeded attempt 全字段（含 `raw_output` 原文） | `... -c "SELECT m.row_no, m.persona_id, m.status, a.attempt_no, a.status, a.error_code, a.started_at, a.finished_at, a.duration_ms, a.usage_json, a.provider_request_id, (a.raw_output IS NULL), length(a.raw_output), a.raw_output FROM run_members m JOIN attempts a ON a.member_id=m.id WHERE m.run_id='1aae...' AND a.status<>'succeeded';"` |
| Q8 | 时间口径核对（span vs duration） | `... -c "SELECT a.attempt_no, a.status, count(*), avg(EXTRACT(EPOCH FROM (a.finished_at-a.started_at))*1000), avg(a.duration_ms) FROM attempts a JOIN run_members m ON m.id=a.member_id WHERE m.run_id='1aae...' GROUP BY 1,2;"`；以及 `count(*) FILTER (WHERE (finished_at-started_at) >= duration_ms)`（结果 **0 / 397**） |
| Q9 | `output_tokens` 分布（length 的间接证据） | `... -c "SELECT (a.usage_json->>'output_tokens')::int, count(*), a.status FROM attempts a JOIN run_members m ON m.id=a.member_id WHERE m.run_id='1aae...' GROUP BY 1,3 HAVING (a.usage_json->>'output_tokens')::int >= 3800;"`；以及 succeeded 的 min/avg/max/p50/p95 |
| Q10 | 本轮 `control_events_json`、`model_snapshot`、`survey_snapshot` | `... -c "SELECT jsonb_pretty(control_events_json) FROM runs WHERE id='1aae...';"` 等 |
| Q11 | **状态机反例扫描（10 项）** | 见 §B.2 表格，单条 psySQL 通过 `docker exec -i survey-pg psql ... <<'SQL'` 执行（10 个探针 UNION ALL） |
| Q12 | 孤儿 attempt 定位 | `... -c "SELECT m.run_id, m.row_no, m.persona_id, m.status, a.attempt_no, a.status, a.started_at, m.lease_expires_at, m.lease_token FROM attempts a JOIN run_members m ON m.id=a.member_id WHERE a.status='running' AND m.status<>'running';"` |
| Q13 | DB 侧在途重建（`generate_series` 0.1s / 0.5s / 1s 三档） | 见 §D.3：区间取 `[a.started_at, a.finished_at + duration_ms]`，结果 peak=98 / avg=52.13（全窗口），steady avg=92.97 / util=93.0% |
| Q14 | `duration_ms` 分位数 | `percentile_cont(0.5/0.9/0.95/0.99)`：p50=4126 / p95=11810 / p99=17111 / max=20987 / mean=5247.257（**仅 397 条**） |
| Q15 | 其它库只读核对 | `for db in survey_test survey_test_qa survey_test_t7 survey_test_t7qa; do docker exec survey-pg psql -U postgres -d $db -tAc "SELECT sample_size||' | '||status||' | '||created_at FROM runs ORDER BY created_at;"; done` |
| Q16 | 数据库清单 | `... -tAc "SELECT datname FROM pg_database WHERE datname LIKE 'survey%' ORDER BY 1;"` |

### F.2 只读命令（非 SQL）

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
docker logs --since 2026-09-21T15:56:30Z --until 2026-09-21T15:58:30Z survey-platform-worker-1   # → 空
docker logs --tail 25 survey-platform-worker-1
docker logs --tail 15 survey-platform-api-1
date; date -u
ls -la  <项目根> / docs / backend / backend/tests / backend/tests/__pycache__(不存在) / deploy / deploy/initdb / frontend / tests / tests/load / tests/load/results / tests/load/fixtures / investigation / investigation/run_05
ls -lad backend/. backend/app backend/app/runs backend/app/worker backend/migrations backend/tests
cat backend/.pytest_cache/v/cache/lastfailed
```

### F.3 只读脚本（对既有 JSON 做解析，无网络、无写库）

```bash
python3 - <<'PY'   # 解析 run_400_cap100.json（位于项目树之外，只读）
  # 结构遍历 / finish_reason_breakdown / error_code_breakdown / latency 分位
  # 3 条非 stop 请求明细 / persona ILB_1b0598a7c1df9bb3 请求明细
  # retry_distribution / counts / concurrency series 统计
  # 区间精确重建 peak(100) 与时间加权均值(52.198)
  # Little 定律同边界 vs 混合边界核算
PY
python3 -c "import ast; ..."   # AST 解析 pending-reason-length-fix-test.py 的 test_* 函数清单
```

### F.4 只读代码定位（行号引用）

`backend/app/inference/provider.py:232`（`finish_reason` 取值）、`backend/app/contracts.py:333`、`backend/app/models.py`（runs/attempts 列）、`backend/app/runs/repository.py:58, 294-454, 485-561, 567-705, 846-951`、`backend/app/runs/service.py:277-360, 378-560, 589-624`、`backend/app/worker/execute.py:50-63, 147-234, 292-377`、`backend/app/worker/main.py:68, 351-368`、`backend/app/inference/validation.py:95-120`、`backend/app/api/routes.py:552-562`、`backend/app/config.py:27-29`。

---

## 附录 G：需要主理人裁决/派单的事项（按优先级）

1. **D-03（P1，已立项）**：`validation.py` 把 pydantic `ValidationError` 转为 `InvalidOutputError`；并把 `pending-reason-length-fix-test.py` **回填**为 `backend/tests/test_reason_length_fix.py`（10 条用例，修复后应全绿，基线变 273）。
2. **D-01 / D-02（可观测性缺陷）**：`execute()` 的 `moment` 应在 provider 调用**之后**重新取值再落 `finished_at`；无效输出路径补传 `duration_ms`。二者都会改变现有「完成时间」语义，**须评估对既有断言的影响**。
3. **C（能力缺口）**：是否新增 `runs.cancel_reason` + 显式 `force_cancel_run(reason=...)`，使「中止来源」成为平台内一等公民（方案见 §C.4）。
4. **D-05**：是否持久化 `finish_reason`（建议加列 `attempts.finish_reason TEXT` 或写入 `usage_json`）。
5. **W-01…W-08**：修正 `run-report-400-at-cap100.md` 的措辞与 Little 定律推导；**尤其是 W-01（`failed` → `retry_wait`）与 W-05（删除「在途未稳在 100」这个伪命题及其两个候选假设）**。
6. **R-04**：把 400 人批次的脚本/JSON/报告纳入交付树（或至少在 `docs/` 留一份索引与复核 SQL）。
7. **G-01…G-04**：明确 `test_reason_length_fix.py` 的归属与去向，并清理 `.pytest_cache` 中指向已失效文件的 8 条 `lastfailed`。
