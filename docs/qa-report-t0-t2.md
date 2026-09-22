# QA 独立复核报告 —— T0 / T1 / T2（对抗性验证）

> 复核人：QA 工程师 严过关（Yan）｜阶段：SOP 阶段 4（第二道防线）
> 复核目标：**尝试证伪**工程师寇豆码关于 T0/T1/T2 的自测结论（其自述「58 passed」），
> 而非复跑其命令。方法以**变异测试 + 数据库直查 + 独立对抗输入**为主。
> 全部实验在本机真实环境执行（Python 3.12.13 / PostgreSQL 16.13 容器 `survey-pg`）。

---

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| 工程师声称「无效输出绝不补默认答案」 | ✅ **成立**（变异测试证明现有测试**真的能抓住**违规实现） |
| 工程师声称「解释器精确钉 3.12.13」 | ✅ **成立**（且在 3.12.12 下该测试**真的失败**，非恒真） |
| 工程师声称「绝不静默丢行 + 20,000 行真导入」 | ⚠️ **部分成立**：丢行/行号/位置化错误成立；但 20,000 行**工程师只测了纯解析、未测入库**——由我补测，**真入库通过** |
| 工程师声称「测试库隔离 fail fast」 | ✅ **成立**（DSN 指向 dev 库时全部用例 fail fast 报错） |
| 工程师声称「五表约束真实」 | ✅ **成立**（非法状态值插入被拒，双射 UNIQUE 均存在） |
| **P0 阻断缺陷** | **0 个** |
| **P1 重要缺陷** | **0 个（原件 1 个已修复并回归锁定）** |
| **P2 建议** | **0 个待办**（原 3 个：P2-a 已修复；P2-b/P2-c 为可忽略的可选建议） |
| 新增 QA 用例 | `backend/tests/test_qa_t0_t2.py`，**51 条**（含参数化展开） |
| 全量测试（工程师 + QA） | **115 passed / 0 failed / 0 skipped** |

> **复核后更新（工程师采纳整改）**
> - **P1 已修复**：`personas/source.py` 的 `_read_csv_rows` / `_read_xlsx_rows` 已包 `try/except`
>   将 `UnicodeDecodeError` / `zipfile.BadZipFile` / `InvalidFileException` / `KeyError` / `OSError`
>   统一转为 `InvalidUserTableError`。我实跑复验：`corrupt-xlsx`、`binary-csv` 现均返回
>   `InvalidUserTableError`（不再抛裸异常）。已加回归用例 `test_qa_malformed_file_maps_to_invalid_user_table`。
> - **P2-a 已修复**：`QuestionOption.value/label` 收紧为 `Literal` + `value↔label↔score` 一致性校验；
>   我核对其对我测试的 1 行等价改动（意图不变），并加回归用例 `test_qa_option_triple_must_consistently_match`。
> - 现存仅 **P2-b / P2-c** 两条**可选**建议（行号语义说明、surveys 排序索引），均非阻断。

**总判：T0–T2 核心不变量全部成立，未发现 P0。存在 1 个 P1 边界缺陷 + 3 个 P2 建议。**

---

## 1. 方法与证据可信度

- 变异测试副本：`backend/.mutate_check/`（复制 `app/`、`tests/`、配置，`.venv` 软链回原 venv，
  用 `uv run --no-sync` 指向副本），**验证后已删除**（见 §8 清理记录）。
- 未修改任何生产源码（`backend/app/**` 只读）；未做任何真实付费 API 调用（全程 fake provider / 纯逻辑）。
- DB 直查用 `docker exec survey-pg psql ...`。

---

## 2. A｜头号红线：绝不补默认答案（最高优先级）

### A1 变异测试 —— 证明「现有测试不是自欺」

**实验**：把 `app/inference/validation.py` 复制到副本，**故意植入**上游 `_normalize_answers`/`_normalize_value`
的补值行为：

```python
    if "value" not in payload:
        value = "unsure"                     # 缺失必答 → 补中性默认值
    else:
        value = payload["value"]
    if not isinstance(value, str) or value not in OPTION_VALUES:
        value = OPTION_VALUES[0]             # 非法 value → 回退首项
```

**结果（副本上跑工程师的测试）**：

```
4 failed, 34 passed in 0.70s
FAILED tests/test_inference.py::test_unknown_value_rejected - Failed: DID NOT RAISE ...
FAILED tests/test_inference.py::test_no_default_answer_fill - Failed: DID NOT RAISE ...
FAILED tests/test_inference.py::test_no_fallback_to_unsure_or_first_option - Failed: DID NOT RAISE ...
FAILED tests/test_inference.py::test_fake_provider_retry_sequence_then_success
        At index 1 diff: 'unsure' != 'INVALID'
```

**结论：✅ 成立。** 现有测试**确实能抓住**补默认答案的实现 → 测试是有效防线，非自欺。

### A1' 第二变异 —— 「只剥单层围栏」

**实验**：把 `strip_single_fence` 改为循环递归剥围栏（违反「仅单层」契约）。

```
1 failed, 26 passed
FAILED tests/test_inference.py::test_double_fence_not_stripped_recursively - Failed: DID NOT RAISE ...
```

**结论：✅ 成立。** 「仅单层围栏」这一唯一容错被测试真实锁定。

### A2 我自补的对抗性输入（绕过工程师可能没想到的路径）

在 `tests/test_qa_t0_t2.py` 中以参数化覆盖 **23 种**无效输出，**全部要求抛 `InvalidOutputError`
（即绝不返回任何答案）**：

| 类别 | 输入 | 实测 |
|---|---|---|
| value 类型错 | `3`（数字）、`true`、`null`、`[]` | 全部被拒 ✅ |
| value 内容错 | `""`、`"   "`、`"UNSURE"`、`" unsure"`、`"unsure "`、`"unsure\n"`、`"Unsure"` | **大小写/空白均无容错** ✅ |
| 结构错 | 纯 JSON 数组 `[{...}]`、散文包裹 `好的，我的答案是 {...}` | 被拒 ✅ |
| 围栏 | 嵌套**两层**围栏（只剥一层）→ 被拒；**单层**围栏合法 JSON → **通过** | 符合契约 ✅ |
| 缺字段 | 缺 `question_id`、`question_id` 对但缺 `value` | 被拒 ✅ |
| 额外字段 | `confidence`、`score` | 被拒 ✅ |
| 截断 | `{"...":"prob`、缺右括号（模拟 256 token 截断） | 被拒（**不猜测补全**）✅ |
| 其它 | `None`、`""`、空白串、自然语言 | 被拒 ✅ |

**结论：✅ 成立。** 特别地，`"UNSURE"` / `" unsure"` 未被容错，`score` 与 `confidence` 均属额外字段被拒。

### A3 `ValidatedAnswer.score` 必须由 value 派生

```
$ parse_and_validate('{"question_id":"purchase_intent","value":"definitely_not","score":5}')
→ InvalidOutputError: unexpected field(s) in model output: ['score']
$ ValidatedAnswer(question_id="purchase_intent", value="definitely_not", score=5)
→ ValidationError（extra=forbid）
```

**结论：✅ 成立。** 模型传入的 `score` 无法生效；`score` 只能由 value 派生，且 `definitely_not=1 … definitely_yes=5` 与主文档 §5.1 完全一致。

---

## 3. B｜解释器钉版（R1）

### B1 干净 shell 实测

```
$ unset UV_PYTHON_INSTALL_DIR && cd backend && uv run python -c "import sys;print(sys.version);print(sys.version_info[:3])"
3.12.13 (main, May 10 2026, 19:20:41) [Clang 22.1.3 ]
(3, 12, 13)
```

**结论：✅ 成立。**

### B2 证伪 `test_interpreter_matches_pinned_patch` 是否恒真

本机存在 3.12.12。用其直接运行该用例（复用 cp312 venv 的 site-packages，ABI 兼容）：

```
$ PY312_12=~/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12
$ PYTHONPATH=".venv/lib/python3.12/site-packages:." $PY312_12 -m pytest \
    tests/test_personas.py::test_interpreter_matches_pinned_patch -q
E   AssertionError: expected Python 3.12.13, got (3, 12, 12) (check backend/.python-version ...)
E   assert (3, 12, 12) == (3, 12, 13)
E     At index 2 diff: 12 != 13
1 failed in 0.52s
```

**结论：✅ 成立。** 该测试**不是恒真**：解释器一旦漂移到 3.12.12 即真实失败 → 能抓住钉版失效。

### B3 全项目不依赖 `UV_PYTHON_INSTALL_DIR`

grep 全仓库：命中**仅出现在文档**中（且均为「**不要**依赖它」的说明），
源码 / `pyproject.toml` / `alembic.ini` / `conftest.py` / 测试 **零命中**。

**结论：✅ 成立。**

---

## 4. C｜数据导入：绝不静默丢行

### C1 第 3 行坏行（persona_id 为空）

```
$ parse_user_table(csv_with_row3_empty_persona_id)
→ InvalidUserTableError(code=INVALID_USER_TABLE, row_no=3, column='user_id')
```

**结论：✅ 成立**，整批被拒（**不是**跳过坏行），且给出**行号 + 列名**。

### C2 重复 ID / 空画像 / 空表 / 20,001 行

| 场景 | 结果 |
|---|---|
| 重复 persona_id | 整批拒，`row_no=2, column='user_id', duplicate_of_row=1` ✅ |
| 空画像 | 整批拒，`row_no=1, column` 含画像列 ✅ |
| 空表（无数据行） | 拒 ✅ |
| 20,001 行 | 拒（上限 20,000）✅ |

### C3 **20,000 行 fixture：真导入还是 stub？**（工程师的盲区）

**关键发现**：工程师的 `test_20000_fixture_import_rowcount` **只调用 `parse_user_table`（纯解析）**，
`test_import_record_roundtrip_and_preview` 只导入 **5 行**。**没有任何用例验证 20,000 行真正落库。**

**我补测（`test_qa_20000_real_db_import_verified_by_sql`）**：生成 20,000 行 → `create_import` 真导入 →
**直查 `imports` 表并用 SQL 展开 JSONB 复核**：

```sql
SELECT row_count, jsonb_array_length(snapshots_json) FROM imports WHERE id=:i;
SELECT min((e->>'row_no')::int), max((e->>'row_no')::int),
       count(*), count(DISTINCT (e->>'row_no')::int),
       count(DISTINCT (e->>'persona_id'))
FROM imports, jsonb_array_elements(snapshots_json) AS e WHERE id=:i;
```

```
tests/test_qa_t0_t2.py::test_qa_20000_real_db_import_verified_by_sql PASSED
断言：row_count=20000；jsonb_array_length=20000；row_no min=1/max=20000；
      count=20000；distinct row_no=20000；distinct persona_id=20000；
      load_snapshots 读回后 row_no == range(1,20001) 连续无缺号。
```

**结论：⚠️ 部分成立 → 现已**通过**。** 20,000 行是**真导入真落库**（非 stub），row_no 连续无缺号；
但工程师**未自测该路径**，属自测盲区（不是他的结论错误，而是未被证据覆盖）。

### C4 CSV BOM / C5 XLSX / C6 行序

- BOM（`utf-8-sig`）与无 BOM **均可读** ✅
- XLSX 指定工作表可选；默认第一张；不存在的工作表报错 ✅
- XLSX **公式单元格**（映射字段 `=1+1`）→ `InvalidUserTableError("formula cell detected ...")` ✅（不执行公式）
- 行顺序按输入顺序保留（`p_030,p_001,p_999,p_abc` 原序，非排序/随机）✅

---

## 5. D｜测试库隔离（R3）

### D1 证伪：把测试 DSN 指向 **dev** 库

```
$ TEST_DATABASE_URL=".../survey" uv run pytest tests/test_personas.py -q
ERROR tests/.../test_*.py - RuntimeError: TEST_DATABASE_URL must point at 'survey_test',
      got 'postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey'
15 errors in 0.58s          ← 所有用例在 setup 阶段 fail fast，无一条执行
```

**结论：✅ 成立**，fail fast，数据**不会**写进 dev 库。

### D2 无任何测试用 SQLite

grep `sqlite|SQLite`：仅命中 `conftest.py` / `db.py` 的**注释**（「不使用 SQLite」），无实际分支。✅

### D3 不污染 dev 库（跑完全量测试后直查）

```
dev 'survey'      : runs=0 members=0 attempts=0 imports=0 surveys=0
test 'survey_test': runs=0 members=0 attempts=0 imports=0 surveys=0
```

**结论：✅ 成立。**

---

## 6. E｜数据库约束的真实性（A1 之外）

### E1 非法状态值插入（`ON_ERROR_STOP=off` 逐条试）+ E2 双射 UNIQUE

```
E1a runs.status='queued'        → ERROR: violates check constraint "ck_runs_status"        ✅
E1b runs.pause_reason='teapot'  → ERROR: violates check constraint "ck_runs_pause_reason"  ✅
E1c runs.sample_size=0          → ERROR: violates check constraint "ck_runs_sample_size"   ✅
E1d run_members.status='queued' → ERROR: violates check constraint "ck_members_status"     ✅
E1e run_members.row_no=0        → ERROR: violates check constraint "ck_members_row_no"     ✅
E2a 重复 (run_id,persona_id)    → ERROR: violates unique constraint "uq_members_run_persona" ✅
E2b 重复 (run_id,row_no)        → ERROR: violates unique constraint "uq_members_run_row"    ✅
E1f attempts.status='pending'   → ERROR: violates check constraint "ck_attempts_status"     ✅
E1g attempts.attempt_no=0       → ERROR: violates check constraint "ck_attempts_attempt_no"  ✅
E2c 重复 (member_id,attempt_no) → ERROR: violates unique constraint "uq_attempts_member_no"  ✅
```

**结论：✅ 成立。** 五表 CHECK 完整枚举主文档 §6.1 要求的全部状态；
`UNIQUE(run_id,row_no)` 与 `UNIQUE(run_id,persona_id)` **均存在**，「N 进 N 出」双射的 DB 级兜底到位。
（`uq_runs_single_active` 由主理人已独立验证，按要求**未重复**。）

### E3 金额 / 时间类型

`runs.budget_limit/actual_cost/reserved_cost`、`attempts.reserved_cost/actual_cost` 均为 **`numeric(18,6)`**；
所有时间字段为 **`timestamp with time zone`**。✅

### E4 索引

主文档 §6.1 要求的三类索引均存在：`idx_members_run_status_next(run_id,status,next_attempt_at)`、
`idx_members_lease(run_id,lease_expires_at)`、`run_id`（由组合索引前缀 + FK 覆盖）。
**未发现「主文档要求但漏建」的索引。**

---

## 7. F｜测试自身可信度 & G｜契约完整性（T0）

### F1 恒真/弱断言扫描

- 全测试**无** `assert True`。
- 唯一偏弱处：`test_no_fallback_to_unsure_or_first_option` 里 `result=None; try/except; assert result is None`
  形式略冗余，但主断言（`pytest.raises` + `code=='INVALID_OUTPUT'`）是强断言，**不构成自欺**。

### F2 skip / xfail 计数

全仓库 `pytest.skip|xfail|skipif` = **0**。无被绕过用例。✅

### F3 `conftest.tmp_path` 覆盖是否掩盖真实 I/O

未掩盖：`tmp_path` 指向项目内 `backend/.pytest_tmp/` 且是真目录（创建→清理），
`test_20000_fixture_import_rowcount` 的确在其中**真写** CSV。✅

### G 契约（T0）

| # | 检查 | 结果 |
|---|---|---|
| G1 | 六个强制模型命名逐字正确（`PersonaSnapshot/SurveyInput/RunCreate/ModelRequest/ModelResponse/ValidatedAnswer`） | ✅ |
| G2 | 「只允许 1 道题」由类型层强制（`SurveyInput.question` 单字段 + `extra=forbid`，列表/复数式被拒） | ✅ |
| G2' | 「5 个固定 value」由 `QuestionInput` 的 `model_validator` 强制：乱序/重复/增减均被拒 | ✅（见 P2-a） |
| G3 | 五档 value↔score 与 §5.1 完全一致 | ✅ |
| G4 | 缺价格 / 缺画像 ID / 缺画像文本被拒 | ✅ |
| G5 | **不存在**按年龄/城市自动过滤输入行的逻辑（`build_snapshots` 年龄 None/0/99 全保留） | ✅ |

---

## 8. 发现的缺陷（按严重度）

### 🟢 P1（重要，1 个 → 已修复并回归锁定）— 文件「格式错误」曾抛裸异常

- **文件:行号**：`backend/app/personas/source.py:108`（`_read_csv_rows` 的 `data.decode("utf-8-sig")`）
  与 `:120`（`_read_xlsx_rows` 的 `load_workbook(...)`）。
- **原始证据（修复前，实跑）**：

```
[corrupt-xlsx-by-magic]  -> RAW BadZipFile (BAD, maps to 500): File is not a zip file
[xlsx-filename-garbage]  -> RAW BadZipFile (BAD, maps to 500): File is not a zip file
[binary-csv]             -> RAW UnicodeDecodeError (BAD, maps to 500): 'utf-8' codec can't decode byte 0xff ...
[empty-bytes]            -> InvalidUserTableError (GOOD)
[header-only-no-data]    -> InvalidUserTableError (GOOD)
```

- **期望 vs 实际**：主文档 §4 要求文件格式错误应返回结构化 invalid-table（T4→422）；
  修复前损坏的 XLSX / 非 UTF-8 CSV **逃逸出领域错误类型**，T4 将变成 **500**。
- **影响**：失败方向为 **fail-closed（不产生错误数据）**，故非 P0；但违反契约的错误语义。
- **处置与复验**：工程师已包 `try/except` 转抛 `InvalidUserTableError`。我实跑复验（修复后）：

```
[corrupt-xlsx] InvalidUserTableError   (FIXED)
[binary-csv]   InvalidUserTableError   (FIXED)
```

  已加回归用例 `test_qa_malformed_file_maps_to_invalid_user_table`（3 参数化）**锁定**。**我未改动生产源码。**

### 🟢 P2-a（已修复并回归锁定）— 五档「固定」未覆盖 `label`，且未用 `Literal` 做类型层加固

- `backend/app/contracts.py:170-172`：`QuestionOption.value` 为 `str`（非 `Literal[...]`），
  `label` 完全自由。实测 `label="FREE-TEXT-LABEL"` 可通过校验（**value/score 仍被严格固定**，故不影响核心不变量）。
- **处置**：工程师已将 `value`/`label` 改为 `Literal`（五个固定值 / 五个固定中文展示），
  并加 `model_validator` 强制 `value↔label↔score` 三元组一致。
- **QA 回归锁定**：新增 `test_qa_option_triple_must_consistently_match`（label 不符 / score 不符 /
  自由文本 label 均被拒；规范三元组通过）——**实测通过**。

### 🟡 P2-b（建议）— 错误 `row_no` 是「数据行序号」，不是工作表行号

- `build_snapshots` 的 `row_no` 从**第一条数据行=1** 计（不含表头）。报错行号沿用该语义。
- 用户若在 Excel 中核对，会与「工作表实际行号（表头=1）」错位一行。与主文档 §4 的 `row_no: 1` 定义**一致**，
  但 T4 返回给前端时建议注明「数据行序号」或同时给出工作表行号，避免歧义。

### 🟡 P2-c（建议）— `surveys` 列表按 `created_at DESC` 排序但无对应索引

- `backend/app/surveys/service.py:126` 按 `created_at DESC` 排序；`surveys` 表仅 `pkey` + `ck_surveys_revision`。
- 主文档**未强制**该索引，首版问卷量小，影响可忽略；后续量大时建议补 `idx_surveys_created_at`。

---

## 9. 新增用例（`backend/tests/test_qa_t0_t2.py`，51 条）

| 分组 | 用例 | 意图 |
|---|---|---|
| A 红线 | `test_qa_illegal_outputs_raise_never_return`（23 参数化） | 23 类无效输出必须抛错、绝不返回答案 |
| A 红线 | `test_qa_single_layer_fence_valid_passes` | 唯一允许的容错（单层围栏合法 JSON）必须通过 |
| A 红线 | `test_qa_fence_with_trailing_text_not_tolerated` | 围栏后带散文不得容错 |
| A 红线 | `test_qa_score_derived_from_value_model_score_ignored` | 模型传入 score 无效，score 由 value 派生 |
| G 契约 | `test_qa_six_required_models_exist_verbatim`（6） | 六个模型命名逐字正确 |
| G 契约 | `test_qa_five_values_exact_and_no_reorder_or_dup` | 五档乱序/重复/缺失均被拒 |
| G 契约 | `test_qa_option_triple_must_consistently_match` | P2-a 回归：value↔label↔score 错配 / 自由文本 label 被拒 |
| G 契约 | `test_qa_question_id_and_type_fixed` | question_id/type 类型层固定 |
| G 契约 | `test_qa_single_question_structurally_enforced` | 只允许 1 道题（结构性） |
| G 契约 | `test_qa_reject_missing_price_persona_and_profile` | 缺价格/画像 ID/画像文本被拒 |
| G 契约 | `test_qa_no_auto_age_filter_in_build_snapshots` | 不按年龄自动过滤输入行 |
| C 导入 | `test_qa_bad_row3_empty_persona_rejects_whole_batch_with_position` | 第 3 行坏行→整批拒+行号列名 |
| C 导入 | `test_qa_duplicate_persona_id_whole_batch_rejected` | 重复 ID→整批拒+定位 |
| C 导入 | `test_qa_empty_table_and_empty_profile_rejected` | 空表/空画像被拒 |
| C 导入 | `test_qa_over_20000_rows_rejected` | 20,001 行被拒 |
| C 导入 | `test_qa_csv_bom_and_no_bom_both_parsed` | BOM/非 BOM 均可读 |
| C 导入 | `test_qa_row_order_preserved_not_sorted` | 行序保留 |
| C 导入 | `test_qa_xlsx_sheet_selection` / `..._formula_in_mapped_field_rejected` | XLSX 选表/公式拒收 |
| C 导入 | `test_qa_malformed_file_maps_to_invalid_user_table`（3 参数化） | **P1 回归**：损坏 XLSX / 非 UTF-8 CSV → `INVALID_USER_TABLE` |
| **C 导入** | **`test_qa_20000_real_db_import_verified_by_sql`** | **20,000 行真入库 + SQL 复核 row_no 连续性** |
| D 隔离 | `test_qa_running_against_isolated_test_db` | 运行时断言 `current_database()='survey_test'` |

---

## 10. 无法证伪、暂时采信的结论

以下结论我**未能证伪**，在证据范围内**采信**（列出以便主理人知晓其证据边界）：

1. **两个 persona 请求不含对方画像**：结构上 `build_user_message` 仅序列化当前 `persona`（`prompt.py:94-100`），
   且工程师用例 `test_two_personas_isolated_context` 通过。我未构造更极端的注入实验，采信。
2. **`ValidatedAnswer` 的 `extra="forbid"` 全面生效**：我仅覆盖 `confidence`/`score`，其余未知字段采信（Pydantic 机制保证）。
3. **`provider` 关闭不透明重试**：`httpx.AsyncHTTPTransport(retries=0)`（`provider.py:107`）静态可证；未做「重试计数」实测，采信。
4. **T2 真实第三方 API 调用未测**：本机无 key，与团队一致，采信「未执行」。

---

## 11. 最终统计

```
$ cd backend && uv run pytest -q
115 passed in 1.69s      （工程师 + QA 51；0 failed / 0 skipped）
$ uv run pytest tests/test_qa_t0_t2.py -q
51 passed
```

**清理记录**：变异测试副本 `backend/.mutate_check/` 已 `rm -rf` 删除；项目内**无**遗留临时目录；
dev 库 `survey` 与 test 库 `survey_test` 跑完后**均为 0 行**；未修改 `backend/app/**` 生产源码；
未触碰上游仓库；无任何真实付费调用。
