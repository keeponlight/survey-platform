# 真实第三方 API 启动前能力检查（主文档 §5.3）

> **范围**：本文件**只**记录「真实 provider 启动前能力检查」的实跑证据（逐项：检查项 → 实跑调用 → 原始输出（脱敏）→ 结论）。
> **不在本文件范围**：50 人批量、万级压测、任何业务代码改动 —— 均未执行/未涉及。
> **真实性声明**：以下结论均由**真实网络请求**产生（`https://api.deepseek.com`），非 mock。**未执行**的项一律标注「未验证」，不写成通过。
> **执行者**：software-engineer-4（Kou）。**执行时间**：2026-09-21（UTC 12:32）。

---

## 0. 方法与边界

- **适配器**：一律用**项目自身**的运行时适配器发请求，未另写裸 `httpx` 调用：
  `Settings.from_env()` → `build_provider(settings)` → `await provider.answer(ModelRequest(...))`。
  校验器用项目自身的 `app/inference/validation.py::parse_and_validate`。
- **配置来源**：`deploy/real.env`（红线 #3：密钥本体只在此文件）。脚本把该文件注入**进程环境**后调用
  `Settings.from_env()`，与生产 `compose` 的 `env_file` 行为一致（`config.Settings.api_key()` 运行时读 `os.environ`）。
- **密钥处置**：
  - 实测 `grep` 全项目：密钥本体 `sk-***` **仅**出现在 `deploy/real.env:11`（1 处命中；脚本/文档/JSON/日志均无）。
  - 所有记录的请求/响应把 `Authorization` 头脱敏为 `Bearer <redacted>`；脚本另做一次全局红线兜底（把密钥本体替换为 `<redacted-key>`）。
  - 本文件、回传报告**不含**密钥本体。
- **请求预算**：任务硬约束「本次能力检查总共不超过 6 次真实请求」。**实际发起 3 次真实请求**（见 §1 汇总），另有 1 次 **0 请求**的干跑仅用于打印配置头。
- **可复现脚本**：`backend/scripts/capability_check.py`（不含任何密钥字面量；`--max-requests N` 控制请求上限，硬上限 6）。

### 请求计数汇总（逐次列出）

| # | 标签 | 真实请求 | 结果 |
|---|---|---|---|
| 1 | `A_normal_deepseek_flash` | 是 | HTTP 200，校验器通过 |
| 2 | `B_wrong_key` | 是 | HTTP 401 → `AUTH` |
| 3 | `C_missing_model` | 是 | HTTP 400 → `PROTOCOL` |
| — | 干跑（打印配置头） | **否**（0 请求） | 仅输出 settings/provider 选择 |

**真实请求合计 = 3**（≤ 6）。**消耗 token 合计：input 585 / output 139**（仅请求 A 成功计费；B、C 均为 4xx，`usage` 缺失）。

### 生效配置（脱敏）

```json
{
  "env_file": "deploy/real.env",
  "settings": {
    "provider": "openai_compatible",
    "model": "deepseek-flash",
    "endpoint": "https://api.deepseek.com",
    "model_config_id": "prod-deepseek-flash",
    "api_key_env": "SURVEY_MODEL_API_KEY",
    "api_key_present": true,
    "api_key_len": 35,
    "timeout_seconds": 60.0,
    "max_output_tokens": 256,
    "rpm": 600,
    "tpm": 1000000,
    "agent_concurrency": 100
  },
  "provider_class": "OpenAICompatibleProvider",
  "is_model_configured": true
}
```

**结论**：`MODEL_PROVIDER=openai_compatible` 使 `build_provider` 返回 `OpenAICompatibleProvider`（非 mock 分支），`is_model_configured()=true`。**通过**。

---

## 检查项 1｜鉴权是否通过

**调用**：请求 A（正确 key）+ 请求 B（故意错 key `sk-wrong-key-capability-check-0000`）。

**原始输出（脱敏）**：

- 请求 A（正确 key）→ **HTTP 200**，正常返回（详见检查项 3）。
- 请求 B（错 key）→ **HTTP 401**，响应体：

```json
{"error":{"message":"Authentication Fails, Your api key: ****0000 is invalid","type":"authentication_error","param":null,"code":"invalid_request_error"}}
```

适配器映射结果（`ModelResponse.error`）：

```json
{
  "raw_text": "{\"error\":{\"message\":\"Authentication Fails, Your api key: ****0000 is invalid\",...}}",
  "input_tokens": null, "output_tokens": null, "provider_request_id": null,
  "finish_reason": null, "duration_ms": 131,
  "error": {"error_code": "AUTH", "message": "provider returned HTTP 401",
            "http_status": 401, "retry_after_seconds": null}
}
```

**结论**：**通过**。正确 key → 200；错 key → 401，且项目分类器（`provider.py:50-59 classify_http_status`）把 401 正确归为 **`AUTH`**（非 RATE_LIMITED/非 SERVER_ERROR）。

> 观察（低风险，非本项目缺陷）：DeepSeek 在 401 错误体里**回显密钥尾部 4 位**（`****0000`）。适配器会把它存进 `raw_text`（`provider.py:179`），理论上可流入 `attempts.raw_output`。仅末 4 位、非完整密钥，**不构成红线 #3 泄漏**，但见 §4 观察 O2。

---

## 检查项 2｜模型名是否有效

**调用**：请求 A（`deepseek-flash`，正确 key）+ 请求 C（故意不存在的模型名 `deepseek-does-not-exist-xyz-2026`，正确 key）。

**原始输出（脱敏）**：

- 请求 A → **HTTP 200**，响应体 `"model":"deepseek-flash"`（服务端回显所服务模型名）。

- 请求 C → **HTTP 400**，响应体：

```json
{"error":{"message":"The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed deepseek-does-not-exist-xyz-2026.","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}
```

适配器映射结果：

```json
{"error": {"error_code": "PROTOCOL", "message": "provider returned HTTP 400",
           "http_status": 400, "retry_after_seconds": null}, "duration_ms": 148}
```

**结论**：**通过**。
- **`deepseek-flash` 模型名有效**（200，且服务端回显 `model=deepseek-flash`）。→ **是**，可用。
- 供应商对「不存在的模型名」返回 **HTTP 400（不是 404）**；项目分类器把 400 归为 **`PROTOCOL`**（`provider.py:59` 的兜底分支）。
- 附带情报：供应商自报当前支持的模型名为 **`deepseek-flash`、`deepseek-v4-pro`**（两者）。

---

## 检查项 3｜返回内容在哪个字段（响应 JSON 结构）

**调用**：请求 A 的响应体原文（脱敏后，逐字节保留结构）。

**原始输出（响应体，已脱敏）**：

```json
{
  "id": "135e02c4-8f79-4314-b2db-74a8e7dd012a",
  "object": "chat.completion",
  "created": 1789993936,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "{\"question_id\":\"purchase_intent\",\"value\":\"probably_not\"}",
        "reasoning_content": "We need answer as persona. ... Ensure no extra."
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 585,
    "completion_tokens": 139,
    "total_tokens": 724,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 124},
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 585
  },
  "system_fingerprint": "aeb56401ca74e127821c4f9126dcb669"
}
```

**顶层键**：`id / object / created / model / choices / usage / system_fingerprint`
**`choices[0]` 键**：`index / message / logprobs / finish_reason`
**`message` 键**：`role / content / reasoning_content`

**结论**：**通过**。
- 答案正文在 **`choices[0].message.content`** —— 与适配器读取路径（`provider.py:213-214`）一致。
- **存在 `reasoning_content`**（本模型会输出思维链）；适配器**有意忽略**它，只取 `content`（符合设计：只校验规定 JSON）。
- `finish_reason = "stop"`（正常结束，未截断）。
- `provider_request_id` 取自顶层 `id`；映射到 `ModelResponse.provider_request_id = "135e02c4-…-74a8e7dd012a"`。

---

## 检查项 4｜是否支持严格 JSON 模式 + 本项目严格校验器

**调用**：请求 A。`output_schema` 非空（本项目 `build_model_request` 恒传），适配器因此带上
`"response_format": {"type": "json_object"}`（`provider.py:148-149`）。

**原始请求（脱敏，节选 payload）**：

```json
{
  "model": "deepseek-flash",
  "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "max_tokens": 256,
  "response_format": {"type": "json_object"}
}
```

请求头（脱敏）：`"authorization": "Bearer <redacted>"`、`"content-type": "application/json"`、`"user-agent": "python-httpx/0.28.1"`。

**原始响应**：HTTP 200；`content = "{\"question_id\":\"purchase_intent\",\"value\":\"probably_not\"}"`。

**本项目严格校验器实跑结果**（`app/inference/validation.py::parse_and_validate`）：

```json
{
  "validator": {
    "ok": true,
    "validated": {"question_id": "purchase_intent", "value": "probably_not", "reason": null, "score": 2}
  }
}
```

**结论**：**通过**。
- `response_format={"type":"json_object"}` **被接受**（未被 4xx 拒绝，200 返回）。
- 返回的 `content` **可被本项目严格校验器直接解析**为合法 `ValidatedAnswer`（`value=probably_not`，派生 `score=2`）。
- **这是能否批量跑的前提，已通过。**
- **未验证**：本次未做「不带 `response_format`」的对照实验，故**不能**宣称「该参数改变了输出」；只能说「带上它时返回仍为合法 JSON 且校验器通过」。

---

## 检查项 5｜token usage 是否存在

**调用**：请求 A 响应体的 `usage`（原文见检查项 3）。

**原始输出（`usage` 字段）**：

```json
"usage": {
  "prompt_tokens": 585,
  "completion_tokens": 139,
  "total_tokens": 724,
  "prompt_tokens_details": {"cached_tokens": 0},
  "completion_tokens_details": {"reasoning_tokens": 124},
  "prompt_cache_hit_tokens": 0,
  "prompt_cache_miss_tokens": 585
}
```

适配器映射（`ModelResponse`）：`input_tokens = 585`（取自 `usage.prompt_tokens`）、`output_tokens = 139`（取自 `usage.completion_tokens`）。

**结论**：**通过**。`usage` **存在且可读**，字段名与适配器读取路径（`provider.py:229-230`）吻合 ⇒ 费用可结算、未知费用**不会**因字段缺失而被记为 `unknown`（就本模型/本次调用而言）。

> ⚠️ **记账要点（给批量任务）**：`completion_tokens`（139）**已包含** `completion_tokens_details.reasoning_tokens`（124）——即本模型是**带思维链**的，计费输出 token 含 reasoning。按输出单价结算时应以 `completion_tokens` 为准。

---

## 检查项 6｜timeout 与 429 的格式

**配置的超时值**：`MODEL_TIMEOUT_SECONDS=60`（`settings.timeout_seconds = 60.0`，传递到 `httpx.Timeout` 与每次 `client.post(timeout=...)`）。
- **本次未触发超时**（3 次调用耗时均 ≪ 60s：A 正常返回，B `duration_ms=131`，C `duration_ms=148`）。
- **超时的实际线格式：未验证**（未构造慢响应）。

**429 格式**：**本次未触发 429，未验证**。未主动打爆限流（遵守「不压测探配额」约束）。
**`Retry-After` 头**：本次三个响应头中**均未出现** `Retry-After`（A/B/C 响应头已逐条记录，未见该头）。故 `parse_retry_after`（`provider.py:62-70`）**未被实际触发**。

**结论**：**部分未验证**。已确认的是**配置的超时值 = 60s**；**429 响应体结构与 `Retry-After` 均为「本次未触发，未验证」**——不编造。

---

## 检查项 7｜RPM / TPM 配额

**结论**：**未验证**。仅发起 3 次单点调用，**无法**推断 RPM/TPM 配额；且**未**为探测配额而压测（遵守约束）。
`deploy/real.env` 中 `MODEL_RPM=600` / `MODEL_TPM=1000000` 为**保守启动基线值**，**不代表**供应商真实配额。

---

## §4 适配器观察与风险（**报告，未改代码**）

> 说明：**未发现** `app/inference/provider.py` 的代码缺陷（file:line 级）。以下两条为**观察/风险**，供主理人决策是否另开任务，**本任务未改动任何业务代码**。

| 编号 | 级别 | 事实（附 file:line） | 建议 |
|---|---|---|---|
| **O1** | **中（影响批量成功率）** | `deepseek-flash` 是**带思维链**模型：`message.reasoning_content` 非空，`completion_tokens(139)` **含** `reasoning_tokens(124)`。适配器把 `request.max_output_tokens` 直传 `max_tokens`（`provider.py:146`）。当前 `MODEL_MAX_OUTPUT_TOKENS=256`：本次 124 reasoning + 15 内容 = 139，尚有余量；但**较长画像**可能把 256 用尽 → `finish_reason="length"` → `content` 为截断 JSON → 严格校验器判 `INVALID_OUTPUT` → 成员失败。 | **主理人已裁定（`TEAM-BRIEF.md §7.7`）：批量前输出上限上调到 `2048`**（`max_tokens` 仅为上限、按实际 token 计费，放宽不增成本），转运行任务执行；并观察 `finish_reason` 分布。**非代码缺陷**，属配置调优。 |
| **O2** | **低（红线 #3 卫生）** | 401 错误体回显密钥**末 4 位**（`****0000`）；适配器把非 200 响应体原文存入 `raw_text`（`provider.py:179`），可流入 `attempts.raw_output`。 | 仅末 4 位、非完整密钥，不构成泄漏。**主理人裁定：暂缓**（本轮真实运行期间不改适配器，以保持「运行时测的代码」与「能力检查验证过的代码」为同一份）；记为项目待办（`TEAM-BRIEF.md §7.7`），运行结束后另开小任务做尾部脱敏。 |

**其它已确认正常的行为**：
- 非 200 分支正确保留 `http_status` 与原始错误体（脱敏后）（`provider.py:176-189`）。
- 400（模型不存在）→ `PROTOCOL`；401/403 → `AUTH`；映射符合 `task-list.md §8.1`。

---

## §5 未验证项清单（**不得当作已证**）

1. **429 限流响应体结构** —— 本次未触发，未验证。
2. **`Retry-After` 头解析** —— 本次未出现该头，`parse_retry_after` 未实际执行。
3. **超时的实际线格式** —— 本次未触发超时（配置值 60s 已知）。
4. **RPM / TPM 真实配额** —— 未压测探测，未验证。
5. **`response_format` 的“因果”作用** —— 未做「不带该参数」的对照实验（只验证「带上它时校验器通过」）。
6. **严格 JSON Schema 模式**（`json_schema` 而非 `json_object`）—— 适配器只发送 `{"type":"json_object"}`，未测试供应商是否支持 `json_schema` 严格模式。
7. **多轮/长上下文下的稳定性、`finish_reason="length"` 发生率** —— 未验证（建议批量任务实测）。
8. **100 路真实并发** —— 本任务未发起；单次调用不涉及并发。

---

## §6 复现方式

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/backend

# 干跑（0 请求）：打印生效配置与 provider 选择
uv run python scripts/capability_check.py --max-requests 0

# 完整能力检查（3 次真实请求；--out 落盘脱敏证据 JSON）
uv run python scripts/capability_check.py --max-requests 3 --out /tmp/capability_evidence.json
```

前置：`deploy/real.env` 已按真实供应商填写；脚本不含密钥，密钥仅经环境注入读取。

---

## §7 一句话结论（给主理人）

| 问题 | 答案 |
|---|---|
| `deepseek-flash` 是否可用？ | **是**（HTTP 200，服务端回显 `model=deepseek-flash`；供应商自报支持 `deepseek-flash`、`deepseek-v4-pro`） |
| 鉴权是否通过？ | **是**（正确 key→200；错 key→401→`AUTH`） |
| 严格 JSON 模式 + 本项目校验器能否跑通？ | **能**（`response_format={"type":"json_object"}` 被接受；`content` 通过 `parse_and_validate`） |
| usage 是否可读？ | **可读**（`prompt_tokens=585` / `completion_tokens=139`；含 `reasoning_tokens=124`） |
| 实际请求次数 / token | **3 次**真实请求；**input 585 / output 139** |
| 适配器代码缺陷？ | **无**（file:line 级）；有 2 条**观察/风险**（O1 输出上限、O2 密钥尾回显） |
| 未验证项 | 429 结构、Retry-After、超时线格式、RPM/TPM 配额、json_schema、对照实验、长上下文 —— 见 §5 |
