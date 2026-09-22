# 修复就绪附录（Fix-Readiness Appendix）

> **性质**：**只读**产物。本文件不修改任何代码、测试、迁移或既有文档。
> **配套文件**：`docs/qa-report-supplementary-audit.md`（下称「主报告」）。本附录为其 §附录 G 的展开。
> **读者**：下一个接手修复的 AI 团队。目标是**不必重新定位**——每条都给到 `文件:行号` 与关键代码片段。
> **编号**：沿用主报告的 `D-01…D-05`（已证实平台缺陷）与 `V-01…V-02`（原设计与实现偏差）。主理人口头点名的三条与本报告编号**一致**（D-01 时长口径 / D-02 duration_ms 丢失 / D-03 reason 超长静默卡死）。
> **纪律**：无全称表述；未确认项写「**未验证**」；涉及架构层或语义变更的条目一律标注「**须主理人裁决**」。
> **行号基准**：`backend/` 下的源码，为本次审查时的磁盘内容（未经修改）。

---

## 0. 一页速览

| 编号 | 一句话 | 主锚点 | 最小修法性质 | 是否架构层／须裁决 | 工作量 |
|---|---|---|---|---|---|
| **D-01** | `attempts.finished_at` 取的是**调用前**时刻 | `worker/execute.py:156` | 改取时点（一处 + 两个落库点） | 否（但改变用户可见的完成时间语义，**建议裁决**） | 小 |
| **D-02** | INVALID_OUTPUT 路径不传 `duration_ms` | `worker/execute.py:212-222` | 加一个实参（一行级） | 否 | 小 |
| **D-03** | `reason`>100 → `ValidationError` 逃逸被吞 → 成员静默卡 `running` | `inference/validation.py:117` | `try/except ValidationError` → `InvalidOutputError` | 否（修法已由主理人裁定 A-3） | 小（代码）＋ 中（回填 10 条用例） |
| **D-04** | force-cancel 后留下不可回收的孤儿 `running` attempt | `runs/repository.py:499-508` | 扩 `abandon_expired` 扫描集 + 孤儿结算（保守版） | **部分须裁决**（是否同时改取消语义） | 小–中 |
| **D-05** | `finish_reason` 从不落库 | `inference/provider.py:232` | 并入 `usage_json`（推荐，无迁移） | **否（推荐方案）／是（加列方案）** | 小 |
| **V-01** | 中止来源不可区分，且本轮连 cancel 事件都没有 | `runs/service.py:475-476` | 加列 + 加 `reason` 入参 + 显式 `force_cancel_run` | **是，须主理人裁决** | 中 |
| **V-02** | `INVALID_OUTPUT` 可重试的表达与常量集合不一致 | `worker/execute.py:50-52` vs `:210-222` | 纯可读性显式化（**零行为变更**） | 否 | 小 |

**建议落地顺序**：D-03（P1，最优先，已有现成用例）→ D-02 → D-05 → D-04（保守版）→ D-01（需裁决语义）→ V-02（顺手）→ V-01（需裁决）。

---

# 第一部分：已证实平台缺陷（D-01 … D-05）

## D-01　`attempts.finished_at` 记的是「调用前」时刻，不是完成时刻

### 症状
`attempts.finished_at` 与 `runs.finished_at` 系统性**早于**真实完成时刻；`finished_at - started_at` 恒小于 `duration_ms`（本轮 400 人批次：**397/397 条，零反例**；平均跨度 70.9 ms vs 平均 duration 5247 ms）。极端样例：run `b7b2156d`（10 人 `completed`）的 `runs.finished_at = 2026-09-21 12:52:34.739253`，而其最后一条 attempt 的真实结束 ≈ `12:52:49.099`（早约 **14.4 s**），且该 run 有 2 条 attempt 的 `started_at`（`12:52:36.934` / `12:52:37.232`)**晚于** `runs.finished_at`。

### 代码锚点

**① 取时点在 provider 调用之前** —— `backend/app/worker/execute.py:147-176`（`AttemptExecutor.execute`）

```python
154    ) -> ExecutionOutcome:
155        """执行一次尝试；返回结果并已（在独立事务内）落库。"""
156        moment = now or datetime.now(UTC)          # ← 调用「前」的时刻
...
175        response = await self._provider.answer(request)   # ← 真正的 HTTP 调用在这里
176        return await self._handle_response(run=run, claim=claim, response=response, now=moment)
```

**② 该时刻被一路传下去** —— `execute.py:302-316`（`_record_success`）与 `execute.py:357-370`（`_record_failure_outcome`）

```python
305                saved = await self._repo.finalize_success(
306                    session,
307                    claim=claim,
...
311                    duration_ms=response.duration_ms,
312                    provider_request_id=response.provider_request_id,
313                    raw_output=self._truncate(response.raw_text),
314                    now=now,                       # ← 仍是调用前的 moment
315                )
316                await self._repo.converge_run(session, run.id, now=now)
```

**③ 落库点** —— `backend/app/runs/repository.py`

```python
620                finished_at=moment,               # finalize_success
...
691                finished_at=moment,               # finalize_failure
...
726-730           finished_at=moment,               # mark_attempt_failed（AUTH 暂停路径）
...
890                        .values(status="cancelled", finished_at=moment)   # converge_run cancelling
916            .values(status=new_status, finished_at=moment)                # converge_run 终态
949            .values(status="cancelled", finished_at=moment)               # cancel_ready_run
```

**④ 对照（正确的一处，不要动）** —— `repository.py:312` + `:414`

```python
312        moment = now or utcnow()                 # claim_members
...
414                    "started_at": moment,        # 领取时刻 → 语义正确
```
> ⚠️ `started_at` **必须保持「领取时刻」**：`repository.py:986-1002` 的 `recent_attempt_tokens` 用 `Attempt.started_at >= since` 保守重建限流窗口（裁决 C3）。改它会改变限流窗口语义。

### 根因
`execute()` 只取一次时钟（`moment`），在 `await provider.answer()` **之前**；该值同时被用作「完成时刻」写入 `finished_at` 与 `runs.finished_at`，于是**完成时刻被钉死在调用开始之前**。

### 最小修法（不改 `started_at`，不改锁语义）
1. 在 `AttemptExecutor.execute`（`execute.py:147-176`）里把时钟拆成两个：
   - `started_moment`（= 现有 `moment`，保留用于 `next_attempt_at` 与退避基准，语义为「本次尝试的记账时刻」）；
   - 在 `await self._provider.answer(request)` **返回之后**取 `settled_moment = datetime.now(UTC)`，作为「完成时刻」。
2. 把 `settled_moment` 作为**新参数** `settled_at` 传给 `_handle_response`（`execute.py:182-188`）→ `_record_success`（`:292-316`）→ `_record_failure_outcome`（`:326-370`），最终只写进 `finished_at`。
3. **可注入性保护（关键，否则会打破既有确定性用例）**：当调用方显式注入 `now=` 时（测试路径），`settled_moment` 回退为 `now`，保持现有测试的确定性不变。
4. 不要回填历史数据：`duration_ms` 缺失的 attempt（本轮 4 条）无法还原真实完成时刻，回填会制造**新的假数据**。仅在主报告 / `KNOWN-ISSUES.md` 记账。

### 回归测试
新增 `backend/tests/test_attempt_timestamps.py`（新文件，不改动既有用例）：

| 用例名 | 证伪方式 |
|---|---|
| `test_attempt_finished_at_brackets_provider_call` | 用一个**真的会 sleep** 的 provider（`answer()` 内 `await asyncio.sleep(0.3)` 并返回 `duration_ms=300`）跑 1 个成员；断言 `attempt.finished_at - attempt.started_at >= 0.3 s`。**未修复时跨度≈0 ⇒ 必红**；修复后 ⇒ 必绿。 |
| `test_run_finished_at_not_earlier_than_last_attempt_end` | 同上 provider，跑 2 个成员；断言 `runs.finished_at >= max(attempts.finished_at)`。**未修复时 `runs.finished_at` 早于最后一条 attempt 的结束 ⇒ 必红**。 |
| `test_injected_now_still_deterministic`（守可注入性） | 显式注入 `now=固定值` 调 `execute()`；断言 `finished_at == 注入值`（即第 3 条回退生效），防止修复顺手破坏既有确定性用例。 |

> 注：`tests/fakes/fake_provider.py:51` 的 `duration_ms` 只是**返回值**、不真 sleep，故用例①必须自建 sleep provider，否则证伪不成立。

### 风险
- **用户可见语义变化**：`runs.finished_at` 经 `api/routes.py:194` / `api/schemas.py:159` 出参，修复后显示时间会**变晚（变正确）**。既有断言只查非空／空（`tests/test_run_controls.py:486 / 496 / 583`），**不受影响**。
- **不得动 `started_at`**：否则影响 `recent_attempt_tokens`（C3 限流窗口重建）与 `idx_attempts_started`。
- CAS 保存、租约、`uq_runs_single_active`、红线 #1/#2/#3、统计口径「五档之和==valid」**均不受影响**。
- 本轮「墙钟 40.97 s」是脚本侧 `serve_duration_s`，不依赖 DB `finished_at`，故已有结论**无需改数**；但「DB `finished_at - started_at = 41.199 s」与 40.97 s 的 0.23 s 差异根因在此，**修复后应在报告里统一口径**。

### 工作量
**小**（一处取时 + 两个落库点 + 1 个新测试文件）。**建议裁决**：因为它改变对外展示的完成时间语义。

---

## D-02　`INVALID_OUTPUT` 路径不传 `duration_ms` ⇒ 最慢的 3 条时长丢失

### 症状
本轮 3 条 `error_code='INVALID_OUTPUT'` 的 attempt，`duration_ms` **全为 NULL**（但 `usage_json` 有值）。后果：DB 侧延迟统计**系统性缺失最慢的一批**（本轮恰好丢掉 22083 / 21715 / 21573 ms 三条，即 3 个 `finish_reason=length`），DB 均值 5247.3 ms 低于真实 401 条均值 5363.1 ms。精确核对：`2,150,616 − 2,083,161 = 67,455 ms = 22083 + 21715 + 21573 + 2084`（第 4 条为 D-03 的孤儿 attempt）。

### 代码锚点

**① 无效输出分支漏传** —— `backend/app/worker/execute.py:206-222`

```python
206        try:
207            answer: ValidatedAnswer = parse_and_validate(
208                response.raw_text, allow_reason=self._allow_reason
209            )
210        except InvalidOutputError:
211            # 无效输出：有限重试，**绝不填默认答案**。
212            return await self._record_failure_outcome(
213                run=run,
214                claim=claim,
215                error_code=ERROR_INVALID_OUTPUT,
216                usage_json=usage_json,          # ← 传了
217                actual_cost=actual_cost,        # ← 传了
218                raw_output=self._truncate(response.raw_text),   # ← 传了
219                retry_after=None,
220                pause_reason=None,
221                now=now,
222            )                                    # ← 唯独没有 duration_ms
```

**② 成功分支是对照组（传了）** —— `execute.py:305-315`

```python
311                    duration_ms=response.duration_ms,
```

**③ 默认值吞掉了它** —— `backend/app/runs/repository.py:636-648`（`finalize_failure` 签名）

```python
646        duration_ms: int | None = None,
...
695                duration_ms=duration_ms,
```

**④ 同类待评估点** —— `repository.py:707-732`（`mark_attempt_failed`，AUTH 暂停路径）签名里**也没有** `duration_ms`（`execute.py:401` 调用处同样未传）。

### 根因
`_record_failure_outcome` 的 `duration_ms` 默认 `None`，而无效输出分支（唯一的「有响应但判无效」路径）没有显式传值 ⇒ 该列在 DB 上恒为 NULL。

### 最小修法
1. **一行级**：在 `execute.py:212-222` 的调用里补 `duration_ms=response.duration_ms`。
2. **同批可选**：给 `mark_attempt_failed`（`repository.py:707-732`）的签名加 `duration_ms: int | None = None` 并在 `:727-730` 写入，同时 `execute.py:385-405`（`_record_auth_pause`）调用处传入 —— 使 AUTH 暂停路径的审计同样完整。**这一条是否纳入本批由主理人定**（会改动服务方法签名）。

### 回归测试
新增（同放 `backend/tests/test_attempt_timestamps.py`）：

| 用例名 | 证伪方式 |
|---|---|
| `test_invalid_output_attempt_records_duration_ms` | 用 `tests/fakes/fake_provider.py` 注入「非法 JSON 原文 + `duration_ms=1234`」（该 Fake 支持 `duration_ms` 注入，见 `fakes/fake_provider.py:51`）；跑一次执行，断言 DB 该 attempt：`status='failed'`、`error_code='INVALID_OUTPUT'`、**`duration_ms = 1234`**。**未修复时 `duration_ms IS NULL` ⇒ 必红**。 |
| `test_failed_attempts_have_no_null_duration`（跨 run 不变量） | `SELECT count(*) FROM attempts WHERE status='failed' AND error_code='INVALID_OUTPUT' AND duration_ms IS NULL` == 0。**未修复时本轮会返回 3 ⇒ 必红**（需先回填/或直接以新 run 断言）。 |

### 风险
- 只多写一个**可空列**，不改状态机、不改 CAS、不改租约、不动 `uq_runs_single_active`；红线 #1/#2/#3 均不涉及。
- `duration_ms` 在 `app/` 内只被 `models.py:260`、`contracts.py:334`、`repository.py`、`execute.py` 使用，**`reports/` 不读它** ⇒ 统计口径「五档之和==valid」不受影响。
- 会改变「DB 侧延迟均值」的既有数值（5247 → 5363 ms 量级）。我**未发现**任何以该均值为阈值的断言，但**未逐条验证**，改动后须跑一次全量基线。

### 工作量
**小**（1 行；含第 2 条则小–中）。非架构层。

---

## D-03（P1，最优先）　`reason` 超 100 字符 → `ValidationError` 逃逸被吞 → 成员静默卡 `running`

### 症状
成员的模型调用**已成功返回**（本轮 row 87：HTTP 200、`finish_reason=stop`、`duration 2084 ms`、`completion_tokens=332`），却既未判成功、也未判 `INVALID_OUTPUT`，而是**永久留在 `running`**：attempt 的 `error_code` / `usage_json` / `raw_output` / `duration_ms` / `provider_request_id` **全为 NULL**，平台只留一行 `unexpected error executing member …`。本轮命中 1/400（0.25%）；按线性外推（**属外推，非实测**）1 万条约 25 条。

### 代码锚点

**① 异常源头** —— `backend/app/inference/validation.py:113-117`（`parse_and_validate`，:56 起）

```python
113    reason_raw = payload.get("reason")
114    if reason_raw is not None and not isinstance(reason_raw, str):
115        raise InvalidOutputError("model output field 'reason' must be a string")
116
117    return ValidatedAnswer(question_id=payload["question_id"], value=value, reason=reason_raw)
```

**② 约束来源** —— `backend/app/contracts.py:343-354`（`ValidatedAnswer`）

```python
350    model_config = ConfigDict(extra="forbid")
...
354    reason: str | None = Field(default=None, max_length=100)
```
> `reason` 超 100 ⇒ pydantic 抛 **`ValidationError`**，而 `parse_and_validate` 只抛 `InvalidOutputError`。

**③ 只捕一种异常** —— `backend/app/worker/execute.py:206-222`
```python
210        except InvalidOutputError:          # ← 捕不到 ValidationError
```

**④ 被兜底吞掉** —— `backend/app/worker/main.py:351-364`（`Worker._process`）

```python
359        try:
360            outcome = await self._executor.execute(run=run, claim=claim, survey=survey)
361        except Exception:
362            # 单个成员异常不得拖垮调度器；成员保持 running，租约到期后由恢复路径处理。
363            logger.exception("unexpected error executing member %s", claim.member_id)
364            return
```
> 注释里的「租约到期后由恢复路径处理」对本场景**不成立**：见下 ⑤。

**⑤ 恢复路径也救不了** —— `backend/app/runs/repository.py:499-508`（`abandon_expired`）

```python
501            RunMember.run_id == run_id,
502            RunMember.status == "running",          # ← 成员一旦被置 cancelled 就再也不匹配
503            RunMember.lease_expires_at.is_not(None),
504            RunMember.lease_expires_at < moment,
```
（本轮该成员已被外部 force-cancel 置 `cancelled` 且 `lease_token=NULL` / `lease_expires_at=NULL` ⇒ **永远不会被回收**。这是 D-04 的内容，两条须一并考虑。）

### 根因
`validation.py:117` 构造 `ValidatedAnswer` 时，超长 `reason` 触发 pydantic `ValidationError`（**不是**本项目的 `InvalidOutputError`）⇒ 不被 `execute.py:210` 捕获 ⇒ 穿透 `execute()` ⇒ 被 `worker/main.py:361` 的兜底 `except Exception` 吞掉 ⇒ 成员不结算、不重试、不带错误码，静默卡在 `running`。

### 最小修法（主理人已裁定 A-3，修法已定）
1. 在 `parse_and_validate`（`validation.py:56-117`）**返回前**把 `ValidatedAnswer(...)` 的构造包起来：
   `try: ... except ValidationError as exc: raise InvalidOutputError(f"model output failed schema validation: {exc}", raw_text=raw_text) from exc`（需 `from pydantic import ValidationError`）。
2. 这样它自然走 `execute.py:210-222` 的既有分支 ⇒ 记 `error_code='INVALID_OUTPUT'`、落 `raw_output`、`duration_ms`（**若 D-02 已修**）、`usage_json`，并进入 `retry_wait` 有限重试（`:340-344`：`attempt_no(1) < attempt_limit(3)` ⇒ `retry_wait` + `next_attempt_at`）。
3. **两条红线（不得违反）**：① **不得**截断 `reason` 让它「凑合通过」；② **不得**因 `reason` 不合法而给 `value` 补默认值 / 首选项 / 中点 / `unsure`（红线 #1）。
4. **另注（不得单边改）**：不得为迁就长 `reason` 而只放宽下发 schema 的 `maxLength`（`inference/prompt.py::build_output_schema(allow_reason=True)`）却不改校验 —— **二者必须一致**，否则本缺陷会以别的形式复现。**未验证** `prompt.py` 中 `maxLength` 的当前取值是否与 100 一致，修复前须核对。
5. **可选加固（建议裁决，非必需）**：`worker/main.py:361-364` 的兜底 `except Exception` **应保留**（防调度器减员），但可在吞掉前调用一次 `repo.mark_attempt_failed(attempt_id, error_code="INTERNAL_ERROR")`，使「静默」变「可见」。`attempts.error_code` 无 CHECK 约束（DB 实测），写入新取值不会撞约束；但 `INTERNAL_ERROR` 是否纳入既有错误码集合（影响报表 `error_summary`）**须主理人确认**。

### 回归测试
**应把项目树外的 `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/pending-reason-length-fix-test.py` 回填为 `backend/tests/test_reason_length_fix.py`**（10 个 `test_*` 函数，AST 解析确认；与 `backend/.pytest_cache/v/cache/nodeids` 里该文件的 10 条记录逐条同名）。其中 8 条在修复前为红（缓存 `lastfailed` 实证），2 条应恒绿。

| 用例名 | 证伪方式（未修复 ⇒ 红；修复后 ⇒ 绿） |
|---|---|
| `test_repro_pydantic_validation_error_must_not_escape` | 直接调 `parse_and_validate(合法JSON但 reason=101字符)`，断言**抛 `InvalidOutputError`**。未修复时抛 pydantic `ValidationError` ⇒ **必红**。 |
| `test_reason_over_limit_raises_invalid_output` | 同上，断言异常类型与 `raw_text` 被保留。 |
| `test_reason_boundary_100_ok_101_rejected` | 边界：100 字符通过、101 字符 `InvalidOutputError`。 |
| `test_long_reason_member_enters_retry_wait_not_stuck_running` | worker 级：跑 1 个成员，断言成员最终状态 **`retry_wait`**（未修复时恒 `running` ⇒ **必红**）。 |
| `test_long_reason_member_is_never_left_without_error_code` | 断言该 attempt 的 `error_code = 'INVALID_OUTPUT'` 且非空（未修复时 NULL ⇒ **必红**）。 |
| `test_long_reason_member_eventually_failed_with_invalid_output` | 额度耗尽后断言 `failed`（`attempt_no >= attempt_limit`）。 |
| `test_member_recovers_after_long_reason_then_valid` | 长 reason 失败 → 重试返回短 reason ⇒ `succeeded` 且答案为真实五档。 |
| `test_no_fabricated_answer_when_reason_too_long` | 断言 `answer_json IS NULL`（**红线 #1 的正面守卫**）。 |
| `test_long_reason_with_illegal_value_still_invalid_output` | reason 超长 + value 非法 ⇒ 仍 `INVALID_OUTPUT`（不得因异常类型改变而漏判）。 |
| `test_short_reason_behaviour_unchanged` | 短 reason 行为不变（**刻画既有契约，修复前后均应绿**）。 |

> ⚠️ 按 §7.12.2 流程纠正纪律：**不得**把「已知缺陷的当前行为」写成通过的用例来固化。`test_short_reason_behaviour_unchanged` 属**刻画性用例**，须在 docstring 显式标注「刻画既有契约，不得作为唯一防线」。
> ⚠️ 回填后基线口径由 **263 变为 273**（263 + 10）。修复前 **8 条预期为红**，不得为了让基线变绿而 `skip`/`xfail`/删除。

### 风险
- **红线 #1**：修法必须是「抛 `InvalidOutputError`」，任何「截断 / 补默认值」的写法都直接违规 —— `test_no_fabricated_answer_when_reason_too_long` 会抓住。
- 统计口径：该成员由「静默卡住」变为「进 `retry_wait`」，**不计入 valid** ⇒ 「五档之和 == valid」仍成立（分母变小，不是变大）。
- 计费：该 attempt 从此会正常结算 ⇒ `unknown_cost_count` 与 `requests_reserved` 由本轮的 **400 vs 401** 变为相等（正是 R-02 的账不平被顺带修掉）。
- CAS / 租约 / `uq_runs_single_active` 均不受影响。
- `ValidationError` 也可能由**其它** `ValidatedAnswer` 约束触发（`extra="forbid"`、`question_id` 校验器 `contracts.py:356-363`）。因此修法应包住**整个 `ValidatedAnswer(...)` 构造**，而不是只处理 `reason`。=> 本条会**顺带**把「未知字段 / 错误 question_id」由「逃逸被吞」变为「正常 `INVALID_OUTPUT`」，这是**改善**，但须确认不与 `test_inference.py::test_reason_field_gating`（`tests/test_inference.py:169`）等既有断言冲突（**未逐条验证**）。

### 工作量
**小**（`validation.py` 一处 try/except）＋ **中**（回填 10 条 worker 级用例并跑通）。**非架构层**。

---

## D-04　force-cancel 后留下不可回收的孤儿 `running` attempt

### 症状
全 `survey` 库 `attempts.status='running' AND run_members.status <> 'running'` **唯一 1 行**：本轮 row_no 87（`ILB_1b0598a7c1df9bb3`）。代码里**没有任何路径**能把 `running` 成员直接置 `cancelled`（只有 `repository.py:883` / `:944` 处理 `pending` / `retry_wait`）⇒ 该状态只能由**服务层之外的手段**产生（本轮最终态也确实没有 cancel 控制事件，见 V-01）。产生后 attempt 永久停留在 `running`。

### 代码锚点

**① 回收扫描集不含「成员已终态」的行** —— `backend/app/runs/repository.py:499-508`

```python
499        stmt = (
500            select(RunMember)
501            .where(
502                RunMember.run_id == run_id,
503                RunMember.status == "running",              # ← 只扫成员 still running
504                RunMember.lease_expires_at.is_not(None),
505                RunMember.lease_expires_at < moment,
506            )
507            .with_for_update(skip_locked=True)
508        )
```

**② 取消路径只转 pending/retry_wait** —— `repository.py:874-894`（`converge_run` 的 `cancelling` 分支）与 `:938-950`（`cancel_ready_run`）

```python
879                    update(RunMember)
880                    .where(
881                        RunMember.run_id == run_id,
882                        RunMember.status.in_(("pending", "retry_wait")),   # ← running 不在内
883                    )
884                    .values(status="cancelled", lease_token=None, lease_expires_at=None)
```

**③ 既有语义（改动前须知）** —— `backend/app/runs/service.py:488`
```
- 在途请求允许完成并保存；**已成功答案保留**。
```

### 根因
`abandon_expired` 的扫描谓词要求成员仍为 `running`；一旦成员被（任何方式）置为终态而 attempt 未结算，该 attempt 就**永远落不进任何回收路径**。

### 最小修法（保守版，推荐；不改既有取消语义）
1. **扩 `abandon_expired`（`repository.py:485-561`）的扫描集**：除现有「成员 `running` 且租约过期」外，增加一类「**孤儿**」：成员状态 ∈ `('cancelled','succeeded','failed')` **且** `lease_token IS NULL`，而存在 `attempts.status='running'` 且 `attempts.lease_token = 该成员最后一次租约`的行。
   - 处理动作复用现有逻辑：`attempt → abandoned`（`finished_at=moment`）、**保留 `reserved_cost` 不按 0 释放**、`unknown_cost_count += 1`（`repository.py:523-533`）。
   - **必须加严格条件**（`lease_token IS NULL` + attempt 仍 `running`），否则会把「成员终态但 attempt 尚在收敛窗口内」的正常 attempt 误判为孤儿。
2. **可选（同批）**：新增一个显式入口（如 `RunRepository.settle_orphan_attempts(session, run_id)`），供运维/脚本在 force-cancel 后调用，使「直接 SQL 置位」不再是唯一手段 —— 这条同时是 V-01 的前置。
3. **不建议在本批做的激进版**：让 `converge_run` 的 `cancelling` 分支把 `running` 成员也一并置 `cancelled` 并作废其 attempt —— 这会**推翻** `service.py:488` 的既有约定「在途请求允许完成并保存」。**若确需，须主理人裁决。**

### 回归测试
新增 `backend/tests/test_orphan_attempt_recovery.py`：

| 用例名 | 证伪方式 |
|---|---|
| `test_abandon_expired_settles_orphan_running_attempt` | 构造：成员置 `cancelled`、`lease_token=NULL`、`lease_expires_at=NULL`，其 attempt 留 `running`；调 `abandon_expired`；断言 attempt 变 **`abandoned`** 且 `runs.unknown_cost_count` **+1**。**未修复时该行不在扫描集内 ⇒ attempt 仍 `running` ⇒ 必红**。 |
| `test_orphan_settlement_keeps_reserved_cost_not_zero` | 断言孤儿 attempt 的 `reserved_cost` **未被清零**（未知计费不得按 0 释放）。 |
| `test_no_orphan_running_attempts`（跨 run 不变量） | `SELECT count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id WHERE a.status='running' AND m.status<>'running'` == 0。**当前为 1 ⇒ 必红**（须先清掉/回填本轮那一行，或对新 run 断言）。 |
| `test_normal_running_attempt_not_mistaken_as_orphan` | 成员 `running` 且租约未过期、attempt `running` ⇒ **不得**被孤儿逻辑触碰（防误伤）。 |

### 风险
- 扫描集扩大 ⇒ 有**误伤**风险：必须严格限定为「成员已终态 + `lease_token IS NULL`」，并在回收时对 attempt 加 CAS 条件（`attempt.status='running'`）。
- 会计口径：`unknown_cost_count` 会 +1（本轮 400 → 401，与 `requests_reserved` 对齐）；`reserved_cost` 保留不释放 —— 与「未知计费 ≠ 0」的既定口径一致。
- **不影响** `uq_runs_single_active`（只依赖 `runs.status`）、CAS 保存、红线 #1/#2/#3、统计口径「五档之和==valid」（孤儿 attempt 无答案，不产生答案桶）。
- 数据侧：本轮那一行**是否回填由主理人决定**（回填 = 写库；本附录不执行）。

### 工作量
**小–中**（改一个谓词 + 1 个新测试文件）。保守版**非架构层**；激进版**须裁决**。

---

## D-05　`finish_reason` 从不落库

### 症状
本轮「3 次 `finish_reason=length`」在 **DB 侧无法直接复核**。`attempts` 表无该列，`usage_json` 只有 `input_tokens` / `output_tokens`；只能靠间接证据推断：全轮 `output_tokens ≥ 3800` 的恰好 3 条、全 = 4096（= `model_snapshot.max_output_tokens`）、全为 `INVALID_OUTPUT` 失败 attempt，succeeded 最大仅 3693；其中 1 条 `raw_output` 是 81 字符被截断的 JSON。

### 代码锚点

**① 取到了但没往下传** —— `backend/app/inference/provider.py:227-234`

```python
227        return ModelResponse(
228            raw_text=str(raw_text)[:MAX_RAW_OUTPUT_CHARS],
229            input_tokens=usage.get("prompt_tokens"),
230            output_tokens=usage.get("completion_tokens"),
231            provider_request_id=data.get("id"),
232            finish_reason=first_choice.get("finish_reason"),     # ← 取到了
233            duration_ms=duration_ms,
234        )
```

**② 契约里有这个字段** —— `backend/app/contracts.py:333`
```python
333    finish_reason: str | None = None
```

**③ 落库时丢弃** —— `backend/app/worker/execute.py:427-442`（`_usage_and_cost`）

```python
432        usage_json: dict[str, Any] = {
433            "input_tokens": response.input_tokens,
434            "output_tokens": response.output_tokens,
435        }                                        # ← finish_reason 不在此
```

**④ 表结构确认无此列** —— `backend/app/models.py:243-260`（`Attempt`：`provider_request_id` / `raw_output` / `error_code` / `usage_json` / `reserved_cost` / `actual_cost` / `duration_ms`，**无 `finish_reason`**）。

### 根因
`provider.py` 把 `finish_reason` 放进 `ModelResponse`，但 `execute.py::_usage_and_cost` 组装 `usage_json` 时未包含它，而 `attempts` 也没有对应列 ⇒ 该信息在进程内被丢弃。

### 最小修法（两个方案，**需择一，方案 A 无迁移**）
- **方案 A（推荐，无迁移）**：在 `execute.py:432-435` 的 `usage_json` 字典里加一个键 `finish_reason: response.finish_reason`（`None` 时不写键，保持「空 ≠ 0 / 空 ≠ 缺」的可辨识性 —— 或按项目口径显式写 `None`，由主理人定）。
  - 安全性已核对：`usage_json` 的唯一读取方是 `repository.py:986-1002` 的 `recent_attempt_tokens`，它**只读 `input_tokens` / `output_tokens` 两个键**（`:996-997`），加键**不影响**限流窗口重建（裁决 C3）。
- **方案 B（需迁移）**：新增列 `attempts.finish_reason TEXT NULL` + 迁移 `0003`，并在 `finalize_success` / `finalize_failure` 写入。查询/聚合更方便，但改动面更大。

### 回归测试
新增 `backend/tests/test_finish_reason_persisted.py`：

| 用例名 | 证伪方式 |
|---|---|
| `test_finish_reason_is_persisted_on_success` | 用 `tests/fakes/fake_provider.py`（其构造参数已支持 `finish_reason` 注入，见 `fakes/fake_provider.py:50,110`）注入 `finish_reason='stop'`；断言 DB `attempts.usage_json->>'finish_reason' = 'stop'`（方案 A）。**未修复时 NULL ⇒ 必红**。 |
| `test_finish_reason_length_is_persisted_and_invalid` | 注入 `finish_reason='length'` + 非法原文；断言 `usage_json->>'finish_reason'='length'` **且** `error_code='INVALID_OUTPUT'` **且** 成员 `retry_wait`。这条直接把本轮「3 个 length」的场景固化进平台内。 |
| `test_usage_json_extra_key_does_not_break_rate_limit_rebuild` | 调 `recent_attempt_tokens`，断言其返回值只由 `input_tokens + output_tokens` 决定，加键不产生影响（防回归 C3）。 |

### 风险
- `usage_json` 为 `JSONB(none_as_null=True)`（`models.py:253-255`），加键**不改变**空值语义。
- 红线 #3（密钥/敏感信息不进日志、快照、CSV）：`finish_reason` 是供应商返回的枚举值，非敏感；但**未验证** `reports/` 或 CSV 导出是否触及 `usage_json`（我 grep 确认 `app/reports/` **不读** `usage_json`，但导出链路未逐条验证）。
- 统计口径「五档之和==valid」不受影响。CAS / 租约 / `uq_runs_single_active` 不受影响。
- 仅影响**新增**数据；历史 attempt（含本轮 401 条）无法回填真实 `finish_reason`（信息已丢失），回填会制造假数据 ⇒ **不回填**，仅在 `KNOWN-ISSUES.md` 记账。

### 工作量
**小**（方案 A：1 处字典 + 1 个测试文件）。**方案 A 非架构层**；**方案 B 需迁移 ⇒ 须主理人裁决**。

---

# 第二部分：原设计与实现偏差（V-01 … V-02）

## V-01　中止来源不可区分（平台内一律只显示 `cancelled`；本轮连 cancel 事件都没有）

### 症状
`用户主动取消` / `质量门禁中止` / `人工运维中止` / `系统异常中止` 四种来源在平台内**无法区分**。本轮 400 人批次终态 `cancelled`，但其 `control_events_json` **只有 `create` 与 `start` 两条，没有 cancel 事件** ⇒ 平台内**零痕迹**。「暂停」侧却有四值 `pause_reason`，可解释性**不对称**。

### 代码锚点

**① 无中止原因列** —— `backend/app/models.py`（`RunRecord` 字段清单；DB `\d runs` 实测无 `cancel_reason` / `abort_reason`）。相关列：`status`（:150-152 CHECK）、`pause_reason`（:153-157 CHECK，四值）、`control_events_json`（:133-134）。

**② 取消服务方法不接受原因** —— `backend/app/runs/service.py:475-476`

```python
475    async def cancel_run(
476        self, session: AsyncSession, run_id: UUID, *, now: datetime | None = None
477    ) -> ControlOutcome:
```

**③ 三个分支都会写 cancel 事件（所以「没有事件」= 不是经此路径）** —— `service.py:497-507`（`ready`）、`:521-531`（`running/pausing/paused`）、`:546-…`（幂等重放）

```python
499            run.control_events_json = list(run.control_events_json) + [
500                _control_event(
501                    "cancel",
502                    idempotency_key=run.idempotency_key,
503                    request_hash=run.request_hash,
504                    result="cancelled",
505                    now=moment,
506                )
507            ]
```

**④ 现成的扩展点** —— `backend/app/runs/service.py:162-181`（`_control_event`）

```python
169    extra: Mapping[str, Any] | None = None,
...
172    event: dict[str, Any] = {
173        "action": action, "idempotency_key": ..., "request_hash": ..., "result": result, "at": ...
179    if extra:
180        event.update(extra)      # ← 新增字段走这里即可，无需改结构
```

**⑤ 端点无 reason 入参** —— `backend/app/api/routes.py:552-562`

```python
552 @api_router.post("/runs/{run_id}/cancel", status_code=202, response_model=RunViewOut)
560     outcome = await run_service.cancel_run(session, run_id)
```

**⑥ 唯一的取消端点** —— 全项目仅此一个，**无 force-cancel 变体**（`Grep "cancel"` 实测）。

### 根因
设计上「取消」没有来源维度（无列、无入参、事件无 source），而运维/看门狗需要一条**不经 `cancelling` 立即收敛**的通道 ⇒ 实际以服务层之外的手段完成 ⇒ 既无列值也无事件，审计链断裂。

### 最小修法（**须主理人裁决**：新增列 + 迁移 + 服务层签名变更）
1. **加列**：`runs.cancel_reason TEXT NULL`，CHECK 取值域建议 `('user_cancel','quality_gate','ops_force','system_error','unknown')`；迁移 `0003`（现有版本：`migrations/versions/0001_initial.py`、`0002_normalize_jsonb_nulls.py`）。
2. **服务层**：`cancel_run(..., reason: str = "user_cancel")`，写列 **并**在 `_control_event(..., extra={"cancel_reason": reason})` 中冗余记录（列便于查询聚合，事件便于追溯，两者同事务写入 —— 与现有 `retry-failed` 既写事件又改 `attempt_limit` 的模式一致，不引入新范式）。
3. **新增显式通道** `RunService.force_cancel_run(session, run_id, reason)`（默认 `ops_force`），走**同一事务与同一审计路径**，从根上消除「直接 SQL 置位」的需求；看门狗类中止（本轮的 `finish_reason_length`）用 `quality_gate`。
4. **历史数据**：本库现有 6 个 `cancelled` run（含本轮）回填 `cancel_reason='unknown'`，并在 `KNOWN-ISSUES.md` 注明「历史 cancelled 批次的中止来源不可考」。
5. 与 D-04 的关系：先有显式 force-cancel 通道，孤儿 attempt 问题才有结构性解（否则仍会被人绕过服务层）。

### 回归测试
在 `backend/tests/test_run_controls.py` 中新增（**不改动**既有断言）：

| 用例名 | 证伪方式 |
|---|---|
| `test_cancel_writes_reason_and_control_event` | `cancel_run(reason='user_cancel')` 后，用 DB 原生 SQL 断言 `runs.cancel_reason='user_cancel'` **且** `control_events_json` 末条 `action='cancel'` 且含 `cancel_reason`。**未修复时列不存在 ⇒ 必红**。 |
| `test_force_cancel_records_ops_force` | 调 `force_cancel_run(reason='ops_force')`；断言列值与事件 `extra.cancel_reason` 均为 `ops_force`，且**终态成员也走同一事务**（无孤儿）。 |
| `test_every_cancelled_run_has_cancel_event`（跨 run 不变量） | `SELECT count(*) FROM runs WHERE status='cancelled' AND NOT control_events_json @> '[{"action":"cancel"}]'` == 0。**当前返回 1（本轮 400 人批次）⇒ 必红**；须先回填历史或对该 run 标注例外。 |

### 风险
- **对 `uq_runs_single_active` 无影响**（已核对）：该索引为 `UNIQUE, btree((true)) WHERE status IN ('running','pausing','paused','cancelling')`（`models.py:161-168`），只依赖 `status` 列；新增可空 TEXT 列**不改变其 WHERE 谓词、不改变行锁语义**，且该列非唯一 ⇒ 不引入新的冲突面。
- 不影响 §7.9.1 的结构性修复（`ready` 一次性收敛、不写 `cancelling`）。
- 不影响 `_control_event` 的既有 5 个键（新字段走 `extra`，向后兼容）。
- 幂等重放逻辑（`service.py:591-596`，只匹配 `action='retry-failed'`）不受影响。
- `routes.py` 若新增可选 query/body 参数，需确认不影响既有 202/200/409 语义。

### 工作量
**中**（列 + 迁移 + 服务层 + 端点 + 测试）。**属架构/契约层改动 ⇒ 须主理人裁决。**

---

## V-02　`INVALID_OUTPUT` 可重试的表达与常量集合不一致（行为正确，表述相反）

### 症状
`RECOVERABLE_ERROR_CODES` **不含** `INVALID_OUTPUT`，但无效输出路径**实际会重试**；两者语义相反。当前行为**正确**（符合主文档 §6.4「无效输出有限重试」），但判定逻辑分散在两处，且将来「顺手改一下」会**静默改变行为**。

### 代码锚点

**① 常量集合** —— `backend/app/worker/execute.py:49-52`

```python
49 #: 可重试的 provider 错误码（有限重试）。
50 RECOVERABLE_ERROR_CODES: frozenset[str] = frozenset(
51     {ERROR_TIMEOUT, ERROR_RATE_LIMITED, ERROR_SERVER_ERROR}
52 )
```

**② 无效输出路径绕过它** —— `execute.py:210-222`（调 `_record_failure_outcome` 时**未传** `force_permanent`，默认 `False`）

**③ 真正的判定位** —— `execute.py:340-344`

```python
340        retry_allowed = (not force_permanent) and claim.attempt_no < claim.attempt_limit
341        next_attempt_at: datetime | None = None
342        status = "failed"
343        if retry_allowed:
344            status = "retry_wait"
```

### 根因
「无效输出可重试」这一契约只以「**不传** `force_permanent`」的隐式方式表达，与显式常量集合 `RECOVERABLE_ERROR_CODES` 的字面语义相反。

### 最小修法（**零行为变更**）
把隐式契约显式化：在 `execute.py:212-222` 的调用里**显式传** `force_permanent=False` 并加一行说明注释；或新增一个明确命名的常量（如 `RETRYABLE_OUTPUT_ERRORS = frozenset({ERROR_INVALID_OUTPUT})`）并在 `retry_allowed` 判定时一并参考，使 `_handle_provider_error` 与无效输出两条路径的语义来源统一。**不得**改变 `retry_allowed` 的实际取值。

### 回归测试
新增（可并入 `backend/tests/test_inference.py` 或 D-03 的 `test_reason_length_fix.py`）：

| 用例名 | 性质 |
|---|---|
| `test_invalid_output_is_retryable_up_to_attempt_limit` | 注入**恒非法输出**跑满额度；断言 attempt 1、2 后成员为 **`retry_wait`**，attempt 3 后为 **`failed`**（`attempt_no >= attempt_limit`）。**修复前后都应绿**（刻画既有契约，不作唯一防线）；其价值在于：若有人误改成 `force_permanent=True`，该用例**立即变红**。 |
| `test_retryable_constants_match_documented_behaviour` | 断言 `RECOVERABLE_ERROR_CODES` 与无效输出路径的重试行为在文档/注释层一致（轻量静态守卫）。 |

> ⚠️ 按 §7.12.2：刻画性用例必须在 docstring 标注「刻画既有契约，缺陷修复后须复核」，且**不得**让它成为验收命令的唯一防线。

### 风险
- 若实现**零行为变更**，则不影响任何不变量。
- **唯一风险是改错方向**：一旦 `force_permanent=True`，本轮 3 个 `length` 会由「1 重试成功 + 2 待重试」变为「3 个直接 `failed`」⇒ 有效率从 397/400 掉到 394/400，且「五档之和==valid」的分母变化。上面第一条用例能抓住。
- CAS / 租约 / `uq_runs_single_active` / 红线 #1 均不涉及。

### 工作量
**小**（可读性改动 + 1 条刻画性用例）。**非架构层**。

---

# 第三部分：未来项（**不实施**，仅给锚点）

> 本节只做**落点设计**，供主理人日后裁决。不在本附录范围内写任何代码。

## F-01　`cancel_reason` / `abort_reason` 与显式 `force_cancel_run(reason=...)`

| 项 | 内容 |
|---|---|
| **建议落点（表/列）** | `runs.cancel_reason TEXT NULL` + CHECK `cancel_reason IS NULL OR cancel_reason IN ('user_cancel','quality_gate','ops_force','system_error','unknown')`。与既有 `ck_runs_pause_reason`（`models.py:153-157`）**同构**，迁移放 `migrations/versions/0003_*.py`（`down_revision = "0002_normalize_jsonb_nulls"`）。 |
| **建议落点（代码）** | ① `app/models.py` `RunRecord` 加字段 + `__table_args__` 加 `CheckConstraint`；② `app/runs/service.py:475-476` `cancel_run` 加 `reason: str = "user_cancel"`；③ 新增 `RunService.force_cancel_run(session, run_id, reason)`（默认 `ops_force`）；④ `app/api/routes.py:552-562` 端点可选接收 reason，或另开 `POST /runs/{id}/force-cancel`（**是否新开端点需主理人定**）。 |
| **与 `control_events_json` 的关系** | 走 `_control_event(..., extra={"cancel_reason": reason})`（`service.py:162-181`，扩展点已存在，`:169/:179-180`）。**列 = 可查询/可聚合的冗余投影，事件 = 可追溯的时间线，两者同事务写入**。这与现有 `retry-failed`（既写事件又改 `attempt_limit`，`service.py:589-624`）同一范式，不引入新概念。事件既有 5 个键（`action`/`idempotency_key`/`request_hash`/`result`/`at`）**不变**，向后兼容。 |
| **对约束无影响的说明** | `uq_runs_single_active` 为 `UNIQUE((true)) WHERE status IN ('running','pausing','paused','cancelling')`（`models.py:161-168`），**只依赖 `status`**。新增可空 TEXT 列：① 不改变其 `WHERE` 谓词；② 不改变行锁/事务语义；③ 该列非唯一 ⇒ 不引入新冲突面。§7.9.1 的「`ready` 一次性收敛、不写 `cancelling`」不受影响。 |
| **与 D-04 的顺序依赖** | **先**有显式 force-cancel 通道，**后**才可能结构性消灭孤儿 attempt；否则仍会被绕过服务层的直接 SQL 制造出来。 |
| **未验证** | 是否需要 `abort_reason` 与 `cancel_reason` **两列**（前者用于「非取消类的异常中止」）——本轮数据不足以判断，暂未设计。 |

## F-02　`finish_reason` 持久化

| 项 | 内容 |
|---|---|
| **建议落点（推荐，无迁移）** | `app/worker/execute.py:432-435` 的 `usage_json` 字典增加 `finish_reason` 键（来源 `response.finish_reason`，`provider.py:232` / `contracts.py:333`）。查询用 `usage_json->>'finish_reason'`。 |
| **建议落点（备选，需迁移）** | `attempts.finish_reason TEXT NULL`（`app/models.py:228-275` 的 `Attempt`） + 迁移 `0003`，并在 `finalize_success`（`repository.py:615-626`）与 `finalize_failure`（`:686-698`）写入。 |
| **安全性已核对** | `usage_json` 的唯一读取方是 `repository.py:986-1002` 的 `recent_attempt_tokens`，**只读 `input_tokens` / `output_tokens`**（`:996-997`）⇒ 加键**不影响**限流窗口重建（裁决 C3）。`usage_json` 为 `JSONB(none_as_null=True)`（`models.py:253-255`），加键不改变空值语义。 |
| **与 `control_events_json` 的关系** | **无关**。`finish_reason` 是**单次调用级**的供应商返回元数据，属 `attempts` 而非 run 级控制事件；不要塞进 `control_events_json`（后者是控制动作时间线，语义不同）。 |
| **对约束无影响的说明** | `ck_attempts_status` / `uq_attempts_member_no` / `ck_attempts_raw_output_len`（`models.py:262-275`）均与新增字段无关。方案 A 不改表结构 ⇒ 零迁移风险。 |
| **收益** | 使主文档 §10 的「格式无效率 > 2% 应停下修复」这类阈值可在**平台内**自查（本轮只能靠 `output_tokens` 触顶 + 截断 `raw_output` 间接推断）。 |
| **未验证** | 报表 / CSV 导出链路是否触及 `usage_json`（我确认 `app/reports/` 不读，但导出全链路未逐条验证）。 |

---

## 附：交叉影响与落地检查表

| 改动 | 可能影响 | 守卫它的既有不变量 / 用例 |
|---|---|---|
| D-01（改 `finished_at` 取时） | 用户可见完成时间变晚；**不得动 `started_at`** | `recent_attempt_tokens`（C3，`repository.py:986-1002`）；`tests/test_run_controls.py:486/496/583` 只断言非空 |
| D-02（补 `duration_ms`） | DB 延迟均值变大 | 无既有阈值断言（**未逐条验证**）；`reports/` 不读 `duration_ms` |
| D-03（`ValidationError`→`InvalidOutputError`） | 成员由「静默卡住」变「`retry_wait`」；`unknown_cost_count` 与 `requests_reserved` 对齐 | 红线 #1（`test_no_fabricated_answer_when_reason_too_long`）；`tests/test_inference.py:169 test_reason_field_gating` **须复核**（未验证是否冲突） |
| D-04（扩孤儿扫描） | 可能误伤正常 attempt；会计 +1 | 严格限定「成员已终态 + `lease_token IS NULL`」；`test_normal_running_attempt_not_mistaken_as_orphan` |
| D-05（`usage_json` 加键） | 无（读取方只读两个键） | `test_usage_json_extra_key_does_not_break_rate_limit_rebuild` |
| V-01（加列 + 迁移 + 签名） | 契约层 | `uq_runs_single_active` 不受影响（已论证）；历史 6 个 `cancelled` run 需回填 `unknown` |
| V-02（纯可读性） | 无（**必须零行为变更**） | `test_invalid_output_is_retryable_up_to_attempt_limit` |

**落地后必须跑的基线（本附录未执行）**：`cd backend && uv run pytest -q`（回填 D-03 用例后基线由 263 → 273，修复前 8 红）、`uv run ruff check app/ tests/`、`docker exec survey-pg psql ...` 复核孤儿 attempt 数为 0。
