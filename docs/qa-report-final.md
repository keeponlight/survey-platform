# QA 最终独立复核报告（N7 / N8 收口）

- 复核人：software-qa-engineer-3（独立复核，不对任何前置结论默认采信）
- 日期：2026-09-21
- 性质：本项目最后一个节点的**证伪式复核**（独立路径重推 / 变异测试 / 非法输入 / 文档诚实性审计）
- 前篇：`docs/qa-report-merged.md`（本轮为其续篇与收口）

## 结论摘要

**0 P0 / 0 P1 / 2 P2。N7 与 N8 可验收。**

一句话总判断：以「100 路在途」为核心的 §11 性能判定经**独立复现成立**（L=3s 下在途峰值 mock=100/db=100，与主理人解释一致）；4 组变异全部被既有测试**捕获变红**（断言有牙齿）；13 条数据不变量在只读审计库上用自写 SQL 全部重推一致；E2E 8/8 独立重跑通过且自建前后端字段对照零偏差；文档存在 2 处 P2 级口径问题但不构成虚假陈述。**未发现有源代码或交付物级缺陷。**

| 维度 | 结果 |
|---|---|
| §5.2 独立复现（100 路在途） | ✅ 复现成功（支持 §11 判定） |
| §5.3 变异测试 MA–MD | ✅ 4/4 变红（有牙齿），全部还原并 diff 确认 |
| §5.4 E2E + 字段保真独立验证 | ✅ 8/8 通过，18 字段零偏差 |
| §5.5 洁净性 / 文档诚实性 | ⚠️ 2 处 P2 表述口径问题 |
| 全量回归（还原后） | ✅ 263 passed, ruff All checks passed |

---

## §1 独立复现：§11「100 路在途」判定（本次最重要验证）

### 1.1 方法

- 隔离库：`survey_test_t7qa`（本人专用，与只读审计库 `survey_test_t7` 完全隔离）。
- 命令（前台 await，一条命令一个场景）：
  ```bash
  cd backend && T7_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test_t7qa" \
    uv run python ../tests/load/run_10000.py --scenario normal --size 2000 --latency 3
  ```
  对照组：`--latency 0.2`，同规模 2000。
- 证据文件（已移出项目树）：`/tmp/qa_final_evidence/normal_2000_latency3_qa.json`、`normal_2000_latency02_qa.json`。

### 1.2 本人实测读数

| 组 | 速率 (rows/s) | 在途峰值 mock / db | 验算：速率 × L |
|---|---|---|---|
| normal@2000, **L=3s** | **29.95** | **100 / 100** | 29.95×3 = **89.9 ≈ 100** ✅ |
| normal@2000, **L=0.2s** | 112.3 | **47 / 76** | （不适用，绑定约束在领取路径） |

### 1.3 判定

- **L=3s 下在途峰值同样达到 100**（非子秒场景的 30–47）⇒ 主理人的「延迟依赖性」解释（`完成速率上限 = min(领取路径上限, C/L)`；L=3s 时 `C/L=33.3` 低于领取上限 ⇒ 瓶颈切换为供应商延迟、在途堆满 100）**被本人独立复现**。
- 「在途 ≈ 速率 × 延迟」关系独立验算成立：29.95 × 3 = 89.9 ≈ 实测峰值 100（误差 ~10%，量级一致；峰值本身是采样上界）。
- 对照组钉死延迟依赖性：同一规模、同一命令，仅改 `--latency`，在途峰值从 47 → 100。**差异只可能来自延迟参数**。
- 附注：本人 L=0.2 速率 112.3 高于主理人 10k 口径的「领取上限 ≈90」——因本人用 2000 规模、行锁竞争更低，与解释不冲突（领取上限随 N 变差）。
- **结论：§11「恰有 100 路真实在途并发」判定站得住，本复核支持。**

---

## §2 变异测试结果表（断言是否有牙齿）

纪律：改前 `cp` 备份到项目树外（`/tmp/qa_final_evidence/mutations/*.orig`），测完 `diff -q` **逐字节确认还原**，并 grep 确认无残留 MUTANT 标记。4 个文件全部 `PRISTINE`（byte-identical）。

| # | 变异内容 | 文件:位置 | 命令 | 原始失败断言文本 | 还原证据 | 结论 |
|---|---|---|---|---|---|---|
| **MA** | 使 mock 故障注入失效（`_planned_error`/`_planned_text` 恒返回合法输出） | `tests/load/mock_provider.py` `_planned_error`/`_planned_text` | `run_10000.py --scenario permanent --size 200 --latency 0.05` | `FAIL call_log … actual calls=200 expected=240 match=False, sequence mismatches=20`；汇总 `IS_PASS=NO`（R2(a) 故障按计划触发断言变红） | `diff -q` → identical，无残留标记 | **有牙齿** |
| **MB** | 向 worker 植入「无效输出 → 补 `unsure`」（红线 #1 违规） | `backend/app/worker/execute.py` `InvalidOutputError` 分支 | ① `pytest tests/test_worker.py tests/test_reliability_fixes.py tests/test_reports.py -q` ② 同 MA 命令跑 R2(b) | ① **4 failed**：`test_invalid_output_never_becomes_valid_answer`、`test_retry_success_yields_single_valid_answer`、`test_failed_member_answer_is_sql_null_not_json_null`、`test_distribution_has_no_null_bucket` ② `db succeeded 但无任何 ok 调用的条数 = 20`（R2(b) 无补值分支产出有效答案 ⇔ 变红） | `diff -q` → identical | **有牙齿**（若任一链路不变红即为 P0——实际双线均红） |
| **MC** | 破坏 cancel 幂等：重复 cancel 返回 409 | `backend/app/runs/service.py` cancel 幂等分支（重复 cancel 改 raise 409） | `run_10000.py --scenario interference --size 200 --latency 0.05` | `FAIL cancel_repeat_200`；step `run_a.cancel_repeat.status = 409`（期望 200） | `diff -q` → identical | **有牙齿** |
| **MD** | worker 入口 `main()` 立刻 return（回归历史缺陷 N7-d） | `backend/app/worker/main.py` `main()` 首行 return | `pytest tests/test_worker_entrypoint.py -q` | **3 failed**：`startup log not found` / `worker did not start`（E2E 入口守卫变红；压测 worker 不在场景进程内故以入口测试为判据） | `diff -q` → identical | **有牙齿** |

**重要过程发现（已处置）**：MC/MB 的变异运行**覆写了交付物** `tests/load/results/permanent_200.json`、`interference_200.json`。已在还原后用 pristine 代码**重跑再生成**，并逐项核验（permanent_200：completed_with_errors / valid=180 / failed=20；interference_200：26 项 checks 全 true、cancel_repeat=200）——与变异前语义一致，交付物无损。本人实验产生的 `normal_2000.json` 已 `mv` 出项目树。

---

## §3 原生 SQL 独立重推不变量（只读审计库 survey_test_t7）

方法：`docker exec survey-pg psql -U postgres -d survey_test_t7 -f` 执行**本人自写** SQL（10 组，未照抄 acceptance §2.9）。**全程只读，零写入。**

### 3.1 本人 SQL 原始输出（要点）

```
(A) N_in_N_out + row_no 连续 + persona 唯一：
    4 个 run 全部 ok = t（members=sample_size，min_rn=1，max_rn=N，
    distinct row_no=N，distinct persona_id=N）
(B) 重复 (run_id,row_no) / (run_id,persona_id)：0 行
(C) 成员状态分布：全部 succeeded（4 run × 全量成员）
(D) succeeded 合法性：null_ans=0，bad_val=0，bad_qid=0（question_id 恒 purchase_intent）
(E) 五档和 == valid == succeeded：4 run 全部 ok = t
(F) 每个 succeeded 成员恰一条 succeeded attempt：无异常行
(G) 重复 (member_id,attempt_no)：0 行
(H) failed/cancelled 成员带 answer：0 行
(I) attempt 状态分布：failed 共 61 = RATE_LIMITED 33 + SERVER_ERROR 28
    （全部归属 burst run：突发窗口内 61 次故障注入）
(J) runs 表计数器 vs 实算 succeeded：一致
```

成员尝试次数分布：1939 人 @1 次 + 61 人 @2 次（61 人先失败后重试成功）——与 burst 场景设计（61 次注入故障、retry 后全部收敛）**自洽**。

### 3.2 与 acceptance.md §2.9 的对照

**一致。** §2.9 声称的全部不变量（N_in_N_out、无重复、成员全 succeeded、答案五档合法且 question_id 固定、每成员恰一条成功 attempt、失败成员 answer 为 SQL NULL、五档和=valid）均被本人独立写法重推成立；§2.9 未覆盖的 (G)(H)(J) 本人补充验证亦通过。**无分歧。**

---

## §4 T6 E2E 独立验证

### 4.1 整链路独立重跑

```bash
docker compose -f deploy/compose.yaml up -d --build     # api/web/worker/postgres 四服务 healthy
cd frontend && PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npx playwright test tests/survey-flow.spec.ts
```

**原始结果：8 passed（1 个 worker 项目，8 用例全过）**，含字段保真用例（INFLIGHT dom=100/raw=100、THROTTLED dom=40/raw=40、FINAL 18 字段全等）。

### 4.2 字段保真自建核对（⚠️ 最易自欺项）

不信任前端归一化，本人自写 Node 探针（`/tmp/qa_final_evidence/qa_field_fidelity.js`，经 API 建 run、运行中抓 `GET /api/v1/runs/{id}` 原始 JSON 与 DOM 逐字段对照）：

| 字段组 | 结果 |
|---|---|
| 运行中动态字段 | `in_flight` raw=99 / dom=99 ✅；`throttled` raw=40 / dom=40 ✅（>0 快照，排除「0 合法值」陷阱） |
| 终态 18 字段（target_concurrency/in_flight/throttled/sample_size/succeeded/failed/pending/retry_wait/cancelled/valid_count/actual_cost/unknown_cost_count/budget_limit/request_limit/requests_reserved/status/pause_reason/allowed_actions 等） | **18/18 MATCH，0 diffs** |

**判定：未发现任何「后端没返、前端兜 0」缺陷。** `api.ts` 的防御式归一化在本对照下未造成假数字。

### 4.3 其余验收项独立确认

| 项 | 方法 | 结果 |
|---|---|---|
| 重启 API 保进度 | 自写 Python 探针：run 运行中 `compose restart api`，持续比对 | ✅ succeeded 51→287（重启期间 worker 继续写库），最终 completed=5000 |
| 关页任务继续 | E2E 用例覆盖（关浏览器后任务完成） | ✅ passed |
| 失败不可隐藏 | E2E completed_with_errors 用例（失败数 + error_summary 可见） | ✅ passed |
| allowed_actions 驱动按钮 | E2E + 4.2 探针（字段在 18 字段对照内） | ✅ passed |
| worker 入口（N7-d） | `compose logs worker` 启动日志 + MD 变异 | ✅ 正常启动；变异后立即被测试捕获 |
| 收尾洁净 | `compose down -v` 后核对 | ✅ 无残留容器/卷/网络（`docker ps -a`/`volume ls`/`network ls` 过滤 survey-platform 均为空） |

---

## §5 洁净性与文档诚实性审计

### 5.1 项目树残留

- 本人工作产生的唯一新文件 `tests/load/results/normal_2000.json` 已 `mv` 至 `/tmp/qa_final_evidence/`；results 目录恢复 13 个交付物文件。
- 无 `__pycache__`/`.pyc`/备份/探针/日志残留（`find` 全树核查）；`.pytest_tmp/`、`playwright-report/`、`test-results/` 属预期（§7.10），不算问题。
- 4 个变异文件 `diff -q` 全部 byte-identical；全树 grep 无 MUTANT 残留。

### 5.2 容器残留

`survey-pg`（项目外宿主容器）在运行；survey-platform compose 栈 **down -v 后零残留**（容器/卷/网络均空）。

### 5.3 站不住的表述清单

| # | 级别 | 位置 | 表述 | 实测 |
|---|---|---|---|---|
| D-1 | P2 | `docs/acceptance.md` §4.3.1（~L491）、§5（L560） | 以「实测」口径引用 normal@10k=**90.3**、@20k=**84.0** rows/s | 留存结果文件为 **91.771 / 79.29**（91.8/79.3）。acceptance L352–353 已声明「§2.7 为上一轮记录、非矛盾」，但后文 §4.3.1/§5 仍作本轮实测引用、未带该 caveat——**口径不统一**，易误导读者。建议统一为留存文件值或注明「上一轮运行」 |
| D-2 | P2 | `PROGRESS-HANDOFF.md` 状态表 | 多个批次状态已落后 | 主理人已声明收口时重写；此处如实列出，**未改动** |

**如实且站得住的表述**（抽验）：acceptance §1「真实第三方 API 实测未执行」声明在位且多处呼应（红线 #2 守住）；e2e-report-t6.md「8 passed」与本人独立重跑一致；runbook.md 的 compose 启动/关停命令与本人实测一致；frontend 含「模拟购买意向（非真实购买转化率）」标识（PRD §4/P0-08）。

### 5.4 秘密泄漏扫描

`frontend/src` 全量 grep（api_key/secret/token/password/Bearer）仅命中 1 处「输出上限（tokens）」UI 文案，**无任何凭据**；`deploy/mock.env` 为占位符。红线 #3 守住。

---

## §6 主文档 §11 最终验收清单逐条勾选

| # | 条目 | 判定 | 依据（本人原始输出） |
|---|---|---|---|
| 1 | 逐行输入输出、不筛选抽样 | ✅ | SQL (A)(B)：row_no 1..N 连续、persona 唯一、N_in_N_out |
| 2 | 单题、冻结快照 | ✅（题面）/ 部分（快照字段） | SQL (D)：question_id 恒 `purchase_intent`；产品/价格/时间范围冻结字段未逐字段核对 |
| 3 | 10k→10k 成员、每成功成员一条有效答案 | ✅ | SQL (E)(F) |
| 4 | 不冒充真实运行 | ✅ | acceptance §1 声明在位；UI 模拟标识；变异 MB 证伪「补值冒充」路径 |
| 5 | 不依赖浏览器、worker 崩溃恢复 | ✅ | 4.3 重启 API succeeded 51→287；E2E 关页用例 passed；MD 变异证入口守卫 |
| 6 | 无效不补中立/首项；重试失败取消有记录 | ✅ | MB 变异 4 个红线用例 + R2(b) 双线变红；SQL (H)(I) 记录可溯 |
| 7 | 429/5xx/401、预算、未知费用、暂停恢复自动化证据 | ✅ | 还原后全量 pytest 263 passed（含可靠性/预算/未知费用用例）；burst_2000.json 26/26 checks（pause_reason=api_unavailable、preserved=1970、resume 后 lost=0） |
| 8 | 100 路真实在途 + RPM/TPM 限额、重启不突发清空限额 | ✅（在途）/ **未验证**（限额窗口重启持久化） | §1 独立复现 L=3s 峰值 100；「重启不突发清空限额」本人未单独构造实验 |
| 9 | 统计只来源有效答案、五档和=valid、Top-2-Box 可复核 | ✅ | SQL (E)；distribution 无 null 桶（MB 变异对应用例变红反证） |
| 10 | UI/CSV 明确模拟性质、失败覆盖率、分组保留分母 | ✅（UI）/ 部分（CSV） | UI 标识 grep + E2E 失败可见用例；CSV 导出内容未独立核对 |
| 11 | 密钥只在后端、日志/导出不含不必要个人字段 | ✅（密钥）/ 部分（导出字段） | 5.4 扫描；导出个人字段未独立核对 |
| 12 | README/runbook 含启动/环境变量/迁移/恢复/备份/真实实测记录/已知限制 | ✅/部分 | runbook 命令实测可用、含「真实 API 未执行」记录；备份一节未验证 |

---

## §7 缺陷清单

| 编号 | 级别 | 内容 | 证据 | 复现 | 建议修法 |
|---|---|---|---|---|---|
| FIN-1 | P2 | acceptance.md §4.3.1/§5 引用上一轮速率值（90.3/84.0）未带口径说明，与留存结果文件（91.771/79.29）口径不一 | `docs/acceptance.md` L491、L560 vs `tests/load/results/normal_10000.json`/`normal_20000.json` | 读文件对照即得 | 统一为留存文件值，或在该两处注明「上一轮运行记录」 |
| FIN-2 | P2 | PROGRESS-HANDOFF.md 状态表过时 | 逐节对照实际交付批次 | 读文件 | 主理人收口时重写（已知计划，本报告不改） |

**无 P0、无 P1。** 无源代码缺陷；4 项变异全部被捕获；不变量零违背。

## §8 无法证伪、暂时采信的项（不当作已证明）

1. **真实第三方 API 行为**（真实 429/5xx 时序、真实 tokenizer/TPM、真实计费）——本机无凭据，全链路为 mock；采信 acceptance §1 的「未执行」声明。
2. **主理人的 10k/20k 规模数字**（90.3/72.9/78.6/84.0 r/s、10k/20k 在途读数）——本人只独立跑了 2000 规模；万级数字采信留存结果文件与审计库（后者已用 SQL 独立重推）。
3. **§4.8 领取成本随 N 的 EXPLAIN 证据**——本人未独立重跑 EXPLAIN 序列。
4. **RPM/TPM 限额窗口在重启后的持久化**（§11 第 8 条后半）——未独立构造实验。
5. **CSV 导出内容、个人字段最小化**——未独立核对导出产物。

## §9 沙箱现象说明

1. **`bash grep` 不可靠**（漏匹配/返回空）：全程改用 Grep 工具与 `find`，结论不依赖 bash grep。
2. **`ps` 被禁用**：进程存活判断改用 `docker compose ps` / `lsof -iTCP:PORT` / 健康检查端点。
3. **`rm` 常被删除守卫拦截**：清理一律 `mv` 到项目树外 `/tmp/qa_final_evidence/`。
4. **后台任务 ID 失效**：中断恢复后原后台 pytest 任务查询返回 not found，已**前台重跑**确认 263 passed（不采信不可查证的后台输出）。
5. **模型 429 限流中断一次**：已按主理人恢复指令续跑，全部实验重新取得本人原始输出，无结论建立在被中断的半截输出上。

---

## 附：本轮本人亲手执行的可复现命令索引

```bash
# §1 独立复现（隔离库 survey_test_t7qa）
T7_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test_t7qa" \
  uv run python ../tests/load/run_10000.py --scenario normal --size 2000 --latency 3   # 峰值 100/100, 29.95 r/s
T7_DATABASE_URL=... uv run python ../tests/load/run_10000.py --scenario normal --size 2000 --latency 0.2  # 峰值 47/76

# §3 SQL 重推（只读）
docker exec -i survey-pg psql -U postgres -d survey_test_t7 -f /dev/stdin < /tmp/qa_final_q2.sql

# §4 E2E
docker compose -f deploy/compose.yaml up -d --build
cd frontend && PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npx playwright test tests/survey-flow.spec.ts  # 8 passed
node /tmp/qa_final_evidence/qa_field_fidelity.js   # 18/18 MATCH
uv run python /tmp/qa_final_evidence/qa_restart_api.py  # 重启 API 保进度通过
docker compose -f deploy/compose.yaml down -v

# 还原后回归
cd backend && uv run pytest -q          # 263 passed
uv run ruff check app/ tests/           # All checks passed
```
