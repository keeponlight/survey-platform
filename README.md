# Survey Platform（万级智能体模拟问卷平台）

> 输入现有用户表（CSV/XLSX，上限 20,000 行）→ 用 **100 个可复用 Agent 并发槽位**逐人调用第三方模型 API → 输出与输入行**一一对应**的逐人**模拟购买意向**表（固定单题 `purchase_intent`、固定五档）。（口径照抄 `PROGRESS-HANDOFF.md` §0）
>
> **口径（写死）**："1 万个智能体" = 1 万个独立 persona 作答任务；`100` = **并行执行槽位数**（`AGENT_CONCURRENCY=100`），不是样本数/机器数/进程数。

## 状态横幅

| 项 | 状态 |
|---|---|
| 节点 N0–N8 | ✅ 全部完成（证据见 `PROGRESS-HANDOFF.md` §3） |
| 后端全量测试 | ✅ **263 passed / 0 failed**（`cd backend && uv run pytest -q`，主理人实测 51.39s，`PROGRESS-HANDOFF.md` §3） |
| ruff | ✅ All checks passed（`PROGRESS-HANDOFF.md` §3） |
| mock 全链路（含万级压测） | ✅ 已验证：六大规模场景 `passed=True`；§11「100 路在途」已验证（`normal@10000 --latency 3` 实测 mock/db 峰值均 = 100，`acceptance.md` §2.8） |
| ✅ 最终独立 QA 复核 | **已通过：0 P0 / 0 P1 / 2 P2**（`docs/qa-report-final.md`，N7/N8 可验收）；交付总结见 `docs/FINAL-SUMMARY.md` |
| 真实第三方 API 实测 | **未执行**（本机无凭据），硬声明见 `acceptance.md` §1；**mock 容量 ≠ 真实模型运行** |

## 快速开始

```bash
# ① 验证环境 + 基线（期望：263 passed / All checks passed）
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/backend && uv run pytest -q && uv run ruff check app/ tests/

# ② compose 起全套服务并打开前端（web → http://127.0.0.1:8080，api → 8000；默认 mock 模式，无密钥）
cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform && docker compose -f deploy/compose.yaml up -d --build

# ③ 万级压测示例（复现命令全文见 acceptance.md §6）
# ⚠️ 脚本默认库 survey_test_t7 是【只读审计证据】且跑前会清库 —— 必须显式指向独立库 survey_test_t7qa！
docker exec survey-pg createdb -U postgres survey_test_t7qa 2>/dev/null; cd /Users/zhao/WorkBuddy/2026-09-20-20-00-42/survey-platform/backend && uv run python ../tests/load/run_10000.py --scenario normal --size 10000 --latency 3 --keep-data --database-url "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test_t7qa"
```

## 项目结构

```text
survey-platform/
  backend/        FastAPI + worker + 迁移 + 单测（app/ migrations/ tests/）
  frontend/       React + TS + Vite 四页前端（SurveyEditor/RunCreate/RunDetail/RunResults + Playwright E2E）
  deploy/         compose.yaml（web/api/worker/postgres）与 env 模板（mock.env / real.env.example）
  tests/load/     T7 压测：run_10000.py + mock_provider.py + results/*.json
  docs/           全部设计与验收文档（见下表）
  THIRD_PARTY_NOTICES.md  项目原创性声明与第三方运行时依赖说明
```

## 文档地图

| 文档 | 用途 | 何时读 |
|---|---|---|
| `PROGRESS-HANDOFF.md` | **最新进度交接**（权威索引/环境事实/节点/未完成清单/踩坑/自检） | **最先读**，尤其 §6.1（唯一阻塞） |
| `docs/ONBOARDING.md` | 下一个 AI/开发者的接管操作手册 | 继续开发前 |
| `docs/TEAM-BRIEF.md` | 本机环境事实 + **§7 主理人裁决记录**（§7.9–§7.17 为权威裁定） | 动手前，尺度把握 |
| `docs/acceptance.md` | T7 万级压测与验收报告（§1 真实 API 未执行声明、§2.9 复核 SQL、§6 复现命令） | 核对万级证据 |
| `docs/qa-report-final.md` | 最终独立复核报告（**0 P0 / 0 P1 / 2 P2**，N7/N8 可验收） | 收口证据 |
| `docs/FINAL-SUMMARY.md` | 最终交付总结（§11 逐条结论、关键实测数据、未执行清单） | 交付/汇报 |
| **`docs/FIX-HANDOFF.md`** | **修复交接包**：待修缺陷优先级 + 修法边界 + 开工 SOP（**已查清、未修**，基线 263；回填测试后 273） | **要修问题时第一读** |
| `docs/e2e-report-t6.md` | T6 E2E 与 compose 实跑报告（8 用例 + 连接池缺陷根因） | 核对 N7 证据 |
| `docs/runbook.md` | 启动/测试/迁移/运维/故障排查命令 | 日常运维 |
| `docs/architecture.md` | 五表 DDL、状态机、可靠执行路径、Mock Provider | 改后端前 |
| `docs/task-list.md` | T0–T7 文件级任务清单 + §8 共享知识 | 下阶段派单 |
| `docs/KNOWN-ISSUES.md` | 已知问题台账：未执行/未验证/已接受限制/待办 40 条，每条带来源节号 | 动手前核对「还有什么没做完」 |
| `docs/DECISIONS.md` | 关键裁决索引：27 条 ADR 式摘要（TEAM-BRIEF §7 的蒸馏，冲突以 §7 为准） | 想改任何既有决策前 |
| `/Users/zhao/Desktop/2026-09-20-survey-platform.md` | **需求唯一来源**（主文档，12 节；§10=T0–T7，§11=验收清单） | 一切契约以它为准 |

## 关键约束与红线

1. **红线 #1**：任何路径不得「无有效输出 → 填默认答案/首选项/中点/unsure」。
2. **红线 #2**：不得把 mock 容量说成「真实万人模拟已完成」；真实 API 实测未执行须如实标注。
3. **红线 #3**：API key 只在后端环境/受控文件，不进前端、日志、快照、CSV。
4. **方法论**：不用「全部/已确认/全项目」全称表述；无法验证的项明确写「未验证」。

## 开源许可

本项目以 **GNU General Public License v3.0 或更高版本（GPL-3.0-or-later）** 开源发布。

- 完整许可文本见根目录 [`LICENSE`](./LICENSE)。
- 任何分发、修改或基于本项目的二次开发，均须遵循 GPL-3.0 条款（衍生作品须同样以 GPL 开源并保留版权与许可声明）。
- 本项目不附加任何担保；详情见 LICENSE 第 15–16 节。
- 版权声明（SPDX 标识符）：`SPDX-License-Identifier: GPL-3.0-or-later`。建议在各源码文件头部保留此标识。
- 本项目为从零开始独立开发的原创代码，与任何上游开源项目无关；第三方运行时依赖的说明见 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。

```
Copyright (C) 2026 keeponlight (https://github.com/keeponlight)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

## 环境前提

| 项 | 值 |
|---|---|
| Python | **3.12.13**（`backend/.python-version` 钉版，uv 管理；禁用 3.13，勿用 `UV_PYTHON_INSTALL_DIR`） |
| Node | v22.22.2 / npm 10.9.7（前端） |
| Docker | `survey-pg` 容器（`postgres:16` @ `127.0.0.1:55432`，user/pass=`postgres`）；compose 内 postgres **无宿主端口** |
| 模型 API | **本机无真实 key** → 全链路 mock（`MODEL_PROVIDER=mock` 是唯一选择依据）；真实万人实测未执行 |
