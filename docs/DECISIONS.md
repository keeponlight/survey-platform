# 关键裁决索引（DECISIONS）

> **本文件是什么**：这是 [`TEAM-BRIEF.md`](./TEAM-BRIEF.md) §7「主理人裁决记录」的 **ADR 式索引/摘要**，
> 目的是让接手者**不必通读 600 行** §7 就能抓住每个关键决策。
>
> **本文件不是什么**：**唯一权威来源是 [`TEAM-BRIEF.md`](./TEAM-BRIEF.md) §7。**
> 本索引与 TEAM-BRIEF 冲突时**一律以 TEAM-BRIEF 为准**；每条仅保留「背景 / 裁决 / 理由」各一句话的蒸馏，
> 完整论证、实测证据与必测要求请以来源节号为准。
> **改动任何裁决必须先经主理人**，任何人不得据本索引自行改判。
>
> 日期说明：§7.9–§7.11 为 2026-09-20 夜，§7.12 为 2026-09-20 深夜，§7.13–§7.17 为 2026-09-21 凌晨；
> 更早批次（C/Q/A/R 与 §7.1–§7.6）TEAM-BRIEF 未标注具体日期，统一记「§7 初版（2026-09-20 前）」。

---

### D-01 解释器钉版：Python 3.12.13，弃用 UV_PYTHON_INSTALL_DIR（C1 / R1 / A2）
- **背景**：C1 初版靠 `UV_PYTHON_INSTALL_DIR` 把 3.12 装进项目内 `.pythons/`，R1 指出该变量是会话级、新 shell 会静默回退 3.13；architecture.md 又锁了 3.12.12（语义模糊的旧命名目录）。
- **裁决**：精确钉 **3.12.13**（`backend/.python-version`、`requires-python = ">=3.12,<3.13"`），**禁止**依赖 `UV_PYTHON_INSTALL_DIR` 与项目内 `.pythons/`，删除旧方案；**禁用 3.13**；干净 shell 验收须输出 `(3, 12)`。
- **理由**：主理人实测 `uv venv --python 3.12.13` 无需环境变量即可解析成功；且同一机器上 `3.12` 会解析到 3.12.12/3.12.13 两个补丁，不精确钉版无法跨 shell 一致。
- **来源**：TEAM-BRIEF §7.2（改判，C1 / R1 / A2 的唯一权威）

### D-02 API 与 worker 复用同一镜像、以不同命令启动（C2）
- **背景**：API 与长任务 worker 的部署形态需定死。
- **裁决**：API 与 worker **复用同一镜像、启动命令不同**；严禁同一进程承载长任务；此为**非冲突**，勿再提出改动。
- **理由**：主文档既定架构；进程隔离是「关页任务继续」「重启 API 不丢进度」的前提（后由 E2E 实测证实）。
- **来源**：TEAM-BRIEF §7（C2）

### D-03 worker 重启按 attempts 近 60 秒记录重建限流窗口（C3）
- **背景**：worker 重启后限流窗口（RPM/TPM/预算）是否清零。
- **裁决**：worker 启动**必须**依据 `attempts` 表最近 60 秒记录**保守重建**限流窗口；重启即清空限额**视为缺陷**。
- **理由**：重启清空会使限额形同虚设，违反主文档可靠执行要求。
- **来源**：TEAM-BRIEF §7（C3）；重建幂等性缺陷修复见 §7.6.3 P2-3

### D-04 T0–T6 + T7 mock 容量可验收，真实万人实测标记未执行（Q1）
- **背景**：本机无真实第三方 API key，T7 的「真实 100→1k→10k 实测」无法执行。
- **裁决**：T0–T6 与 T7 容量**按 mock 验收**；真实 API 三级实测**标记未执行**，交付物如实写明，**禁止**以 mock 通过宣称「真实万人模拟已完成」（红线 #2）。
- **理由**：mock 只验证平台自身路径，不证明真实供应商的吞吐/延迟/配额/输出格式。
- **来源**：TEAM-BRIEF §7（Q1）、§4；acceptance §1

### D-05 密钥不进前端（Q3，红线 #3）
- **背景**：无网关，服务端访问口令与 API key 如何存放。
- **裁决**：仅绑 localhost / 受控内网 + 服务端访问口令；**固定管理密钥不进前端静态代码**，API key 只在后端环境/受控文件，不进日志、快照、CSV。
- **理由**：前端静态代码任何人可读，密钥入库即泄漏。
- **来源**：TEAM-BRIEF §7（Q3）

### D-06 Q6 落点改判：模型配置门禁只落在 API 层，mock 豁免（§7.9.2）
- **背景**：QA 实测把 `is_model_configured()` 校验注入服务层会让 16 条合法的服务级用例变红。
- **裁决**：Q6 **只落在 API 层**（`POST /runs/{id}/start` 路由内），**不改** `RunService.start_run` 签名与行为；`model_provider == "mock"` → 跳过门禁，否则要求配置为真且 api_key 非空，不满足 → **422 `MODEL_CONFIG_INVALID`**；mock 豁免与运行时 mock 分支（D-14）**同批交付**。
- **理由**：服务层是内部接口，让服务层承担 HTTP 语义的配置门禁是关注点耦合，且会连带改 16 条无关用例。
- **来源**：TEAM-BRIEF §7.9.2（改判 §7 Q6）

### D-07 前端不引重型 UI 框架，唯一例外 Playwright（Q7）
- **背景**：前端技术选型。
- **裁决**：前端用原生 + 少量样式实现四页，**不引重型 UI 框架**；**唯一例外**是 E2E 用 Playwright（主文档 T6 指定）。
- **理由**：四页表单型界面无需框架；主文档已指定 E2E 工具。
- **来源**：TEAM-BRIEF §7（Q7）

### D-08 活动批次唯一性加 DB 级兜底索引，IntegrityError 映射 409（A1）
- **背景**：应用层 `SELECT … FOR UPDATE` 在结果集为空时不锁任何行，两个并发 `start` 会同时通过检查（竞态不安全）。
- **裁决**：必须补 `CREATE UNIQUE INDEX uq_runs_single_active ON runs ((true)) WHERE status IN ('running','pausing','paused','cancelling');`（**不能**用 `UNIQUE(status)`）；命中冲突时 `IntegrityError` 必须映射 **409** 而非 500；保留应用层检查作前置校验；必测 `test_concurrent_start_only_one_active_run`（恰 1 个 202、其余全 409）。
- **理由**：FOR UPDATE 对空结果集不加锁，只有部分唯一索引能提供真正的并发兜底。
- **来源**：TEAM-BRIEF §7.3（A1）

### D-09 红线 #1 边界：T7 确定性故障注入不属违规（R2）
- **背景**：QA 可能把 T7 故障矩阵的「注入非法输出/429/5xx」误判为红线 #1。
- **裁决**：`tests/load/mock_provider.py` 按 T7 要求**确定性注入**非法输出/429/5xx **属正常故障模拟，不属于红线 #1**；红线 #1 禁止的是平台**消化**无效输出后**补默认答案**；QA 不得据此判违规，也不得据此放过真实补值逻辑。
- **理由**：注入故障是输入侧，补值是平台行为侧，两者必须分开评判。
- **来源**：TEAM-BRIEF §7.1（R2）

### D-10 守库断言放宽为「数据库名以 survey_test 开头」（R3 / §7.6.4）
- **背景**：QA 用隔离库做并行复核时，硬断言 `current_database() == 'survey_test'` 造成 2 个假失败。
- **裁决**：断言放宽为「**数据库名必须以 `survey_test` 开头**」；`survey`（dev）仍被 fail-fast 拒绝；允许 `survey_test_qa`、`survey_test_t7` 等**有意隔离**库。
- **理由**：意图（挡住误连 dev/生产）不变，收益是并行复核与 T7 压测互不污染；**独占规则继续有效**，隔离是显式申请的例外。
- **来源**：TEAM-BRIEF §7.6.4（R3）

### D-11 测试库是独占资源：同一时刻只跑一个 pytest suite（§7.6.1）
- **背景**：并发测试在 `survey_test` 上发生死锁、假失败、采样失真（实测，非推测）。
- **裁决**：`survey_test`（及 `survey_test_*`）**同一时刻只允许一个 pytest suite**；谁跑谁独占，排期由主理人负责；违反期间产生的任何结论**一律作废重跑**；T7 要求更强独占窗口。
- **理由**：并发 `TRUNCATE ... RESTART IDENTITY CASCADE` 与另一事务死锁，并互相清空对方断言数据。
- **来源**：TEAM-BRIEF §7.6.1

### D-12 cancel 对 ready run 一次性收敛 + IntegrityError→409 兜底（§7.9.1）
- **背景**：`cancel_run` 把 `ready` run 写成 `cancelling` 时撞 `uq_runs_single_active`（另一批次 running 中）→ IntegrityError 逃逸 → **500**（唯一会撞索引的转移是 `ready → cancelling`）。
- **裁决**：`ready` run **一次性收敛**：同事务直接落 `cancelled`（含未开始成员、写 `finished_at`），**不写入中间态 `cancelling`**；**同时**捕获 `IntegrityError` → 映射 `ActiveRunConflictError`（409），不得只靠前者；新增回归用例断言不出现任何 5xx。
- **理由**：ready run 没有在途请求，无需经过持久化 cancelling；双保险使冲突路径从根上消失。
- **来源**：TEAM-BRIEF §7.9.1

### D-13 N7-a：三份 env 模板必修（§7.9.4）
- **背景**：`deploy/mock.env`、`deploy/real.env.example`、项目根 `.env.example` **全部不存在**，而 compose 与 runbook 都引用它们（`PROGRESS-HANDOFF.md` §5.2 曾失实声称「均在」）。
- **裁决**：补齐三份模板（**不含密钥本体**，real 模板只放占位符与环境变量名）；修正 PROGRESS-HANDOFF 的失实表述。
- **理由**：无模板则 compose 无法启动、真实部署无从谈起；交付物自述必须真实。
- **来源**：TEAM-BRIEF §7.9.4（N7-a）

### D-14 N7-b：运行时 mock provider 分支——MODEL_PROVIDER 为唯一选择依据（§7.9.4 / T6-d）
- **背景**：compose 声称「默认 mock 模式」，但后端无任何 `MODEL_PROVIDER == "mock"` 分支，`build_provider()` 无条件构造真实 provider，空 endpoint 会发真实 HTTP 并全线失败；草稿还引入过 config 从不读取的幽灵变量 `SURVEY_PROVIDER_MODE`。
- **裁决**：在 `build_provider`（或 worker 装配处）按 **`MODEL_PROVIDER == "mock"`** 返回可运行的确定性 mock provider（按 `(persona_id, attempt_no)` 派生档位，禁止全局 random，与 `tests/load/mock_provider.py` 同接口）；**mock 输出不得被称为「真实模型模拟」**（红线 #2）；mock 分支必须能被测试证伪；幽灵变量已从 env 模板与 runbook 清除。
- **理由**：「mock 豁免」（D-06）等于给名存实亡的模式发通行证，故 Q6 与本条必须同批交付。
- **来源**：TEAM-BRIEF §7.9.4（N7-b）、§7.5（T6-d）

### D-15 N7-c：compose 内 postgres 不映射宿主 55432（§7.9.4）
- **背景**：`compose.yaml` 映射 `55432:5432` 与本机已独占 `0.0.0.0:55432` 的 `survey-pg` 冲突。
- **裁决**：compose 内 postgres **不映射宿主端口**（Compose 网络内用服务名 `postgres:5432`）；若确需宿主访问，改用未占用端口并同步 `runbook.md`。
- **理由**：宿主映射仅为本地工具便利，不应与本机开发库抢端口。
- **来源**：TEAM-BRIEF §7.9.4（N7-c）

### D-16 测试基建 teardown 级联假失败：修法 + 严禁 TMPDIR 绕过（§7.10）
- **背景**：沙箱 safe-delete 守卫在 `tmp_path` 清理时抛 `SystemExit(1)`（`BaseException`，`ignore_errors=True` 吞不掉）→ teardown 链断 → `db_session` 的 TRUNCATE 被跳过 → 3 个 FAILED 全是下游假失败。
- **裁决**：**必修**——捕获 `BaseException` 使清理绝不中断 teardown 链，并加固 `db_session` 起点清库；**严禁把 `TMPDIR="$PWD/.pytest_tmp"` 当作验收前提**（那是绕过安全删除守卫，换来的绿不作为验收证据）；必证原样 `uv run pytest -q` 全绿、3 个 FAILED 逐条单跑通过、连跑两轮不退化。
- **理由**：`uv run pytest -q` 是主文档明列的验收命令，交付时他人照原样执行必须全绿。
- **来源**：TEAM-BRIEF §7.10

### D-17 flaky 竞态断言改确定性判据：GatedMockProvider（§7.11.1 / §7.12.1）
- **背景**：`test_recovery.py:210` 断言「取消瞬间 `running >= 1」是竞态快照，QA 约 5 次命中 1 次；红不可接受却又不能削弱语义。
- **裁决**：新增 `GatedMockProvider`（复用 barrier 思路，**未新增产品代码**）：成员在 provider.answer() 前已在其 claim 事务提交为 running，故「闸门内有调用在等待」⇔「DB 有 running 成员」且不可能自行完成——断言由随机时序快照变为结构性成立；原断言**一条未删**；严禁删用例 / skip / xfail / 放宽容差。
- **理由**：一条间歇可红的断言会让最终验收无法确定性通过，这是交付门槛问题。
- **来源**：TEAM-BRIEF §7.11.1、§7.12.1

### D-18 QA 不得把「已知缺陷的当前行为」写成通过的用例来固化（§7.12.2）
- **背景**：QA 在 `test_qa_merged.py` 写的用例把 P2-⑤ 的**缺陷行为**当成期望值，与 §7.11.2「必须修 P2-⑤」的裁定必然冲突。
- **裁决**：批准工程师越界把该用例期望值**翻转为修复后契约**并改名（红不可接受 + 主动申报 + 断言强度未削弱）；同时立纪律：缺陷应记入 QA 报告并由主理人裁定； characterization 用例必须**显式标注**「刻画的是缺陷行为，修复后必须翻转」，且不得进入验收命令——把缺陷固化成绿色契约会反向阻止修复。
- **理由**：「修好缺陷」看起来像「破坏测试」会真实发生，本次即实例。
- **来源**：TEAM-BRIEF §7.12.2

### D-19 recoverable 改间歇子集注入；全量突发独立为 burst 场景（§7.13.1）
- **背景**：按 §5.2 **全量**注入 recoverable 会让所有成员首调同时失败，触发 §6.4「连续 5 次同类供应商失败 → 暂停 api_unavailable」这条**正确**的保护，批次停而不收敛——**需求内部冲突**。
- **裁决**：① 这是保护机制正常工作，**不是缺陷，不得削弱**；② `recoverable` 改为**确定性间歇子集注入**（按 persona 选 1/3 子集，连续同类失败恒 < 5；确定性来源仍是 `(persona_id, attempt_no)`，未改 `tests/load/mock_provider.py`）；③ 「全量突发」**保留为独立场景 `burst`**（§7.13.2 五条硬断言：paused / pause_reason=api_unavailable（DB 原生 SQL）/ 未执行成员保留且无误判 failed / resume 后收敛且 lost=0 / 失败如实呈现）；④ acceptance.md 必须**显式写出该冲突**与裁定，不得掩盖为「场景设计选择」。
- **理由**：全量突发是证明 §6.4 保护链路的自动化证据，主文档 §11 明确要求「暂停恢复」有自动化测试证据。
- **来源**：TEAM-BRIEF §7.13.1、§7.13.2；acceptance §2.3

### D-20 permanent 场景用「恒非法输出」变体而非 fail_personas（§7.13.3）
- **背景**：主文档 §6.4 对 400/PROTOCOL（供应商配置错误）的正确处理是**暂停**而非判成员失败，无法让 run 收敛为 `completed_with_errors`。
- **裁决**：`permanent` 用「每次非法输出直至额度耗尽」变体（按 §5.2 permanent 规则）实现，**批准**，理由写入脚本 docstring 与 acceptance §5。
- **理由**：只有非法输出路径才会走到成员 failed → `completed_with_errors`，且不违反红线 #1（非法原文被拒，绝不补值）。
- **来源**：TEAM-BRIEF §7.13.3

### D-21 burst 场景采纳：命名与「时间相位事件」定性正确（§7.14.1）
- **背景**：burst 不按 `(persona_id, attempt_no)` 采样，per-member 不可复现。
- **裁决**：**采纳** burst 场景；**不得**假装 per-member 可复现——把「不可复现的部分」明确标出来是本项目要的；不变量（succeeded=30、preserved=170、failed=0、pause_reason、lost=0）与随调度时序变化的部分（pending/retry_wait 拆分、429/503 构成）**分开标注**；脚本侧错误计数与事后独立 psql 读 attempts **逐一吻合**作为交叉证据。
- **理由**：外部供应商瞬时宕机本质上是时间相位事件，诚实标注不可复现性优于伪造确定性。
- **来源**：TEAM-BRIEF §7.14.1；acceptance §2.3

### D-22 纪律：断言勿取「异步取消/停止/收敛」传播窗口内的瞬时快照（§7.14.2）
- **背景**：同一物种的坑第 3 次出现：杀 worker 前快照 `succeeded` 再断言精确相等，竞态窗口内的在途成功使计数 +1（实测 35→36）误判 FAIL。
- **裁决**：写入团队纪律——取值点若落在异步取消/停止/收敛的传播窗口内，**必须先证明取值在窗口内不变**；否则把取值点移到**稳定点之后**（任务已 await、worker 已 stop、sampler 已停），或改用**不变量**（单调不减、恒等式、唯一性）而非瞬时计数；判据：「这条断言在窗口边界上是否可能取到两值？」——能，就是坑。
- **理由**：三次复发（§7.11.1 running 快照、§7.12 取消瞬间成员状态、§7.14.2 succeeded 相等）证明这是系统性陷阱。
- **来源**：TEAM-BRIEF §7.14.2

### D-23 连接池降到 10+20（每引擎 30，两引擎合计 60 < max_connections=100）（§7.15.1）
- **背景**：原 `pool_size=20 + max_overflow=100 = 120/引擎`，api 与 worker 各一引擎 ⇒ 最坏 240 > Postgres `max_connections=100`，100 成员批次跑到一半抛 `TooManyConnectionsError` 卡死——**只有真跑 compose 才暴露**的缺陷。
- **裁决**：`DEFAULT_POOL_SIZE=10` / `DEFAULT_MAX_OVERFLOW=20`（30/引擎），并在 `db.py` 写明依据（**模型调用不占连接**、DB 仅短事务）；修复后实测 `current_connections=18`；方法论结论：**「真跑」不是可选项**，compose up + E2E 不得以「其他层已绿」为由跳过。
- **理由**：模型调用不在任何事务/连接内，30 条/引擎足以支撑 100 路模型并发。
- **来源**：TEAM-BRIEF §7.15.1；e2e-report-t6 §1.1、§2

### D-24 mock 哨兵前缀采纳，附三条约束（§7.15.2）
- **背景**：为让 `INVALID_OUTPUT → failed → completed_with_errors` 与 `api_auth` 暂停链路在 mock 下确定性可达，`MockProvider` 新增哨兵前缀 `__fault_auth__` / `__fault_invalid__`；风险是 `persona_id` 来自用户输入表，理论上可能撞前缀。
- **裁决**：**采纳**，三约束：① **爆炸半径受限且必须明说**——哨兵只存在于 `MockProvider`，仅 `MODEL_PROVIDER=mock` 时被选中，**真实 provider 路径不受影响**，须写进 `mock_provider.py` 注释与 runbook，QA 不得判为红线 #1 违规；② **可溯源**——使用哨兵必须留明确日志；③ **文档化保留前缀**——`deploy/mock.env` 与 runbook 注明两前缀在 mock 模式下为保留值。
- **理由**：哨兵返回的是非法模型原文，由 validation 拒绝，**不是**「消化无效输出后补默认答案」——相反它正是用来证明拒绝路径存在。
- **来源**：TEAM-BRIEF §7.15.2

### D-25 T6 前端四项改动采纳：服务端下发优先（§7.15.3 / §7.15.4）
- **背景**：T6 收官涉及前端目标并发硬编码、暂停文案本地映射、E2E 文件名断言写错等。
- **裁决**：全部**采纳**：`RunDetail.tsx` 目标并发改用服务端 `run.targetConcurrency`（硬编码 100 违反「服务端下发」）；暂停文案优先取服务端 `run.pauseHint`；E2E 导出文件名断言改为 `/^run-.*\.csv$/`（**测试写错修正，非放宽**）；新增 `test_failures_not_hidden` / `test_field_fidelity_vs_raw` / `test_allowed_actions_drive_buttons` 与 `cancelActiveRuns` 跨用例隔离；新增项目根 `.dockerignore`；§5.4 五项验收**认定通过**（含运行中 in_flight/throttled >0 快照证伪「假 0」）。
- **理由**：服务端驱动原则 + 断言强度未削弱（8/8 通过，0 skip/fixme/only/xfail）。
- **来源**：TEAM-BRIEF §7.15.3、§7.15.4；e2e-report-t6 §5.4

### D-26 「100 路真实在途」判定更正：未判定 → 已验证（§7.17.1，承接 §7.16.2）
- **背景**：子秒延迟（`--latency 0.1/0.2`）下在途峰值只有 37–44，工程师据此判「§11 未达标」；主理人认定该推论无效——用**延迟相关性质在自身极端取值下的表现否定该性质本身**是无效推论。
- **裁决**：以 `normal@10000 --latency 3`（对齐主文档 §7 官方算例 L=3s）为**决定性实验**：mock/db 在途峰值**均 = 100**、始终 ≤100、速率 31.95 rows/s ≈ 算例 33.3、`在途 ≈ 31.95×3 = 95.9 ≈ 100` ⇒ §11「恰有 100 路在途」**判定为「已验证」**；知识增量：完成速率上限 = `min(领取路径上限, C/L)`，在途是延迟相关性质，两端实测均印证。
- **理由**：L=3s 下 `C/L = 33.3 < 领取上限 90`，绑定约束是供应商延迟，在途应达 ≈100——实测吻合。
- **来源**：TEAM-BRIEF §7.17.1、§7.16.2；acceptance §2.8、§3.1 第 1 行

### D-27 run 行锁串行化：裁定不改代码，作已记录的限制 + 未来优化项（§7.17.3）
- **背景**：`claim_members` 对同一 run 行 `FOR UPDATE` 使 100 个消费者在领取路径串行化（`lock_waiters` 恒 46–47；起始态领取 10k 21.3ms / 20k 45.2ms；速率 91.8 → 79.3 rows/s，−13.6%）。
- **裁决**：**不实施优化**，理由：① 目标工况（L=3s ⇒ 吞吐上限 33.3 rows/s）下领取路径上限 ≥79–90 rows/s，**有约 2.4× 余量**，不是绑定约束；② 现有「先成员行、后 run 行」锁顺序是**为避死锁环刻意设计**，改无锁原子自增/分片会改变锁语义，**须先重做死锁/竞态论证**（架构层改动，工程师不得自行改代码）；③ 避免镀金。量化事实与优化杠杆写入 acceptance，**不改代码**。
- **理由**：已定位的扩展性上限 ≠ 阻断缺陷；真实回归风险大于当前收益。
- **来源**：TEAM-BRIEF §7.17.3；acceptance §4.3.1、§4.8

---

## 附：索引统计与使用建议

- 本索引共 **27** 条裁决（D-01 ~ D-27）。
- 按主题速查：解释器/环境（D-01、D-13、D-15、D-16）；并发与 DB（D-08、D-11、D-12、D-23、D-26、D-27）；
  模型 provider 与红线（D-04、D-05、D-06、D-09、D-14、D-24）；测试方法论（D-10、D-16、D-17、D-18、D-22）；
  T7 场景设计（D-19、D-20、D-21）；前端（D-07、D-25）。
- **冲突处理**：本索引与 TEAM-BRIEF 冲突时**以 TEAM-BRIEF 为准**；任何改动裁决必须先经主理人。
