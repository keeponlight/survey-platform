# Survey Platform（万级智能体模拟问卷平台）

> 输入现有用户表（CSV/XLSX，上限 20,000 行）→ 用 **100 个可复用 Agent 并发槽位**逐人调用第三方模型 API → 输出与输入行**一一对应**的逐人**模拟购买意向**表（固定单题 `purchase_intent`、固定五档）。
>
> **口径（写死）**："1 万个智能体" = 1 万个独立 persona 作答任务；`100` = **并行执行槽位数**（`AGENT_CONCURRENCY=100`），不是样本数/机器数/进程数。

## 快速开始

```bash
# ① 验证环境 + 基线测试
cd survey-platform/backend && uv run pytest -q && uv run ruff check app/ tests/

# ② compose 起全套服务并打开前端（web → http://127.0.0.1:8080，api → 8000；默认 mock 模式，无密钥）
cd survey-platform && docker compose -f deploy/compose.yaml up -d --build
```

## 项目结构

```text
survey-platform/
  backend/        FastAPI + worker + 迁移 + 单测（app/ migrations/ tests/）
  frontend/       React + TS + Vite 四页前端（SurveyEditor/RunCreate/RunDetail/RunResults + Playwright E2E）
  deploy/         compose.yaml（web/api/worker/postgres）与 env 模板（mock.env / real.env.example）
  tests/load/     万级压测脚本：run_10000.py + mock_provider.py + results/*.json
```

## 关键约束与红线

1. **红线 #1**：任何路径不得「无有效输出 → 填默认答案/首选项/中点/unsure」。
2. **红线 #2**：不得把 mock 容量说成「真实万人模拟已完成」；真实 API 实测未执行须如实标注。
3. **红线 #3**：API key 只在后端环境/受控文件，不进前端、日志、快照、CSV。

## 环境前提

| 项 | 值 |
|---|---|
| Python | **3.12.13**（`backend/.python-version` 钉版，uv 管理） |
| Node | v22.22.2 / npm 10.9.7（前端） |
| Docker | postgres:16（compose 内 postgres 无宿主端口） |
| 模型 API | 无真实 key 时全链路 mock（`MODEL_PROVIDER=mock`） |

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
