# 接管操作手册（Onboarding）

> 面向**下一个 AI / 开发者**：在无对话历史的情况下，按本文档 30 分钟内建立完整上下文并继续开发。
> 与 `../PROGRESS-HANDOFF.md` 互为补充：那边是「现状全景」，这边是「怎么继续干活」。
> 最后更新：2026-09-21（与 PROGRESS-HANDOFF 同步）。

## 1. 30 秒环境自检

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform

# 环境：postgres 容器存活 + Python 钉版 3.12
docker exec survey-pg pg_isready -U postgres                    # → accepting connections
cd backend && uv run python -c "import sys; print(sys.version_info[:2])"   # → (3, 12)

# 基线（主理人实测 263 passed；全量约 51s）
uv run pytest -q                                                # → 263 passed
uv run ruff check app/ tests/                                   # → All checks passed

# 独占确认：跑测试前确认无他人正在使用 survey_test 系数据库
docker exec survey-pg psql -U postgres -tAc "SELECT datname, count(*) FROM pg_stat_activity WHERE datname LIKE 'survey%' GROUP BY 1"

# T7 可审计 run（只读！2000×1 / 10000×2 / 20000×1，均 completed）
docker exec survey-pg psql -U postgres -d survey_test_t7 -tAc "SELECT sample_size, status, count(*) FROM runs GROUP BY 1,2"
```

语义：若任一命令输出与注释不符，先修环境（容器没起 → `docker start survey-pg`；解释器不对 → 见 `runbook.md` §2.1），再谈开发。

## 2. 当前状态速览

N0–N8 全部完成：后端 **263 passed / 0 failed**，ruff 全绿，mock 全链路（含万级压测六场景与「100 路在途」决定性实验）已验证并留可审计数据；**唯一硬阻塞**是最终独立 QA 复核（任务 #13）被模型 429 中断、`docs/qa-report-final.md` 未产出。细节不复制，直接读 `PROGRESS-HANDOFF.md` §3（节点总览）与 §6（未完成清单）。

## 3. 渐进式阅读顺序

1. **本文档** —— 建立「怎么干活」的框架。
2. `PROGRESS-HANDOFF.md` 全文 —— 现状权威：重点 §0（口径）、§2（环境事实）、§5（T7 结论）、§6（未完成清单）、§7（工作规则）、§8（踩坑）。
3. `docs/TEAM-BRIEF.md` **§7** —— 全部历史裁决：尺度与红线都在这里，细节引用节号（如 §7.16/§7.17 是万级判定口径）。
4. `docs/acceptance.md` §1/§2.8/§2.9/§5 + `docs/e2e-report-t6.md` —— 万级与 E2E 的实测证据、可执行复核 SQL、未执行项清单。
5. `docs/architecture.md` + `docs/task-list.md` §8 —— 要改代码前读：DDL、状态机、共享枚举/字段名/红线。
6. 主文档 `/Users/zhao/Desktop/2026-09-20-survey-platform.md` —— 契约唯一来源：§1（边界）、§6（状态机）、§8（API/统计）、§10–§11（任务与验收清单）。

> **🔧 若你的任务是「修问题」**：先读 [`FIX-HANDOFF.md`](./FIX-HANDOFF.md)（待修清单 + 优先级 + 修法边界 + 验收命令），再按 [`fix-readiness-appendix.md`](./fix-readiness-appendix.md) 里的精确 `文件:行号` 定位。**注意基线：当前 263 passed；回填 `backend/tests/test_reason_length_fix.py` 后为 273（修 D-03 前应有 8 红）。**

## 4. ⚠️ 如何恢复被中断的最终复核（任务 #13，当前唯一硬阻塞）

**目标产物**：`docs/qa-report-final.md`（N7/N8 最终独立复核报告）。

**复核要点**（原任务书，详见 PROGRESS-HANDOFF §6.1）：
- 独立复现 §11「100 路在途」：用自己的隔离库 `survey_test_t7qa` 跑 `--size 2000 --latency 3`；
- 原生 SQL 独立重推 13 条不变量（可执行 SQL 见 `acceptance.md` §2.9）；
- 4 组变异证伪：破坏 mock 故障注入 → R2(a) 红；植入「补 unsure」→ R2(b)/红线红；破坏 cancel 幂等 → 控制矩阵红；破坏 worker 入口 → E2E/压测失败；
- 独立重跑 compose + Playwright 8 条 E2E + 字段保真核对；
- 主文档 §11 逐条勾选（✅/❌/未验证）。

**硬规则**：`survey_test_t7` 库**只读**——其中 4 个可审计 run 是「可独立重推」的唯一凭据，严禁任何写入（含压测脚本默认的跑前清库，故复核必须用**新库** `survey_test_t7qa`）。

**主理人恢复方式**：对 `software-qa-engineer-3` SendMessage 续跑（其 429 重置时间 2026-09-21 22:38:59 UTC+8，届时或切换模型）；或按任务 #13 描述重派一名新 QA。**报告通过后**，主理人执行收口动作（重写 PROGRESS-HANDOFF + 生成最终交付总结，见 §6.2）。

## 5. 约定与纪律

1. **测试库独占**：`survey_test`（或 `survey_test_*`）同一时刻只允许一个 pytest suite；并发 `TRUNCATE` 会死锁制造假失败。跑前用 `pg_stat_activity` 确认。
2. **每个新任务单开新 agent**：不复用同一 sub-agent 继续做下一件事（防上下文溢出）。
3. **QA 独立复核**：对抗性证伪（变异测试），不得把「已知缺陷的当前行为」写成通过的用例来固化（§7.12.2）。
4. **跨成员信息经主理人中转**，成员之间不得直连。
5. **命名照抄主文档 snake_case**：`purchase_intent`、`definitely_not/.../definitely_yes`、`retry_wait`、`completed_with_errors` 等；时间 UTC；金额 `Decimal`；`unknown` 不写成 `0`。
6. **诚实报告**：不用「全部/已确认/全项目」全称表述；无法验证的项写「未验证」。

## 6. 常用命令速查

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform

# ---- 测试 / 静态检查 ----
cd backend && uv run pytest -q                 # 全量基线（期望 263 passed，独占窗口！）
uv run ruff check app/ tests/                  # lint（期望 All checks passed）

# ---- compose 全栈 ----
docker compose -f deploy/compose.yaml up -d --build      # 起 web(8080)/api(8000)/worker/postgres
docker compose -f deploy/compose.yaml ps                 # 状态
docker compose -f deploy/compose.yaml logs -f worker     # worker 日志

# ---- 压测（务必前台 await 到返回；独占窗口）----
# ⚠️ 脚本默认库 survey_test_t7 是【只读审计证据】且跑前会清库 —— 必须显式 --database-url 指向独立库！
cd backend
uv run python ../tests/load/run_10000.py --scenario normal --size 10000 --latency 3 --keep-data \
  --database-url "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test_t7qa"

# ---- 直查库（无宿主 psql，全走 docker exec）----
docker exec survey-pg psql -U postgres -d survey_test_t7 -c "SELECT sample_size, status, count(*) FROM runs GROUP BY 1,2"   # 只读
docker exec -i survey-pg psql -U postgres -d survey_test_t7   # 粘贴 acceptance.md §2.9 的复核 SQL

# ---- 容器 ----
docker start survey-pg                                     # 本机 postgres 容器（宿主 55432）
docker exec survey-pg createdb -U postgres survey_test_t7qa  # 为 QA 复核建新隔离库
```

## 7. 已知坑入口

> 踩过的坑（JSONB `'null'`、连接池 240 打爆上限、后台进程被回收、断言取值点落在取消传播窗口内等）**不要重复踩**：完整清单与正解见 `PROGRESS-HANDOFF.md` §8。
