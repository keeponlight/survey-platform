# 真实第三方 API 实测报告：冒烟 + 100 人试点（A-01 第一步）

> 执行人：software-engineer-11（2026-09-21，UTC）
> 范围：**只做** ① 直连冒烟（2 次调用）② 100 人真实试点。1,000 / 10,000 未执行（未获放行）。
> 所有「真实调用」均指对 `https://ooioo.work`（OpenAI-compatible 网关，newapi 聚合）发起的真实 HTTP 请求。
> 密钥纪律：key 只存在于 `deploy/real.env` 与运行进程环境；本报告只出现变量名 `SURVEY_MODEL_API_KEY`（**已配置**），无任何 key 值。

---

## 1. 网关信息与配置改动

| 项 | 值 |
|---|---|
| 网关 base | `https://ooioo.work/v1`（endpoint **含 `/v1`**；provider 直连 `POST {endpoint}/chat/completions` 一次命中，**无需改路径**） |
| 鉴权 | `Authorization: Bearer $SURVEY_MODEL_API_KEY`（401/403 全程为 0） |
| `GET /v1/models` 返回的模型 | `codex-auto-review`、`gpt-5.5`、`gpt-5.6-sol`、**`gpt-5.6-terra`**、`gpt-6-astra` |
| **配置改动 1** | `deploy/real.env` 的 `MODEL_NAME`：改前 `gpt-terra`（不存在的 ID）→ 改后 **`gpt-5.6-terra`**（以枚举结果为准）。无其它配置改动（`MODEL_ENDPOINT` / RPM / TPM 均未改） |
| 代码改动 | **0**。`backend/app/**` 未动 |

## 2. 阶段 0：直连冒烟（共 2 次调用，≤5 上限内）

1. `GET https://ooioo.work/v1/models` → **HTTP 200**，2.88s，`{"data":[...5 models...],"success":true}`。
2. `POST /v1/chat/completions`（payload 与 `provider.py` 完全同形：system+user、`max_tokens=256`、`response_format={"type":"json_object"}`；user 消息按 `prompt.py` 模板手写一个真实画像）→ **HTTP 200**，11.73s：

```
top_level_keys = [choices, created, id, model, object, service_tier, usage]
usage          = {"prompt_tokens": 5013, "completion_tokens": 54, "total_tokens": 5067}
finish_reason  = stop
raw_content    = '{"question_id":"purchase_intent","value":"probably_not","reason":"预算有限，…399元偏贵。"}'
```

- 严格 JSON 五档解析：**通过**（按 `validation.py` 同规则手工校验，无额外字段、value 合法）。
- `response_format=json_object` 被网关遵守（输出为纯 JSON，无围栏、无前导文本）。
- usage 字段存在（`prompt_tokens/completion_tokens`；provider 侧命名，入库时映射为 `input_tokens/output_tokens`）。
- 错误形态：本轮未触发 4xx/5xx/429（100 并发下的真实错误形态见 §3.4）。

**观察（未查成因，仅记录）**：单请求 `prompt_tokens` 计量在 **4,311–5,155** 区间（见 §3.3），而实际 user 消息 JSON 约 1.5–2k 字符（粗估 1–1.5k tokens），计量显著高于字面消息规模；可能是网关/上游计入额外开销。**未验证**，按「未知计费」口径记账不影响平台行为。

## 3. 阶段 1：100 人真实试点

### 3.1 全流程

`compose.real` 栈（api/worker 覆盖 `real.env`，postgres 无宿主端口）一次拉起成功；`/api/v1/health/ready` = ok。worker 容器实测 env：`MODEL_PROVIDER=openai_compatible`、`MODEL_ENDPOINT=https://ooioo.work/v1`、`MODEL_NAME=gpt-5.6-terra`、`AGENT_CONCURRENCY=100`、`SURVEY_MODEL_API_KEY` 非空（未回显）。

完整链路（全部走 HTTP API）：
`POST /surveys`（201）→ 生成 100 行 CSV（列 user_id/age/city/gender/note）→ `POST /imports`（multipart + column_mapping，201，row_count=100，errors=0）→ `POST /runs/preview`（sample_size=100、target_concurrency=100、**cost_is_unknown=true**）→ `POST /runs`（**Idempotency-Key**，201，request_limit=300）→ `POST /runs/{id}/start`（202，Q6 门禁通过：真实 provider + key 非空）。

- run_id：`3ab0801b-cc83-413e-b2b8-a8629b0b0c89`
- started_at `02:12:35Z` → finished_at `02:25:41Z`（含暂停窗口）

### 3.2 运行过程与暂停事件（如实记录）

- t=4s 起 **in_flight=100**（满并发槽位全部占满，`in_flight` 观测峰值 = 100，与 `AGENT_CONCURRENCY=100` 一致）。
- **t≈12s 起部分成员开始失败**（突发 100 路并发下网关侧约 15% 失败）；t≈12–80s 期间 run 处于 `pausing`（在途收敛中）。
- **t=80.3s run 收敛为 `paused`，`pause_reason=api_unavailable`**（DB 原生 SQL 读得原值；`control_events_json` 无 user 干预）。成因：连续 5 次同类供应商失败触发主文档 §6.4 保护 —— **保护机制按设计工作**：`failed=0`，11 个未完成成员保留为 `retry_wait`，未执行成员无一被误判 failed。
- **恢复动作（§6.4 语义）**：`POST /runs/{id}/resume` → 202，`pause_reason` 归 NULL，11 个 retry_wait 成员重试。**未扩大规模**（仅完成原 100 人批次）。恢复后低并发下无新增 SERVER_ERROR 突发。
- 终态 **`completed_with_errors`**（如实下发，未被兜成 `completed`）：succeeded=99，failed=1（`p_000076`：3 次尝试 TIMEOUT/TIMEOUT/SERVER_ERROR，attempt 额度耗尽），pending/retry_wait/running 全 0。

### 3.3 门槛判定（主文档 §10，逐条实测数字）

| 门槛 | 判定 | 实测 |
|---|---|---|
| 最终有效率 ≥ 98% | ✅ | **99/100 = 99%**（valid=99，5 档计数之和 53+44+2 = 99 = valid，硬不变量成立） |
| 无 401/403 | ✅ | **AUTH 类错误 = 0**（attempts 错误码仅 SERVER_ERROR×10、TIMEOUT×11；无 AUTH / PROTOCOL / RATE_LIMITED） |
| 格式无效率 ≤ 2% | ✅ | **INVALID_OUTPUT = 0 / 119 attempts = 0%**（119 次真实调用中无一非法输出；`response_format=json_object` 全程被遵守） |
| 预算未耗尽 | ✅ | `requests_reserved=119 ≤ request_limit=300`，run 正常收敛。费用口径=**未知**：网关计价未知 → 价格留空，`unknown_cost_count=119`（每笔调用如实计数为未知）、`actual_cost=0.000000`、`reserved_cost=0.000000`（unknown≠0 语义由 unknown_cost_count 承载，与 §6.4/裁决一致） |

### 3.4 真实调用统计（DB 原生 SQL，attempts 表）

- attempts 总数 **119** = 100 首调 + 19 重试（请求预留 119 与之精确吻合）。重试分布：85 一次成功 / 14 次重试后成功（11×attempt2 + 3×attempt3）/ 1 三次全失败。
- 失败 attempts 构成：attempt1 = SERVER_ERROR×9 + TIMEOUT×6；attempt2 = TIMEOUT×4；attempt3 = SERVER_ERROR×1。
- **429/RATE_LIMITED：0 次**（本网关在该并发/配额下未回 429；限流窗口未成为瓶颈）。
- 延迟（99 个 succeeded attempts 的 `duration_ms`）：**min 5,971 / p50 23,586 / p95 45,453 / max 60,179 ms**。秒级～数十秒级；p50 远高于单请求空载延迟（冒烟 11.7s），主因是 100 路并发下网关排队。6 次 TIMEOUT 均为 60s 超时（`MODEL_TIMEOUT_SECONDS=60`）。
- 真实 usage：**usage_json 非空 = 99/99（100%）**；样本 `{"input_tokens": 4311, "output_tokens": 230}` 等；output_tokens 146–230 区间（≤ max_output_tokens=256，未发生截断）。provider_request_id 形如 `resp_…`。

### 3.5 导出核对

`GET /runs/{id}/export.csv` → 200，100 行。列：run_id,row_no,persona_id,age,city_tier,member_status,question_id,value,score,reason,attempt_count,error_code,model,prompt_version,as_of。
- 与 results API 逐行比对：**value 不一致 = 0 / 100**；`attempt_count`（1/2/3）与 DB 重试分布吻合。
- **无画像泄漏**：导出仅含派生字段（age/city_tier/value/score/reason），**不含** note/profile_text 等画像原文（全量扫描 0 命中）。
- 失败成员 p_000076 如实在导出中（member_status=failed、value 空、error_code 非空口径见 API），**未被隐藏、未被补值**（红线 #1）。

### 3.6 与 mock 的差异（实测对照）

| 维度 | mock（T7 实测） | 真实网关（本轮） |
|---|---|---|
| 单请求延迟 | 0.1–0.2s（注入） | **p50 23.6s / p95 45.5s**（含 100 路并发排队） |
| 突发 100 并发失败 | 0（mock 恒成功/按计划注入） | **~15% 首调失败**（SERVER_ERROR/TIMEOUT），触发 `api_unavailable` 暂停 |
| 输出格式有效率 | 100%（确定性合法输出） | **100%**（0/119 非法）——`response_format=json_object` 真实生效 |
| usage | mock 不产生 | **100% 返回**（prompt/completion tokens） |
| 费用 | mock 无费用 | **未知口径**（网关计价未知，unknown_cost_count 如实记账） |
| 暂停链路 | burst 场景由注入触发 | **真实触发**一次 `api_unavailable`（非注入），resume 后收敛、0 丢失 |

### 3.7 结论与建议

1. **四条门槛全部 ✅**（99% 有效率 / 0 AUTH / 0% 格式无效 / 预算未耗尽）。
2. 平台侧（并发槽位、租约重试、暂停-恢复、幂等、导出、错误分类）在真实供应商下**行为符合设计**。
3. **建议进入 1,000 人阶段**（仅建议，未执行、未获放行）。前置建议供裁决：
   - 100 路突发在真实网关下有 ~15% 首调失败并触发一次 `api_unavailable` 暂停；1k 规模下大概率多次暂停+resume。可考虑（裁决项）：初始并发阶梯爬升（如 30→100），或接受暂停-恢复语义照常工作（本轮已实证恢复无损）。
   - 费用仍为未知口径；按本轮 usage 外推 1,000 人 ≈ **5.0–5.4M tokens**（粗估，供预算决策；非承诺值）。
   - 网关 `prompt_tokens` 计量偏高成因未查（§2 观察），若需精确计费建议先向网关方核实计价口径。

### 3.8 未验证 / 假设项（显式声明）

- **1,000 / 10,000 人真实实测：未执行**（未获放行）。本报告任何结论**不外推**到更大规模。
- 网关 429 行为、TPM/RPM 真实上限：**未验证**（本轮 0 次 429，不证明无配额）。
- `prompt_tokens` 计量偏高成因：**未查**。
- 真实费用结算（网关侧实际扣费）：**未验证**。
- 100 路在途的**连续时间**上界：本轮为 4s 采样（峰值 in_flight=100），连续性证明沿用 T7 阶段结构性保证，未重复做连续测量。
- 长时内存/连接泄漏：未测（单批次试点范围外）。
