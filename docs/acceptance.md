# T7 万级验收与实测报告（acceptance.md）

> 本文件对应主文档 `2026-09-20-survey-platform.md` **§10 T7** 与 **§11 最终验收清单**，
> 以及 `docs/architecture.md` §5（Mock Provider）、§6.1–§6.7（可靠执行 / 100 路并发可验证性）。
>
> **诚实性约束（本文件贯穿始终）**：只写**可被原始输出或 SQL 结果直接印证**的结论；
> 无法在本机验证的一律标注「**未执行**」或「**未验证**」；**不使用**「全部 / 已确认 / 全项目」
> 这类全称表述，除非附覆盖全项目的原始输出。**不美化**。

---

## 0. 元信息

| 项 | 值 |
|---|---|
| 脚本 | `tests/load/run_10000.py`（T7 压测入口，复用 `tests/load/mock_provider.py`） |
| 结果落盘 | `tests/load/results/{normal,recoverable,permanent,interference,burst}_<size>.json` |
| 隔离压测库 | `survey_test_t7`（PostgreSQL 容器 `survey-pg`，`127.0.0.1:55432`） |
| 万级实测（本轮，mock，独占窗口） | `normal`/`recoverable`/`permanent` @ **10,000**；`normal` @ **20,000**；`interference` @ **10,000**；`burst` @ **2,000**（文件 `*_10000.json` / `normal_20000.json` / `burst_2000.json`，汇总见 §2.7） |
| 小规模基线 | `--size 200`（三数据场景 + 干扰 + 突发，见 §2.1–§2.3）、`--size 60`（recoverable，见 §4.7） |
| 真实第三方 API 实测 | **未执行**（本机无凭据）——见 §1 |
| 样本来源 | 脚本内确定性生成（`user_id = p_000001 ...`），**非**真实用户表 |
| Provider | **mock**（运行时不注入故障；故障注入仅存在于 `tests/load/mock_provider.py`） |

### 测量机器配置（本机）

| 项 | 值 |
|---|---|
| CPU | Apple M1 Pro（`sysctl machdep.cpu.brand_string`），`hw.ncpu = 10` |
| 内存 | 16 GiB（`hw.memsize = 17179869184` 字节） |
| OS | macOS 15.7.5（arm64） |
| Python | 3.12.13（`uv` 托管） |
| PostgreSQL | 16.13（Debian，aarch64）容器化 |

---

## 1. 真实第三方 API 实测声明（红线 #2，**硬要求**）

> **本节结论：真实第三方 API 实测「未执行」。**

- **`AGENT_CONCURRENCY=100` 是并行执行槽位数**（不是样本数 / 机器数 / 进程数）。
- 主文档 §10 T7 要求「使用用户实际第三方 API 做 **100 → 1,000 → 10,000 人实测**，上一阶段达门槛再扩大」。
  **本轮未执行该三级实测**，原因是 **本机无第三方 API 凭据（无真实 key）**。
- 因此本项目现有的全部容量结果**均为 mock**：mock 只验证**平台自身**的领取 / 租约 / CAS /
  限流 / 重试 / 收敛 / 统计路径，**不证明任何真实供应商的吞吐、延迟、配额或输出格式**。
- **mock 容量测试 ≠ 真实模型运行。不得据此声称「真实万人模拟已完成」。**
- 主文档 §10 T7 的 **100 人门槛为「未执行的待办」**：
  - [ ] 最终**有效率 ≥ 98%** —— **未执行**
  - [ ] **无 401/403（认证/权限失败为 0）** —— **未执行**
  - [ ] **预算未耗尽** —— **未执行**
  - [ ] 1,000 人同等门槛 —— **未执行**
  - [ ] 10,000 人实测 —— **未执行**
- 主文档 §10 T7 亦明确：无凭据时「交付已验证的平台和 mock 容量结果，并将真实万人测试标为未执行」。
  本文件即为该交付物；**真实万人测试状态 = 未执行**。

---

## 2. 小规模基线验证（`--size 200` / `60`，mock）

复现命令（在 `backend/` 目录下）：

```bash
cd backend
uv run python ../tests/load/run_10000.py --scenario all --size 200 --latency 0.1
```

`--scenario all` = `normal` + `recoverable` + `permanent` + `interference` 顺序执行，
每个场景**独立 run**（各自独立批次、独立 `run_id`）。

### 2.1 三数据场景摘要（原始输出节选，`--size 200 --latency 0.1`）

| 场景 | final_status | valid/failed/cancelled | 重试分布 | attempts | 峰值在途(mock/db) | 耗时 | 速率 | 进程峰值 RSS |
|---|---|---|---|---|---|---|---|---|
| `normal` | `completed` | 200 / 0 / 0 | `{1:200}` | 200 | 26 / 69 | 3.03s | 66.1 rows/s | 129.5 MB |
| `recoverable` | `completed` | 200 / 0 / 0 | `{1:172, 3:28}` | 256 | 22 / 60 | 4.25s | 47.1 rows/s | 130.5 MB |
| `permanent` | `completed_with_errors` | 180 / 20 / 0 | `{1:180, 3:20}` | 240 | 20 / 59 | 4.23s | 47.3 rows/s | 130.9 MB |

> 三场景 `==> passed = True`；`[T7 SUMMARY] IS_PASS = YES`。
> 数据来源：`tests/load/results/{normal,recoverable,permanent}_200.json`（含逐项断言布尔值）。

### 2.2 干扰场景摘要（原始输出节选）

`tests/load/results/interference_200.json`：

| 步骤 | 结果 |
|---|---|
| `run_a.start` | `202`；重复 `start` → `409 INVALID_RUN_STATE` |
| `run_a.pause` | `202`；重复 `pause` → `200`；状态收敛为 `paused` |
| `run_a.resume` | `202`；重复 `resume` → `200` |
| `run_a.cancel` | `202`；重复 `cancel` → `200`；最终 `cancelled` |
| `run_a.start_after_cancel` | `409 INVALID_RUN_STATE` |
| 杀 worker（run_b） | 已成功 `54` 条落库；在途 `49` 条租约随崩溃遗留 |
| 重启 API（全新实例） | `GET /runs/{run_b}` 读到 `status=running`、`in_flight=49`、`succeeded=54`（**进度在 DB，不在进程内存**） |
| 过期租约恢复 | `scan_expired_once` 回收 `49` 条 |
| 恢复后收敛 | `completed`，`succeeded=200`，**`lost=0`**，恢复耗时 `2.79s`（该场景实测） |
| 在途上限 | 杀 worker 前峰值 mock=22 / db=65（**≤ 100**） |
| API p95（10 并发，200 请求） | `run_p95=0.0149s`、`summary_p95=0.0187s`（**均 < 1s**） |

干扰动作覆盖：**杀 worker（`serve` 任务取消 + 释放 advisory lock + 令在途租约过期）**、
**重启 API（重建 `create_app` 实例，无共享进程内存）**、**重复控制命令矩阵**
（`start` 重复→409；已达目标的 `pause`/`resume`/`cancel` 重复→200；取消后 `start`→409）。

### 2.3 突发场景 `burst`（§6.4 暂停/恢复保护链路的自动化证据）

> **背景（需求内部冲突，TEAM-BRIEF §7.13 裁定）**：主文档 §10 T7 期望 `recoverable` **全量**注入后
> run 仍收敛为 `completed`；但按 `architecture.md` §5.2 **全量**注入会让**所有**成员首调同时失败，
> 触发主文档 §6.4「连续 5 次同类供应商失败 → 暂停 `api_unavailable`」这条**正确**的总中断保护
> （实测 `final_status="paused"`、`counts={"pending":126,"retry_wait":74}`）。**这是保护在正常工作，
> 不是缺陷**，**不得**为迁就该期望而削弱它。故：
> ① `recoverable` 改用**确定性间歇子集注入**（行序步长 → 连续同类失败恒 < 5）；
> ② 把「全量突发」**独立为 `burst` 场景**，用于证明 §6.4 的
> **暂停 + 保留未执行成员 + 修复配置后恢复**链路（主文档 §11 明确要求「暂停恢复」有自动化测试证据）。

命令：`uv run python ../tests/load/run_10000.py --scenario burst --size 200 --latency 0.05`
（原始输出见 `tests/load/results/burst_200.json`）。`burst` **内部固定用真实退避 2s/8s**
（与 CLI `--retry-backoff` 横幅无关），以忠实对应 §6.4「保留未执行成员，修复配置后恢复」。

| 步骤 | 结果 |
|---|---|
| `start` | `202` |
| 阶段一：全量突发（前 `normal_call_budget=30` 次恒合法，其后**全部** 429/503） | 收敛 `status="paused"`；`peak_in_flight` mock=20 / db=59（≤100） |
| **DB 原生 SQL 读原值** `runs.status` / `runs.pause_reason` | `status="paused"`、`pause_reason="api_unavailable"` |
| 未执行成员被保留 | `counts={pending:123, retry_wait:47, succeeded:30}` → **preserved=170**；**`failed=0`（无成员被误判为 failed）** |
| 突发前已成功（确定性） | `succeeded=30` == `normal_call_budget`（**确定值**，不随并发时序变化） |
| 突发调用日志 | `total=77`，`statuses={ok:30, error:47}`，`errors={SERVER_ERROR:21, RATE_LIMITED:26}`；**非 ok 调用携带档位值的条数=0**（不产默认答案） |
| 阶段二：`provider.recover()` + `POST /runs/{id}/resume` | `202` |
| 恢复后收敛 | `final_status="completed"`、`succeeded=200`、**`lost=0`（不丢任何已成功答案）**、恢复耗时 `2.79s` |

`burst` 场景全部检查项（26 项，含 6 条通用不变量与 `no_default_fill_db`）**均 PASS**。

> **数值稳定性说明（如实）**：`succeeded=30`（=`normal_call_budget`）、`preserved=170`、`failed=0`、
> `pause_reason="api_unavailable"`、`lost=0` 是本场景的**不变量**，多次运行均成立。但
> `pending` 与 `retry_wait` 的**拆分比例**、以及突发错误的 429/503**构成**取决于调度时序（何人撞上突发），
> **每次运行会变**（例如另一次运行为 `pending:105 / retry_wait:65`、`{RATE_LIMITED:39, SERVER_ERROR:26}`）。
> 上表数值取自本轮**最终一次**运行（`tests/load/results/burst_200.json`）。
> **诚实声明**：`burst` 模拟的是**外部供应商瞬时宕机**这一**时间相位**事件，故障由「是否处于突发窗口」
> 决定，**不**按 `(persona_id, attempt_no)` 采样；「哪些成员恰好撞上突发」取决于调度时序，
> **不是** per-member 确定的 —— 这是外部宕机场景的**固有性质**，本场景**不**假装可 per-member 复现。
> 但相位切换由**确定性调用计数**驱动，故「突发前成功人数」= `normal_call_budget` 是**确定**的。

### 2.4 R2 两条同时断言（每个数据场景都跑）

**R2(a) 故障按计划触发**——核对 `MockCallLog` 的 `(persona_id, attempt_no, status, error_code)`：

| 场景 | 期望调用总数 | 实际调用总数 | 序列不符 persona | mock status 计数 | mock error 计数 |
|---|---|---|---|---|---|
| `normal` | 200 | 200 | 0 | `{ok:200}` | `{}` |
| `recoverable` | 256 | 256 | 0 | `{ok:200, error:28, invalid:28}` | `{RATE_LIMITED:20, SERVER_ERROR:8, INVALID_OUTPUT:28}` |
| `permanent` | 240 | 240 | 0 | `{ok:180, invalid:60}` | `{INVALID_OUTPUT:60}` |

**R2(b) 该链路上没有任何补值分支产出有效答案**（mock 侧 + DB 原生 SQL 双侧）：

| 断言（应为 0） | normal | recoverable | permanent |
|---|---|---|---|
| mock 非 ok 调用携带档位值 | 0 | 0 | 0 |
| mock ok 调用携带错误码 | 0 | 0 | 0 |
| db `succeeded` 值不等于任何 ok 调用 | 0 | 0 | 0 |
| db `succeeded` 但无任何 ok 调用 | 0 | 0 | 0 |

> 说明：`recoverable` 的失败流为**间歇**（按 persona 确定性选取约 1/3 子集注入
> 「attempt 1 错误 / attempt 2 非法输出 / attempt≥3 合法」），实证「最大连续同类失败 < 5」，
> **未触发**主文档 §6.4「连续 5 次同类供应商失败 → 暂停 `api_unavailable`」这条**正确**的
> 总中断保护。全量 `recoverable` 注入会触发该保护而使批次**停**而非收敛 —— 这是**实测发现**，
> 已在脚本注释中如实记录（见 §5）。

### 2.5 DB 原生 SQL 抽查（`succeeded` 成员 `answer_json` 全为合法五档）

库：`survey_test_t7`，命令：
`docker exec survey-pg psql -U postgres -d survey_test_t7 -c "<SQL>"`。

**抽查 1 —— 合法五档 + 固定 question_id：**

```
                run_id                |        status         | succeeded | legal | illegal
--------------------------------------+-----------------------+-----------+-------+---------
 dfaa523d-... (normal)                | completed             |       200 |   200 |       0
 85b95ae3-... (recoverable)           | completed             |       200 |   200 |       0
 e2bf0e3d-... (permanent)             | completed_with_errors |       180 |   180 |       0
 ff3bcadf-... (interference run_a)    | cancelled             |         0 |     0 |       0
 77f4f19d-... (interference run_b)    | completed             |       200 |   200 |       0
 32ceeb40-... (burst)                 | completed             |       200 |   200 |       0
```

**抽查 2 —— 答案可归因于某次合法成功尝试（无补值分支）：**

```
                run_id                | succeeded | no_matching_ok | failed_with_answer
--------------------------------------+-----------+----------------+--------------------
 dfaa523d-... (normal)                |       200 |              0 |                  0
 85b95ae3-... (recoverable)           |       200 |              0 |                  0
 e2bf0e3d-... (permanent)             |       180 |              0 |                  0
 ff3bcadf-... (interference run_a)    |         0 |              0 |                  0
 77f4f19d-... (interference run_b)    |       200 |              0 |                  0
 32ceeb40-... (burst)                 |       200 |              0 |                  0
```

（以上为本轮**最终一次** `--scenario all --size 200` 落库后的实测输出；run 的 UUID 每次运行会变，
计数与合法/非法统计为不变量。）

结论：本库内**已执行的 200 规模运行**中，`succeeded` 成员的 `answer_json` 值全部命中
`('definitely_not','probably_not','unsure','probably_yes','definitely_yes')`、
`question_id='purchase_intent'`，且都能由某次 `status='succeeded'` 的 attempt 的
`raw_output` 解释；`failed` 成员 `answer_json` 均为 NULL。**此为 200 规模本库抽样结论，
不外推到 10,000 / 20,000（未执行）。**

### 2.6 突发场景的 DB 原生 SQL 证据（`burst`）

**（a）暂停时刻的 `pause_reason` 原值（脚本内 DB 原生 SQL 读取，断言来源即此）：**

脚本断言**不读 API 出参**，而是执行原生 SQL：

```sql
SELECT status, pause_reason FROM runs WHERE id = :run_id;
```

暂停时刻读出原值（录于 `tests/load/results/burst_200.json` 的 `burst.db_run_row`）：
`{"status": "paused", "pause_reason": "api_unavailable"}`。

> **诚实说明**：`resume_run()` 按 §6.4 语义会**清空** `pause_reason`（`run.pause_reason=None`），
> 故**事后**用 `psql` 直查该 run 只会读到 `pause_reason=NULL`。暂停时刻的原值由脚本在暂停点**即时**用
> 上述原生 SQL 捕获并落盘，这是本项目可提供的最强证据形式。

**（b）事后再用 `psql` 独立复核（该 run 的终态与尝试级错误，均与脚本结论一致）：**

```
                  id                   |  status   | pause_reason | sample_size
--------------------------------------+-----------+--------------+-------------
 32ceeb40-838b-4a45-8b78-38f5ba8a938d | completed |              |         200

  status   |  error_code  | count
-----------+--------------+-------
 failed    | RATE_LIMITED |    26
 failed    | SERVER_ERROR |    21
 succeeded |              |   200
```

- 26 条 `RATE_LIMITED` + 21 条 `SERVER_ERROR` = **47 次突发失败**，与脚本记录的
  `burst.call_log.errors={SERVER_ERROR:21, RATE_LIMITED:26}`（`error:47`）**逐一吻合**
  → 独立证实「突发确实发生在 DB 层（`attempts` 表）」。
- 该 run 终态 `completed`、`run_members` 全为 `succeeded` → 证实「消除故障源 + `resume` 后完整收敛」。

### 2.7 万级实测汇总（本轮，mock，独占窗口）

> 以下为本轮在**独占整机窗口**下、**前台逐个**执行的大规模运行实测值。所有场景 `==> passed = True`、`IS_PASS = YES`。
> 数据来源：`tests/load/results/` 下 `normal_10000.json`、`recoverable_10000.json`、`permanent_10000.json`、
> `normal_20000.json`、`interference_10000.json`、`burst_2000.json`。命令统一
> `cd backend && uv run python ../tests/load/run_10000.py --scenario <s> --size <n> --latency 0.2`。

| 场景 | 规模 | final_status | valid/failed | 重试分布 | attempts | 峰值在途(mock/db) | 耗时 | 速率 | peak RSS | DB 体积 | 连接(峰值/active/lock_waiters/max) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `normal` | 10,000 | `completed` | 10000 / 0 | `{1:10000}` | 10000 | 41 / 74 | 110.7s | 90.3 rows/s | 178.7 MB | 55 MB | 56 / 56 / 47 / 100 |
| `recoverable` | 10,000 | `completed` | 10000 / 0 | `{1:8572, 3:1428}` | 12856 | 40 / 76 | 137.2s | 72.9 rows/s | 178.3 MB | 58 MB | 56 / 56 / 47 / 100 |
| `permanent` | 10,000 | `completed_with_errors` | 9000 / 1000 | `{1:9000, 3:1000}` | 12000 | 37 / 73 | 127.2s | 78.6 rows/s | 180.8 MB | 58 MB | 56 / 56 / 47 / 100 |
| `normal` | 20,000 | `completed` | 20000 / 0 | `{1:20000}` | 20000 | 44 / 74 | 238.2s | 84.0 rows/s | 232.3 MB | 70 MB | 56 / 56 / 47 / 100 |
| `interference` | 10,000 | run_a `cancelled` / run_b `completed` | 10000 / 0 | — | — | 44 / 58（崩溃前峰值） | run_b 恢复 104.3s | — | 178.1 MB | 62 MB | 56 / 56 / 47 / 100 |
| `burst` | 2,000 | `paused`→`completed` | 2000 / 0 | — | — | 41 / 60（暂停时刻峰值） | 恢复 17.2s | — | 138.9 MB | 21 MB | 55 / 55 / 47 / 100 |

要点（逐条可在对应 JSON 中核对）：

- **`--latency 0.1/0.2`（子秒）下在途峰值未达 100**：db 侧 73–76、mock 侧 37–44（10k/20k）；`normal`@20k 与 @10k 的 db 峰值几乎相同（74 vs 74），**未随规模上升**（根因见 §4.3.1）。⚠️ **此为该延迟取值的表现**：**L=3s（§7 官方算例口径）下实测在途峰值 = 100**（见 §2.8），故「未达 100」**不可外推为并发能力不足**。
- **`lock_waiters` 峰值恒为 47**（所有场景/规模一致），约为连接峰值（55–56）的 84% —— 见 §4.3.1 与 §5 实测发现 2。
- **连接峰值 55–56 < `max_connections=100`**，未触顶；T6 的连接池修复（每引擎 10+20=30）后无连接泄漏。
- **`permanent` 的 1000 失败为预置 `INVALID_OUTPUT`**（`failed_error_codes={INVALID_OUTPUT:1000}`），`valid=9000==size-1000`，符合「永久失败不补值、明确归类」预期；`completed_with_errors`。
- **API p95 @10k（§11 证据，`interference_10000.json`）**：在**已完成** run_b 上 10 并发 × 200 请求，`run_p95=0.0283s`、`summary_p95=0.0738s`（**均 < 1s**）。
- **恢复不丢答案**：`interference` run_b `lost=0`（回收 58 条过期租约、恢复 104.3s）；`burst` `lost=0`（恢复 17.2s、`preserved=1970`、`succeeded_before=30`）。

### 2.8 `--latency 3`（对齐主文档 §7 官方算例 L=3s）：100 槽位真实在途实测（本轮纠正核心）

> **为什么要补跑这一条**：§3.1 第 1 项「恰有 100 个请求同时在途」原被判「**未达标**」，唯一依据是
> `--latency 0.1/0.2`（子秒级）场景下在途峰值只有 37–44。**该推论无效**，理由可算：稳态关系为
> **在途 ≈ 完成速率 × 单请求延迟**，而完成速率上限 = `min(领取路径上限, AGENT_CONCURRENCY / L)`。
> 六个子秒场景里 `C/L = 100/0.2 = 500 rows/s`，**远高于**领取路径实测上限（≈90 rows/s）⇒ 瓶颈落在**领取路径**，
> 在途自然只能到 37–44。**用一个延迟相关性质在自身极端取值下的表现去否定该性质本身，是无效推论。**
> 主文档 **§7 的官方算例取 L = 3 秒**（N=20,000、C=100 ⇒ 有效吞吐 ≈ 33.3 rows/s），故必须在 L=3s 下实测。

**命令**（`cd backend`；**前台 await**；`--keep-data` 保留数据以便独立复核）：

```bash
uv run python ../tests/load/run_10000.py --scenario normal --size 10000 --latency 3 --keep-data
```

**原始输出尾部**（`run_id = 91c81f78-cd7d-4578-bc69-01073774322d`）：

```text
  final_status        : completed
  valid / failed / cancelled : 10000 / 0 / 0
  member counts       : {"succeeded": 10000}
  retry distribution  : {"1": 10000}
  failed error codes  : {}
  attempts total      : 10000
  peak in-flight      : mock=100 db=100 (db samples=5379)
  DB connections peak : 55 (active=55, lock_waiters=46, max_connections=100)
  DB size             : 36 MB
  duration / rate     : 312.9875s / 31.9502 rows/s
  cost known/unknown  : actual=0.400000 reserved=0.000000 unknown_count=0
  peak RSS (进程高水位) : 185.9 MB
  ...
     PASS  在途峰值 ≤ 100(mock)
     PASS  在途峰值 ≤ 100(DB)
  ...
  ==> passed = True
```

产物文件：`tests/load/results/normal_10000_latency3.json`（见 §2.9 的重写说明）。

**五个必答问题 —— 全部按实测数字回答**：

| # | 问题 | 实测回答 |
|---|---|---|
| 1 | mock 侧在途峰值是否达到 ≈100？db 侧呢？ | **是，两者均达 100**：`peak in-flight : mock=100 db=100`。 |
| 2 | 峰值是否**始终 ≤ 100**（不越界）？ | **是**：两路断言 `在途峰值 ≤ 100(mock/DB)` 均 true（mock 精确 = 100）。⚠️ 采样式证据只能证明**采样点**不越界（db 采样 5379 次 / 313s）；**连续时间**的「≤ 100」由 §7.6.3 P2-2 的**结构性保证**（先占槽、后领取，`worker/main.py:_consumer`）承担，**非**仅靠采样。 |
| 3 | 释放后是否**滚动补位**（非「取 100 行等整轮」）？ | **是**：消费者逐成员领取（`claim_members(limit=1)`），一次调用返回即独立领下一行；**峰值达到 100 本身即要求滚动补位**（若整轮等待，在途会呈锯齿而不会稳定顶到 100）。 |
| 4 | 实测速率是否 ≈ **33 rows/s**（对齐 §7 算例）？ | **是，实测 31.95 rows/s**，对齐 §7 算例 `C/L = 100/3 = 33.3 rows/s`（差 ~4%，来自领取路径开销）。`在途 ≈ 31.95 × 3 = 95.9 ≈ 100` ✅。 |
| 5 | `lock_waiters` 是否**显著下降**？ | **否，实测未下降**：L=3s 峰值 **46**，与 L=0.2s 的 **47** 基本持平。⇒ `lock_waiters` 在该配置下是**结构性常量（≈46–47）**，**不是**随领取速率变化的敏感指标。**如实报告：本实验证伪了「L 增大后 lock_waiters 会显著下降」的预期。** |

**延迟口径总表（本轮新增，可逐格核对）**：

| 场景 | L (s) | 领取路径上限 (rows/s) | 供应商上限 `C/L` (rows/s) | 绑定约束 | 实测速率 (rows/s) | 在途 ≈ 速率×L | 实测在途峰值 mock / db | lock_waiters |
|---|---|---|---|---|---|---|---|---|
| `normal@10000` @0.2（本轮重跑） | 0.2 | ≈90 | 500 | **领取路径** | 91.8 | 18.4 | 40 / 78 | 47 |
| **`normal@10000` @3.0（决定性）** | 3.0 | ≈90 | **33.3** | **供应商延迟** | **31.95** | **95.9** | **100 / 100** | **46** |

> **结论（本次纠正的知识增量）**：`min(领取路径上限, C/L)` 模型被实测**两端同时印证**——L=0.2 绑定领取路径
> （实测 91.8 ≈ 90）、L=3.0 绑定供应商延迟（实测 31.95 ≈ 33.3）。**「恰有 100 路在途」是延迟相关性质，
> 在 §7 算例口径 L=3s 下成立**；子秒延迟下的低在途**不是**并发能力不足。该结论用于**改写 §3.1 第 1 项的判定**。

### 2.9 审计完整性：哪些 run 的数据**保留**（可被独立复核）、哪些**已清理**（仅脚本自述）

> **背景（TEAM-BRIEF §7.16.4）**：`run_10000.py` 的 `_run_all` 在**开始时**执行
> `if not args.keep_data: await _truncate_all(...)`。因此**后一个 run 会清掉前一个 run 的数据** ⇒ 上一轮 6 个场景
> 只有**最后一个**留在库里，其余 run 事后查库为 **0 行**，其 13 条不变量**只有脚本自述（`invariant_checks`）**，
> 外部复核者**无法用独立路径重推**。本轮按 §7.16.4 要求，对**判定用（L=3s）run** 与**旗舰规模（20,000）run**
> 补跑并 **`--keep-data`**，使旗舰规模的不变量证据不止于自述。

**本轮 `survey_test_t7` 库内现存、可被任何复核者用独立 SQL 重推的 run**：

| run_id | 场景 / 规模 / 延迟 | 本轮角色 | 数据 |
|---|---|---|---|
| `91c81f78-cd7d-4578-bc69-01073774322d` | `normal` / 10,000 / **L=3s** | §3.1 第 1 项判定依据（§2.8） | **保留（可审计）** |
| `003c3562-3f3e-47ef-93ad-d9b834769f35` | `normal` / **20,000** / L=0.2s | 旗舰规模 `N_in_N_out` 证据 | **保留（可审计）** |
| `6229914a-7cd0-4aad-8a10-9a57104ecbd2` | `normal` / 10,000 / L=0.2s | 恢复被重写的 `normal_10000.json` 基线 | **保留（可审计）** |
| `71b57ffd-acf7-44f5-8868-9d4b7402257f` | `burst` / 2,000 / L=0.2s | 上一轮遗留（§2.6 已用 psql 独立复核） | **保留（可审计）** |

**仅脚本自述、数据已被后续 run 清空（无法事后独立复核）**：上一轮 `normal` / `recoverable` / `permanent` @10,000、
上一轮 `normal`@20,000、`interference`@10,000。这些 run 的不变量**仅有脚本 `invariant_checks` 自述**，
**外部无法重推**——这是诚实的审计缺口，故本轮以两个 `--keep-data` run 补齐旗舰规模的可审计性。

> ⚠️ **产物文件命名与重写的诚实说明**：脚本产物固定命名 `{scenario}_{size}.json`。本轮
> `--scenario normal --size 10000 --latency 3` 因此**重写**了 `normal_10000.json`；为使 §2.7 引用的 0.2s 基线文件
> 仍对应其数值，已用**同命令（L=0.2）重跑恢复**该文件，并把决定性 L=3s 结果**另存**为
> `tests/load/results/normal_10000_latency3.json`。`normal_20000.json` 亦被本轮旗舰复跑重写。
> §2.7 表中 `normal` 行的数值（10k=90.3 / 20k=84.0 rows/s）为**上一轮运行**的记录；本轮两次复跑的**同配置**实测为
> **10k@0.2 = 91.8 rows/s、20k@0.2 = 79.3 rows/s** —— 属**同一配置的两次不同运行**（正常波动），**非矛盾**。

**可复现的独立复核 SQL（DB 原生；直接对保留数据重推不变量）**：
把下列整段交给 `docker exec -i survey-pg psql -U postgres -d survey_test_t7`（或等价 psql 客户端）：

```sql
-- 变量：把 :run_ids 换成本轮保留的两个 run id（或任意 run）。
-- (A) N_in_N_out：成员数 == sample_size
SELECT r.id AS run_id, r.sample_size, count(m.id) AS members,
       (count(m.id) = r.sample_size) AS n_in_n_out
FROM runs r JOIN run_members m ON m.run_id = r.id
WHERE r.id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
GROUP BY r.id, r.sample_size;

-- (B1) row_no↔persona_id 双射 方向1：同一 run 内 persona_id 重复（应为 0 行）
SELECT run_id, persona_id, count(*) FROM run_members
WHERE run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
GROUP BY run_id, persona_id HAVING count(*) > 1;

-- (B2) 双射 方向2：同一 run 内 row_no 重复（应为 0 行）
SELECT run_id, row_no, count(*) FROM run_members
WHERE run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
GROUP BY run_id, row_no HAVING count(*) > 1;

-- (C) succeeded 成员 answer_json 非 NULL、value 全在合法五档、question_id 固定
SELECT m.run_id,
  count(*) FILTER (WHERE m.status='succeeded') AS succeeded,
  count(*) FILTER (WHERE m.status='succeeded' AND m.answer_json IS NULL) AS null_answer,
  count(*) FILTER (WHERE m.status='succeeded'
        AND COALESCE(m.answer_json->>'value','') NOT IN
            ('definitely_not','probably_not','unsure','probably_yes','definitely_yes')) AS illegal_value,
  count(*) FILTER (WHERE m.status='succeeded'
        AND COALESCE(m.answer_json->>'question_id','') <> 'purchase_intent') AS bad_qid
FROM run_members m
WHERE m.run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
GROUP BY m.run_id;

-- (D1) 五档分布（该 run 实际出现的合法档位）
SELECT run_id, answer_json->>'value' AS value, count(*) AS cnt FROM run_members
WHERE run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
  AND status='succeeded' GROUP BY run_id, value ORDER BY run_id, value;

-- (D2) 五档计数之和 == valid
SELECT run_id,
  count(*) FILTER (WHERE status='succeeded') AS valid,
  count(*) FILTER (WHERE status='succeeded' AND answer_json->>'value' IN
        ('definitely_not','probably_not','unsure','probably_yes','definitely_yes')) AS five_bucket_sum,
  (count(*) FILTER (WHERE status='succeeded') = count(*) FILTER (WHERE status='succeeded'
        AND answer_json->>'value' IN
        ('definitely_not','probably_not','unsure','probably_yes','definitely_yes'))) AS sum_eq_valid
FROM run_members
WHERE run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
GROUP BY run_id;

-- (E) 无重复 (member_id, attempt_no)（应为 0 行；DB 侧另有 UNIQUE 约束 uq_attempts_member_no）
SELECT a.member_id, a.attempt_no, count(*) FROM attempts a
JOIN run_members m ON m.id = a.member_id
WHERE m.run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
GROUP BY a.member_id, a.attempt_no HAVING count(*) > 1;

-- (F) 每个成功成员恰一条成功 attempt
SELECT m.run_id, count(*) AS succeeded_members,
  count(*) FILTER (WHERE COALESCE(sa.n,0)=1) AS exactly_one_ok_attempt,
  count(*) FILTER (WHERE COALESCE(sa.n,0)<>1) AS not_exactly_one
FROM run_members m
LEFT JOIN (SELECT member_id, count(*) AS n FROM attempts WHERE status='succeeded' GROUP BY member_id) sa
  ON sa.member_id = m.id
WHERE m.run_id IN ('91c81f78-cd7d-4578-bc69-01073774322d','003c3562-3f3e-47ef-93ad-d9b834769f35')
  AND m.status='succeeded' GROUP BY m.run_id;
```

**上述 SQL 的本轮实际运行结果（原始输出）**：

```text
--- (A) N_in_N_out ---
                run_id                | sample_size | members | n_in_n_out
 91c81f78-cd7d-4578-bc69-01073774322d |       10000 |   10000 | t
 003c3562-3f3e-47ef-93ad-d9b834769f35 |       20000 |   20000 | t

--- (B1) duplicate persona_id 方向  (0 rows) ---
--- (B2) duplicate row_no     方向  (0 rows) ---

--- (C) succeeded answer 合法性 ---
 003c3562-... | succeeded=20000 | null_answer=0 | illegal_value=0 | bad_qid=0
 91c81f78-... | succeeded=10000 | null_answer=0 | illegal_value=0 | bad_qid=0

--- (D1) 五档分布 ---
 91c81f78-... (10000): definitely_not=2067, probably_not=1981, unsure=2043,
                        probably_yes=1938, definitely_yes=1971
 003c3562-... (20000): definitely_not=4052, probably_not=3961, unsure=4109,
                        probably_yes=3879, definitely_yes=3999

--- (D2) sum(five buckets) == valid ---
 91c81f78-... | valid=10000 | five_bucket_sum=10000 | sum_eq_valid=t
 003c3562-... | valid=20000 | five_bucket_sum=20000 | sum_eq_valid=t

--- (E) duplicate (member_id, attempt_no)   (0 rows) ---

--- (F) 每个成功成员恰一条成功 attempt ---
 003c3562-... | succeeded_members=20000 | exactly_one_ok_attempt=20000 | not_exactly_one=0
 91c81f78-... | succeeded_members=10000 | exactly_one_ok_attempt=10000 | not_exactly_one=0
```

> **结论**：两个保留 run 的**全部**不变量经 **DB 原生 SQL 独立重推**成立（`N_in_N_out`、`row_no↔persona_id`
> **双向**双射、合法五档 + 固定 `question_id`、五档和 == valid、无重复 `(member_id, attempt_no)`、每个成功成员
> 恰一条成功 attempt）。旗舰规模 **20,000** 的 `N_in_N_out` 证据不再只有脚本自述。

---

## 3. 不变量核对表（100 槽位与 20,000 规模）

> **本轮已在独占窗口执行 10,000 / 20,000 规模（mock）**。下表结论列填**实测值**；
> 未能覆盖的子项如实标注「**未达标**」或「**未执行**」——**不预填「通过」**。
> 「核对方法」列为脚本中**已实现**的断言；万级原始实测汇总见 §2.7，逐条可在 JSON 中核对。
> **100 槽位满载（恰 100 同时在途）本轮判定更正为「已验证」**：在 `--latency 3`（对齐主文档 §7 官方算例 L=3s）下
> 实测 mock / db 在途峰值**均为 100**（见 §2.8）；`--latency 0.1/0.2` 下的 37–44 是**延迟相关性质在子秒极端的退化**，
> **不构成并发能力不足的证据**（延迟口径见 §2.8）。

### 3.1 100 槽位（并发）核对表

| # | 核对项 | 核对方法（脚本已实现） | 100 槽位满载结论（实测） |
|---|---|---|---|
| 1 | 恰有 100 个请求同时在途 | `RunningSampler` 按 DB `status='running'` 采样 + `TrackingMockProvider.peak` | **已验证**：`normal@10000 --latency 3`（对齐主文档 §7 算例 L=3s）实测 **mock 峰值 = 100、db 峰值 = 100**（`在途峰值 ≤ 100(mock/DB)` 均 true），速率 **31.95 rows/s ≈ §7 算例 33.3 rows/s**，`在途 ≈ 31.95×3 = 95.9 ≈ 100`（见 §2.8）。**该不变量依赖供应商延迟 L**：`--latency 0.1/0.2`（子秒）下 `C/L = 100/0.2 = 500 rows/s` 高于领取路径上限（≈90 rows/s）⇒ 瓶颈落领取路径、在途退化为 ≈ 速率×延迟（实测 37–44）；此为该性质在自身极端的表现，**不得据此判「未达标」**（延迟口径见 §2.8、§4.3.1 补充） |
| 2 | 滚动补位，峰值始终 ≤ 100 | 同上逐次采样取 max | **满足**：峰值 db ≤ 76 ≤ 100（10k/20k 逐次采样取 max）；`在途峰值 ≤ 100(mock/DB)` = true |
| 3 | `UNIQUE(run_id, persona_id)` 双射 | DB 约束 + `invariant_checks` | **满足**：`N_in_N_out`+双射断言 = true（10k 三场景、20k） |
| 4 | `UNIQUE(run_id, row_no)` 逐行对齐 | DB 约束 + `row_no↔persona_id` 断言 | **满足**：`row_no↔persona_id 双射` = true |
| 5 | 无重复 `(member_id, attempt_no)` | DB 唯一约束 + `duplicate_attempt_no_groups==0` | **满足**：`无重复 (member_id, attempt_no)` = true（interference 恢复后亦 true） |
| 6 | 每个成功成员恰一条成功 attempt | SQL 聚合 | **满足**：`每个成功成员恰一条成功 attempt` = true |
| 7 | RPM/TPM 与预算未越界 | 领取事务内预留 + 结算断言 | **部分满足**：`reserved_cost`/`actual_cost` 无越界断言 = true；但 mock 下 RPM/TPM 被刻意调高，**真实越界未执行** |
| 8 | 五档计数之和 == valid | SQL 聚合 | **满足**：`五档计数之和 == valid` = true |

### 3.2 20,000 规模核对表

| # | 核对项 | 核对方法 | 20,000 结论（实测） |
|---|---|---|---|
| 1 | 20,000 进 20,000 出（无丢行） | `N_in_N_out` 断言 | **满足**：`N_in_N_out` = true（`normal_20000.json`，succeeded=20000、failed=0） |
| 2 | 20,000 个唯一有效答案、无重复 | `成功成员答案互不重复` + SQL | **满足**：`成功成员答案互不重复` = true |
| 3 | 五档计数之和 == valid(20000) | SQL 聚合 | **满足**：`五档计数之和 == valid` = true |
| 4 | 实际速率 / 理想时长 | `size / duration` + §7 算例对照 | **实测**：79.3 rows/s、耗时 252.2s（留存 `normal_20000.json`；§2.7 的 84.0/238.2s 为上一轮同配置运行，口径见本文件 L352–353 说明） |
| 5 | DB 体积增长 | `pg_database_size()` | **实测**：70 MB（`normal_20000.json` 的 `db_size`） |
| 6 | 进程峰值 RSS | `peak_rss_mb()` | **实测**：232.3 MB（进程高水位，语义见 §4.1） |
| 7 | API p95 < 1s @ 10 并发 | `measure_api_p95` | **10,000 规模实测**：`run_p95=0.0283s`、`summary_p95=0.0738s`（**在已完成 run 上**）；**20,000 规模**与**运行中 p95 未执行** |
| 8 | 内存/句柄不随规模线性泄漏 | 长时采样 | **未执行**：仅有单批峰值，无长时采样证据，**不能断言「不泄漏」** |

> 说明：`--size 10000` 与 `--size 20000` 已通过 `--size` 旋钮支持（见 §4.7），且**已在本轮独占整机窗口执行**。
> §3.1 第 1 行「恰有 100 同时在途」原判「未达标」**已更正为「已验证」**（§7 算例口径 L=3s 下实测 mock/db 峰值
> 均 = 100，见 §2.8）。唯一**未执行**项为 §3.2 第 8 行「内存/句柄长时不泄漏」（无长时采样证据）。其余子项均已实测且为 true。

---

## 4. 测量方法

### 4.1 机器配置 / RSS

- 机器配置见 §0。
- **RSS 测法**：脚本内 `peak_rss_mb()` 读取
  `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`（**进程高水位**，不可回落；
  macOS 单位为字节、Linux 为 KB，按 `sys.platform` 归一后输出 MB）。
- **注意**：该值是「**压测脚本所在进程**」的整体高水位，涵盖脚本本身 + 注入的 mock provider
  + 内嵌 `Worker` 调度协程 + httpx/asyncpg 缓冲，**不等于**生产形态（API / worker 独立进程）
  的 RSS，**仅作量级参考**。200 规模实测：单场景结束时约 **129–139 MB**；
  **万级实测**：10,000 约 **178 MB**、20,000 约 **232 MB**（`normal_20000.json` 的 `rss_peak_mb`）。

### 4.2 DB 体积

- `docker exec survey-pg psql -U postgres -d survey_test_t7 -tAc "SELECT pg_size_pretty(pg_database_size('survey_test_t7'))"`
- 200 规模执行后为 **16 MB**（含多次运行历史）。
- **万级实测**（各含其批次）：`normal`@10k = 55 MB、`normal`@20k = **70 MB**、`recoverable`@10k = 58 MB、
  `permanent`@10k = 58 MB、`interference`@10k = 62 MB、`burst`@2k = 21 MB（见 §2.7）。

### 4.3 实际在途曲线（并发验证）

- **两条独立证据**：
  1. `RunningSampler`：后台协程按固定间隔执行 `SELECT count(*) FROM run_members WHERE run_id=:r AND status='running'`，
     取样本最大值记为 `peak_in_flight_db`（**真实 DB 状态**，非进程内计数）。
  2. `TrackingMockProvider.peak`：mock 每次 `answer()` 进出维护在途计数，取最大值记为
     `peak_in_flight_mock`（**真实调用侧**）。
- 200 规模两路峰值均 **≤ 100**（如 normal：mock=26 / db=69；burst：mock=10 / db=59）。
- **万级两路峰值**：10k/20k db 侧 **73–76**、mock 侧 **37–44**（见 §2.7）；均 **≤ 100**，且**未随规模上升**
  （`normal`@20k 与 @10k 的 db 峰值同为 74）。
- 主文档 §10 T7 的「**恰有 100 个同时在途**」：`--latency 0.1/0.2` 下峰值 73–79（**≤ 100**）；**在 L=3s（对齐 §7 算例）下 mock/db 峰值均 = 100**（见 §2.8）。故该目标在 §7 算例口径下**已验证**；子秒延迟下的低在途属延迟相关退化（口径见 §3.1 第 1 行 / §2.8）。

#### 4.3.1 瓶颈定位：`claim_members` 对单 run 行的 `FOR UPDATE` 串行化（假设，已被实测强烈支持）

- **代码定位**（**只读复核，未改动**）：`backend/app/runs/repository.py` 的 `claim_members`（约 L294）先对
  成员行 `SELECT ... FOR UPDATE SKIP LOCKED`，随后由 `_lock_run_counters(...)` 对**同一条 run 行**执行
  `.with_for_update()` 更新计数（约 L220 / L238）。因此**所有**领取事务都会争用**同一个** run 行的行锁，
  100 个消费者在领取阶段被串行化。
- **实测证据**：`ConnectionSampler` 在万级各场景同时采样 `pg_stat_activity` 中 `wait_event_type='Lock'`
  的连接数，峰值**恒为 47**（≈ 连接峰值 55–56 的 **84%**）；且 db 侧在途峰值（73–76）显著低于理论满并发
  **100**，实测速率（72.9–90.3 rows/s，@0.2 上一轮运行；留存文件为 91.8 / 79.3，见 L352 口径说明）远低于「100 槽位 ÷ 0.2s 延迟 = **500 rows/s**」的理想值。
- **结论**：**假设成立**——领取路径对单个 run 行的行锁串行化，是**子秒延迟下**当前吞吐上限（进而在途无法堆到 100）的主因。
- **延迟口径补充（本轮新增，纠正原「未达标」推论）**：上述「领取路径是吞吐上限」成立的前提是 **`C/L` 高于领取路径上限**。
  `--latency 0.1/0.2` 时 `100/0.2 = 500 rows/s` **>** 领取上限（≈90 rows/s）⇒ 领取路径**是**绑定约束，在途被压到 37–44。
  当 L 增大到 **≈1.1s**（使 `100/L ≤ 90`）后，**绑定约束切换为供应商延迟**：L=3s 时 `100/3 = 33.3 rows/s < 领取上限 90`，
  实测在途峰值 = **100**、速率 **31.95 rows/s**（§2.8）。因此「在途无法堆到 100」**只在子秒延迟下成立，不可外推**；
  两条约束由 `min(领取上限, C/L)` 决定谁绑定（两端实测均已印证）。
- **最小修复建议（仅建议，本轮**未实施**，须由主理人 / 后端负责人决策，`backend/**` 保持冻结）**：
  ① 将 run 级计数改为**无锁原子自增**（`UPDATE runs SET counter = counter + :n WHERE id = :r`，去掉
  `with_for_update()`），或把计数改为对 `attempts`/`run_members` 的聚合读取；② 或按成员分片
  （如 `hash(member_id) % K`）对 K 个「计数行」分散加锁。任一方案都**不改变**对外不变量
  （至多一个活动批次 / 不补值 / 双射），但可解除单行锁串行化。**本轮不修改 `backend/**`。**

### 4.4 实测速率

- `rate_per_s = size / duration_s`（`duration_s` 为从 `serve()` 起至批次终态）。
- 200 规模：`normal` 66.1 rows/s、`recoverable` 47.1 rows/s、`permanent` 47.3 rows/s。
- **万级实测**：`normal`@10k **91.8**（§2.7 记 90.3，为上一轮运行）、`recoverable`@10k **72.9**、`permanent`@10k **78.6**、`normal`@20k **79.3**（§2.7 记 84.0，为上一轮运行）rows/s；@10k/@20k 同配置两次运行的口径说明见本文件 L352–353（见 §2.7）。
- **此为 mock 延迟（`--latency 0.2s`）下的平台调度速率，与真实供应商延迟无关**；实测速率（≈73–90 rows/s）
  远低于「100 槽位 ÷ 0.2s = 500 rows/s」的理论值，原因见 §4.3.1（领取串行化）。
  §7 的「N=20,000 约 10 分钟」仅为**算例**：本轮 `normal`@20k 实测 **238.2s**（≈4 分钟）。

### 4.5 API p95（目标 < 1s @ 10 并发页面请求）

- 测法：干扰场景末对**已完成的 run_b** 发起 `concurrency=10`、`total=200` 的并发请求，
  分别打 `GET /api/v1/runs/{id}`（状态，页面 3s 轮询同款）与 `GET /api/v1/runs/{id}/summary`（报告）。
- 记录 p50/p95/p99/max；200 规模实测：`run_p95=0.0149s`、`summary_p95=0.0187s`（**均 < 1s**）。
- **10,000 规模实测（§11 证据，`interference_10000.json`）**：在**已完成的 run_b** 上 10 并发 × 200 请求，
  `run_p95=0.0283s`（p50 0.0136 / p99 0.0305 / max 0.0376）、`summary_p95=0.0738s`
  （p50 0.0580 / p99 0.0819 / max 0.0885）—— **均 < 1s**。
- **未执行的对照**：20,000 规模、以及「**运行中**（非终态）」的 p95 **未执行**（本轮 p95 均在**已完成** run 上测）。

### 4.6 恢复耗时

- 测法 1（干扰场景）：杀 worker 后令在途租约过期 → **重启 API** → 新 `Worker` 执行
  `scan_expired_once()` 回收过期租约 → 记录 `serve()` 到终态耗时。
  - 200 规模实测：恢复后 `succeeded=200`、`lost=0`、**恢复耗时 2.79s**。
  - **10,000 规模实测**（`interference_10000.json`）：杀 worker 时 `succeeded=10`、崩溃遗留 **58** 条在途租约，
    重启 API 后读回 `in_flight=58`，`scan_expired_once` 回收 58 条，收敛 `succeeded=10000`、`lost=0`、
    **恢复耗时 104.3s**（大批量过期租约恢复）。
- 测法 2（突发场景）：全量突发致暂停 → 消除故障源 + `POST /resume` → 新 `Worker` 收敛 → 记录耗时。
  - 200 规模实测：恢复后 `succeeded=200`、`lost=0`、**恢复耗时 3.57s**。
  - **2,000 规模实测**（`burst_2000.json`）：全量突发致 `paused`/`api_unavailable`（保留 `preserved=1970`、
    `failed=0`）→ `provider.recover()` + `POST /resume` → 收敛 `succeeded=2000`、`lost=0`、**恢复耗时 17.2s**。
- **未执行**：**运行中**的大批量重试恢复耗时、以及 20,000 规模的恢复耗时 **未测**。

### 4.7 确定性 / 可复现

- 一切随机性由 `seed` + `(persona_id, attempt_no)` 派生；**不使用全局 `random`**；
  故障判定用 SHA-256（`mock_provider.stable_hash`），**不用**内置 `hash()`（`PYTHONHASHSEED` 会破坏跨进程可复现）。
- **`--size` 可调已实测**：`--size 60`（recoverable）→ `valid=60`、`attempts=76`、重试分布 `{1:52, 3:8}`；
  `--size 200` 见 §2；**`--size 10000` / `--size 20000` 已实测**（见 §2.7）。

### 4.8 领取查询随 N 的量化（`EXPLAIN (ANALYZE, BUFFERS)`，本轮新增）

> §4.3.1 / TEAM-BRIEF §7.16.3 提出「`claim_members` 的 `ORDER BY row_no LIMIT n FOR UPDATE SKIP LOCKED`
> 候选扫描/排序成本随 N 增长」，但上一轮**只有怀疑、没有量化**。本轮在**保留数据**上对**真实领取谓词**
> 做 `EXPLAIN (ANALYZE, BUFFERS)`。做法：在**单个事务内**临时把成员置为可领取、`EXPLAIN ANALYZE` 执行，
> **随后 `ROLLBACK`** ⇒ **未改动**任何保留 run 的数据（已复核两 run 仍为 `succeeded 10000/20000`）。

**被测量的谓词**（等价于 `claim_members` 的候选 SELECT，`limit` 取 100）：

```sql
SELECT * FROM run_members
WHERE run_id = :r AND status IN ('pending','retry_wait')
  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
ORDER BY row_no LIMIT 100 FOR UPDATE SKIP LOCKED;
```

**① run 起始态（全部成员可领取，P = N）**：

| 规模 | 计划（顶层 → 底） | Index Scan 返回行 | Sort 方法 | 执行耗时 | Buffers |
|---|---|---|---|---|---|
| 10,000 | `Limit → LockRows → Sort(row_no) → Index Scan idx_members_run_status_next` | **10000** | **external merge, Disk 4056 kB** | **21.3 ms** | shared hit=1111, temp w=508 |
| 20,000 | 同上 | **20000** | **external merge, Disk 8120 kB** | **45.2 ms** | shared hit=2205, temp w=1017 |

**② run 后期（仅尾部 100 行可领取，P = 100）**：10k = **1.6 ms**（quicksort, Mem 66 kB）；20k = **3.2 ms**（quicksort, Mem 66 kB）。

**原始输出（节选，逐字）**：

```text
########## 10k run: ALL members claimable ##########
 Limit  (actual time=20.731..20.817 rows=100 loops=1)
   Buffers: shared hit=1111, temp read=286 written=508
   ->  LockRows  (actual time=20.730..20.807 rows=100 loops=1)
         ->  Sort  (actual time=20.720..20.735 rows=100 loops=1)
               Sort Key: row_no
               Sort Method: external merge  Disk: 4056kB
               ->  Index Scan using idx_members_run_status_next on run_members
                     (actual time=0.037..2.824 rows=10000 loops=1)
                     Index Cond: ((run_id = ...::uuid) AND (status = ANY ('{pending,retry_wait}'::text[])))
                     Filter: ((next_attempt_at IS NULL) OR (next_attempt_at <= now()))
 Execution Time: 21.327 ms

########## 20k run: ALL members claimable ##########
 Limit  (actual time=44.324..44.398 rows=100 loops=1)
   ->  LockRows  (actual time=44.323..44.390 rows=100 loops=1)
         ->  Sort  (actual time=44.308..44.323 rows=100 loops=1)
               Sort Method: external merge  Disk: 8120kB
               ->  Index Scan using idx_members_run_status_next on run_members
                     (actual time=0.029..4.942 rows=20000 loops=1)
 Execution Time: 45.175 ms

########## 10k run: only LAST 100 claimable ##########
 Sort Method: quicksort  Memory: 66kB   |  Index Scan ... rows=100  | Execution Time: 1.618 ms
########## 20k run: only LAST 100 claimable ##########
 Sort Method: quicksort  Memory: 66kB   |  Index Scan ... rows=100  | Execution Time: 3.247 ms
```

**量化解读**：
- 计划**未**用 `idx_members_run_row(run_id,row_no)` 做有序扫描，而是用
  `idx_members_run_status_next(run_id,status,next_attempt_at)` **取出当前「全部可领取行」**（Index Scan 返回 P 行），
  再 **Sort by row_no** 取前 100 ⇒ **单次领取成本 ∝ 当前可领取行数 P**，而非 ∝ `limit`。
- 起始态 P=N 时排序**已外排到磁盘**（10k 4 MB / 20k 8 MB，`work_mem` 默认 4 MB），单次领取 21.3 ms → 45.2 ms（**随 N 单调上升**）；
  P=100 时降为 1.6 / 3.2 ms ⇒ 成本由 **P** 而非规模恒定项决定。
- 由于消费者**逐成员领取**（`claim_members(limit=1)`，`worker/main.py:379`）且每次领取都重读当前全部可领取行，
  **整条领取路径的累计工作量随 N 超线性增长**（≈ `Σ P` 量级）。与实测吻合：同为 L=0.2s，
  `normal`@10k = **91.8** rows/s → @20k = **79.3** rows/s（**−13.6%**）。**幅度说明**：领取路径是「随 N 变差的贡献项」，
  但在 L=3s 下**并非**唯一绑定约束（绑定的是供应商延迟，见 §2.8）。
- **影响与建议（仅建议；本轮**未改代码**，`backend/**` 保持冻结）**：若要消除该超线性项，可让领取谓词与 `ORDER BY row_no`
  走 `(run_id,row_no)` 有序索引并把 `status`/`next_attempt_at` 做成可索引下推的谓词，避免每次对全部可领取行重排；
  或按 §4.3.1 的方向解除单 run 行锁串行化。任一方案均须经主理人裁决后派单。

---

## 5. 已知限制 / 未执行项

> 明确区分**已执行**与**未执行**。

**已执行（mock）**

*小规模基线（200 / 60）*
1. `normal` / `recoverable` / `permanent` 三个独立 run 的完整不变量 + R2(a)(b) 断言。
2. 干扰场景：重复控制命令矩阵 + 杀 worker + 重启 API + 过期租约恢复（`lost=0`）。
3. **突发场景 `burst`**：全量突发 → §6.4 暂停 `api_unavailable`（DB 原生 SQL 读原值）→ 保留未执行成员
   → 消除故障源 + `resume` → 收敛且 `lost=0`（§11「暂停恢复有自动化测试证据」）。
4. DB 原生 SQL 双侧抽查（合法五档 / 答案可归因 / failed 无答案）+ 突发尝试级错误码复核。
5. `--size` 旋钮实测可用（60 与 200）。

*万级实测（本轮，独占窗口，见 §2.7）*
6. `normal` / `recoverable` / `permanent` @ **10,000**：全不变量 + R2(a)(b) 断言均 `true`（`passed=True`）。
7. `normal` @ **20,000**：`N_in_N_out` / 无重复答案 / 五档和 == valid / 单活动批次 均 `true`（`passed=True`）。
8. `interference` @ **10,000**：控制命令矩阵 + 杀 worker + 重启 API + 回收 **58** 条过期租约 → `lost=0`、
   恢复 104.3s；**API p95 @10k** = run 0.0283s / summary 0.0738s（§11 证据）。
9. `burst` @ **2,000**：全量突发 → `paused`/`api_unavailable` → 保留 **1970** 未执行成员（`failed=0`）→ `resume`
   → `completed`、`lost=0`、恢复 17.2s（`passed=True`）。
10. **连接压力**：万级各场景连接峰值 **55–56** < `max_connections=100`（T6 连接池修复后未触顶）。
11. `cd backend && uv run pytest -q` → **263 passed**（后端未改动，改动仅限 `tests/load/**` 与本文档）。

*本轮纠正补测（§2.8 / §2.9 / §4.8）*
12. **`normal@10000 --latency 3`（对齐 §7 官方算例 L=3s）**：mock/db 在途峰值 **均 = 100**、速率 **31.95 rows/s**、
    `在途 ≈ 31.95×3 = 95.9`、`passed=True`（见 §2.8）——**用于改写 §3.1 第 1 项判定**。
13. **旗舰规模保留数据复跑**：`normal@20000 --keep-data`（`run_id=003c3562-...`，§2.9），使其不变量**可被独立重推**。
14. **独立复核 SQL**：对两个 `--keep-data` run 用 **DB 原生 SQL** 重推全部不变量，**全部成立**（§2.9 含原始输出）。
15. **领取查询随 N 的量化**：`EXPLAIN (ANALYZE, BUFFERS)` 原始输出，显示单次领取成本 ∝ 当前可领取行数 P、
    起始态排序外排（10k 21.3ms / 20k 45.2ms）（见 §4.8）。

**未执行（诚实声明）**
1. **真实第三方 API 的 100 → 1,000 → 10,000 人实测**（本机无凭据）——见 §1。
2. ~~**100 槽位满载（恰 100 同时在途）**：实测峰值仅 **73–76**，**未达标**；根因见 §4.3.1（对应 §3.1 第 1 行）。~~
   **【本轮更正】** 该项**已改为「已验证」**：对齐主文档 §7 算例 **L=3s** 实测 mock/db 在途峰值均 = **100**（§2.8）；
   原「未达标」系用子秒延迟（`--latency 0.1/0.2`，`C/L = 500 rows/s` 高于领取上限）这一**延迟相关性质的极端取值**
   去否定该性质本身，属**无效推论**，已作废。
3. **运行中（非终态）的 API p95**、以及 **20,000 规模的 API p95**（本轮 p95 仅在**已完成** run、**10k** 规模上测）。
4. **内存/句柄长时采样**：无「不随规模线性泄漏」的长时证据，仅有单批峰值（对应 §3.2 第 8 行）。
5. 供应商 429/5xx/401 的**真实**行为、真实 tokenizer/TPM 校准、真实费用结算 —— 均**未执行**。
6. `RPM/TPM` 或预算**真实越界**场景（mock 下 RPM/TPM 被刻意调高以避免限流成为瓶颈）。

**实测发现 / 假设（如实记录）**
1. **主文档 T7 的 `recoverable` 期望与 §6.4 的总中断保护相冲突（需求内部冲突，TEAM-BRIEF §7.13 已裁定）**：
   若按 `architecture.md` §5.2 对 `recoverable` **全量**注入，会让**所有**成员首调同时失败，触发主文档
   §6.4「连续 5 次同类供应商失败 → 暂停 `api_unavailable`」这条**正确**的总中断保护，批次**停**而非收敛
   （实测：并发 100 下 `final_status="paused"`、`counts={"pending":126,"retry_wait":74}`）。
   **这是保护在正常工作，不是缺陷**，**不得**为迁就 T7 期望而削弱它。故按 §7.13 裁定：
   - `recoverable` 改用**确定性间歇子集注入**（行序步长，连续同类失败恒 < 5），使批次能收敛；
   - 「全量突发」**保留为独立场景 `burst`**（见 §2.3），作为 §6.4 暂停/恢复保护链路的自动化证据。
2. **在途峰值与供应商延迟强相关（万级实测，本轮更正口径）**：`--latency 0.1/0.2` 下万级 db 侧在途峰值 **73–79**
   （mock 侧 36–44）；`normal`@20k 与 @10k 的 db 峰值几乎相同（74–79）——原因是**子秒延迟下 `C/L` 远高于领取路径上限**，
   瓶颈落在**领取事务对单个 run 行的锁串行化**：`claim_members`（`backend/app/runs/repository.py`）先
   `FOR UPDATE SKIP LOCKED` 锁成员行，再由 `_lock_run_counters(...).with_for_update()` 更新**同一条 run 行**，使
   100 个消费者在领取阶段串行。实测 `pg_stat_activity` 的 `wait_event_type='Lock'` 连接峰值**恒为 46–47**（各场景/规模一致）。
   **但这是延迟相关性质**：在 **L=3s（对齐 §7 算例）**下 `C/L = 33.3 rows/s < 领取上限 90`，绑定约束切换为**供应商延迟**，
   实测在途峰值 = **100**、速率 **31.95 rows/s**（§2.8）⇒ **§11「恰有 100 路真实在途并发」在 §7 算例口径下已验证**；
   子秒延迟下的低在途**不是**并发能力不足。领取路径随 N 的量化见 §4.8。修复建议见 §4.3.1；**本轮未改动 `backend/**`。**
3. **`peak_rss_mb` 语义**：见 §4.1，为进程高水位、非生产形态 RSS，仅量级参考。
4. **故障注入位置假设**：运行时 `app/inference/mock_provider.py` **恒返回合法答案、不做故障注入**；
   故障注入能力**只在** `tests/load/mock_provider.py`。因此脚本在**进程内直接**
   `Worker(provider=<故障注入 mock>)`，不依赖环境变量切换（依据 TEAM-BRIEF §7.5 / §7.9.2）。

---

## 6. 复现命令

```bash
# 0) 隔离压测库（不存在才建）
docker exec survey-pg createdb -U postgres survey_test_t7

# 1) 小规模基线：200 规模，三数据场景 + 干扰 + 突发（mock）
cd backend
uv run python ../tests/load/run_10000.py --scenario all --size 200 --latency 0.1

# 2) 万级实测（本轮已在独占窗口【逐个前台】执行，结果见 §2.7）
uv run python ../tests/load/run_10000.py --scenario normal       --size 10000 --latency 0.2
uv run python ../tests/load/run_10000.py --scenario recoverable  --size 10000 --latency 0.2
uv run python ../tests/load/run_10000.py --scenario permanent    --size 10000 --latency 0.2
uv run python ../tests/load/run_10000.py --scenario normal       --size 20000 --latency 0.2
uv run python ../tests/load/run_10000.py --scenario interference --size 10000 --latency 0.2
uv run python ../tests/load/run_10000.py --scenario burst        --size 2000  --latency 0.2

# 2b) 判定「100 槽位在途」的决定性实验（对齐 §7 官方算例 L=3s）+ 旗舰规模保留数据（见 §2.8 / §2.9）
uv run python ../tests/load/run_10000.py --scenario normal --size 10000 --latency 3 --keep-data
uv run python ../tests/load/run_10000.py --scenario normal --size 20000 --keep-data

# 3) 单场景示例（--size 旋钮）
uv run python ../tests/load/run_10000.py --scenario recoverable  --size 60  --latency 0.05

# 4) 后端回归（确认未破坏基线）
uv run pytest -q          # 期望：263 passed

# 5) 独立复核（DB 原生 SQL，对 --keep-data 的 run 重推不变量；SQL 见 §2.9）
docker exec -i survey-pg psql -U postgres -d survey_test_t7   # 粘贴 §2.9 的复核 SQL
```

> 上述第 2 组为本轮**已执行**的万级实测。**真实第三方 API** 命令（主文档 §10 T7 建议入口）
> 因**本机无凭据**仍**未执行**（见 §1）。
