# 50 人真实第三方 API 运行报告（网络 / 并发全链路插桩）

> **执行者**：software-engineer-5（Kou / 寇豆码）
> **日期**：2026-09-21（UTC）
> **范围**：真实 provider（DeepSeek）下 **50 人**全流程，对「网络」与「并发」做全链路插桩取证。
> **红线 #2**：本轮是 **50 人真实样本**，**不是**「万级真实模拟已完成」——本报告**不**对 1,000 / 10,000 规模作任何结论或外推。
> **红线 #3（密钥纪律）**：密钥本体**只**存在于 `deploy/real.env`（gitignored）；脚本/本报告/结果 JSON/日志**均无**任何密钥字面量；所有请求头脱敏为 `Bearer <redacted>`，密钥子串脱敏为 `<redacted>`。本报告只出现变量名 `SURVEY_MODEL_API_KEY`。
> **规范性声明**：全文所有数字均来自**原始产物**（`tests/load/results/*.json`、DB 原生 SQL）；**未执行的项一律标注「未验证」**，不以「全部通过」收尾。

---

## 0. 结论摘要（分条，带限定词）

- **真实 50 人**两轮（cap=50）、切片一轮（10 行 cap=8）均 **`completed`、成员全 `succeeded`、错误 attempts = 0**；`invariants.all_pass = True`。
- **真实截断路径**首次实证红线 #1：`max_tokens=2048` 时 3/53 首调触顶 `finish_reason=length` → 判 **`INVALID_OUTPUT`** → **重试**，**未补默认档位**（见 §8）。
- 输出上限 **4096** 后：`finish_reason=length` = **0**；`reasoning_tokens` 与 `input/output_tokens` **分列**（§10、§11）。
- **限流器收紧**：**仅以第 2 轮（cap=8，10 样本）为证据**——峰值恒为 **8**、且含**机制级证据**（§5.3）。**cap=50 档（50 样本/上限 50）属同义反复、不构成证据**（§5.3）。
- **未验证项**：429/`Retry-After`（本轮 0 次 429）、RPM/TPM 真实配额、真实费用、更大规模 —— 逐条见 §13。

---

## 1. 配置、执行方式与「单进程」证据

### 1.1 生效配置（`deploy/real.env`，脱敏）

| 项 | 值 |
|---|---|
| `MODEL_PROVIDER` | `openai_compatible` |
| `MODEL_NAME` | `deepseek-flash` |
| `MODEL_ENDPOINT` | `https://api.deepseek.com` |
| `MODEL_CONFIG_ID` | `prod-deepseek-flash` |
| `MODEL_API_KEY_ENV` | `SURVEY_MODEL_API_KEY`（key 本体仅在此文件，长度 35，未回显） |
| `MODEL_TIMEOUT_SECONDS` | 60 |
| `MODEL_MAX_OUTPUT_TOKENS`（文件内） | **256**（**未改动**，作为能力检查基线留档） |
| `AGENT_CONCURRENCY`（文件内） | 100（**运行时按轮覆盖**为 50 / 8） |
| `MODEL_RPM` / `MODEL_TPM` | 600 / 1000000（**启动基线猜测值**，非实测配额） |
| `BUDGET_CURRENCY` | CNY（**无单价**） |

### 1.2 输出上限 `max_tokens` 的历程（本报告的对照主线）

| 阶段 | 生效值 | 施加方式 | 触发/依据 |
|---|---|---|---|
| 能力检查 | 256 | `real.env` | 启动前能力检查基线 |
| 第 1 轮（首跑） | **2048** | **进程环境覆盖** | `TEAM-BRIEF §7.7` 裁定 O1 |
| 第 1 轮（重跑）/ 第 2 轮 | **4096** | **进程环境覆盖** | 首跑实测 `length×3`（截断率 5.7% > §10 阈值 2%）→ 团队裁定 R-1 |

**施加方式（团队点名确认 b）**：`build_settings()` 在**进程环境 merged 字典**里写入
`MODEL_MAX_OUTPUT_TOKENS = 4096` 后再 `Settings.from_env()`；**`deploy/real.env` 未改**。
`.env` 与进程的两值分别留档于结果 JSON 的 `execution_env.real_env_model_max_output_tokens=256`
与 `execution_env.model_max_output_tokens_effective=4096`。

**生效值取证（团队点名确认 a，非默认成立）**：脚本在 httpx **request 事件钩子**里解析**真实发出的请求体**，
记录 `max_tokens` 取值集合。三轮实测 `network.summary.observed_request_max_tokens_values` 分别为
`[2048]` / `[4096]` / `[4096]`（逐请求 100% 命中）。

**「执行真实调用的 Worker 是否同进程」（团队点名确认 c）**：**是**。
`execution_env.worker_in_same_process = true`，证据：
- `Worker` / `InstrumentedExecutor` / `RecordingProvider` / `ConcurrencyLimiter` 均由**本脚本进程内**构造；
- 真实调用经 `worker.serve()` 在**本进程 asyncio loop** 内执行（`http` 控制面走 `httpx.ASGITransport` 调同一 `app` 对象）；
- **无容器 / 子进程 / 独立网络栈**，故进程环境覆盖**必然传到**真正发请求的 worker（本次 PID 记录于 `execution_env.python_pid`）。

> **结论**：若任何一环在容器内，`max_tokens` 覆盖将传不进去而仍为 256 且**无法察觉**——本轮已显式核验**不存在**该情形（on-wire 实测 `[2048]/[4096]` 为最终判据）。

### 1.3 执行方式（可复现命令）

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/backend
# 第 1 轮（全 50 行，cap=50）—— 分两次调用以获得「第 1 轮达标后自检」的人工闸门
uv run python ../tests/load/run_real_50.py --round cap50 --confirm-real
# 第 2 轮（10 行切片，cap=8）—— 仅在第 1 轮「length=0 等四条门槛全过」后执行
uv run python ../tests/load/run_real_50.py --round cap8 --confirm-real
```

- 落地库：开发库 `survey`（`127.0.0.1:55432`）——**未占用** `survey_test`。
- 全程**未跑 pytest**（按团队要求保持环境独占）；运行期间与运行后 DB 活动批次数 = 0。
- **未改动任何业务代码**（`backend/app/**` 未动）；仅新增/修改本任务的 `tests/load/run_real_50.py`。

### 1.4 结果文件留档（对齐 `KNOWN-ISSUES.md` B-05 的 `--keep-data` 纪律）

| 文件 | 内容 | 说明 |
|---|---|---|
| `tests/load/results/real_50_maxtok2048.json` | 第 1 轮首跑（`max_tokens=2048`） | **证据留档**（原 `real_50.json` 改名，未覆盖） |
| `tests/load/results/real_50_maxtok4096.json` | 第 1 轮重跑（`max_tokens=4096`，cap=50） | 主结果 |
| `tests/load/results/real_50_cap8_maxtok4096.json` | 第 2 轮（10 行切片，cap=8，`max_tokens=4096`） | 限流器收紧证据 |

---

## 2. 样本与口径

| 项 | 值 |
|---|---|
| 源文件 | `/Users/zhao/Desktop/User_360_Analysis_sample50.csv`（UTF-8 BOM，50 行） |
| `persona_id` | `user_id` |
| `profile_text_columns` | **仅 L1 画像列，共 25 列**（见下） |
| 第 2 轮切片 | `tests/load/fixtures/sample50_head10.csv`（**项目内**，10 行；**非** Desktop） |

**L1 画像 25 列**：`age_band, gender, city_tier, city, education, occupation, marital_status,
monthly_disposable_income, beauty_monthly_spend, consumption_style, preferred_categories,
price_band_pref, ingredient_focus, ingredient_tags, purchase_channels, media_platforms,
content_preferences, kol_types, big5_openness, big5_conscientiousness, big5_extraversion,
big5_agreeableness, big5_neuroticism, decision_style, brand_affinity`。

**已排除**：元数据/标识（`_sample_group`/`_sample_note`/`user_id`/`respondent_id`/`data_source`/
`has_l2_survey`/`has_l3_review`/`has_l2_behavior`）、全部 `Q1_*`…`Q8_*`（含 `Q4_purchase_intent`）、
L3 评论正文（`review_*`）、L2 交易/行为（`order_*`/`beh_*`/`total_gmv_cny`…）、数据质量标记（`dq_*`）。

> **口径提示（未验证/待确认）**：样本 `_sample_note` 文本自述含「27 列 L1」，而脚本按「L1」口径实际解析得 **25 列**。本报告以**实际解析列（25）**为准，差异**未进一步核实**（列为待确认项）。

**产品设定（用户已定，中性客观描述，不编造功效）**：`怡兰葆 白茶赤芝系列`；
价格口径 **`328.00 CNY / 30ml 精华`**——⚠️ **本轮设定值，非从库存/官方价核实**；时间范围「未来 30 天」；
问题 `purchase_intent`（`prompt_version = purchase-intent-zh-v1`）。

---

## 3. 三轮运行总览（含 2048 vs 4096 对照）

| # | 源 | 行 | cap | `max_tokens` | 请求(首调+重试) | 错误 | `length` | valid | 延迟 p50/p95/max (ms) | in/out tok | 去重连接 | 并发峰值 | 终态 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R1a | 全样本 | 50 | 50 | 2048 | 53 (50+**3**) | 0 | **3** | 50 | 4579 / 11617 / 12097 | 45436 / 44886 | 49 | 50 | completed |
| R1b | 全样本 | 50 | 50 | 4096 | 50 (50+0) | 0 | **0** | 50 | 4094 / 13988 / 19734 | 42616 / 44050 | 50 | 50 | completed |
| R2 | **10 行切片** | 10 | 8 | 4096 | 10 (10+0) | 0 | **0** | 10 | 3905 / 14360 / 14360 | 8558 / 8075 | **8** | **8** | completed |

- **R2 明确标注**：该 run 使用 **10 行切片（`sample50_head10.csv`）**，**非全样本**；切片是刻意的插桩手段（验证限流器——cap=8——是否真的收紧）。
- 三轮 `final_status` 均由 DB 原生 SQL 复核为 `completed`，`pause_reason = NULL`，成员状态分布均 `{succeeded: N}`。
- 五档分布（`summary_endpoint.buckets`）：R1a `pn17/unsure6/py27`（top2box 0.54）；R1b `pn16/unsure5/py29`（top2box 0.58）；R2 `pn7/unsure0/py3`（top2box 0.30）。

---

## 4. 数据 A：逐请求网络明细 + 汇总

### 4.1 逐请求记录字段（`network.requests[]`，每行一条）

`seq`、`persona_id`、`attempt_no`、`method`、`url`（脱敏）、`http_status`、`wire_status`、
`duration_ms`、`wall_ms`、`wire_ms`、`retry`/`retry_no`、`retry_after_s`、`prompt_tokens`、
`completion_tokens`、`reasoning_tokens`、`finish_reason`、`provider_request_id`、`error_code`、
`error_message`（脱敏）、`req_max_tokens`、`req_has_response_format`、`t_start_offset_s`/`t_end_offset_s`。

### 4.2 汇总（`network.summary`）

| 指标 | R1a(2048) | R1b(4096) | R2(cap8) |
|---|---|---|---|
| total / ok / error | 53 / 53 / 0 | 50 / 50 / 0 | 10 / 10 / 0 |
| 成功率 | 1.0 | 1.0 | 1.0 |
| 429 / 5xx / timeout / AUTH / 4xx(其他) | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 |
| 重试请求 / 重试次数 | 3 / 3 | 0 / 0 | 0 / 0 |
| 延迟 p50/p95/p99/max (ms) | 4579 / 11617 / 12097 / 12097 | 4094 / 13988 / 19734 / 19734 | 3905 / 14360 / 14360 / 14360 |
| 纯 wire p50/p95/max (ms) | 228 / 393 / 526 | 4093 / 13988 / 19734 | 3905 / 14360 / 14360 |
| input / output tok | 45436 / 44886 | 42616 / 44050 | 8558 / 8075 |
| reasoning tok | **未采集**(见 §10) | **41525** | **7532** |
| 实测 RPM / TPM | 153.98 / 262404 | 121.49 / 210576 | 40.09 / 66675 |
| 配置 RPM / TPM | 600 / 1000000 | 600 / 1000000 | 600 / 1000000 |
| 去重 HTTP 连接数 | 49 | 50 | 8 |
| on-wire `max_tokens` 实测 | [2048] | [4096] | [4096] |
| `finish_reason` 分布 | stop×50, **length×3** | stop×50 | stop×10 |

- **实测 RPM/TPM 远低于配置基线** ⇒ 本轮**未逼近**任何配额上限，**不构成**对真实配额的下界或上界证据。
- **`429 / Retry-After`：本轮 0 次 429**，`Retry-After` 仍**未触发、未验证**（保留限定词）。

---

## 5. 数据 B：并发插桩（limiter 侧 + DB 侧）

### 5.1 方法（三路各自度量什么 + **共享观测面**声明）
采样间隔 **100ms**，**三路采样**。⚠️ **三路并非三条完全独立的证据**：

| 路 | 字段 | **度量对象** |
|---|---|---|
| ① 限制器侧 | `ConcurrencyLimiter.in_flight` | **槽位占用**（含「已占槽但正处于领取/写库等非 HTTP 阶段」的时刻）——度量**进程内并发控制**，**不是** HTTP 在飞数 |
| ② DB 成员侧 | `run_members.status='running'` | 已领取、处理中的**成员**数 |
| ③ DB attempt 侧 | `attempts.status='running'`（`attempts` **无 `run_id`**，按成员 JOIN） | 已开始、未结束的 **attempt** 数 |

**共享观测面（P2-2 要求明示）**：① **包含非 HTTP 阶段**，而②/③落在「领取→写库→发请求→写回」窗口内 ⇒ **①与②/③共享部分观测面**，**不能**当成三条独立证据相加。
**佐证**：R2 后段（t>4.7s、DB 仅 1 个 running attempt）①出现 **8↔1 振荡** ⇒ ①度量的确实是**槽位占用**而非 HTTP 在飞数。

> **修正说明**：任务书原文给的 `SELECT … FROM attempts WHERE run_id=…` 在**本库 schema 下不可执行**（无该列），已按 JOIN 语义实现并在结果 JSON 中记录 `series_columns`。

### 5.2 结果

| 指标 | R1a(2048) | R1b(4096) ⚠️同义反复 | R2(cap8) ✅证据 |
|---|---|---|---|
| cap | 50 | 50 | 8 |
| 采样次数 | 165 | 187 | 143 |
| 峰值 in_flight(limiter) | 50 | 50 | **8** |
| 峰值 DB running members | 50 | 50 | **8** |
| 峰值 DB running attempts | 50 | 50 | **8** |
| always ≤ cap | True | True | True |
| 由请求区间**独立重建**峰值 | 40 | 37 | 8 |
| 首完成时刻 / 其后开始次数 | t=2.65s / 34 | t=2.94s / 28 | t=2.57s / 2 |
| 单次「完成→下次开始」最大等待 | 7641.63ms | **89.96ms** | 49.46ms |
| **是否构成限流器证据** | ❌ 同义反复 | ❌ 同义反复 | ✅ **唯一证据** |

> ⚠️ **cap=50 两列（R1a/R1b）为同义反复、不构成证据**：样本量(50) = 上限(50)，峰值**必然**=50，**无法区分「限流器卡在 50」与「根本没有限流器」**。限流器收紧的结论**仅取 R2**（见 §5.3）。

### 5.3 限流器「真的收紧」的证明（**仅以 R2 为准**）

> ⚠️ 前置：cap=50 档（R1a/R1b）为**同义反复**，**不作证据**（理由见 §5.2 注）。以下**只**用 **R2（10 样本 / cap=8）**。

R2 的**三层证据**（层层递进，第 3 层为机制级 —— QA 独立重建，**这是本复核中最强的一条**）：

1. **时间序列从未出现 > 8**：143 次采样（100ms）全程 `in_flight ≤ 8`，`always_le_cap = True`。
2. **由请求区间独立重建峰值 = 8**：用 10 条请求各自的 `[t_start, t_end]` 做区间重叠统计，重建并发峰值 = **8**（**不依赖**限制器自报的 `in_flight`）。
3. **★ 机制级证据（最关键）**：首批 **8 个**于 **0.38–1.09s** 内启动；**另 2 个并未与首批同期启动**，而是**等到首个请求完成（t=2.5688s）之后**才于 **2.610s / 2.909s** 补位（「完成→下次开始」最小间隔 `max_wait=49.46ms`）。
   ⇒ **若没有 limiter，这 2 个会与首批 8 个同时发出**；它们被**推迟到有槽位释放**才启动，正是**并发上限 = 8 生效**的直接机制证据。

**结论（限定）**：R2 以三层证据表明在途数**被限制在 8 且 ≤ cap**；这是**一次 run 内**的观测（另有 cap=50 档作对照，但该档本身不构成证据），**不是**同一 run 内动态改 cap 的实验。

### 5.4 两种「峰值」的语义（由原 §9.1 前移）
- **采样峰值**（`peak_in_flight_limiter` 等）：**每 100ms 瞬时采样**最大值 → 度量**槽位占用**（含非 HTTP 阶段）。R1b = 50、R2 = 8。
- **重建峰值**（`rebuilt_peak_in_flight_from_requests`）：由请求 `[t_start, t_end]` **区间重叠**统计的最大**同时在飞 HTTP 请求数**。R1b = 37、R2 = 8。
- 两者**可不同且均正确**（cap=50 时槽位峰值 50，而同时在飞 HTTP 峰值 37，因存在「占槽未发请求 / 已返回未释放」短窗）；**不同口径，不可互证或对冲**。

---

## 6. 数据 C：全流程步骤耗时（ms）

| 步骤 | R1b(4096) | R2(cap8) | 断言/说明 |
|---|---|---|---|
| create_survey | 48.92 (201) | 48.06 (201) | |
| import | 8.67 (201) | 5.74 (201) | multipart + column_mapping |
| preview | 38.88 (200) | 38.94 (200) | **断言：preview 期间 0 模型调用** |
| create_run | 22.83 (201) | 18.41 (201) | **带 `Idempotency-Key`** |
| start | 9.94 (202) | 8.32 (202) | Q6 门禁（真实 provider + 密钥）通过 |
| **worker_serve** | **25268.18** (converged) | **15152.70** (converged) | 真实调用发生于此窗口 |
| results / summary / export.csv / get_run | 6.59 / 8.82 / 7.45 / 3.62 | 5.22 / 7.04 / 6.96 / 3.32 | export.csv 含 UTF-8 BOM |

- **preview 不调模型**：`preview` 之后脚本断言 `wire 请求数 == 0`（结构上也未构造 provider），三轮均通过。
- **幂等**：`create_run` 均带 `Idempotency-Key`；`request_limit = 3 × sample_size`（150 / 30）。

---

## 7. 数据 D：预算与费用口径

| 指标 | R1a | R1b | R2 |
|---|---|---|---|
| `budget_limit` / 币种 | 50.000000 / CNY | 同 | 同 |
| `request_limit`（=3×样本） | 150 | 150 | 30 |
| `requests_reserved` | 53 | 50 | 10 |
| `unknown_cost_count` | 53 | 50 | 10 |
| `actual_cost` / `reserved_cost` | 0.000000 / 0.000000 | 同 | 同 |

**费用口径（团队批准）**：`deploy/real.env` **未配置单价** ⇒
**「费用未知（未配置单价），已记录 token 用量」**；`unknown_cost_count` 承载「未知≠0」语义。
**本报告不给出任何金额**（团队倾向「不给金额更不易误读」，采纳；亦不提供「若按 X 元/百万 token」的换算示例，避免被当作实际费用）。

---

## 8. 硬要求专节 ①：红线 #1 在**真实截断路径**上的实证（最重要）

**事实（原始数据）**：`max_tokens=2048` 首跑中，**3 次首调**被思维链吃满上限、`finish_reason=length`、`completion_tokens=2048`（provider 仍返回 200）：

| persona_id | attempt | `finish_reason` | output_tokens | DB `error_code` | 后续 |
|---|---|---|---|---|---|
| `ILB_5a7d63f3cfaf7a6a` | 1 | length | 2048 | `INVALID_OUTPUT` | 重试→成功 |
| `ILB_434713078788e9ce` | 1 | length | 2048 | `INVALID_OUTPUT` | 重试→成功 |
| `ILB_b4d2bd4612abf2a5` | 1 | length | 2048 | `INVALID_OUTPUT` | 重试→成功 |

**DB 原生 SQL 复核**（`attempts JOIN run_members`，按 `error_code` 分组）：
```
INVALID_OUTPUT = 3
NULL           = 50      （合计 53，与逐请求记录数吻合）
```
且 3 个成员 `attempt 2` 输出 370 / 875 / 1276、`finish_reason=stop`，最终均 `succeeded`。

**为什么这是红线 #1 的硬证据**：截断导致 JSON 非法时，平台**判 `INVALID_OUTPUT` 并重试**、
**没有补任何默认档位**（无 `probably_not` 兜底）；首跑 50/50 有效是**靠重试**换来的，
**不是**靠「编一个答案」。这是本项目头号红线在**真实截断路径**上（而非 mock 注入）的**首次实证**。

### 8.1 关联观察（已查清）：3 条失败里 2 条 `raw_output` 为空串 → 判定 **(a) 模型行为**

- **现象**：3 条 `INVALID_OUTPUT` attempt 中，仅 1 条 `raw_output` 保留截断 JSON（99 字符），**另 2 条为空串 `''`**。三条 `usage_json` 均为 `{input_tokens: ~860, output_tokens: 2048}`（预算被吃满）。
- **判定：属 (a) 模型行为，不是「我们没落盘」**。证据链：
  1. **落库路径确实写了 `raw_output`**：无效输出分支 `execute.py` 传 `raw_output=self._truncate(response.raw_text)`（**无条件写**）⇒ `''` 是**写入的空串**，不是没写。
  2. **空串源于模型 `content` 为空**：适配器 `provider.py` 仅在 `message.content` **为 `None`** 时判 `PROTOCOL`；**为空串 `""` 时正常返回** ⇒ 这 2 条 `content` 是**空串**（推理把 2048 预算吃满、尚未来得及产出正文即触顶）。
  3. **成功路径对照（DB 原生 SQL）**：本 run `succeeded` 50 条 **50/50** 均有 `raw_output`（且 50/50 均有 `provider_request_id` / `duration_ms`）⇒ 管线**能**正常落盘正文。
  4. **附带澄清**：失败 attempt 的 `provider_request_id` / `duration_ms` = `None`，是**落库路径本就不写这两个字段**（仅成功路径写，见 `runs/repository.py`）——**与模型行为无关**，**非**本次数据缺失。
- **结论**：**不削弱红线 #1**；**无需**另开「落盘缺陷」任务；仅需在报告说明清楚（即本条）。

---

## 9. 硬要求专节 ②：两处数字差异的**语义**（避免被读成自相矛盾）

### 9.1 并发峰值（**已前移至 §5.4**）
见 **§5.4**：采样峰值 = 槽位占用（含非 HTTP 阶段）；重建峰值 = 同时在飞的 HTTP 请求数。**不同口径、均正确、不可互证或对冲**。

### 9.2 延迟：`duration_ms` p50（4579）vs `wire_ms` p50（228）
- **`duration_ms`**：适配器端到端（provider 在 `client.post` 前后计时，**含响应体读取**）。
- **`wire_ms`**：httpx `request` 钩子 → `response` 钩子 的间隔。
- **首跑（2048）**为 **228ms**，是因为该轮 httpx 的 **response 钩子在响应体读取之前**触发（见 §10），
  故 228ms = **响应头到达**时间，**不等于**端到端；而 `duration_ms` 4579ms = 端到端。
- ⚠️ **跨轮不可比**：修复 §10 后（`ProbeTransport` 在传输层读全量正文再返回），
  response 钩子在正文读完后触发，故 **R1b/R2 的 `wire_ms` ≈ `duration_ms`**（R1b：4093 ≈ 4094）。
  **报告读者须知**：`wire_ms` 的语义在 2048→4096 两轮之间**发生了变化**（因修复），
  故**不要**用 R1a 与 R1b 的 `wire_ms` 直接比较。

---

## 10. 硬要求专节 ③：`reasoning_tokens` 缺列的根因与修复前后对照

### 10.1 现象
- **R1a（2048）**：53 条记录 `reasoning_tokens` **全为 `None`**（即 §4.2 表里「未采集」）。
  （首跑控制台曾打印 `reasoning_tokens: 0`，那是「**0 条有值**」的计数，**不是** reasoning 为 0。）

### 10.2 根因（已定位）
`deepseek-flash` 的 `reasoning_tokens` 位于响应体 `usage.completion_tokens_details.reasoning_tokens`，
而**本项目适配器 `ModelResponse` 只映射 `input_tokens/output_tokens`**（`provider.py`），**不暴露**该字段。
故只能从**响应原文**取；而 **httpx 的 `response` 事件钩子在响应体尚未读取时触发**，
真实 transport 下钩子内 `response.json()` 抛 `ResponseNotRead`，被 `except` 兜底静默吞掉 → 恒为 `None`。

### 10.3 为何未被提前发现（**重要教训**）
**离线用 `httpx.MockTransport` 自测时，钩子内 body 是可读的**（MockTransport 的响应体已在内存），
因此「钩子内能读 body」在离线条件下**恒真**，**掩盖了真实 transport 的时序缺陷**。
→ **教训**：**离线 mock 会掩盖传输层时序缺陷；涉及 HTTP 生命周期的观测点必须在真实 transport 路径上复测。**

### 10.4 修复（只改本任务脚本，未动业务代码）
新增 `ProbeTransport(httpx.AsyncHTTPTransport)`：在 `handle_async_request` 里对 200 的
`…/chat/completions` 响应 `await response.aread()`（**读取会缓存**正文，httpx 后续读取命中缓存，provider 行为不变）
再解析 usage，按当前 `(persona_id, attempt_no)` 上下文写入观测记录。
**离线端到端验证**（本地 HTTP 服务，非外网）：客户端正文完整；
`wire_prompt_tokens=11 / wire_completion_tokens=9 / reasoning_tokens=7` 正确捕获。

### 10.5 修复前后对照

| | 采集途径 | R1a(2048) | R1b(4096) | R2(cap8) |
|---|---|---|---|---|
| `reasoning_tokens` 有值记录数 | 响应原文 usage | **0 / 53** | **50 / 50** | **10 / 10** |
| `reasoning_tokens` 合计 | — | **未采集** | **41525** | **7532** |

- **R1a 的 reasoning 已不可回溯**：DB `attempts.usage_json` 仅存 `{input_tokens, output_tokens}`（已查证），
  响应原文未持久化 ⇒ 该轮 reasoning 永久缺失（如实声明）。
- **修复未在 R1a 上复测**（R1a 已结束），**修复后的真实路径**由 **R1b / R2** 复测通过。

---

## 11. 硬要求专节 ④：tokens 结构（剔截断后的正常均值）

**⚠️ 关键**：R1a 的 `output ≈ input`（44886 vs 45436）**不能**被读成正常水平——它被 **3 次 2048 截断（=6144 tok）**抬高了。

| 口径 | R1a(2048) | R1b(4096) | R2(cap8) |
|---|---|---|---|
| input 合计 | 45436 | 42616 | 8558 |
| output 合计 | 44886 | 44050 | 8075 |
| ├ reasoning 合计 | **未采集** | 41525 | 7532 |
| └ 正文(content)合计 | — | 2525 | 543 |
| **正常 calls 平均 output** | **774.8**（剔除 3 次截断：`(44886−3×2048)/(53−3)`） | **881.0** | **807.5** |
| 平均 reasoning / 平均正文 | 未采集 | **830.5 / 50.5** | 753.2 / 54.3 |
| output 最大/最小 | 2048 / 249 | **3574 / 196** | 2562 / 174 |

**读法**：「**正常单次输出 ≈800–880 tok**」**仅适用于 R1b / R2**（两轮正常均值分别为 **881.0 / 807.5**）；**R1a 的正常均值是 774.8**、**略低于 800**——勿以为三轮都落在 800–880。其中**思维链占 ~830（≈94%）**，正文 JSON 仅 **~50 tok**。
故「输出长度」几乎全是 reasoning；`output≈input` 在 R1a 中是**截断假象**，正常口径下 output 与 input **同量级但构成不同**。
R1b 的 output 最大值 **3574 < 4096**（未触顶），但**已 > 2048**——即该条在 2048 下也必截断，
说明 **2048→4096 的裁定必要性**不仅来自那 3 条。

---

## 12. 暂停 / 恢复
- 三轮 **均未触发自动暂停**（`resume_events = []`，DB `pause_reason = NULL`）。
- 按团队要求：**未触发即不人为制造 resume**。

---

## 13. 未验证 / 假设项（逐条保留限定词，**不得当作已证**）

1. **`429` 响应体结构** —— 三轮 **0 次 429**，**未触发、未验证**。
2. **`Retry-After` 头解析** —— 未出现该头，**未验证**。
3. **超时的实际线格式** —— 未触发超时（配置 60s 已知），**未验证**。
4. **RPM / TPM 真实配额** —— `600 / 1e6` 为**启动基线猜测**；实测 122–154 RPM / 210k–262k TPM 远未逼近，**不证明**真实配额。
5. **真实费用** —— **未配置单价 ⇒ 未知**；未向供应商结算核对，**未验证**。
6. **R1a 的 `reasoning_tokens`** —— **未采集且不可回溯**（见 §10）。
7. **修复后真实路径的 reasoning** —— 已在 R1b/R2 复测**通过**，但仅 2 轮、n=60。
8. **规模外推** —— 本报告结论**不外推**到 1,000 / 10,000 人（红线 #2）。
9. **`wire_ms` 语义跨轮变化** —— 因修复，2048 与 4096 两轮的 `wire_ms` **口径不同**，不可直接比较（§9.2）。
10. **L1 列数** —— `_sample_note` 文本自述 27 列 vs 实际解析 25 列，**差异未核实**（§2）。
11. **产品价格口径** —— `328.00 CNY / 30ml 精华` 为**本轮设定值**，**非**核实价。
12. **`json_schema` 严格模式 / `response_format` 因果** —— 未做对照实验，**未验证**（沿用能力检查结论）。
13. **价格换算** —— 按团队决定，**不提供**任何金额或换算示例。
14. **失败 attempt 的观测字段（项目观察，非本任务未验证项）** —— `provider_request_id` / `duration_ms` 在**失败路径不落库**（仅成功路径写，`runs/repository.py`）⇒ 失败 attempt 缺这两列是**既有设计**；`raw_output` 为空串属**模型行为**（§8.1），非缺陷。

---

## 14. 一句话总览（给主理人）
> `deepseek-flash` 带思维链：**正常单次输出 ~800–880 tok（R1b/R2；R1a 正常均值 774.8）、其中 ~94% 是 reasoning、正文仅 ~50 tok**；
> **2048 不够**（首跑 3/53 触顶截断、靠重试救回、**未补默认答案**——红线 #1 实测），**4096 后 `length=0`**；
> 50 人两轮 + 10 行切片（cap=8）**均完成、0 错误**；**限流器收紧仅以 cap=8 档为证（cap=50 档同义反复，§5.3）**；**费用未知**，**未验证项见 §13**。
