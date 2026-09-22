# 50 人真实运行独立复核报告（QA 严过关 · 尝试证伪）

> **复核者**：software-qa-engineer-2（严过关 / Yan）
> **日期**：2026-09-21
> **被复核对象**：`docs/real-run-50.md`（工程师 5 报告）+ 三份结果 JSON + DB 落库（三轮 run）
> **核对方式（零付费调用）**：① 只读 SQL（`docker exec survey-pg psql …`）；② 重新解析已落盘 JSON
> （脚本 `tests/load/qa_verify_real50.py`，**不含任何网络调用**）；③ 读源码与报告。
> **未执行**：未重跑 `run_real_50.py`、未写任何调用 `api.deepseek.com` 的脚本、**未跑 pytest**、未启动 worker。
> **付费调用次数：0**。
> **立场**：以**证伪**为目标。对每个数字先自算、再与报告比对；只采信"原始产物能独立复推"的结论。

---

## 0. 结论摘要

| 复核项 | 报告声称 | 我的独立结果 | 结论 |
|---|---|---|---|
| ① 限流器随 cap 收紧 | cap50→50 / cap8→**8**，`always_le_cap=true` | 复算一致；**R1b(cap=50) 档为同义反复**；**R2 提供真实证据** | **成立（需限定）** |
| ① 两处语义差异（50 vs 37/40） | 采样峰值 vs 区间重建，两种定义 | 独立复算：R1a 50/40、R1b 50/37、R2 8/8，**确为两种定义** | 成立 |
| ② 红线 #1 真实截断路径 | 3 条截断→`INVALID_OUTPUT`→重试，未补默认档 | SQL 复推一致；**无"答案凭空出现"** | **成立** |
| ③ `length=0`（4096） | R1b/R2 length=0；R1a 3 次 length 均撞 2048 上限 | JSON 复算一致 | 成立 |
| ④ `reasoning_tokens` 修复前后 | R1a=null，R1b=41525（≈94%），R2=7532；R1a 不可回溯 | 独立复算一致；占比 94.27% | 成立 |
| ⑤ 密钥 / 代码 / 越界 | 0 密钥泄漏；业务代码未改；不外推 | 复核通过（附 1 项工具缓存旁证） | 成立 |

**缺陷清单：0 × P0，0 × P1，2 × P2（均为报告措辞/证据精度，非结论错误），1 项信息性观察。**
**未发现可推翻报告总结论的证据。**

---

## 1. 逐项复核（声称 → 我的实验 → 原始输出 → 结论）

### ① 并发上限「随 cap 收紧」是否成立（最重要）

**报告声称（§0/§5.2/§5.3）**：cap=50 → 峰值 50；cap=8 → 峰值 **8**；三路（limiter / DB members / DB attempts）一致且恒 ≤ cap；
并由请求区间独立重建峰值（R1a 40 / R1b 37）；采样峰值与重建峰值是**两种不同定义**。

**我的实验**：不采信报告现成数字，直接从 `concurrency.series`（列 `t_offset_s, in_flight_limiter,
db_running_members, db_running_attempts`）逐点取最大；并对 `network.requests[].t_start/t_end` 做
区间重叠独立重建（半开与闭区间两种口径各算一遍）；同时检查**是否存在任何采样点 > cap**。

**原始输出**（`tests/load/qa_verify_real50.py`）：

```
R1a(2048) cap=50 : series peak in_flight/members/attempts = 50/50/50 ; rows with ANY col>50 = 0
                   rebuilt peak (half-open)=40  (closed)=40   ; report claims 50 / rebuilt 40  -> match
R1b(4096) cap=50 : series peak = 50/50/50                     ; rows with ANY col>50 = 0
                   rebuilt peak = 37                          ; report claims 50 / rebuilt 37  -> match
R2(cap8)  cap=8  : series peak = 8/8/8                        ; rows with ANY col>8  = 0
                   rebuilt peak = 8                           ; report claims 8 / rebuilt 8     -> match
```

**关键判定一：R1b 那一档是不是同义反复？—— 是。**
R1b 是 50 行样本、cap=50，**样本量本身即上限**：无论限流器是否存在，50 个成员全并发时"在途"必然是 50。
故 `cap=50 → 峰值 50` **不构成"限流器生效"的证据**（它甚至不能区分"限流器恰好卡在 50"与"根本没有限流器"）。

**关键判定二：R2 是否真的提供了限流器收紧的证据？—— 是，未被证伪。**
- **样本量 10 > cap 8**，故非样本量束缚，非同义反复；
- 时间序列**从未出现 >8**（`rows with ANY col>8 = 0`）；
- 请求区间独立重建（真 HTTP 重叠）= **8**（与采样峰值一致，但口径独立）；
- **机制级证据（滚动补位）**：10 个成员中 **8 个在 0.38–1.09s 内启动**，其余 **2 个直到首个请求
  完成后才启动**（R2 `rolling_refill`：`first_completion_offset_s = 2.5688`，
  `starts_strictly_after_first_completion = 2`，`max_wait = 49.46ms`）。若无 cap，第 9/10 个成员会与
  前 8 个同时在 1.1s 左右启动；实测它们等到 2.610s / 2.909s（首个完成于 2.569s 之后 41–340ms）。这正是
  槽位释放→补位的签名。

> **① 的明确结论**：**R2 确实提供了"限流器会把有效并发收紧到 cap"的可信证据，且未被证伪**；而
> **R1b(cap=50) 一档是无证据力的同义反复**。报告的最终结论（限流器收紧）成立，但把两档并列当证据的
> **措辞会误导**（见 P2-1）。

**两处语义差异核对**：
- **采样峰值（50） vs 区间重建峰值（R1a 40 / R1b 37）**：我分别按两种定义复算，数值确为 50 vs 40/37，
  **不是同一定义的两种叫法**——前者是每 100ms 读一次 `limiter.in_flight` 的**槽位占用**瞬时峰值，
  后者是请求 `[t_start,t_end]` 的**重叠**峰值。报告 §9.1 已分别定义，解释清楚。**成立。**
- **`duration_ms` p50(4579) vs `wire_ms` p50(228)（R1a）**：我从 JSON 复算，R1a 二者差距巨大、R1b 二者近似相等
  （wire 4093 ≈ duration 4094），与 §9.2"2048→4096 之间 `wire_ms` 语义因 `ProbeTransport` 修复而变化、
  跨轮不可比"的解释吻合。**成立。**

**追加观察（信息性）**：R2 的 `in_flight_limiter` 列在 t>4.7s 后（此时 DB 仅剩 1 个 running attempt）
出现 `8 ↔ 1` 振荡。这并非"限流器失效"（峰值仍=8、恒 ≤cap），而是该列度量的是**槽位占用**——worker 轮询时
会有消费者短暂占槽（领取/写库等非 HTTP 阶段）。报告 §9.1 已给出该语义，但 §5.3 的"三路一致"表述容易让读者
把该列当作与 DB 独立的第三证据（实际是同一限制器的不同投影）。见 P2-2。

---

### ② 红线 #1 在真实截断路径上的实证是否成立

**报告声称（§8）**：`max_tokens=2048` 首跑 3 次首调 `finish_reason=length`、`completion_tokens=2048`，
被判 `INVALID_OUTPUT` 并**重试**、**未补任何默认档位**；50/50 有效靠重试而非"编答案"。

**我的实验（只读 SQL）**：

```sql
-- 失败 attempt 的错误码分布
SELECT COALESCE(a.error_code,'NULL'), count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id
WHERE m.run_id='f6c6a643-…' GROUP BY 1;                 --  INVALID_OUTPUT|3   NULL|50
-- 成员答案编码
SELECT count(*), count(*) FILTER (WHERE answer_json IS NULL),
       count(*) FILTER (WHERE answer_json::text='null'),
       min(attempt_count), max(attempt_count) FROM run_members WHERE run_id='f6c6a643-…';
                                                         --  50 | 0 | 0 | 1 | 2
-- 证伪查询：有答案但「零成功 attempt」的成员
SELECT … WHERE rm.answer_json IS NOT NULL AND
  (SELECT count(*) FROM attempts a WHERE a.member_id=rm.id AND a.status='succeeded')=0;  --  0 行
-- attempt_count 与实际 attempt 行数不一致的成员            --  0 行
```

**原始输出（3 个截断成员）**：

| persona | 失败 attempt1 | attempt2 |
|---|---|---|
| `ILB_5a7d63f3cfaf7a6a` | `INVALID_OUTPUT`, usage `output=2048`, raw_output 99 字符被截断 JSON | `succeeded`, `stop`, output=1276 |
| `ILB_434713078788e9ce` | `INVALID_OUTPUT`, usage `output=2048`, raw_output **空** | `succeeded`, `stop`, output=875 |
| `ILB_b4d2bd4612abf2a5` | `INVALID_OUTPUT`, usage `output=2048`, raw_output **空** | `succeeded`, `stop`, output=370 |

三人最终 `answer_json` 均为**真实五档值**（`unsure` / `probably_not` / `probably_not`），且与 attempt2
的 `raw_output` 正文一致 → 答案来自**重试的真实响应**，非兜底。

**结论：成立。** 证伪失败——
- 不存在"有答案却无成功 attempt"的成员（0 行）；
- `attempt_count` 与 attempt 行数完全一致（无错配/无凭空答案）；
- 有效答案的桶分布独立复推与报告一致：R1a `pn17/unsure6/py27`、R1b `pn16/unsure5/py29`、R2 `pn7/unsure0/py3`。

**追加观察（信息性，不削弱结论）**：3 个失败 attempt 中**仅 1 个** `raw_output` 保留了截断 JSON 原文，
另 2 个为**空字符串**（非 NULL）。这与 §11"思维链吃满上限时正文可能为空"自洽，但**复核指引中"失败 attempt 的
`raw_output` 是被截断的 JSON 原文"这一期望仅 1/3 成立**（报告 §8 本身并未明确作此声明）。此项不改变红线 #1 结论，
因为答案可追溯到 attempt2 的真实响应。

---

### ③ `length=0` 是否真成立

**我的实验**：从 JSON 独立统计 `finish_reason` 分布，并逐个检查 `length` 记录是否 `completion_tokens == max_tokens`。

**原始输出**：

```
R1a finish_reason = {stop:50, length:3}   3 条 length 全部 completion=2048 == req_max_tokens=2048  → True
R1b finish_reason = {stop:50}             0 条 length
R2  finish_reason = {stop:10}             0 条 length
req_max_tokens observed values（逐请求 100% 命中）: R1a [2048] / R1b [4096] / R2 [4096]
```

**结论：成立。** R1b/R2 确无 `length`；R1a 的 3 次 `length` 均同时满足 `completion_tokens == max_tokens(2048)`。

---

### ④ `reasoning_tokens` 修复前后对照

**我的实验**：独立从 JSON 统计 reasoning/output 总和、占比、正文均值。

**原始输出**：

```
R1a: reasoning 记录数 = 0/53   (全 None -> 未采集)
R1b: reasoning 记录数 = 50/50  sum=41525 ; output sum=44050 ; content(=out-reasoning)=2525
     占比 reasoning/output = 41525/44050 = 94.27% ; mean output=881.0 ; mean reasoning=830.5 ; mean content=50.5
R2 : reasoning 记录数 = 10/10  sum=7532  ; output sum=8075  ; mean 807.5 / 753.2 / 54.3
R1a 正常 calls 平均 output（剔除 3 次截断）= 774.8
```

**结论：成立（含 1 处口径说明）。**
- R1a 确为 `null`（未采集），R1b/R2 为真实数值；**占比我自算 = 94.27%**，与"≈94%"一致。
- "正常单次输出 ≈800–880、正文仅 ≈50 tok"由 **R1b/R2** 数据支撑（881/50.5、807.5/54.3）；
  R1a 正常均值 **774.8** 略低于 800（因其为截断轮），故该区间应理解为 **R1b/R2 口径**，非全轮普适。
- **R1a reasoning 确不可回溯**：SQL 复核 `attempts.usage_json` 仅含 `{input_tokens, output_tokens}`（R1a/R1b 均如此），
  适配器不落 reasoning；R1a 的 2/3 失败 attempt `raw_output` 还是空的 → 该轮 reasoning 永久缺失，报告如实声明。

---

### ⑤ 密钥、代码与越界检查

**我的实验**：对 `tests/load/`、`docs/real-run-50.md`、三份 JSON 做 `sk-` 与 `Bearer` 全量扫描；核对 `backend/app/**`
运行窗口内的 mtime；审读报告是否越界外推。

**原始输出**：

```
sk-[A-Za-z0-9]{4,} 命中：
  run_real_50.py / run_10000.py / real-run-50.md / 三 JSON = 0
  mock_provider.py = 1  →  假阳性（"task-list" 的子串 "sk-list"）
真实密钥：仅存在于 deploy/real.env（长度 35），全项目仅此 1 处命中（gitignored）
三 JSON 内 Authorization / Bearer 字段出现次数 = 0（请求头未持久化，故无泄漏面）；
        url 字段 = https://api.deepseek.com/chat/completions（无密钥）
backend/app/** mtime：运行窗口（20:48–20:53 本地）内 0 个文件被改（最新 mtime 02:01）
   旁证：backend/scripts/capability_check.py mtime 20:33（运行前）；backend/.ruff_cache 于 20:51 有更新（工具缓存，非业务代码）
provider_request_id：R1a 53/53、R1b 50/50、R2 10/10，全部非空且互异（佐证每请求均获真实服务端响应）
报告越界扫描：§1 / §13.8 明确「不外推 1,000/10,000」；429 / Retry-After / RPM·TPM / 费用 均标「未验证」
```

**结论：成立。** 无密钥泄漏（`sk-` 0 真命中）；业务代码在运行窗口内未被改动；报告**无越界外推**，未把未触发项
（429 / Retry-After / 真实 RPM·TPM / 费用）写成已验证。

---

## 2. 缺陷清单

### P0（阻断）：无

### P1（严重）：无

> 说明：本轮复核**未发现 P0/P1**。下列 P2 均为**报告措辞/证据精度**问题，**不影响任何总结论**。按任务要求，
> 不为"显得有产出"而编造缺陷。

### P2-1（报告措辞）：把 `cap=50 → 峰值 50` 与 `cap=8 → 峰值 8` 并列作为限流器证据
- **位置**：`docs/real-run-50.md` §0 第 17 行、§5.2 表、§5.3 首条 bullet。
- **问题**：cap=50 档的样本量（50）恰等于 cap，`峰值=50` 属**同义反复**，无证据力；两档并列会让读者误以为
  两档各自独立证明了限流器生效。
- **证据**：R1b 50 请求 / cap 50 → 峰值 50；R2 10 请求 / cap 8 → 峰值 8（样本量>cap，才有证据力）。
- **建议**：显式标注"cap=50 档受样本量束缚、不具区分力；限流器证据仅来自 R2"。

### P2-2（证据精度）：`in_flight_limiter` 单列不宜作为独立并发证据
- **位置**：§5.2/§5.3「三路一致」表述。
- **问题**：R2 的 `in_flight_limiter` 在 t>4.7s（DB 仅 1 running attempt）出现 `8↔1` 振荡，说明该列度量的是
  **槽位占用（含非 HTTP 的领取/写库阶段）**，与 DB 路并非完全独立；"三路一致"易被读作三条独立证据。
- **证据**：R2 `concurrency.series` 原始行（`in_flight=8` 与 `db_attempts=1` 同刻共存）。
- **建议**：在 §5 注明该列语义，并以"请求区间重建 = 8"作为更硬的独立证据。
- **注**：§9.1 已给语义解释，本项仅为 §5 的措辞强化建议。

### 信息性观察（非缺陷）
- 3 个失败 attempt 中 2 个 `raw_output` 为空字符串（见 ② 追加观察）。属"思维链吃满上限、正文为空"的合理表现，
  报告未就此作错误声明；若后续复核以此项为判据需注意其仅 1/3 成立。

---

## 3. 无法证伪 / 暂采信（继续跟踪，不得当作已证）

在**零付费调用**约束下，以下项我**无法独立证伪**，暂采信但标注：
1. **R1b/R2 的 `reasoning_tokens` 数值**来自响应原文的 `usage.completion_tokens_details.reasoning_tokens`
   （由 `ProbeTransport` 抓取）。我不重发请求，故无法与 provider 侧二次对账；仅能确证 JSON 内部自洽。
2. **真实网络目的地**：`url` 字段与 `provider_request_id` 支持"确由 api.deepseek.com 返回"，但我不做网络回放，
   故"确实打到该端点"依赖脚本实现与 53/50/10 个互异 request-id。
3. **RPM/TPM 真实配额、429/Retry-After 线格式、真实费用**——本轮未触发/未配置，报告已如实标"未验证"，我亦无法证伪或证实。
4. **`wire_ms` 语义跨轮变化**（§9.2）依赖"修复在 2048→4096 之间生效"这一时序事实；我从 R1a/R1b 数值差异
   间接印证（228 vs 4093），未做代码级时序注入复现。

---

## 4. 复现方式（只读，零网络）

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform
python3 tests/load/qa_verify_real50.py          # 仅解析 tests/load/results/*.json，无网络
# DB 只读核验（示例）
docker exec survey-pg psql -U postgres -d survey -tAc \
  "SELECT COALESCE(error_code,'NULL'),count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id \
   WHERE m.run_id='f6c6a643-d2ac-4474-94e7-560b0f815b32' GROUP BY 1;"
```

> 新增脚本 `tests/load/qa_verify_real50.py` 仅做 JSON 解析与统计，**不含任何真实网络调用**；未改动
> `backend/app/**`，未改动工程师产物。

---

## 5. 复核者声明

- **付费调用次数：0**（未重跑真实脚本、未新增任何调用 `api.deepseek.com` 的脚本）。
- **未跑 pytest**、**未启动 worker**（遵守测试库独占规则与服务约束）。
- 未修改 `backend/app/**`；未修改任何工程师产物（发现错误只报不修）。
- 本报告所有数字均来自**原始产物**（JSON 复算 / DB 原生 SQL），可独立复推。
