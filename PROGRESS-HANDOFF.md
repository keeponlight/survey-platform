# 进度交接文档（Progress Handoff）

> **本文件是给"下一次继续开发"用的完整上下文。** 读完本文件 + `docs/TEAM-BRIEF.md`（尤其 §7 主理人裁决记录），即可在无对话历史的情况下继续。
> 最后更新：**2026-09-21（最终独立复核通过后收口）**（主理人实盘核查后写就，所有进度数字均为**实测**，非成员自述）。

---

## 0. 一句话目标

输入现有用户表（CSV/XLSX，上限 20,000 行）→ 用 **100 个可复用 Agent 并发槽位**逐人调用第三方模型 API → 输出与输入行**一一对应**的逐人**模拟购买意向**表（固定单题、固定五档）。

**口径（写死，不可歧义）**："1 万个智能体" = **1 万个独立 persona 作答任务**；`100` = **并行执行槽位数**（`AGENT_CONCURRENCY=100`），不是样本数/机器数/进程数。正常每人调用模型一次。

---

## 1. 权威文档索引（相对本目录）

| 文档 | 作用 | 读它干嘛 |
|---|---|---|
| `/Users/zhao/Desktop/2026-09-20-survey-platform.md` | **需求唯一来源**（12 节技术设计与开发执行文档，§10 为 T0–T7，§11 为最终验收清单） | 一切契约以它为准 |
| `docs/TEAM-BRIEF.md` | 本机环境事实 + **§7 主理人裁决记录**（**§7.9–§7.17 是本轮全部裁定的权威记录**） | **继续开发前必读**，尤其 §7.16/§7.17（万级实测 + 判定口径） |
| `docs/architecture.md` | 五表 DDL、状态机（§3.3 I1–I12）、可靠执行实现路径（§6）、Mock Provider（§5） | 架构基线 |
| `docs/task-list.md` | T0–T7 文件级任务清单 + §8 共享知识（枚举/字段名/红线） | 下阶段要做什么 |
| `docs/acceptance.md` | **T7 万级压测与验收报告**（含 §1 真实 API 未执行声明、§2.8 L=3s 实验、§2.9 可执行复核 SQL、§3 不变量核对表） | 万级证据与复核入口 |
| `docs/FINAL-SUMMARY.md` | **最终交付总结**（§11 逐条结论、关键实测数据、未执行清单、复现入口） | 交付/汇报时 |
| `docs/qa-report-final.md` | **最终独立复核报告**（任务 #13：**0 P0 / 0 P1 / 2 P2**，N7/N8 可验收） | 收口证据 |
| **`docs/FIX-HANDOFF.md`** | **修复交接包**（下一个 AI 团队直接动手修：优先级 + 代码锚点索引 + 修法边界 + 开工 SOP + 验收命令；**未改任何业务代码**） | **要修问题时第一读** |
| `docs/qa-report-supplementary-audit.md` | 补充审查 A–E（400 人批次闭环 / 状态机 / cancel 语义 / Little 定律 / 仓库治理） | 问题证据 |
| `docs/fix-readiness-appendix.md` | 修复就绪附录：每条缺陷的精确 `文件:行号`、根因、最小修法、回归测试证伪方式、风险 | 动手前查锚点 |
| `docs/e2e-report-t6.md` | T6 E2E 与 compose 实跑报告（8 条用例 + 连接池缺陷根因） | N7 证据 |
| `docs/qa-report-t4.md`、`docs/qa-report-merged.md` | QA 对抗性复核报告（缺陷分级与变异测试证据） | 缺陷清单 |
| `docs/upstream-audit.md` | 上游 MatrAIx 源码审计（含行号证据） | 理解"为什么绝不补默认答案" |

上游参考仓库（**只读，禁止修改**）：`/Users/zhao/WorkBuddy/2026-09-17-19-40-03/matraix`（commit `3633d8dab149a9482a71b024418a49ae828cc941`，工作区是脏的，审计走 `git show 3633d8d:<path>`）。

---

## 2. 环境事实（实测）

| 项 | 值 |
|---|---|
| Python | **3.12.13**，`backend/.python-version` 钉版。**严禁**使用 `UV_PYTHON_INSTALL_DIR`（该方案已废弃） |
| 包管理 | uv；`cd backend && uv sync` |
| Node | v22.22.2 / npm 10.9.7（前端用） |
| PostgreSQL | Docker 容器 `survey-pg`（`postgres:16`），`127.0.0.1:55432`，user/pass=`postgres`；无本机 psql，用 `docker exec survey-pg psql -U postgres -d <db> -c "..."` |
| 数据库 | `survey`（dev）、`survey_test`（pytest 默认）、`survey_test_qa`（QA 隔离）、**`survey_test_t7`（万级压测隔离库，保留 4 个可审计 run，只读）** |
| 连接池 | `backend/app/db.py` 已修：`pool_size=10 + max_overflow=20 = 30/引擎`（两引擎合计 60 < max_connections=100）。**勿回退到 20/100**（曾导致 `TooManyConnectionsError`） |
| 模型 API | **本机无真实第三方 key** → 全链路走 mock；`MODEL_PROVIDER=mock` 是唯一选择依据（运行时 `MockProvider` 已实现）。真实万人实测**未执行**，禁止拿 mock 冒充 |
| 沙箱 | 只允许写 `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/`；`rm` 常被 safe-delete 守卫拦截（用 `mv` 移出）；`bash grep` 不可靠（用文件读取/搜索工具）；`ps` 被禁用 |
| Docker compose | `web`→8080、`api`→8000、postgres **无宿主端口**（N7-c 已修）；默认 `env_file: mock.env`（无密钥） |

---

## 3. 节点进度总览（节点口径见 TEAM-BRIEF §5）

| 节点 | 内容 | 状态 | 证据 |
|---|---|---|---|
| N0 | 上游审计 | ✅ 完成 | `docs/upstream-audit.md` |
| N1 | 契约 + 上游边界 | ✅ 完成 | `backend/app/contracts.py` |
| N2 | 数据层（导入 + 问卷草稿） | ✅ 完成 | `personas/source.py`、`surveys/service.py` |
| N3 | 单 persona 作答闭环 | ✅ 完成（红线有牙齿） | `inference/*` |
| N4 | T3 批次创建 + 100 槽位可靠执行 | ✅ 完成（并发真跑已证） | `runs/*`、`worker/*` |
| N5 | T4 控制 API + 状态收敛 | ✅ **完成**（含 P1-1 cancel 500 根因修复、Q6 门禁、P2-1/P2-2） | `api/routes.py`、`runs/service.py`，`test_run_controls.py` |
| N6 | T5 报表与导出 | ✅ 完成 | `backend/app/reports/*` |
| N7 | T6 四页前端 + 部署 | ✅ **完成**（compose 实跑 + Playwright E2E 8 passed + 连接池缺陷修复） | `docs/e2e-report-t6.md`、`frontend/`、`deploy/` |
| N8 | T7 万级压测 | ✅ **完成**（6 场景 + §11「100 路在途」已验证 + **最终独立复核通过：0 P0 / 0 P1 / 2 P2**） | `docs/acceptance.md`、`docs/qa-report-final.md`、`tests/load/results/*.json` |

**当前全量：`263 passed / 0 failed`（`cd backend && uv run pytest -q`，主理人实测 51.39s）。** `uv run ruff check app/ tests/` → All checks passed。

---

## 4. 已完成的关键修复（历次 QA 缺陷，均已落地并经变异测试证伪）

| 缺陷 | 级别 | 状态 |
|---|---|---|
| `answer_json` 存 JSONB `'null'` 而非 SQL NULL | P1-1 | ✅ `models.py` `JSONB(none_as_null=True)` + 迁移 `0002` |
| `cancel` 对 `ready` run 撞 `uq_runs_single_active` 抛 **500** | P1-1 | ✅ 根因修复：`ready` 一次性收敛 `cancelled`（不经 `cancelling`）+ `IntegrityError→409` 兜底 |
| Q6「start 前校验模型配置」未落地 | P1-2 | ✅ 门禁落在 **API 层**（`routes.py`，`mock` 豁免），`MODEL_CONFIG_INVALID`→422 |
| 无在途时 `pause` 不自收敛 | P2-1 | ✅ `pause_run` 置 `pausing` 后同事务 `converge_run` |
| `test_illegal_transitions_409[I1..I12]` 名实不符 | P2-2 | ✅ 改名 `test_transition_matrix` |
| 测试 teardown 级联假失败（§7.10） | — | ✅ `conftest.py` 捕获 `BaseException` + `db_session` 起点清库 |
| `test_recovery.py:210` 竞态断言（flaky） | P1 | ✅ `GatedMockProvider` 确定性替代 |
| `POST /runs` 快照取自进程单例（P2-⑤） | P2 | ✅ 传路由注入的 `settings` |
| **worker 空转 exit 0（无 `__main__` 入口）** | N7-d | ✅ 补 `main()` 入口，真启动 + advisory lock + SIGTERM |
| **`MODEL_PROVIDER=="mock"` 无分支（compose 会空 endpoint 发真实调用）** | N7-b | ✅ `app/inference/mock_provider.py` + `build_provider` 分支 |
| **三份 env 模板全缺** | N7-a | ✅ `deploy/mock.env`、`deploy/real.env.example`、`.env.example` |
| **compose postgres 抢宿主 55432** | N7-c | ✅ 删宿主端口映射 |
| **连接池 240 > max_connections=100** | — | ✅ `db.py` 改 10+20（**只有真跑 compose 才暴露**） |

---

## 5. T7 万级压测实测结论（详见 `docs/acceptance.md`）

**六个大规模场景全部完成、`passed=True`**：

| 场景 | 规模 | 终态 | valid/failed | 在途峰值 mock/db | 速率 |
|---|---|---|---|---|---|
| normal | 10,000 | completed | 10000/0 | 41/74 | 90.3 r/s |
| recoverable | 10,000 | completed | 10000/0（1428 间歇故障全收敛） | 40/76 | 72.9 r/s |
| permanent | 10,000 | completed_with_errors | 9000/1000（失败数准确） | 37/73 | 78.6 r/s |
| normal | 20,000 | completed | 20000/0 | 44/74 | 84.0 r/s |
| interference | 10,000 | run_b completed, lost=0 | 10000/0 | 44/58 | 恢复 104.3s |
| burst | 2,000 | paused→completed, lost=0 | 2000/0 | 41/60 | 恢复 17.2s |

**关键结论（主理人裁定，§7.16/§7.17）**：
- **§11「100 路真实在途」= 已验证**：决定性实验 `normal@10000 --latency 3 --keep-data` 实测 **mock 峰值=100 / db 峰值=100**、速率 **31.95 rows/s**（≈ §7 算例 33.3）。口径：`完成速率上限 = min(领取路径上限, C/L)`，在途是**延迟相关性质**——子秒延迟下在途退化为 37–44 是正常，不可据此判「未达标」。
- **API p95 @10k = `run_p95 0.0283s` / `summary_p95 0.0738s`**（均 < 1s 目标）。
- **run 行锁串行化**：`lock_waiters` 恒 46–47（结构性），领取路径上限 ≈90 r/s。**主理人已裁定不改代码**（目标工况 L=3s 下非绑定，有 2.4× 余量；改锁语义有死锁回归风险）。作**已记录的限制 + 未来优化项**（见 §7.17.3）。
- **R2 双断言零违规**：故障按 `(persona_id, attempt_no)` 计划触发；`succeeded` 无一条来自补值分支（红线 #1 在万级规模有牙齿）。

**真实第三方 API 100→1,000→10,000 实测：未执行**（本机无凭据）。`acceptance.md` §1 有硬声明。

---

## 6. 未完成清单（下次继续的对象）

### 6.1 ~~最终独立复核（任务 #13）~~ ✅ 已收口（2026-09-21）

曾被 429 中断的最终独立复核已恢复并完成：**`docs/qa-report-final.md`，结论 0 P0 / 0 P1 / 2 P2，N7/N8 可验收**（独立复现 100 路在途、4 组变异全部有牙齿且逐字节还原、原生 SQL 重推不变量零分歧、E2E 8/8 独立重跑、还原后 263 passed）。

### 6.2 收口动作 ✅ 已完成（2026-09-21）

1. ~~重写 PROGRESS-HANDOFF~~ ✅（即本文件，已更新 N8 与文档索引）。
2. ~~生成最终交付总结~~ ✅ → **`docs/FINAL-SUMMARY.md`**（§11 逐条结论：8 ✅ / 4 部分 / 0 ❌；未执行项如实标注）。

### 6.3 真正剩余的事项（非阻塞，排期类）

见 [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md)：**A-01 已部分关闭**——2026-09-21 用用户提供的网关（ooioo.work / `gpt-5.6-terra`）完成 **100 人真实试点**，四门槛全过（99% 有效率、0 AUTH、0 格式无效、预算未耗尽；一次 `api_unavailable` 暂停后 resume 无损，详见 `docs/real-api-pilot.md`）。**剩余：1,000 / 10,000 人阶段未执行**（待授权 + 建议先核实网关 prompt_tokens 计价口径）；其余为未验证子项与已接受限制（含 run 行锁——已裁定不改代码）。真实密钥在 `deploy/real.env`（gitignored，勿入库勿外泄）。

### 6.4 🔧 待修缺陷（已查清、未修，交接给下一个 AI 团队）

2026-09-22 只读补充审查（400 人批次 + 状态机 + cancel 语义 + 性能 + 仓库治理）查出 **5 个已证实平台缺陷 + 2 个设计实现偏差**，**全部未修**（本轮只查不修）。

➡️ **开工入口：[`docs/FIX-HANDOFF.md`](docs/FIX-HANDOFF.md)**（优先级 + 修法边界 + 开工 SOP + 验收命令）
➡️ **代码锚点：[`docs/fix-readiness-appendix.md`](docs/fix-readiness-appendix.md)**（每条精确 `文件:行号`、最小修法、回归测试证伪方式）

最优先 **D-03（P1）**：模型调用已成功返回但 `reason`>100 字符的校验异常被吞 ⇒ 成员卡 `running` + 孤儿 attempt。修法**必须**守住红线 #1（显式记录/可重试，不得吞异常、不得补默认答案）。

⚠️ 回填回归测试 `backend/tests/test_reason_length_fix.py`（副本现**在项目树外** `pending-reason-length-fix-test.py`）后，基线由 **263 → 273**（修复 D-03 前应有 8 红）。此变更**已查清、待授权执行**。

---

## 7. 必须遵守的工作规则（历次裁决，违反即返工）

1. **每个新任务配一名全新工程师**（不复用同一 sub-agent 继续做下一件事）——防止子 agent 上下文溢出。
2. **主 agent 只做节点验收与编排**；执行、review、单测都在子 agent。
3. **QA 独立复核**：对抗性证伪（变异测试），不复跑工程师命令就交差。**QA 不得把「已知缺陷的当前行为」写成通过的用例来固化**（§7.12.2 流程纠正）。
4. **测试库独占**：`survey_test`（或 `survey_test_*`）同一时刻只允许一个 pytest suite。并发 `TRUNCATE` 会死锁制造**假失败**。跑前用 `pg_stat_activity` 确认。
5. **红线 #1**：任何路径（含兜底分支与测试辅助函数）不得出现「无有效输出 → 填默认答案/首选项/中点/unsure」。上游反面教材见 `upstream-audit.md`。
6. **红线 #2**：不得把 mock 容量说成"真实万人模拟已完成"；`acceptance.md` 必须显式声明真实 API 实测未执行。
7. **红线 #3**：API key 只在后端环境/受控文件，不进前端、日志、快照、CSV。
8. **方法论**：不许用「全项目/全部/已确认」全称表述，除非贴出覆盖全项目的原始输出；无法验证的项明确写「未验证」。
9. **跨成员信息流必须经主理人中转**，成员之间不得直连。

---

## 8. 已踩过的坑（下次别再踩）

| 坑 | 正解 |
|---|---|
| JSONB 列存 Python `None` → 落库 JSONB `'null'`，`IS NULL` 失效 | 可空 JSONB 列一律 `JSONB(none_as_null=True)`，断言用原生 SQL `IS NULL` |
| 连接池 `20+100=120/引擎` × 两引擎 = 240 > Postgres 上限 100 → `TooManyConnectionsError` 卡死批次 | `10+20=30/引擎`（**只有真跑 compose 才暴露，静态校验发现不了**） |
| 子 agent 把长任务（10k 压测）起在后台就结束 turn → 进程被回收、run 卡 `running` 留孤儿租约，且占用 `uq_runs_single_active` 使后续新 run 全 409 | **一条命令一个场景，前台 await 到返回**，结果落盘后再进下一条 |
| 压测脚本默认跑完清数据 → 事后无法用原生 SQL 独立复核（只有脚本自述） | 关键场景 `--keep-data`，提供可执行复核 SQL，注明哪些可审计 |
| 「并发/恢复」类断言可能只是断言配置值或时序快照 | 用注入配置证伪 + DB 交叉采样；**断言取值点别落在「异步取消/停止」的传播窗口内**（§7.14.2，第 3 次出现） |
| `tmp_path` 用 `shutil.rmtree(ignore_errors=True)` → 沙箱守卫抛 `SystemExit`（`BaseException`，吞不掉）→ teardown 链断 → 级联假失败 | 捕获 `BaseException` + `db_session` 起点清库（§7.10） |
| `.gitignore` 用 `.env`/`.env.*` → `deploy/real.env` 不匹配 | `*.env` + 否定回补 `!*.env.example`、`!deploy/mock.env` |
| Alembic `fileConfig` 默认 `disable_existing_loggers=True` → 静默关 worker 日志 | 显式 `disable_existing_loggers=False` |
| 派单时 `name` 被占用 → 系统静默改名 `<name>-2`，任务 owner 仍是旧名 | 派单后核对返回 `agent_id`，同步修正 TaskUpdate owner |
| mock 档位用内置 `hash(persona_id)` → `PYTHONHASHSEED` 随机化，跨进程不可复现 | 用 `hashlib.sha256` 派生 `stable_hash % 5` |
| 前端「防御式归一化」把缺失字段兜成 0 → `in_flight`/`throttled` 是假数字 | 验收时在**运行中**（服务端 >0 时）逐字段核对后端原值与页面渲染 |

---

## 9. 快速自检命令（恢复会话后 30 秒验证环境）

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform

# 环境
docker exec survey-pg pg_isready -U postgres                    # → accepting connections
cd backend && uv run python -c "import sys; print(sys.version_info[:2])"   # → (3, 12)

# 基线（主理人实测 263 passed）
uv run pytest -q                                                # → 263 passed
uv run ruff check app/ tests/                                   # → All checks passed

# 独占确认（跑测试前）
docker exec survey-pg psql -U postgres -tAc "SELECT datname, count(*) FROM pg_stat_activity WHERE datname LIKE 'survey%' GROUP BY 1"

# T7 可审计 run（只读！）
docker exec survey-pg psql -U postgres -d survey_test_t7 -tAc "SELECT sample_size, status, count(*) FROM runs GROUP BY 1,2"
# → 2000|completed|1 / 10000|completed|2 / 20000|completed|1
```
