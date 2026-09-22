# 修复交接包（FIX HANDOFF）

> **本包只交付「问题清单 + 代码锚点 + 修法 + 验收标准」，未修改任何业务代码。**
> 生成：2026-09-22。面向**下一个 AI 团队**：读完本文件 + 两份附录即可直接动手修，无需重新定位问题。
>
> ⚠️ **基线提醒（最重要）**：当前 `cd backend && uv run pytest -q` = **263 passed**。
> 回填回归测试 `backend/tests/test_reason_length_fix.py` 后基线变为 **273**（其中 **8 红**，修复 D-03 后应全绿）。**不要把 273 当成回归，也不要把 263 当成最终目标。**

## 1. 交接范围与当前状态

| 项 | 状态 |
|---|---|
| 项目节点 N0–N8 | ✅ 全部完成并通过验收（最终复核 0 P0 / 0 P1 / 2 P2） |
| 后端基线 | 263 passed；`uv run ruff check app/ tests/` All checks passed |
| 本轮补充审查 | **只读**完成：7 类共 37 条（缺陷 5 / 设计偏差 2 / 措辞 8 / 风险 5 / 性能候选 3 / 未验证 7 / 治理 7） |
| 业务代码 | **零改动**（`backend/app/**`、`backend/migrations/**` 在审查窗口后未变） |
| 真实 API | 100 人试点已过四门槛；1,000/10,000 阶段**未执行**（待授权） |

**两份必读附录**：
- [`qa-report-supplementary-audit.md`](./qa-report-supplementary-audit.md) —— 问题清单与证据（7 类分类）
- [`fix-readiness-appendix.md`](./fix-readiness-appendix.md) —— **每条缺陷的代码锚点（`文件:行号`）、根因、最小修法、回归测试证伪方式、风险、工作量**

## 2. 修复优先级与主理人裁定

| 优先级 | 编号 | 一句话 | 代码锚点 | 工作量 | 状态 |
|---|---|---|---|---|---|
| **P1** | **D-03** | 模型调用已成功返回但 `reason`>100 的校验异常**被吞** ⇒ 成员卡 `running` + 孤儿 attempt | `validation.py:117`、`worker/main.py:361-364` 兜底吞异常 | 小（代码）+ 中（用例） | **必修**（非架构层） |
| P2 | D-01 | `attempts.finished_at` 取的是**调用前**时刻（397/397 span<duration） | `execute.py:156`（在 `:175 answer()` 之前） | 小 | 修（见 §4 裁定） |
| P2 | D-02 | INVALID_OUTPUT 路径**不传 `duration_ms`** ⇒ 最慢 3 条时长丢失 | `execute.py:212-222` | 小 | 修（一行级） |
| P3 | D-04 | 孤儿 running attempt（`abandon_expired` 扫描集不含） | `repository.py:499-508` | 小–中 | 修（**保守版**，见 §4） |
| P3 | D-05 | `finish_reason` 未落库 | 丢弃于 `execute.py:432-435` | 小 | 修（**方案 A：写入 usage_json**） |
| 可选 | V-02 | INVALID_OUTPUT「可重试」的表达与常量集合相反（行为正确、表达误导） | `execute.py:50-52` vs `:210-222` | 小 | 显式化（零行为变更） |
| **未来** | **V-01 / F-01** | 中止来源不可区分（四类一律 `cancelled`） | `service.py:475` 无 reason 入参 | 中 | **不实施**（架构/契约层，需授权） |
| 未来 | F-02 | `finish_reason` 持久化（列 + 迁移） | — | 中 | 不实施（已选方案 A） |

## 3. 必须守住的修法边界（违反即返工）

1. **红线 #1**：D-03 的修法必须**显式记录或可重试**——**不得吞异常、不得截断 reason、不得补默认答案/首选项/中点/unsure**。
2. **不得动 `started_at`**：`repository.py:987-989` 的限流窗口重建（裁决 C3）依赖它。D-01 只改 `finished_at`。
3. **注入 `now` 时必须回退**：否则会打破既有确定性用例。
4. **不得推翻`在途请求允许完成并保存`**（`service.py:488`，主文档 §6.4 既定语义）⇒ D-04 只能走保守版。
5. **统计口径不变量**：`五档计数之和 == valid`、`UNIQUE(run_id,row_no)`、`UNIQUE(run_id,persona_id)`、每成功成员恰一条有效答案。
6. **`uq_runs_single_active`**：任何改 `runs.status` 的分支都要保证 `IntegrityError → 409`（不得泄漏 500）。

## 4. 两个必须先定的裁定（主理人已裁）

- **D-01 → 修。** 采用「双时钟」：`answer()` 返回后再取 `settled_moment` 写 `finished_at`；注入 `now` 时回退；**绝不动 `started_at`**。它会改变对外展示的完成时间语义，属**已知且有意为之**的修正。
- **D-04 → 保守版。** 扩 `abandon_expired` 扫描集以纳入「成员已终态 + `lease_token IS NULL` + attempt 仍 running」的孤儿，置 `abandoned`、保留预留、`unknown_cost_count+1`。**激进版**（取消时立即作废在途成员）**不予采纳**——它会推翻 §6.4 的取消语义。

## 5. 下一个 AI 团队的开工 SOP

1. **环境自检（5 分钟）**：`docker exec survey-pg pg_isready`；`cd backend && uv run python -c "import sys;print(sys.version_info[:2])"`（应 `(3, 12)`）。
2. **取基线（1 分钟）**：`uv run pytest -q` → 应 **263 passed**（独占确认：`pg_stat_activity` 查 `survey%`）。
3. **阅读顺序（20 分钟）**：本文件 → `fix-readiness-appendix.md`（按编号找锚点）→ `qa-report-supplementary-audit.md`（证据）→ `TEAM-BRIEF.md` §7（红线与裁决）。
4. **动手顺序**：**D-03 → D-02 → D-01 → D-04 → D-05 → V-02**（D-01 与 D-02 相邻，可同批改；每条改完即跑相关用例）。
5. **回填回归测试**：把 `pending-reason-length-fix-test.py`（**在项目树外** `/Users/zhao/WorkBuddy/2026-09-20-20-00-42/`）回填为 `backend/tests/test_reason_length_fix.py` ⇒ 基线 263→273，修复 D-03 前**应有 8 红**（附录已逐条列出 10 个用例名与证伪方式）。
6. **修完回填台账**：更新 `KNOWN-ISSUES.md` 对应条目状态（D-04 待办 → 已修），并在 `DECISIONS.md` 追加一条裁决记录。

⚠️ **修前必须复核的前置项**：`backend/tests/test_inference.py:169 test_reason_field_gating` 是否与 D-03 的修法冲突（附录列为**未验证**）。

## 6. 验收命令清单（修复后应跑）

```bash
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/backend
uv run pytest -q                    # 回填后 273，修 D-03 后应全绿
uv run ruff check app/ tests/       # All checks passed
# 只读复核（不改数据）
docker exec survey-pg psql -U postgres -d survey -c \
  "SELECT status, count(*) FROM run_members WHERE run_id='1aae0492-a2b6-48a1-bf32-2b019dd324bd' GROUP BY 1"
# 可选回归（需 compose/E2E，非每次必跑）
docker compose -f deploy/compose.yaml up -d --build
cd frontend && PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npx playwright test tests/survey-flow.spec.ts
```

## 7. 仓库治理（已知，未清理）

- `backend/tests/test_reason_length_fix.py` **当前不存在但曾存在并被跑过**（`.pytest_cache` nodeids 10 条、273=263+10）；副本在项目树外 `pending-reason-length-fix-test.py`（mtime 09-22 00:18）。**回填会改变基线数，须显式授权。**
- 开发库 `survey` 有 **9 个批次**（含 400 人 `1aae0492…`），**8 个归属待确认**（无 actor 字段，不作断言）。400 人批次的 `cancelled` **非经服务层产生**（`control_events_json` 只有 create/start）⇒ 来自外部 SQL。
- 项目**不是 git 仓库**，无法 `git diff`；只能靠 mtime + 文档对照 ⇒ **不能证明「没有改动」**，只能证明「审查窗口后未变」。

## 8. 不得越权的事项

- 不得修改 `backend/migrations/**`（新增列需迁移 ⇒ 属 V-01/F-02 类，须授权）。
- 不得删除/回滚任何文件、数据库行、容器（治理问题只报告）。
- 性能候选（`claim_members(limit=1)`、run 行锁、HTTP 连接复用）**维持「未验证优化候选」**，**不得**在无证据时当缺陷修（补充 D 已用 Little 定律同边界核算：误差 0.01%，足以解释吞吐）。
- 真实 API 1,000/10,000 阶段**未授权**，不得擅自放量（会产生真实费用）。
