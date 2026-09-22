# 已知问题台账（KNOWN-ISSUES）

> **本文件用途**：把散落在 [`TEAM-BRIEF.md`](./TEAM-BRIEF.md) §7、[`acceptance.md`](./acceptance.md)、
> [`e2e-report-t6.md`](./e2e-report-t6.md)、[`../PROGRESS-HANDOFF.md`](../PROGRESS-HANDOFF.md) 中的
> 「没做完 / 没验证 / 已知限制 / 待办」**合并为一个分类台账**，供下一个 AI / 开发者快速接手。
>
> **阅读规则**：
> 1. **严禁把任何「未执行 / 未验证」项当作已完成使用**；引用本台账数字时必须同时引用来源节号。
> 2. §C 类是**已被主理人裁定接受**的限制，**勿当缺陷重开**。
> 3. 唯一权威来源是 [`TEAM-BRIEF.md`](./TEAM-BRIEF.md) §7；本台账与 TEAM-BRIEF 冲突时以 TEAM-BRIEF 为准。
> 4. 本台账只收录**能在来源文档中找到出处**的项；无法溯源的内容不收。
> 5. 状态取值：`未执行` / `未验证` / `部分验证` / `已接受（裁定）` / `待办`。

---

## §A 未执行（硬项）

> 可执行、但因本机无凭据或本轮窗口未排期而**没有跑过**的实测项。

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| A-01 | 红线 | **真实第三方 API 三级实测：100 人试点已完成（2026-09-21），1,000 / 10,000 人未执行**。试点实测（网关 ooioo.work，模型 `gpt-5.6-terra`，`real-api-pilot.md`）：有效率 **99/100 = 99%**（≥98% ✅）、AUTH=0 ✅、INVALID_OUTPUT=0/119（response_format 全程被遵守）✅、requests_reserved=119 ≤ 300 ✅；出现一次 `pause_reason=api_unavailable`（100 路突发下约 15% 首调失败触发 §6.4 保护）→ resume 后收敛、lost=0；延迟 p50 23.6s / p95 45.5s（100 路并发）；usage 100% 返回；费用口径仍为「未知」。**红线 #2 仍守住**：1k/10k 未做，不得声称「真实万人已完成」。⚠️ 网关限流/延迟特性见 C-12（429 随时间剧烈变化，放量前必须核实） | [`real-api-pilot.md`](./real-api-pilot.md)；[`real-api-latency-51s-investigation.md`](./real-api-latency-51s-investigation.md)；[`acceptance.md`](./acceptance.md) §1 | 部分执行 | ① 授权后进入 1,000 人阶段（**并发阶梯爬升**，C=60 档实测最划算；或接受暂停-恢复语义）② 先向网关核实 prompt_tokens 计价口径（实测 4.3–5.2k/请求，偏高）③ **核实限流策略**（见 C-12：429 随时间变化）④ 真实扣费未验证 |
| A-02 | T7 压测 | `interference` / `burst` 的 **≥2000 规模复跑本轮未做**（10k 与 2000 的首次运行已有，复跑未做） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.16.5、§7.17.5-6 | 未执行 | 在独占窗口复跑两场景（参考 `--size 10000`），回填 acceptance §3 核对表 |

---

## §B 未验证 / 部分验证

> 有证据但**不构成完整验证**：采样而非连续测量、静态推断、单进程、人造态、采信未证伪等。
> **不得当作已证。**

### B.1 T7 万级实测的验证边界

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| B-01 | 并发测量 | 连续时间「在途 ≤100」**仅靠采样**（5379 点 / 313s）+ §7.6.3 P2-2 的结构性保证，**未做连续测量** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.5-1；[`acceptance.md`](./acceptance.md) §2.8 问题 2 | 部分验证 | 如需连续证明，须在 worker 占槽路径加结构性计数（改代码，须先经主理人裁决） |
| B-02 | 并发测量 | `lock_waiters` 在 L=3s 时未显著下降（46 vs 47）的**成因未查**（仅记录事实；主理人原预期被证伪） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.2、§7.17.5-2 | 未验证 | 若要优化领取路径，先查明因再动锁 |
| B-03 | 性能推断 | 「领取累计成本 ≈ ΣP 超线性」由**单次 EXPLAIN + `limit=1` 设计推得**，未 instrument 每 run 领取总耗时 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.5-3 | 未验证 | 需要精确账时再加观测，当前非绑定约束 |
| B-04 | 性能推断 | EXPLAIN 的「全可领取 / 尾部 100」为事务内**人造态**，与真实运行中途的分布不完全等同 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.5-4 | 未验证 | 同 B-03 |
| B-05 | 审计缺口 | 上一轮 `normal`/`recoverable`/`permanent` @10,000、上一轮 `normal`@20,000、`interference`@10,000 的 13 条不变量**仅脚本自述**（数据被后续 run 清空，外部无法重推）；现仅 4 个 `--keep-data` run 可独立复核 | [`acceptance.md`](./acceptance.md) §2.9；[TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.16.4 | 部分验证 | 关键场景一律 `--keep-data` + 提供复核 SQL（本轮已补齐两个 run，维持该纪律） |
| B-06 | 规模外推 | 200 规模 DB 抽查结论（合法五档 / 可归因 / failed 无答案）**不外推到 10,000 / 20,000** | [`acceptance.md`](./acceptance.md) §2.5 | 部分验证 | 万级不变量已改由 §2.9 复核 SQL 独立重推，勿引用 200 规模结论代证 |

### B.2 合并复核轮（§7.11.6 / §7.12.4）登记、且**至今仍未消除**的项

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| B-07 | 并发验证 | 多进程真并发下的唯一索引（`uq_runs_single_active`）行为**未重复验证** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.11.6-7、§7.12.4 | 未验证 | 起多 uvicorn worker 或 compose 多实例复测并发 start |
| B-08 | 静态推断 | P2-⑤ 的「生产中性」为**静态推断**，未起真实 uvicorn 进程实测（仅 ASGI 进程内验证注入 settings 生效） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.11.6-8、§7.12.4 | 未验证 | 风险低（生产二者同单例），重测时顺带 |
| B-09 | 单进程验证 | P1 新机制（`GatedMockProvider`）**仅在单进程 asyncio 下验证** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.12.4 | 部分验证 | 多进程形态下 recovery 语义由 T7 interference 场景（进程内独立 Worker）间接覆盖，真多进程未测 |
| B-10 | mock 一致性 | 运行时 `app/inference/mock_provider.py` 与 `tests/load/mock_provider.py` 的**故障矩阵未逐条比对** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.11.6-6 | 未验证 | 下一轮 QA 复核时逐条核对故障矩阵 |

> **已由后续批次闭环的 §7.11.6 项（留痕，勿再当未验证）**：① T7 万级压测未执行 → 已由 §7.16.1 四场景 + §7.17 补测闭环；
> ③ 100 路真实在途未重测 → 已由 §7.17.1 `normal@10000 --latency 3` 闭环（判定「已验证」）；④ `docker compose up` 未执行 → 已由 §7.15 闭环；
> ⑤ Playwright E2E 未执行 → 已由 §7.15.4（8 passed）闭环。

### B.3 T0–T2 QA「无法证伪、暂时采信」项（T3–T7 复核时须继续跟踪）

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| B-11 | 采信未证 | 两个 persona 请求互不含对方画像（结构上仅序列化当前 persona，**未做极端注入**） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.4 | 未验证 | 终局 QA 复核补注入测试 |
| B-12 | 采信未证 | `ValidatedAnswer` 的 `extra="forbid"` 对未知字段全面生效（**依赖 Pydantic 机制**，未独立验证） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.4 | 未验证 | 同上 |
| B-13 | 采信未证 | provider 关闭不透明重试（`provider.py:107 retries=0` **静态可证**，未做重试计数实测） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.4 | 未验证 | 用故障 mock 数实际调用次数 |
| B-14 | 采信未证 | T2 真实第三方 API 调用未测（本机无 key）——并入 A-01 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.4 | 未执行 | 同 A-01 |

### B.4 T6 E2E 的验证边界（工程师已如实声明，主理人确认）

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| B-15 | 前端 | 前端 normalize 仅验证「服务端**有**字段时相等」；「服务端**缺**字段时的兜底行为」**未构造场景** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.15.4；[`e2e-report-t6.md`](./e2e-report-t6.md) §7-5、§5.4 第 4 项注 | 未验证 | 终局 E2E 补一个 mock 缺字段场景 |
| B-16 | E2E | E2E 以 `workers:1` **串行**运行，多 worker 并行场景未验证 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.15.4；[`e2e-report-t6.md`](./e2e-report-t6.md) §7-6 | 未验证 | 风险低（用例已做跨用例隔离 `cancelActiveRuns`），必要时并行跑一轮 |
| B-17 | E2E | Chromium 仅落地「优先级 1」（默认缓存）；优先级 2/3（项目内路径 / 系统 Chrome）**未启用未验证** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.15.4；[`e2e-report-t6.md`](./e2e-report-t6.md) §7-4 | 未验证 | 换机/换环境时按需启用 |
| B-18 | 真实 provider | 真实 provider 路径未跑：`OpenAICompatibleProvider` 的真实 HTTP / 鉴权 / 限流（429/5xx）/ 超时路径未跑；mock 响应**瞬时**（`duration_ms=0`），真实网络延迟 / 供应商限流下行为未覆盖 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.15.4；[`e2e-report-t6.md`](./e2e-report-t6.md) §7-1 | 未验证 | 同 A-01（取得凭据后首测） |
| B-19 | E2E 规模 | E2E 最大批次 2000 成员；浏览器链路在万级规模下未压测（万级为 T7 职责，非 E2E） | [`e2e-report-t6.md`](./e2e-report-t6.md) §7-2 | 未验证 | 如需，前端页面在 10k run 上抽样核对 |

### B.5 性能与恢复测量缺口

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| B-20 | 性能 | **运行中（非终态）**的 API p95、以及 **20,000 规模**的 API p95 未测（本轮 p95 仅在已完成 run、10k 规模上测） | [`acceptance.md`](./acceptance.md) §4.5、§5 未执行-3、§3.2-7 | 未执行 | 独占窗口补测运行中 p95 |
| B-21 | 性能 | **内存 / 句柄长时采样未执行**：仅有单批峰值，无「不随规模线性泄漏」的长时证据，**不能断言「不泄漏」** | [`acceptance.md`](./acceptance.md) §3.2-8、§5 未执行-4 | 未执行 | 长跑采样（多批连续）后回填 §3.2 第 8 行 |
| B-22 | 性能 | RPM/TPM 或预算**真实越界**场景未执行（mock 下 RPM/TPM 被刻意调高以避免限流成为瓶颈） | [`acceptance.md`](./acceptance.md) §3.1-7、§5 未执行-6 | 未执行 | 真实凭据到位后测；mock 下可用哨兵模拟但仍是 mock |
| B-23 | 性能 | 供应商 429/5xx/401 的**真实**行为、真实 tokenizer/TPM 校准、真实费用结算 —— 均未执行 | [`acceptance.md`](./acceptance.md) §5 未执行-5 | 未执行 | 同 A-01 |
| B-24 | 恢复 | **运行中**的大批量重试恢复耗时、以及 20,000 规模的恢复耗时未测 | [`acceptance.md`](./acceptance.md) §4.6 | 未执行 | 旗舰 run `--keep-data` 后补测 |

---

## §C 已知限制（已被裁定接受，勿当缺陷重开）

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| C-01 | 扩展性 | **run 行锁串行化扩展性上限（已定位，非阻断）**：`claim_members` 对同一 run 行 `FOR UPDATE` 使领取路径串行化；起始态单次领取 10k `21.3ms` / 20k `45.2ms`（外排 4.1MB / 8.1MB），速率 91.8 → 79.3 rows/s（**−13.6%**）；目标工况（L=3s）下非绑定，余量约 **2.4×**。**主理人裁定不改代码**；未来优化项 = run 计数无锁原子自增 / 分片计数行，**须先重做锁顺序与死锁论证** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.3、§7.16.3；[`acceptance.md`](./acceptance.md) §4.3.1、§4.8 | 已接受（裁定） | 仅在需要「亚秒供应商延迟 + >90 rows/s」时启动优化，先重做死锁论证 |
| C-02 | 设计行为 | `resume_run()` 按 §6.4 **清空 `pause_reason`**（事后查库读到 NULL 属设计行为）；暂停原值必须在暂停点即时捕获 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.14.1；[`acceptance.md`](./acceptance.md) §2.6 | 已接受（裁定） | 断言 `pause_reason` 一律在暂停点用 DB 原生 SQL 读 |
| C-03 | 设计行为 | `burst` 是**时间相位事件**，「哪些成员撞上突发」取决于调度时序，**不可 per-member 复现**（拆分比例与 429/503 构成每次运行会变；`succeeded=30`、`preserved=170`、`lost=0` 等为不变量） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.14.1；[`acceptance.md`](./acceptance.md) §2.3 | 已接受（裁定） | 勿给 burst 写 per-member 确定性断言 |
| C-04 | 测量语义 | `peak_rss_mb` 是**压测脚本进程高水位**（含 mock + 内嵌 Worker + httpx/asyncpg），**不等于生产形态**（API/worker 独立进程）的 RSS，仅量级参考 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.13.4；[`acceptance.md`](./acceptance.md) §4.1 | 已接受（裁定） | 引用 RSS 数字时必须带此语义 |
| C-05 | mock 特性 | mock 哨兵前缀 `__fault_auth__` / `__fault_invalid__` 为**保留值**（仅存在于 `MockProvider`，仅 `MODEL_PROVIDER=mock` 时被选中；**真实 provider 路径不受影响**）；约束：① 爆炸半径受限且须写明；② 可溯源（留日志）；③ `deploy/mock.env` 与 runbook 注明保留前缀。QA 不得判为红线 #1 违规 | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.15.2 | 已接受（裁定） | 真实 provider 模式勿复用此前缀；用户表若撞前缀属 mock 特性 |
| C-06 | 设计行为 | `lock_waiters` 恒 **46–47**（≈连接峰值 84%）为**结构性常量**，不是随领取速率变化的敏感量（主理人「L 增大后下降」的预期已被实测证伪，成因未查） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.2、§7.16.1 | 已接受（裁定） | 同 B-02 |
| C-07 | 首版接受 | `surveys` 列表按 `created_at DESC` 排序但**无对应索引**，首版接受不新增（主文档未强制，量级 20 行内无影响） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.4 P2-c | 已接受（裁定） | 量级变大后再加索引 |
| C-08 | 语义约定 | 错误信息的 `row_no` 是「数据行序号」（**不含表头**，比 Excel 工作表行号小 1），与主文档 §4 一致，接受现状，不为此改代码（API 响应与 runbook 注明该语义） | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.4 P2-b | 已接受（裁定） | 前端展示时注明口径即可 |
| C-09 | 配置决策 | Postgres `max_connections` **保持默认 100 未调整**；应用侧连接池降到 30/引擎（两引擎合计 60 < 100） | [`e2e-report-t6.md`](./e2e-report-t6.md) §7-3；[TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.15.1 | 已接受（裁定） | 调 DB 上限属部署决策，勿回退应用侧池配置 |
| C-10 | 工作约束 | **测试库独占**：`survey_test`（或 `survey_test_*`）同一时刻只允许一个 pytest suite（并发 `TRUNCATE` 会死锁制造假失败）；隔离库（`survey_test_qa` / `survey_test_t7`）是**显式申请**的例外；`survey_test_t7` 的 4 个可审计 run **只读** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.6.1、§7.6.4；[`../PROGRESS-HANDOFF.md`](../PROGRESS-HANDOFF.md) §6.1、§7-4 | 已接受（裁定） | 跑测试前用 `pg_stat_activity` 双确认独占 |
| C-11 | 环境残留 | `.pytest_tmp` 累积残留**属预期**（沙箱 safe-delete 守卫限制批量删），不影响测试正确性，**勿手工批量删** | [TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.10、§7.11.5 | 已接受（裁定） | 写进 runbook 即可（已记） |
| C-13 | 状态语义缺口 | **cancel 无法区分中止来源**：`runs` 无 `cancel_reason`/`abort_reason`；`cancel_run` 无 reason 入参且控制事件无 source 字段（pause 侧反而有 4 值 `pause_reason`）⇒ `用户主动取消 / 质量门禁中止 / 人工运维中止 / 系统异常中止` **一律只显示 `cancelled`**。补充审查实证：400 人批次（run `1aae0492…`）`control_events_json` **只有 create/start，无 cancel 事件**，且代码无任何路径能把 running 成员直接置 cancelled ⇒ 该 `cancelled` **非经服务层产生**（来自外部 SQL） | [qa-report-supplementary-audit.md](./qa-report-supplementary-audit.md) 补充 C | 已接受（缺口，不实施） | 未来可加 `runs.cancel_reason`（可空 + CHECK）+ 显式 `force_cancel_run(reason=...)`，与 control_events 同事务冗余投影；对 `uq_runs_single_active` 无影响。**须经主理人裁决后派单** |：① C=1 零并发直连 e2e 即 **18.5–39.7s**（TTFT 2.2–16.7s + 生成 2.5–12 tok/s）——数十秒级延迟与并发无关；② **429 行为随时间剧烈变化**（试点凌晨 0 个 vs 上午直连 C=100 突发 **96/100 个 429**；突发并发阈值在 60–100 之间，未精确定位）；③ **平台链路实测非瓶颈**（p50 23.6s 与直连 C=1 同量级；领取路径上限 90 rows/s ≫ 需求 4.2 rows/s），且平台的重试+暂停-恢复把直连 C=100 的 **0% 成功提升到 99%**——是必要韧性层而非开销 | [real-api-latency-51s-investigation.md](./real-api-latency-51s-investigation.md)（桌面版 `~/Desktop/真实API延迟51s调查报告-2026-09-21.md`）；关联 [`real-api-pilot.md`](./real-api-pilot.md) | 已接受（实测特性） | 1,000 人阶段前向网关方核实限流策略与容量；并发阶梯爬升（C=60 档实测最划算：58/60 成功、p50 6.6s）；若需个位数秒级延迟须换接入点/网关 |

---

## §D 待办（下一步）

| 编号 | 类别 | 内容（一句话） | 来源（文档 + 节号） | 状态 | 下一步建议 |
|---|---|---|---|---|---|
| D-01 | 硬阻塞 | **最终独立复核报告（任务 #13，`docs/qa-report-final.md`）尚未产出**：`software-qa-engineer-3` 被模型 429 限流中断（重置时间 2026-09-21 22:38:59 UTC+8，或切换模型）；要点：用自己的库 `survey_test_t7qa` 独立复现「100 路在途」（`--size 2000 --latency 3`）、4 组变异证伪、原生 SQL 重推 13 条不变量、独立重跑 compose + Playwright 8 条、主文档 §11 逐条勾选 | [`../PROGRESS-HANDOFF.md`](../PROGRESS-HANDOFF.md) §6.1；[`acceptance.md`](./acceptance.md) §3.2 说明（N8「唯待最终 QA 独立复核收口」） | 待办 | **下次开工第一件事**：恢复或重派最终复核；`survey_test_t7` 只读 |
| D-02 | 收口 | **主理人收口**：最终复核通过后重写 `PROGRESS-HANDOFF.md`（状态表曾因落后于批次 A 被 §7.11.3 P2-④ 裁定须由主理人统一重写）+ 生成最终交付总结（含 §11 逐条结论、所有未执行/未达标项如实标注） | [`../PROGRESS-HANDOFF.md`](../PROGRESS-HANDOFF.md) §6.2；[TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.11.3 P2-④ | 待办 | 依赖 D-01 通过 |
| D-03 | 长期 | 真实第三方 API 三级实测（见 A-01）与 B 区性能补测，**仅在取得凭据 / 排期到独占窗口后执行** | [`acceptance.md`](./acceptance.md) §1、§5；[TEAM-BRIEF.md](./TEAM-BRIEF.md) §7.17.5 | 待办 | 随 D-01/D-02 后按优先级排期 |
| D-04 | 待裁定 | **补充审查（2026-09-22）已查清、未修**（只读审查）；完整优先级与修法边界见 **[`FIX-HANDOFF.md`](./FIX-HANDOFF.md)**，每条的精确 `文件:行号` 与回归测试证伪方式见 **[`fix-readiness-appendix.md`](./fix-readiness-appendix.md)**。**5 个已证实缺陷**：① **D-03（P1）** `reason`>100 字符的校验异常被吞（锚点 `validation.py:117` → 被 `worker/main.py:361-364` 兜底吞）⇒ 成员卡 `running` + 孤儿 attempt，**修法必须守住红线 #1**（显式记录/可重试，不得吞异常、不得补默认答案）；② **D-01** `attempts.finished_at` 取调用前时刻（`execute.py:156`）；③ **D-02** INVALID_OUTPUT 不传 `duration_ms`（`execute.py:212-222`）；④ **D-04** 孤儿 running attempt（`repository.py:499-508` 扫描集不含）；⑤ **D-05** `finish_reason` 未落库（丢弃于 `execute.py:432-435`）。**2 个设计实现偏差**：V-01 cancel 无 reason（已记 C-13）、V-02 INVALID_OUTPUT 可重试的表达与常量集合相反（零行为变更的显式化） | [qa-report-supplementary-audit.md](./qa-report-supplementary-audit.md)、[fix-readiness-appendix.md](./fix-readiness-appendix.md) | 待办 | 按 FIX-HANDOFF §2 顺序修：D-03 → D-02 → D-01 → D-04 → D-05 → V-02；**回填回归测试 `backend/tests/test_reason_length_fix.py` 后基线 263→273（修 D-03 前 8 红），回填与修复均需授权**；V-01/F-01/F-02 为未来项不实施 |

---

## 附：统计

- §A 未执行硬项：**2** 条（A-01 含 5 个子门槛）
- §B 未验证 / 部分验证：**24** 条（B-01 ~ B-24；另留痕 5 项已闭环）
- §C 已接受限制：**13** 条
- §D 待办：**4** 条
